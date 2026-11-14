import assert from "node:assert/strict";
import fs from "node:fs";
import { bindFormatControls, compactActivity, createActivityPoller, createDeliveryHistory, createEngine,
  createLifecycle, profileProgress, stageTiming, timingPreference } from "./demo.js";

{
  class Control extends EventTarget {
    constructor(mode) { super(); this.dataset = { view: mode }; this.attributes = new Map(); }
    setAttribute(key, value) { this.attributes.set(key, value); }
  }
  const tabs = ["mixed", "image", "rich", "text"].map((mode) => new Control(mode));
  const pick = new Control();
  const picks = [];
  const paint = bindFormatControls(tabs, pick, (mode) => picks.push(mode));
  paint("mixed");
  assert.equal(pick.value, "mixed");
  assert.deepEqual(tabs.map((tab) => tab.attributes.get("aria-pressed")), ["true", "false", "false", "false"]);
  assert.deepEqual(picks, [], "painting never edits the config");
  pick.value = "image";
  pick.dispatchEvent(new Event("change"));
  tabs[2].dispatchEvent(new Event("click"));
  assert.deepEqual(picks, ["image", "rich"], "tabs and the narrow-screen select ask for a preset");
  assert.equal(tabs[3].attributes.get("aria-pressed"), "false", "a pick repaints only after the config changes");
  paint(null);
  assert.equal(pick.value, "", "a mix that matches no preset selects Custom");
  assert.deepEqual(tabs.map((tab) => tab.attributes.get("aria-pressed")), ["false", "false", "false", "false"]);
  pick.value = "";
  pick.dispatchEvent(new Event("change"));
  assert.deepEqual(picks, ["image", "rich"], "Custom is not a preset");

  const interval = { cycling: true, running: true, live: false, start: 1000, end: 61000, now: 1000 };
  assert.deepEqual(profileProgress(interval), { kind: "pie", value: 0, left: 60 });
  assert.deepEqual(profileProgress({ ...interval, now: 31000 }), { kind: "pie", value: 0.5, left: 30 });
  assert.deepEqual(profileProgress({ ...interval, now: 62000 }), { kind: "pie", value: 1, left: 0 });
  assert.deepEqual(profileProgress({ ...interval, start: 31000, now: 31000 }),
    { kind: "pie", value: 0, left: 30 }, "manual profile change resets visible progress");
  for (const override of [{ cycling: false }, { running: false }, { live: true }, { start: 0, end: 0 },
    { start: NaN }, { end: Infinity }, { now: NaN }]) {
    assert.deepEqual(profileProgress({ ...interval, ...override }), { kind: "none" });
  }
  assert.deepEqual(profileProgress({ ...interval, running: false,
    activity: { state: "running", stage: "render" } }), { kind: "spinner" },
  "selected-frame rendering replaces the pie even during on-demand playback");
  for (const stage of ["upload", "edit", "send", "boot"]) {
    assert.equal(profileProgress({ ...interval, running: false, activity: { state: "running", stage } }).kind,
      "none", "only selected-frame rendering shows a profile spinner");
  }
  assert.equal(compactActivity({ state: "running", stage: "render", targets: ["System and long section"] }),
    "Rendering...");
  assert.equal(compactActivity({ state: "failed", reason: "full detail remains in the activity label" }),
    "Update failed");
  assert.equal(compactActivity(null, { what: "A very long section" }, 12), "Next in 12s");
  assert.equal(compactActivity(null, { what: "A very long section" }, 0), "Waiting for update");
  assert.equal(compactActivity(null, null, 0), "");
}
console.log("PASS native format controls sync tabs and profile indicators distinguish render, interval and pause");

