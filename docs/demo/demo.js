import { mountPreview } from "./preview.js";
import { boot, fail as splashFail, finish, purgeAndReload, stage } from "./boot.js";
import { mountLog } from "./logpanel.js";
import { tips } from "./tip.js";
import { activityText, advance, names, profileLabel } from "./bar.js";
import { dur, ticker } from "./ticker.js";
import { breakdown, equation, perMinute, span } from "./schedule.js";
import { currentMix, setRender } from "./renderedit.js";

const $ = (id) => document.getElementById(id);
const API = "../../api/";
export function editorSchema(live, fallback) {
  const schema = live?.live === true ? live.schema : fallback;
  if (schema?.format !== 1 || !schema.sections?.config || !schema.editor) {
    throw new Error("Config schema is unavailable; reload the editor");
  }
  return schema;
}

const STAGE_MS = 500;
export const PER_RENDER_TIMEOUT_MS = 30000;
const BOOT_STALL_MS = 120000;
const state = { live: false, useLive: false, sets: null, frames: null, names: [], set: "helper", index: 0,
  timing: "bot", ok: false, rendered: false, timer: 0, when: null, sim: 0, simAt: 0, nextEdit: 0, nextImage: 0,
  req: 0, cycle: 0, busyTimer: 0, mode: "default", shown: null, label: "", activity: null, next: null, step: 0,
  cycling: true, ready: false, profileStart: 0, profileEnd: 0 };
const images = new Map();
const held = new Map();
let preview = null;
let note = () => {};
let editor = null;
let engine = null;

// The mode tabs and the narrow-screen select: paint shows the preset the config matches (null for none); a pick asks
// for that preset and the editor change repaints.
export function bindFormatControls(tabs, pick, picked) {
  const modes = ["mixed", "image", "rich", "text"];
  const paint = (mode) => {
    pick.value = modes.includes(mode) ? mode : "";
    for (const tab of tabs) tab.setAttribute("aria-pressed", String(tab.dataset.view === mode));
  };
  const choose = (mode) => { if (modes.includes(mode)) picked(mode); };
  for (const tab of tabs) tab.addEventListener("click", () => choose(tab.dataset.view));
  pick.addEventListener("change", () => choose(pick.value));
  return paint;
}

export function profileProgress({ activity, cycling, running, live, start, end, now }) {
  if (activity && activity.state === "running" && activity.stage === "render") return { kind: "spinner" };
  if (!cycling || !running || live || !Number.isFinite(start) || !Number.isFinite(end)
    || !Number.isFinite(now) || end <= start)
    return { kind: "none" };
  return { kind: "pie", value: Math.max(0, Math.min(1, (now - start) / (end - start))),
    left: Math.max(0, Math.ceil((end - now) / 1000)) };
}

export function compactActivity(activity, next, left) {
  if (activity) {
    if (activity.state === "failed") return "Update failed";
    return { boot: "Starting preview...", render: "Rendering...", upload: "Uploading...",
      edit: "Editing...", send: "Sending..." }[activity.stage] || "Updating...";
  }
  return next ? left <= 0.5 ? "Waiting for update" : "Next in " + dur(left) : "";
}

export function timingPreference(storage, value) {
  try {
    if (value === undefined) return storage.getItem("shazzzbot-timing") !== "off";
    storage.setItem("shazzzbot-timing", value ? "on" : "off");
  } catch {}
  return value === undefined ? true : value;
}

export function createDeliveryHistory() {
  const history = new Map();
  const key = (item) => (item.key || item.embed?.title || "Panel") + ":"
    + (item.timing?.kind || (item.name ? "image" : "edit"));
  return {
    apply(frame, posted = "default") {
      for (const [mode, messages] of Object.entries(frame)) {
        for (const item of messages.flat()) {
          const previous = mode === posted ? history.get(key(item)) : undefined;
          item.timing = { ...item.timing, last_success: previous ?? null, last_failed: false,
            reason: "", sent: mode === posted };
          if (Number.isFinite(previous) && Number.isFinite(item.at)) {
            item.timing.planned_gap = Math.max(0, item.at - previous);
          }
        }
      }
    },
    completed(items, upload, at) {
      for (const item of items) if (!item.name || upload) history.set(key(item), at);
    },
  };
}

const demoDelivery = createDeliveryHistory();
let paintMode = () => {};
let SECTIONS = [];

