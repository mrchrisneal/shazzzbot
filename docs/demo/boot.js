// Loads every asset the preview needs behind the splash, with progress, retries, and recovery buttons.
import { AuthError, BUDGET, load, openCache, purge } from "./loader.js";
import { ticker } from "./ticker.js";

const $ = (id) => document.getElementById(id);
const GUESS = 20000;
const TYPES = { wasm: "application/wasm", mjs: "text/javascript", js: "text/javascript", json: "application/json",
  zip: "application/zip", whl: "application/zip" };
let said = { at: 0, file: "" };
let stop = () => {};
let finished = [];

export async function within(task, label, milliseconds = BUDGET, deps = {}) {
  const later = deps.setTimeout || ((f, ms) => setTimeout(f, ms));
  const cancel = deps.clearTimeout || ((timer) => clearTimeout(timer));
  let timer;
  try {
    return await Promise.race([Promise.resolve().then(task), new Promise((_resolve, reject) => {
      timer = later(() => reject(new Error(label + " timed out")), milliseconds);
    })]);
  } finally {
    cancel(timer);
  }
}

async function json(url) {
  const abort = new AbortController();
  try {
    return await within(async () => {
      const res = await fetch(url, { cache: "no-store", credentials: "same-origin", signal: abort.signal });
      if (!res.ok) throw new Error("Could not load " + url + " (" + res.status + ")");
      return res.json();
    }, "Loading " + url);
  } finally {
    abort.abort();
  }
}

export function boundedCache(cache) {
  return {
    async match(url) {
      const held = await within(() => cache.match(url), "Reading preview cache");
      return held ? { arrayBuffer: () => within(() => held.arrayBuffer(), "Reading cached asset") } : held;
    },
    put(url, response) {
      return within(() => cache.put(url, response), "Saving preview cache");
    },
  };
}

function percent(value) {
  const pct = Math.max(Number($("progress").getAttribute("aria-valuenow")), Math.round(Math.min(value, 1) * 100));
  $("progress").setAttribute("aria-valuenow", String(pct));
  $("ring").setAttribute("stroke-dashoffset", String(100 - pct));
  $("pct").textContent = pct + "%";
}

function line(text, announce) {
  $("splash-text").textContent = text;
  if (announce) $("splash-live").textContent = text;
}

function announceFile(file, text) {
  const now = Date.now();
  if (file !== said.file && now - said.at > 3000) {
    said = { at: now, file };
    $("splash-live").textContent = text;
  }
}

function show(state, overall) {
  stop();
  if (state.status === "loading") {
    const text = "Loading " + state.file + " (" + (state.pct || 0) + "%)...";
    line(text);
    announceFile(state.file, "Loading " + state.file);
  } else if (state.status === "retrying") {
    const draw = () => line("Retrying " + state.file + " in " + Math.max(0, Math.ceil((state.until - Date.now()) / 1000)) +
      "s (attempt " + state.attempt + ") -- " + state.why);
    $("splash-live").textContent = "Retrying " + state.file + " -- " + state.why;
    stop = ticker.every(draw);
  }
  if (overall !== undefined) percent(overall);
}

export function stage(text, value) {
  line(text);
  announceFile(text, text);
  percent(value);
}

export function finish() {
  stop();
  for (const timer of finished) clearTimeout(timer);
  finished = [];
  percent(1);
  line("Finished loading!", true);
  finished.push(setTimeout(() => {
    $("splash").classList.add("done");
    finished.push(setTimeout(() => { $("splash").hidden = true; }, 400));
  }, 500));
}

export function fail(text) {
  stop();
  for (const timer of finished) clearTimeout(timer);
  finished = [];
  $("splash").classList.add("splash-error");
  line(text, true);
  $("splash-actions").hidden = false;
}