{
  const values = new Map();
  const storage = { getItem: (key) => values.get(key), setItem: (key, value) => values.set(key, value) };
  assert.equal(timingPreference(storage), true);
  timingPreference(storage, false);
  assert.equal(timingPreference(storage), false);
  timingPreference(storage, true);
  assert.equal(timingPreference(storage), true);
  const blocked = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } };
  assert.equal(timingPreference(blocked), true);
  assert.equal(timingPreference(blocked, false), false);

  const history = createDeliveryHistory();
  const image = { key: "system", name: "system.png", timing: { nominal: 1800, planned_gap: 1800 } };
  const text = { key: "tasks", at: 220, timing: { nominal: 60, planned_gap: null } };
  const frame = { default: [[image, text]], image: [[{ ...image }]], text: [[{ ...text }]] };
  history.apply(frame);
  assert.equal(image.timing.last_success, null, "rendering does not count as a send");
  assert.equal(image.timing.sent, true, "the configured output is monitored before its first success");
  history.completed(frame.default[0], true, 100);
  history.apply(frame);
  assert.equal(image.timing.last_success, 100);
  assert.equal(text.timing.last_success, 100);
  history.completed(frame.default[0], false, 160);
  history.apply(frame);
  assert.equal(image.timing.last_success, 100, "text-only send preserves image success");
  assert.equal(text.timing.last_success, 160);
  assert.equal(text.timing.planned_gap, 60);
  assert.equal(frame.image[0][0].timing.sent, false, "virtual image mode was never sent");
  assert.equal(frame.text[0][0].timing.sent, false, "virtual text mode was never sent");
  history.apply(frame, "text");
  assert.equal(frame.text[0][0].timing.sent, true, "the selected mode is the one the demo posts");
  assert.equal(frame.text[0][0].timing.last_success, 160, "same section and kind share history across modes");
  assert.equal(frame.default[0][0].timing.sent, false);
}

function clock() {
  let now = 0;
  let serial = 0;
  const tasks = new Map();
  return {
    now: () => now,
    later(fn, delay) { const id = ++serial; tasks.set(id, { fn, at: now + delay }); return id; },
    cancel(id) { tasks.delete(id); },
    tick(ms) {
      const target = now + ms;
      for (;;) {
        const due = [...tasks.entries()].filter(([, item]) => item.at <= target)
          .sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        now = due[1].at;
        tasks.delete(due[0]);
        due[1].fn();
      }
      now = target;
    },
    size: () => tasks.size,
  };
}

const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };

function harness(options = {}) {
  const time = clock();
  const workers = [];
  const stages = [];
  const measured = [];
  let mode = "done";
  let failBoot = false;
  const engine = createEngine({ timeout: 20, bootStall: options.bootStall || 100,
    now: time.now, later: time.later, cancel: time.cancel,
    stage: (...args) => stages.push(args), measured: (ms) => measured.push(ms),
    spawn() {
      const worker = {
        terminated: false, messages: [],
        terminate() { this.terminated = true; },
        postMessage(message, transfer = []) {
          const msg = structuredClone(message, { transfer });
          this.messages.push(msg);
          if (msg.op === "boot") {
            if (mode === "boot-throw") throw new Error("boot post failed");
            if (failBoot) return;
            this.onmessage({ data: { stage: "Starting Python", value: .9 } });
          } else if (msg.op === "render") {
            if (mode === "throw") throw new Error("post failed");
            if (mode === "crash") { this.onerror({ message: "worker crashed" }); return; }
            if (mode === "messageerror") { this.onmessageerror(); return; }
            if (mode === "hang") return;
          }
          this.onmessage({ data: { id: msg.id, ...(msg.op === "render" && mode === "reject"
            ? { error: "Python render failed" } : { ok: true, diagnostics: [], frame: {} }) } });
        },
      };
      workers.push(worker);
      return worker;
    } });
  const files = { "previewdata.py": new Uint8Array([1, 2, 3]) };
  return { engine, time, workers, stages, measured, files,
    mode(value) { mode = value; }, bootHang(value) { failBoot = value; },
    boot: () => engine.call({ op: "boot", files, base: "./pyodide/" }) };
}