export function createEngine({ spawn, timeout = PER_RENDER_TIMEOUT_MS, bootStall = BOOT_STALL_MS,
  later = setTimeout, cancel = clearTimeout, stage = () => {}, measured = () => {}, now = () => performance.now() }) {
  let worker = null;
  let booting = null;
  let master = null;
  let feed = null;
  let seq = 0;
  let initialized = false;
  const pending = new Map();

  function arm(item) {
    cancel(item.timer);
    item.timer = later(() => stop(new Error(item.op === "boot" ? "Preview engine startup stalled" :
      "Image render timed out after " + Math.ceil(timeout / 1000) + "s")), item.op === "boot" ? bootStall : timeout);
  }

  function stop(error) {
    if (worker) worker.terminate();
    worker = booting = null;
    initialized = false;
    for (const item of pending.values()) {
      cancel(item.timer);
      item.reject(error);
    }
    pending.clear();
  }

  function send(message, transfer = []) {
    return new Promise((resolve, reject) => {
      const id = ++seq;
      const item = { resolve, reject, timer: 0, at: now(), op: message.op };
      arm(item);
      pending.set(id, item);
      try {
        worker.postMessage({ ...message, id }, transfer);
      } catch (error) {
        stop(error);
      }
    });
  }

  function start() {
    if (!master) return Promise.reject(new Error("the preview engine has not started"));
    try {
      const active = spawn();
      worker = active;
      active.onmessage = ({ data }) => {
        if (active !== worker) return;
        if (data.stage !== undefined || data.progress) {
          for (const item of pending.values()) if (item.op === "boot") arm(item);
          if (data.stage !== undefined) stage(data.stage, data.value);
          return;
        }
        const item = pending.get(data.id);
        if (!item) return;
        pending.delete(data.id);
        cancel(item.timer);
        if (item.op === "render") measured(now() - item.at);
        if (data.error) item.reject(new Error(data.error));
        else item.resolve(data);
      };
      active.onerror = (event) => {
        if (active === worker) stop(new Error("Preview engine failed: " + (event.message || "worker error")));
      };
      active.onmessageerror = () => {
        if (active === worker) stop(new Error("Preview engine returned an unreadable reply"));
      };
      const files = Object.fromEntries(Object.entries(master.files).map(([name, bytes]) => [name, bytes.slice()]));
      booting = send({ ...master, files }, Object.values(files).map((bytes) => bytes.buffer))
        .then(async (reply) => {
          if (feed) await send(feed);
          initialized = true;
          return reply;
        }).catch((error) => {
          if (active === worker) stop(error);
          else if (!worker) booting = null;
          throw error;
        });
      return booting;
    } catch (error) {
      stop(error);
      return Promise.reject(error);
    }
  }

  return {
    get ready() { return initialized; },
    prepare: () => booting || start(),
    async call(message) {
      if (message.op === "boot") {
        master = { ...message, timeout, files: Object.fromEntries(Object.entries(message.files)
          .map(([name, bytes]) => [name, bytes.slice()])) };
        stop(new Error("Preview engine restarted"));
        feed = null;
        return start();
      }
      await (booting || start());
      if (message.op === "feed") feed = message;
      return send(message);
    },
    supersede() {
      if (![...pending.values()].some((item) => item.op !== "boot")) return;
      const error = new Error("Preview render superseded");
      error.name = "SupersededError";
      stop(error);
    },
    destroy() { stop(new Error("Preview engine stopped")); },
  };
}

export function createLifecycle({ changed, timeout = PER_RENDER_TIMEOUT_MS, now = Date.now,
  later = setTimeout, cancel = clearTimeout }) {
  let serial = 0;
  let timer = 0;
  let current = { state: "done" };
  function end(id, state, reason = "") {
    if (id !== serial) return false;
    cancel(timer);
    current = { ...current, state, reason };
    changed(current);
    return true;
  }
  return {
    begin(stage, targets = [], deadline = now() + timeout, source = "preview", ceiling = timeout) {
      if (current.state === "running") end(serial, "superseded");
      cancel(timer);
      const id = ++serial;
      current = { state: "running", stage, targets, deadline, source };
      changed(current);
      timer = deadline === null ? 0 : later(() => end(id, "failed", "timed out after " +
        Math.ceil(ceiling / 1000) + "s"), Math.max(0, deadline - now()));
      return id;
    },
    end,
    current: () => current,
    clear(state = "done", reason = "") { return end(serial, state, reason); },
  };
}

export function stageTiming(activity, now = Date.now()) {
  const timeout = Number.isFinite(activity.timeout) && activity.timeout > 0
    ? Math.min(2147483647, activity.timeout * 1000) : PER_RENDER_TIMEOUT_MS;
  const since = Number.isFinite(activity.since) && activity.since > 0 ? activity.since * 1000 : now;
  return { timeout, deadline: Math.min(since + timeout, now + timeout) };
}