export async function purgeAndReload() {
  try {
    await within(async () => {
      await purge();
      const regs = navigator.serviceWorker ? await navigator.serviceWorker.getRegistrations() : [];
      await Promise.all(regs.map((r) => r.unregister()));
    }, "Clearing preview cache");
  } finally {
    location.reload();
  }
}

async function worker(stamp) {
  if (!navigator.serviceWorker || !window.isSecureContext) return;
  try {
    await within(async () => {
      await navigator.serviceWorker.register("sw.js?v=" + stamp);
      await navigator.serviceWorker.ready;
      if (!navigator.serviceWorker.controller) {
        await new Promise((resolve) => {
          navigator.serviceWorker.addEventListener("controllerchange", resolve, { once: true });
          setTimeout(resolve, 3000);
        });
      }
    }, "Starting service worker");
  } catch {
    // Without the service worker Pyodide fetches its own files; loading still works, only uncached.
  }
}

async function local(url) {
  const abort = new AbortController();
  try {
    return await within(async () => {
      const res = await fetch(url, { method: "HEAD", cache: "no-store", credentials: "same-origin",
        signal: abort.signal });
      return res.ok;
    }, "Checking local Python runtime");
  } catch {
    return false;
  } finally {
    abort.abort();
  }
}

// Loads assets in order; on failure waits for Retry, which resumes from the failed asset.
async function loadAll(assets, deps, done) {
  const total = assets.reduce((n, a) => n + (a.bytes || GUESS), 0);
  let base = 0;
  for (const a of assets) {
    if (done.has(a.url)) {
      base += a.bytes || GUESS;
      continue;
    }
    for (;;) {
      try {
        const at = base;
        const bytes = await load(a, { ...deps,
          on: (s) => show(s, (0.85 * (at + ((s.pct || 0) / 100) * (a.bytes || GUESS))) / total) });
        done.set(a.url, bytes);
        break;
      } catch (e) {
        const why = e instanceof AuthError ? "Sign-in required to load " + e.file : e.message;
        fail(why + ".");
        await new Promise((resolve) => $("retry").addEventListener("click", resolve, { once: true }));
        $("splash").classList.remove("splash-error");
        $("splash-actions").hidden = true;
      }
    }
    base += a.bytes || GUESS;
  }
}

export async function boot() {
  $("purge").addEventListener("click", purgeAndReload);
  const manifest = await json("manifest.json");
  await worker(manifest.stamp);
  let cache = null;
  try {
    const opened = await within(() => openCache(manifest.stamp), "Opening preview cache");
    cache = opened && boundedCache(opened);
  } catch {
    cache = null;
  }
  const own = await local("pyodide/pyodide.mjs");
  const base = own ? new URL("pyodide/", location.href).href : manifest.runtime[0].url.replace(/[^/]*$/, "");
  const runtime = manifest.runtime.map((r) => ({ url: r.url.startsWith(base) || !own ? r.url : base + r.name,
    bytes: r.bytes, cache: true, type: TYPES[r.name.split(".").pop()] }));
  const { files } = await json("sources.json");
  const sources = files.map((f) => ({ url: f === "VERSION" ? "../../VERSION" : "../../shazzzbot/" + f, name: f }));
  const data = ["demo.toml", "snapshots.json", "frames.json", "schema.json"].map((url) => ({ url }));
  const vendor = manifest.vendor.map((v) => ({ url: v.path, bytes: v.bytes, cache: true, type: TYPES.js }));
  const assets = [...runtime, ...vendor, ...data, ...sources];
  const done = new Map();
  const deps = { fetch: (u, o) => fetch(u, o), sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
    now: () => Date.now(), cache };
  await loadAll(assets, deps, done);
  const text = (url) => new TextDecoder().decode(done.get(url));
  return {
    base,
    files: Object.fromEntries(sources.map((s) => [s.name, done.get(s.url)])),
    data: { toml: text("demo.toml"), snapshots: JSON.parse(text("snapshots.json")),
      frames: JSON.parse(text("frames.json")), schema: JSON.parse(text("schema.json")) },
  };
}