for (const [mode, reason, restarts] of [["reject", "Python render failed", false],
  ["throw", "post failed", true], ["crash", "worker crashed", true],
  ["messageerror", "unreadable reply", true], ["hang", "timed out", true]]) {
  const h = harness();
  await h.boot();
  await h.engine.call({ op: "feed", snapshot: "saved snapshot" });
  h.mode(mode);
  const failed = assert.rejects(h.engine.call({ op: "render", toml: "", images: true }), new RegExp(reason));
  await flush();
  if (mode === "hang") h.time.tick(20);
  await failed;
  assert.equal(h.workers[0].terminated, restarts);
  assert.equal(h.time.size(), 0);
  h.mode("done");
  await h.engine.call({ op: "render", toml: "", images: true });
  assert.equal(h.workers.length, restarts ? 2 : 1);
  if (restarts) {
    assert.deepEqual([...h.workers[1].messages[0].files["previewdata.py"]], [1, 2, 3]);
    assert.equal(h.workers[1].messages[1].snapshot, "saved snapshot");
  }
  assert.deepEqual([...h.files["previewdata.py"]], [1, 2, 3]);
  assert.equal(h.engine.ready, true);
  h.engine.destroy();
}
console.log("PASS rejected renders, post throws, worker errors, message errors, timeout and fresh-worker recovery");

{
  const h = harness();
  h.mode("boot-throw");
  await assert.rejects(h.boot(), /boot post failed/);
  h.mode("done");
  await h.engine.prepare();
  assert.equal(h.workers.length, 2);
  assert.equal(h.engine.ready, true);
  h.engine.destroy();
}
console.log("PASS a synchronous bootstrap post failure permits a fresh worker on the next request");

{
  const h = harness();
  h.bootHang(true);
  const boot = h.boot();
  await flush();
  h.engine.supersede();
  assert.equal(h.workers[0].terminated, false, "initial editor lint cannot cancel bootstrap");
  for (let chunk = 0; chunk < 20; chunk++) {
    h.time.tick(90);
    h.workers[0].onmessage({ data: { progress: true } });
  }
  assert.equal(h.workers[0].terminated, false, "healthy bootstrap has no total render deadline");
  const rejected = assert.rejects(boot, /bootstrap crashed/);
  h.workers[0].onerror({ message: "bootstrap crashed" });
  await rejected;
  h.bootHang(false);
  await h.engine.prepare();
  assert.equal(h.engine.ready, true);
  h.mode("hang");
  const stale = assert.rejects(h.engine.call({ op: "render" }), { name: "SupersededError" });
  await flush();
  const previous = h.workers.at(-1);
  h.engine.supersede();
  await stale;
  const count = h.stages.length;
  previous.onmessage({ data: { stage: "stale stage", value: 1 } });
  assert.equal(h.stages.length, count);
  assert.equal(h.time.size(), 0);
  h.mode("done");
  await h.engine.call({ op: "render" });
  h.engine.destroy();
}
console.log("PASS bootstrap is outside render deadline and superseded workers cannot update stages");

for (const event of ["onerror", "onmessageerror"]) {
  const h = harness();
  await h.boot();
  h.mode("hang");
  const stale = assert.rejects(h.engine.call({ op: "render" }), { name: "SupersededError" });
  await flush();
  const previous = h.workers[0];
  h.engine.supersede();
  await stale;
  h.bootHang(true);
  h.mode("done");
  const preparing = h.engine.prepare();
  await flush();
  const current = h.workers[1];
  previous[event]({ message: "stale worker error" });
  assert.equal(current.terminated, false);
  current.onmessage({ data: { id: current.messages[0].id, ok: true } });
  await preparing;
  h.mode("hang");
  const rendering = h.engine.call({ op: "render" });
  await flush();
  previous[event]({ message: "another stale worker error" });
  assert.equal(current.terminated, false);
  current.onmessage({ data: { id: current.messages.at(-1).id, ok: true } });
  assert.deepEqual(await rendering, { id: current.messages.at(-1).id, ok: true });
  assert.equal(h.engine.ready, true);
  assert.equal(h.time.size(), 0);
  h.engine.destroy();
}
console.log("PASS stale worker errors cannot terminate a newer bootstrap or render");

{
  const h = harness();
  h.bootHang(true);
  const boot = h.boot();
  await flush();
  h.engine.supersede();
  const preparing = h.engine.prepare();
  assert.equal(h.workers.length, 1);
  const first = h.workers[0];
  first.onmessage({ data: { id: first.messages[0].id, ok: true } });
  await Promise.all([boot, preparing]);
  await h.engine.call({ op: "feed", snapshot: "first snapshot" });
  await h.engine.call({ op: "render", toml: "", images: true });
  assert.deepEqual(first.messages.map((message) => message.op), ["boot", "feed", "render"]);
  assert.equal(first.terminated, false);
  h.engine.destroy();
}
console.log("PASS initial lint preserves bootstrap and the first snapshot precedes rendering");