const lifecycle = createLifecycle({ changed: (entry) => {
  state.activity = entry.state === "done" || entry.state === "superseded" ? null : entry;
  if (preview) preview.setStage(entry);
  if (entry.state === "failed") {
    const text = "Image render failed: " + entry.reason;
    status(text, true);
  }
  busy(entry.state === "running");
  tick();
} });

function status(text, error) {
  $("status").textContent = text;
  $("status").classList.toggle("status-error", !!error);
}

function call(msg) { return engine.call(msg); }

// Mirrors delivery.outgoing: a text edit keeps each image's last upload until that image's own upload time.
function show(frame, upload, tag) {
  const stale = [];
  for (const view of Object.values(frame)) {
    for (const msg of Array.isArray(view) ? view : []) {
      for (const [i, item] of msg.entries()) {
        if (!item.name) continue;
        if (item.png) {
          if (images.has(item.name)) stale.push(images.get(item.name));
          images.set(item.name, URL.createObjectURL(new Blob([item.png], { type: "image/png" })));
          delete item.png;
          held.set(item.name, { embed: item.embed, tag: upload ? undefined : tag && tag(item.at) });
        } else if (!upload) {
          const h = held.get(item.name);
          msg[i] = h ? { ...item, embed: h.embed, tag: h.tag } : { ...item, embed: item.held || item.embed };
        }
        if (images.has(item.name)) msg[i].src = images.get(item.name);
      }
    }
  }
  state.rendered = true;
  state.shown = frame;
  if (!state.useLive) demoDelivery.apply(frame, state.mode);
  preview.update(frame, now());
  for (const u of new Set(stale)) if (![...images.values()].includes(u)) URL.revokeObjectURL(u);
  nextChange();
}

// The pending bar waits 150 ms so instant renders do not flash; the last render stays, dimmed, until the next lands.
function busy(on) {
  clearTimeout(state.busyTimer);
  $("stage").setAttribute("aria-busy", String(on));
  if (on) {
    state.busyTimer = setTimeout(() => {
      $("pending").hidden = false;
      $("stage").classList.add("stale");
    }, 150);
  } else {
    $("pending").hidden = true;
    $("stage").classList.remove("stale");
  }
}

// Only the newest render request may draw, so a late reply never replaces newer output.
async function run(withImages, upload) {
  const id = ++state.req;
  engine.supersede();
  await prepareEngine();
  if (id !== state.req) return { diagnostics: [] };
  const operation = lifecycle.begin("render", withImages ? dueImages() : []);
  let r;
  try {
    r = await call({ op: "render", toml: editor.text(), images: withImages });
  } catch (error) {
    lifecycle.end(operation, error.name === "SupersededError" ? "superseded" : "failed", error.message);
    throw error;
  }
  if (id !== state.req) return { diagnostics: r.diagnostics };
  lifecycle.end(operation, "done");
  if (withImages && r.frame) state.stale = false;
  state.ok = !r.diagnostics.some((d) => d.severity === "error");
  $("save").disabled = !state.live || !state.ok;
  if (r.timing) state.when = r.timing;
  if (r.frame) show(r.frame, upload || !auto(), (at) => "preview -- uploads at " + hm(at));
  schedule();
  return r;
}

async function lint() {
  if (!state.ready) return [];
  note("Config changed; rendering the preview");
  state.stale = true;
  try {
    const r = await run(false);
    if (r.frame) {
      const pending = run(true, false);
      const id = state.req;
      pending.then(() => { if (id === state.req) plan(); })
        .catch((e) => { if (e.name !== "SupersededError") status(e.message, true); });
    }
    return r.diagnostics;
  } catch (error) {
    if (error.name !== "SupersededError") status(error.message, true);
    return [];
  }
}

function snapshots() {
  return state.sets ? state.sets[state.set] : [];
}

async function snapshot() {
  if (!state.useLive) return snapshots()[state.index];
  const res = await fetch(API + "snapshot", { credentials: "same-origin", cache: "no-store",
    signal: AbortSignal.timeout(PER_RENDER_TIMEOUT_MS) });
  if (!res.ok) throw new Error("Live data failed (" + res.status + "); showing the last good render");
  return res.json();
}

function now() {
  return state.useLive ? Date.now() / 1000 : state.sim + (performance.now() - state.simAt) / 1000;
}

function auto() {
  return state.useLive || state.timing === "bot";
}