{
  const h = harness();
  h.bootHang(true);
  const stalled = assert.rejects(h.boot(), /startup stalled/);
  await flush();
  h.time.tick(100);
  await stalled;
  assert.equal(h.workers[0].terminated, true);
  assert.equal(h.time.size(), 0);
}
console.log("PASS bootstrap inactivity fails while continued progress may exceed the total render ceiling");

{
  const time = clock();
  const changes = [];
  const lifecycle = createLifecycle({ changed: (entry) => changes.push(entry), timeout: 20,
    now: time.now, later: time.later, cancel: time.cancel });
  const old = lifecycle.begin("render", ["System"]);
  const fresh = lifecycle.begin("upload", ["Disks"]);
  assert.equal(changes.at(-2).state, "superseded");
  assert.equal(lifecycle.end(old, "failed", "late error"), false);
  assert.equal(lifecycle.current().stage, "upload");
  time.tick(20);
  assert.equal(lifecycle.current().state, "failed");
  assert.match(lifecycle.current().reason, /timed out/);
  assert.equal(time.size(), 0);
  lifecycle.end(fresh, "done");
  assert.equal(lifecycle.current().state, "done");
  lifecycle.begin("boot", [], null);
  time.tick(1000000);
  assert.equal(lifecycle.current().state, "running");
  lifecycle.clear("superseded");
  assert.equal(time.size(), 0);
}
console.log("PASS stage generations, timeout, done, failed and superseded terminal states");

{
  const time = clock();
  time.tick(41000);
  const timing = stageTiming({ since: 1, timeout: 170 }, time.now());
  const lifecycle = createLifecycle({ changed: () => {}, now: time.now, later: time.later, cancel: time.cancel });
  lifecycle.begin("upload", ["System"], timing.deadline, "live", timing.timeout);
  time.tick(129999);
  assert.equal(lifecycle.current().state, "running", "a healthy upload may outlive the browser render ceiling");
  time.tick(1);
  assert.equal(lifecycle.current().state, "failed");
  assert.equal(lifecycle.current().reason, "timed out after 170s");
  for (const timeout of [0, -1, "170", Infinity, NaN]) {
    assert.equal(stageTiming({ timeout }, 100).timeout, 30000);
  }
  assert.equal(stageTiming({ since: 1000, timeout: 20 }, 100).deadline, 20100);
  assert.equal(stageTiming({ timeout: 1e308 }, 100).timeout, 2147483647);
}
console.log("PASS live uploads use their actual send ceiling and renders retain the measured 30s ceiling");

{
  const h = harness();
  const changes = [];
  const lifecycle = createLifecycle({ changed: (entry) => changes.push(entry), timeout: 20,
    now: h.time.now, later: h.time.later, cancel: h.time.cancel });
  const profiles = JSON.parse(fs.readFileSync(new URL("snapshots.json", import.meta.url))).names;
  await h.boot();
  for (let interval = 0; interval < profiles.length * 5; interval++) {
    const profile = profiles[interval % profiles.length];
    await h.engine.prepare();
    await h.engine.call({ op: "feed", snapshot: profile });
    const id = lifecycle.begin("render", [profile]);
    h.mode(interval % 7 ? "done" : "hang");
    try {
      const render = h.engine.call({ op: "render", toml: "", images: true });
      const handled = render.catch((error) => { throw error; });
      const settled = handled.then(() => lifecycle.end(id, "done"),
        (error) => lifecycle.end(id, "failed", error.message));
      await flush();
      if (interval % 7 === 0) h.time.tick(20);
      await settled;
    } finally {
      assert.notEqual(lifecycle.current().state, "running");
      assert.equal(h.time.size(), 0);
      h.time.tick(1000);
    }
  }
  assert.equal(changes.filter((entry) => entry.state === "running").length, profiles.length * 5);
  assert(changes.some((entry) => entry.state === "failed"));
  h.engine.destroy();
}
console.log("PASS five intervals per real profile leave no expired stage or pending timer");

{
  const messages = [];
  let attempts = 0;
  const oldSelf = globalThis.self;
  globalThis.self = { postMessage(message) {
      if (++attempts === 1) throw new Error("first reply failed");
      messages.push(message);
    } };
  const { installSources } = await import("./worker.js");
  const written = new Map();
  const directories = [];
  const py = { FS: { mkdirTree: (path) => directories.push(path), writeFile: (path, data) => written.set(path, data) } };
  const bytes = new TextEncoder().encode("9.8.7\n").buffer;
  installSources(py, { VERSION: bytes, "__init__.py": bytes, "helper/constants.py": bytes });
  assert.deepEqual([...written.keys()],
    ["/src/VERSION", "/src/shazzzbot/__init__.py", "/src/shazzzbot/helper/constants.py"]);
  assert.equal(new TextDecoder().decode(written.get("/src/VERSION")), "9.8.7\n");
  assert(!written.has("/src/shazzzbot/VERSION"));
  assert.deepEqual(directories, ["/src", "/src/shazzzbot", "/src/shazzzbot/helper"]);
  self.onmessage({ data: { id: 1, op: "feed" } });
  await flush();
  self.onmessage({ data: { id: 2, op: "feed" } });
  await flush();
  assert.deepEqual(messages.map((message) => message.id), [1, 2]);
  assert.equal(messages[0].error, "first reply failed");
  assert.match(messages[1].error, /not started/);
  globalThis.self = oldSelf;
}
console.log("PASS a failed worker reply does not poison the next queued request");

{
  const { bytes } = await import("./worker.js");
  const old = { fetch: globalThis.fetch, self: globalThis.self, setTimeout, clearTimeout };
  const time = clock();
  let receive;
  let signal;
  let progress = 0;
  globalThis.setTimeout = time.later;
  globalThis.clearTimeout = time.cancel;
  globalThis.self = { postMessage: () => { progress++; } };
  globalThis.fetch = async (_url, options) => {
    signal = options.signal;
    return { ok: true, body: { getReader: () => ({ read: () => new Promise((resolve, reject) => {
      receive = resolve;
      signal.addEventListener("abort", () => reject(new Error("download stalled")), { once: true });
    }) }) } };
  };
  try {
    const downloading = bytes("https://example.invalid/wheel");
    await flush();
    for (let i = 0; i < 5; i++) {
      time.tick(19000);
      receive({ done: false, value: new Uint8Array([i]) });
      await flush();
      assert.equal(signal.aborted, false);
    }
    receive({ done: true });
    assert.deepEqual([...new Uint8Array(await downloading)], [0, 1, 2, 3, 4]);
    assert.equal(progress, 6);
    assert.equal(time.size(), 0);
    const stalled = assert.rejects(bytes("https://example.invalid/stalled"), /download stalled/);
    await flush();
    time.tick(10000);
    receive({ done: false, value: new Uint8Array() });
    await flush();
    time.tick(10000);
    await stalled;
    assert.equal(signal.aborted, true);
    assert.equal(time.size(), 0);
  } finally {
    Object.assign(globalThis, old);
  }
}
console.log("PASS wheel reads reset the stall watchdog for bytes and have no total download deadline");