function hm(t) {
  return new Date(t * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function soonest(list) {
  return list.length ? Math.min(...list) : Infinity;
}

// The wall-clock ms at which sim time t arrives; every countdown renders target minus now.
function wall(t) {
  return Date.now() + (t - now()) * 1000;
}

function clock(t) {
  return new Date(t * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
}

// The messages of the selected mode: what the demo posts, so the schedule and playback follow the mode buttons.
const posted = () => (state.when && state.when.messages && state.when.messages[state.mode]) || [];
const shownPosted = () => (state.shown && state.shown[state.mode]) || [];

function deadlines() {
  const msgs = posted();
  state.nextEdit = soonest(msgs.map((m) => m.edit_at));
  state.nextImage = soonest(msgs.flatMap((m) => m.images.slice(0, 1)));
}

function schedule() {
  const w = state.when;
  const msgs = posted();
  const kinds = (m) => m.embeds.map((e) => e.kind);
  const text = msgs.filter((m) => kinds(m).some((x) => x !== "image"));
  const imgs = msgs.filter((m) => kinds(m).includes("image"));
  const label = w ? "Bot schedule (" + [text.length && "text every " + span(Math.min(...text.map((m) => m.edit_every))),
    imgs.length && "images every " + span(Math.min(...imgs.map((m) => m.image_every))),
    msgs.length + " message" + (msgs.length === 1 ? "" : "s")].filter(Boolean).join(", ") + ")" : "Bot schedule";
  $("activity").dataset.tip = label + ". Config changes preview 300 ms after you stop typing.";
  const el = (tag, cls, ...kids) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    node.append(...kids);
    return node;
  };
  const request = (name, at, tail, parts) => {
    const meta = el("span", "sched-meta");
    if (at !== null) {
      const left = el("span", "sched-in");
      left.dataset.target = String(wall(at));
      meta.append(left, tail ? ", " + tail : "");
    } else meta.append(auto() ? "Not scheduled" : "On demand");
    const line = el("p", "sched-line", el("span", "sched-label", name),
      el("span", "sched-time", at !== null ? clock(at) : ""), meta);
    const list = el("ul", "sched-parts", ...parts.map((g) => el("li", "", el("span", "sched-kind",
      g.kind[0].toUpperCase() + g.kind.slice(1) + " (" + g.titles.length + ")"), " " + g.titles.join(", "))));
    return el("div", "sched-req", line, ...(parts.length ? [list] : []));
  };
  let total = 0;
  const blocks = msgs.map((m, i) => {
    const b = breakdown(m, auto());
    total += b.rate;
    const out = el("section", "sched-msg", el("h3", "sched-name", "Message " + (i + 1) + " ",
      el("span", "sched-count", b.count + " embed" + (b.count === 1 ? "" : "s"))));
    if (b.edit.groups.length) out.append(request("Edit", b.edit.at, "every " + span(b.edit.every), b.edit.groups));
    if (b.upload) out.append(request("Upload", b.upload.at, b.upload.gap !== null ? "then " + span(b.upload.gap)
      + " later" : "", b.upload.groups));
    const calc = equation(b);
    if (calc) out.append(el("p", "sched-calc", calc));
    return out;
  });
  $("sched-body").replaceChildren(...blocks);
  const foot = [];
  if (w && msgs.length > 1 && total) foot.push(el("p", "", "All messages: about " + perMinute(total) + " sends per minute"));
  foot.push(el("p", "", w ? "Rate budget: " + (w.learned ? "learned by this bot" : "defaults for a fresh bot")
    : "Waiting for schedule"));
  $("sched-state").replaceChildren(...foot);
  if (state.label && label !== state.label) {
    $("sched").classList.remove("flash");
    void $("sched").offsetWidth;
    $("sched").classList.add("flash");
  }
  state.label = label;
  tick();
}

function nextChange() {
  const view = (state.shown && state.shown[state.mode]) || [];
  let best = null;
  for (const msg of view) {
    for (const item of msg) {
      if (Number.isFinite(item.at) && (!best || item.at < best.at)) best = item;
    }
  }
  state.next = best && auto() ? { target: wall(best.at),
    what: ((best.embed && best.embed.title) || "Panel") + (best.name ? " image" : " embed") } : null;
  tick();
}

function doing(a, source = "preview") {
  const signature = JSON.stringify(a);
  if (source === "live" && lifecycle.current().source === "live" && state.liveActivity === signature) return;
  if (source === "live") state.liveActivity = signature;
  if (!a) {
    lifecycle.clear();
  } else if (a.stage === "failed") {
    lifecycle.begin(a.operation || "render", a.targets || [], undefined, source);
    lifecycle.clear("failed", a.reason || "unknown error");
  } else {
    const timing = stageTiming(a);
    lifecycle.begin(a.stage, a.targets || [], timing.deadline, source, timing.timeout);
    state.activity = { ...lifecycle.current(), n: a.n, of: a.of };
    tick();
  }
}

async function prepareEngine() {
  if (engine.ready) return;
  const operation = lifecycle.begin("boot", [], null);
  try {
    await engine.prepare();
    lifecycle.end(operation, "done");
  } catch (error) {
    lifecycle.end(operation, error.name === "SupersededError" ? "superseded" : "failed", error.message);
    throw error;
  }
}

// The activity line: what runs now, else the next scheduled change. At zero it already reads as in flight.
function tick() {
  const t = Date.now();
  for (const el of $("sched").querySelectorAll(".sched-in")) {
    el.textContent = "in " + dur((Number(el.dataset.target) - t) / 1000);
  }
  let text = "";
  let busy = !!state.activity && state.activity.state !== "failed";
  if (state.activity) text = state.activity.state === "failed" ? "Image render failed: " + state.activity.reason
    : state.activity.stage === "boot" ? "Starting preview engine..." : activityText(state.activity);
  else if (state.next) {
    const left = (state.next.target - t) / 1000;
    text = left <= 0.5 ? "Waiting for next update: " + state.next.what
      : "Next: " + state.next.what + " in " + dur(left);
  }
  const key = state.activity ? text : state.next && "Next: " + state.next.what;
  if (key && key !== state.logged) note(text);
  state.logged = key;
  $("activity").classList.toggle("busy", busy);
  $("activity").hidden = !text;
  $("activity").setAttribute("aria-label", text);
  $("activity").dataset.tip = text;
  const full = document.createElement("span");
  full.className = "activity-full";
  full.textContent = text;
  const short = document.createElement("span");
  short.className = "activity-compact";
  short.textContent = compactActivity(state.activity, state.next, state.next && (state.next.target - t) / 1000);
  $("activity-text").replaceChildren(full, short);
  const progress = profileProgress({ activity: state.activity, cycling: state.cycling,
    running: state.timing === "bot", live: state.useLive, start: state.profileStart, end: state.profileEnd, now: t });
  const progressTarget = matchMedia("(max-width: 899px)").matches ? "chip-progress" : "picker-progress";
  for (const id of ["chip-progress", "picker-progress"]) {
    const indicator = $(id);
    indicator.hidden = progress.kind === "none" || id !== progressTarget;
    indicator.classList.toggle("is-spinning", progress.kind === "spinner");
    indicator.setAttribute("role", progress.kind === "pie" ? "progressbar" : "status");
    indicator.setAttribute("aria-label", progress.kind === "pie" ? "Next profile in " + progress.left + "s"
      : progress.kind === "spinner" ? "Rendering selected profile" : "");
    for (const attr of ["aria-valuenow", "aria-valuemin", "aria-valuemax"]) indicator.removeAttribute(attr);
    if (progress.kind === "pie") {
      indicator.setAttribute("aria-valuemin", "0");
      indicator.setAttribute("aria-valuemax", "100");
      indicator.setAttribute("aria-valuenow", String(Math.round(progress.value * 100)));
      indicator.style.setProperty("--progress", String(progress.value));
    } else indicator.style.setProperty("--progress", "0");
  }
}

async function cycle(forceImages) {
  clearTimeout(state.timer);
  state.profileStart = state.profileEnd = 0;
  const mine = ++state.cycle;
  const mode = state.mode;
  ++state.req;
  engine.supersede();
  lifecycle.clear("superseded");
  const t = now();
  const due = forceImages || !auto() || t >= state.nextImage - 0.5;
  let gathering;
  try {
    await prepareEngine();
    if (mine !== state.cycle) return;
    gathering = lifecycle.begin("render");
    const snap = { ...await snapshot() };
    if (!state.useLive) snap.now = t;
    if (mine !== state.cycle) return;
    await call({ op: "feed", snapshot: JSON.stringify(snap), step: state.useLive ? 0 : state.step,
      seed: state.set + ":" + state.index });
    if (mine !== state.cycle) return;
    state.ready = true;
    lifecycle.end(gathering, "done");
    await run(false, false);
    if (mine !== state.cycle) return;
    const wanted = due ? dueImages() : [];
    if (due || state.stale) {
      await run(true, due);
      if (mine !== state.cycle) return;
    }
    if (mode === state.mode) {
      if (!state.useLive && auto()) await deliver(posted(), wanted, mine, mode);
      if (mode === state.mode) deadlines();
    }
    if (mine === state.cycle) status(state.useLive ? "Live data, updated " + new Date().toLocaleTimeString() : "");
  } catch (e) {
    if (mine !== state.cycle || e.name === "SupersededError") return;
    lifecycle.end(gathering, "failed", e.message);
    if ($("splash").hidden || state.rendered) status(e.message, true);
    else fail(e.message);
  }
  if (mine !== state.cycle) return;
  if (!state.useLive && lifecycle.current().state !== "failed") doing(null);
  if (!$("splash").hidden && state.rendered) finish();
  plan();
}

// The demo's stand-in for the bot's sends: each message in turn, uploading where images are due.
async function deliver(msgs, wanted, mine, mode) {
  for (const [i, m] of msgs.entries()) {
    if (mine !== state.cycle || mode !== state.mode) return;
    const up = wanted.length && m.images.length;
    doing({ stage: up ? "upload" : "edit", targets: up ? wanted : [], n: i + 1, of: msgs.length });
    await new Promise((r) => setTimeout(r, STAGE_MS));
    if (mine !== state.cycle || mode !== state.mode) return;
    demoDelivery.completed(shownPosted()[i] || [], !!up, now());
    demoDelivery.apply(state.shown, mode);
    preview.update(state.shown, now());
  }
}

function dueImages() {
  const view = shownPosted();
  return names([...new Set(view.flat().filter((i) => i.name).map((i) => i.name))]);
}

function plan() {
  counter();
  clearTimeout(state.timer);
  state.profileStart = state.profileEnd = 0;
  schedule();
  if (!auto()) return;
  if (!Number.isFinite(state.nextEdit)) state.nextEdit = now() + ((state.when && state.when.edit_every) || 60);
  const wait = Math.max(state.nextEdit - now(), 1) * 1000;
  state.profileStart = Date.now();
  state.profileEnd = state.profileStart + wait;
  tick();
  state.timer = setTimeout(() => {
    if (!state.useLive) {
      state.sim = Math.max(state.nextEdit, now());
      state.simAt = performance.now();
      Object.assign(state, advance(state, 1, snapshots().length, state.cycling));
      note("Profile: " + profileLabel(state.names[state.index] || "Profile", state.step));
    }
    cycle(false);
  }, wait);
}

// Below 900 px the playback controls and the cycle switch live in the profile dialog; the header keeps two lines.
function narrow() {
  const query = window.matchMedia("(max-width: 899px)");
  const moving = [document.querySelector(".bar-play"), $("cycle"), $("cycle-help")];
  const homes = moving.map((el) => {
    const mark = document.createComment("");
    el.before(mark);
    return mark;
  });
  const place = () => {
    if (query.matches) $("play-slot").append(...moving);
    else {
      if ($("play-dialog").open) $("play-dialog").close();
      moving.forEach((el, i) => homes[i].after(el));
    }
  };
  query.addEventListener("change", place);
  place();
}

function playing(on) {
  state.timing = on ? "bot" : "manual";
  const b = $("play");
  b.setAttribute("aria-label", on ? "Pause" : "Play");
  b.querySelector("path").setAttribute("d", on ? "M9 6v12M15 6v12" : "M8 5v14l11-7z");
}

export function createActivityPoller({ read, live, hidden, version, current, update }) {
  let pending = false;
  return async () => {
    if (pending || !live() || hidden()) return;
    pending = true;
    const generation = version();
    try {
      const result = await read();
      if (!live() || generation !== version()) return;
      const active = current();
      if ((result.activity || active.source === "live")
          && !(active.state === "running" && active.source === "preview")) update(result.activity);
    } catch {
      if (live() && generation === version() && current().source === "live") update(null);
    } finally {
      pending = false;
    }
  };
}

const readActivity = createActivityPoller({
  read: () => fetch(API + "snapshot?only=activity", { credentials: "same-origin", cache: "no-store",
    signal: AbortSignal.timeout(PER_RENDER_TIMEOUT_MS) }).then((r) => (r.ok ? r.json() : { activity: null })),
  live: () => state.useLive,
  hidden: () => document.hidden,
  version: () => state.cycle + ":" + state.req,
  current: () => lifecycle.current(),
  update: (activity) => doing(activity, "live"),
});

// Live: the bot's own stage, polled while the tab is visible.
function pollActivity() {
  setTimeout(pollActivity, 2000);
  readActivity();
}

function counter() {
  for (const o of $("frame-pick").options) {
    if (o.value !== "live") {
      const i = Number(o.value);
      o.text = state.names[i] || "Profile " + (i + 1);
    }
  }
  $("frame-pick").value = state.useLive ? "live" : String(state.index);
  $("chip-text").textContent = state.useLive ? "Live" : state.names[state.index] || "Profile";
  $("cycle").setAttribute("aria-checked", String(state.cycling));
  $("helper").setAttribute("aria-checked", String(state.set === "helper"));
  $("helper").disabled = state.useLive;
}

function picker() {
  const live = new Option(state.live ? "Live" : "Live (offline)", "live");
  live.disabled = !state.live;
  $("frame-pick").replaceChildren(live, ...snapshots().map((_, i) =>
    new Option(state.names[i] || "Profile " + (i + 1), String(i))));
  $("frame-pick").addEventListener("change", () => {
    const v = $("frame-pick").value;
    if (v === "live") {
      state.useLive = true;
      state.nextImage = 0;
      counter();
      cycle(true);
    } else {
      state.useLive = false;
      Object.assign(state, { index: Number(v), step: 0 });
      go(0);
    }
  });
}

function prebuilt() {
  if (state.rendered || state.useLive || !state.frames) return;
  const frames = state.frames[state.set] || [];
  const f = frames[state.index % frames.length];
  if (f) {
    state.shown = f;
    demoDelivery.apply(f, state.mode);
    preview.update(f, snapshots()[state.index] && snapshots()[state.index].now);
    nextChange();
  }
}

function go(delta) {
  const n = snapshots().length;
  if (!n) return;
  if (!state.useLive) Object.assign(state, advance(state, delta, n, state.cycling));
  state.useLive = false;
  note("Profile: " + profileLabel(state.names[state.index] || "Profile", state.step));
  if (!state.step) prebuilt();
  counter();
  cycle(true);
}

async function save() {
  const draft = editor.text();
  $("save").disabled = true;
  status("Saving...");
  try {
    const res = await fetch(API + "config", {
      method: "PUT", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Shazzzbot-Preview": "1" },
      body: JSON.stringify({ toml: draft }),
    });
    const body = await res.json().catch(() => ({}));
    if (res.ok) {
      editor.saved(draft);
      status("Saved to config.toml. The bot uses it from its next update.");
    }
    else status("Not saved: " + (body.error || (body.diagnostics && body.diagnostics[0] && body.diagnostics[0].message)
      || "error " + res.status), true);
  } catch (e) {
    status("Not saved: " + e.message, true);
  } finally {
    $("save").disabled = !state.ok;
  }
}

function toggle(id, cls, what, openWhenOff) {
  const b = $(id);
  const sync = () => {
    const open = document.body.classList.contains(cls) !== openWhenOff;
    b.setAttribute("aria-expanded", String(open));
    if (b.classList.contains("icon-btn")) b.setAttribute("aria-label", (open ? "Hide " : "Show ") + what);
    else b.textContent = (open ? "Hide " : "Show ") + what;
  };
  b.addEventListener("click", () => {
    document.body.classList.toggle(cls);
    sync();
  });
  sync();
}

async function main() {
  let timing = timingPreference({ getItem: (key) => localStorage.getItem(key) });
  preview = mountPreview($("stage"), { mode: "default", timing, imageTimeoutMs: PER_RENDER_TIMEOUT_MS,
    onImageState: (entry) => {
      if (entry.state === "failed") {
        const operation = lifecycle.begin("render", entry.targets);
        lifecycle.end(operation, "failed", entry.reason);
      }
    } });
  const timingSwitch = $("show-timing");
  timingSwitch.setAttribute("aria-checked", String(timing));
  timingSwitch.addEventListener("click", () => {
    timing = !timing;
    timingPreference({ setItem: (key, value) => localStorage.setItem(key, value) }, timing);
    timingSwitch.setAttribute("aria-checked", String(timing));
    preview.setTiming(timing);
  });
  preview.setMode(state.mode);
  paintMode = bindFormatControls(document.querySelectorAll("[data-view]"), $("format-pick"), (preset) => {
    const result = setRender(editor.text(), SECTIONS, preset);
    if (result.error) {
      status(result.error, true);
      paintMode(currentMix(editor.text(), SECTIONS));
    } else editor.replace(result.text);
  });
  $("undo").addEventListener("click", () => editor.undo());
  examples();
  $("prev").addEventListener("click", () => go(-1));
  $("next").addEventListener("click", () => go(1));
  playing(!window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  $("play").addEventListener("click", () => {
    playing(!auto());
    state.sim = now();
    state.simAt = performance.now();
    state.nextImage = 0;
    plan();
    nextChange();
  });
  $("cycle").addEventListener("click", () => {
    state.cycling = !state.cycling;
    state.step = 0;
    state.profileStart = Date.now();
    counter();
    tick();
  });
  $("helper").addEventListener("click", () => {
    state.set = state.set === "helper" ? "nohelper" : "helper";
    state.index = Math.min(state.index, snapshots().length - 1);
    go(0);
  });
  $("save").addEventListener("click", save);
  toggle("side", "side-closed", "settings", true);
  for (const id of ["settings-pop", "more-pop"]) {
    $(id).addEventListener("toggle", (e) => {
      if (e.newState !== "open") return;
      const anchor = $("settings-btn").checkVisibility() ? $("settings-btn") : $("more");
      const r = anchor.getBoundingClientRect();
      const pop = $(id);
      const vw = document.documentElement.clientWidth;
      pop.style.setProperty("top", Math.round(r.bottom + 8) + "px");
      pop.style.setProperty("left", Math.round(Math.min(Math.max(r.right - pop.offsetWidth, 16), vw - 16 - pop.offsetWidth)) + "px");
    });
  }
  $("more-log").addEventListener("click", () => {
    $("more-pop").hidePopover();
    $("log-btn").click();
  });
  narrow();
  $("chip").addEventListener("click", () => $("play-dialog").showModal());
  $("play-close").addEventListener("click", () => $("play-dialog").close());
  $("play-dialog").addEventListener("click", (e) => { if (e.target === $("play-dialog")) $("play-dialog").close(); });

  $("purge-cache").addEventListener("click", purgeAndReload);
  $("sched-btn").addEventListener("click", () => {
    $("settings-pop").hidePopover();
    if (document.body.classList.contains("side-closed")) $("side").click();
    $("sched-title").scrollIntoView({ block: "start" });
    $("sched-title").focus();
  });
  let toml, liveConfig;
  try {
    const res = await fetch(API + "config", { credentials: "same-origin", cache: "no-store",
      signal: AbortSignal.timeout(PER_RENDER_TIMEOUT_MS) });
    const live = res.ok ? await res.json() : {};
    liveConfig = live;
    state.live = live.live === true;
    toml = live.toml;
  } catch {
    state.live = false;
  }
  if (!state.live) $("save").dataset.tip = "This demo has no config.toml to write to. Copy the text into your own config.toml, or open this editor from your Shazzzbot to save there";
  const loaded = await boot();
  state.sets = loaded.data.snapshots.sets;
  state.names = loaded.data.snapshots.names || [];
  state.sim = snapshots()[0].now;
  state.simAt = performance.now();
  state.frames = Object.fromEntries(Object.entries(loaded.data.frames.sets).map(([k, v]) => [k, v.frames]));
  state.useLive = state.live;
  if (state.live) {
    document.body.classList.add("live");
    $("lede").textContent = "Live data from this server. Edit the display settings, then save them.";
  } else {
    toml = loaded.data.toml;
    prebuilt();
  }
  note = mountLog(state.live ? API : null);
  picker();
  tips();
  counter();
  ticker.every(tick);
  pollActivity();
  engine = createEngine({ spawn: () => new Worker("worker.js", { type: "module" }), stage });
  const { mountEditor } = await import("./editor.js");
  SECTIONS = loaded.data.schema.sections.config.order.default;
  const edited = (text) => {
    paintMode(currentMix(text, SECTIONS));
    $("undo").disabled = !editor || !editor.canUndo();
  };
  editor = mountEditor($("editor"), { doc: toml, lint, schema: editorSchema(liveConfig, loaded.data.schema),
    persisted: state.live, onChange: edited });
  edited(toml);
  await call({ op: "boot", base: loaded.base, files: loaded.files });
  cycle(true);
}

// Example configs replace the whole editor text after a confirmation; Undo puts the old text back.
async function examples() {
  const pick = $("example");
  const dialog = $("example-dialog");
  let list = [];
  try {
    list = await (await fetch("examples.json")).json();
  } catch {
    pick.hidden = true;
    return;
  }
  for (const [i, ex] of list.entries()) pick.append(new Option(ex.name, String(i)));
  pick.addEventListener("change", () => {
    const ex = list[Number(pick.value)];
    pick.value = "";
    if (!ex) return;
    $("example-text").textContent = ex.help + " This replaces the editor contents. Undo puts them back.";
    $("example-title").textContent = "Load " + ex.name + "?";
    dialog.returnValue = "";
    dialog.onclose = async () => {
      if (dialog.returnValue !== "load") return;
      try {
        const res = await fetch(ex.file);
        if (!res.ok) throw new Error("HTTP " + res.status);
        editor.replace(await res.text());
      } catch (e) {
        status("Cannot load the " + ex.name + " example: " + e.message, true);
      }
    };
    dialog.showModal();
  });
}

function fail(text) {
  lifecycle.clear("failed", text);
  status(text, true);
  if ($("splash").hidden) return;
  splashFail(text);
  $("retry").addEventListener("click", () => location.reload(), { once: true });
}

if (typeof document !== "undefined") main().catch((e) => fail(e.message));