{
  const { withDownloadProgress } = await import("./worker.js");
  const old = { fetch: globalThis.fetch, self: globalThis.self, setTimeout, clearTimeout };
  const h = harness({ bootStall: 120000 });
  h.bootHang(true);
  const boot = h.boot();
  await flush();
  let source;
  let signal;
  let heartbeat = 0;
  globalThis.setTimeout = h.time.later;
  globalThis.clearTimeout = h.time.cancel;
  globalThis.self = { postMessage(data) {
    if (data.progress) heartbeat++;
    h.workers[0].onmessage({ data });
  } };
  const fetcher = async (_url, options) => {
    signal = options.signal;
    const response = new Response(new ReadableStream({ start(controller) {
      source = controller;
      signal.addEventListener("abort", () => controller.error(signal.reason), { once: true });
    } }), { status: 206, statusText: "Partial Content", headers: { "Content-Type": "application/wasm" } });
    Object.defineProperty(response, "url", { value: "https://example.invalid/runtime.wasm" });
    return response;
  };
  globalThis.fetch = fetcher;
  try {
    const downloading = withDownloadProgress(async () => {
      const response = await fetch("https://example.invalid/runtime.wasm");
      assert.equal(response.url, "https://example.invalid/runtime.wasm");
      assert.equal(response.status, 206);
      assert.equal(response.statusText, "Partial Content");
      assert.equal(response.headers.get("Content-Type"), "application/wasm");
      return response.arrayBuffer();
    });
    await flush();
    for (let i = 0; i < 8; i++) {
      h.time.tick(19000);
      source.enqueue(new Uint8Array([i]));
      await flush();
      assert.equal(signal.aborted, false);
      assert.equal(h.workers[0].terminated, false, "real download bytes keep the bootstrap alive");
    }
    source.close();
    assert.deepEqual([...new Uint8Array(await downloading)], [0, 1, 2, 3, 4, 5, 6, 7]);
    assert.equal(heartbeat, 8);
    assert.equal(globalThis.fetch, fetcher);
    h.workers[0].onmessage({ data: { id: h.workers[0].messages[0].id, ok: true } });
    await boot;
    const stalled = assert.rejects(withDownloadProgress(async () => {
      const response = await fetch("https://example.invalid/stalled-runtime");
      return response.arrayBuffer();
    }), /download stalled/);
    await flush();
    h.time.tick(10000);
    source.enqueue(new Uint8Array());
    await flush();
    h.time.tick(10000);
    await stalled;
    assert.equal(signal.aborted, true);
    assert.equal(globalThis.fetch, fetcher);
    const upstream = new AbortController();
    const cancelled = assert.rejects(withDownloadProgress(async () => {
      const response = await fetch("https://example.invalid/cancelled-runtime", { signal: upstream.signal });
      return response.arrayBuffer();
    }), /caller cancelled/);
    await flush();
    upstream.abort(new Error("caller cancelled"));
    await cancelled;
    assert.equal(globalThis.fetch, fetcher);
    await assert.rejects(withDownloadProgress(async () => { throw new Error("boot failed"); }), /boot failed/);
    assert.equal(globalThis.fetch, fetcher);
    assert.equal(h.time.size(), 0);
  } finally {
    h.engine.destroy();
    Object.assign(globalThis, old);
  }
}
console.log("PASS internal Pyodide downloads forward real bytes, preserve responses and restore fetch after stalls");

{
  let live = true;
  let hidden = false;
  let generation = 1;
  let active = { state: "running", source: "live" };
  let calls = 0;
  let settle;
  const updates = [];
  const poll = createActivityPoller({
    read: () => { calls++; return new Promise((resolve, reject) => { settle = { resolve, reject }; }); },
    live: () => live, hidden: () => hidden, version: () => generation, current: () => active,
    update: (activity) => updates.push(activity),
  });
  const stale = poll();
  await poll();
  assert.equal(calls, 1, "a pending request prevents overlapping activity polls");
  generation++;
  settle.resolve({ activity: null });
  await stale;
  assert.deepEqual(updates, [], "an old cycle cannot clear the new stage");
  const fresh = poll();
  const upload = { stage: "upload", since: 10 };
  settle.resolve({ activity: upload });
  await fresh;
  assert.deepEqual(updates, [upload]);
  const toggled = poll();
  live = false;
  settle.resolve({ activity: null });
  await toggled;
  assert.deepEqual(updates, [upload], "leaving live mode ignores its outstanding response");
  live = true;
  const oldError = poll();
  generation++;
  settle.reject(new Error("old connection failed"));
  await oldError;
  assert.deepEqual(updates, [upload]);
  active = { state: "running", source: "preview" };
  const duringPreview = poll();
  settle.resolve({ activity: upload });
  await duringPreview;
  assert.deepEqual(updates, [upload], "live replies cannot replace an active preview render");
  active = { state: "running", source: "live" };
  const failed = poll();
  settle.reject(new Error("current connection failed"));
  await failed;
  assert.deepEqual(updates, [upload, null]);
  hidden = true;
  const before = calls;
  await poll();
  live = false;
  hidden = false;
  await poll();
  assert.equal(calls, before);
}
console.log("PASS activity polling is single-flight and stale cycle or mode replies cannot replace current work");
