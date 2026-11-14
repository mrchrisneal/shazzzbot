// Runs shazzzbot's own Python (render and config checks) under Pyodide, off the page's thread.
// Messages in: {id, op: "boot", base, files} first, then {id, op: "feed", snapshot, step?, seed?} | {id, op: "render", toml, images}.
// Out: {id, ok, ...}, {id, error}, or {stage, value} while booting.
import { STALL } from "./loader.js";

let ready = Promise.reject(new Error("the preview engine has not started"));
ready.catch(() => {});
let pd = null;
let pv = null;
let snapshot = null;

export async function withDownloadProgress(action) {
  const original = globalThis.fetch;
  globalThis.fetch = async (input, options = {}) => {
    const abort = new AbortController();
    const upstream = options.signal || (typeof Request !== "undefined" && input instanceof Request && input.signal);
    const signal = upstream ? AbortSignal.any([upstream, abort.signal]) : abort.signal;
    let timer = null;
    const clear = () => { clearTimeout(timer); timer = null; };
    const arm = () => {
      if (timer === null) timer = setTimeout(() => abort.abort(new Error("download stalled")), STALL);
    };
    arm();
    let response;
    try {
      response = await original.call(globalThis, input, { ...options, signal });
    } catch (error) {
      clear();
      throw error;
    }
    if (!response.body) { clear(); return response; }
    const reader = response.body.getReader();
    const body = new ReadableStream({
      async pull(controller) {
        arm();
        try {
          for (;;) {
            const { done, value } = await reader.read();
            if (signal.aborted) throw signal.reason;
            if (done) { clear(); controller.close(); return; }
            if (value.length) {
              clear();
              self.postMessage({ progress: true });
              controller.enqueue(value);
              return;
            }
          }
        } catch (error) {
          clear();
          controller.error(error);
        }
      },
      cancel(reason) { clear(); abort.abort(reason); return reader.cancel(reason); },
    });
    const watched = new Response(body, response);
    for (const name of ["url", "type", "redirected"]) {
      Object.defineProperty(watched, name, { value: response[name] });
    }
    return watched;
  };
  try {
    return await action();
  } finally {
    globalThis.fetch = original;
  }
}

export async function bytes(url) {
  const abort = new AbortController();
  let timer;
  const progress = () => {
    clearTimeout(timer);
    timer = setTimeout(() => abort.abort(), STALL);
    self.postMessage({ progress: true });
  };
  try {
    progress();
    const res = await fetch(url, { credentials: "same-origin", signal: abort.signal });
    if (!res.ok) throw new Error("could not load " + url + " (" + res.status + ")");
    const reader = res.body.getReader();
    const parts = [];
    let size = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (value.length) {
        parts.push(value);
        size += value.length;
        progress();
      }
    }
    const result = new Uint8Array(size);
    let offset = 0;
    for (const part of parts) { result.set(part, offset); offset += part.length; }
    return result.buffer;
  } finally {
    clearTimeout(timer);
  }
}

function stage(text, value) {
  self.postMessage({ stage: text, value });
}

export function installSources(py, files) {
  for (const [name, data] of Object.entries(files)) {
    const path = name === "VERSION" ? "/src/VERSION" : "/src/shazzzbot/" + name;
    py.FS.mkdirTree(path.slice(0, path.lastIndexOf("/")));
    py.FS.writeFile(path, new Uint8Array(data));
  }
}

async function boot(base, files, timeout) {
  return withDownloadProgress(async () => {
    const pins = await (await fetch("runtime.json", { credentials: "same-origin",
      signal: AbortSignal.timeout(timeout) })).json();
    stage("Starting Python", 0.87);
    const mod = await import(base + "pyodide.mjs");
    const py = await mod.loadPyodide({ indexURL: base });
    stage("Installing packages", 0.9);
    await py.loadPackage(pins.packages, { messageCallback: () => self.postMessage({ progress: true }) });
    const site = py.runPython("import site; site.getsitepackages()[0]");
    for (const [name, wheel] of Object.entries(pins.wheels)) {
      py.unpackArchive(await bytes(base === pins.cdn ? wheel.url : base + name), "zip", { extractDir: site });
    }
    stage("Importing Shazzzbot", 0.93);
    installSources(py, files);
    py.runPython("import sys; sys.path.insert(0, '/src')");
    pd = py.pyimport("shazzzbot.previewdata");
    pv = py.pyimport("shazzzbot.previewvary");
    stage("Rendering the first frame", 0.96);
    return py;
  });
}

let queue = Promise.resolve();
self.onmessage = ({ data }) => {
  queue = queue.catch(() => {}).then(() => handle(data)).catch((error) => {
    try {
      self.postMessage({ id: data.id, error: String((error && error.message) || error) });
    } catch {
      setTimeout(() => { throw error; }, 0);
    }
  });
};

async function handle(msg) {
  try {
    if (msg.op === "boot") {
      ready = boot(msg.base, msg.files, msg.timeout);
      await ready;
      self.postMessage({ id: msg.id, ok: true });
      return;
    }
    await ready;
    if (msg.op === "feed") {
      snapshot = msg.step ? pv.vary(msg.snapshot, msg.step, msg.seed || "") : msg.snapshot;
      self.postMessage({ id: msg.id, ok: true });
      return;
    }
    const result = await pd.render(snapshot, msg.toml, !!msg.images);
    let out;
    try {
      out = result.toJs({ dict_converter: Object.fromEntries });
    } finally {
      result.destroy();
    }
    const transfer = new Set();
    for (const view of Object.values(out.frame || {})) {
      for (const msg of view) for (const item of msg) if (item.png) transfer.add(item.png.buffer);
    }
    self.postMessage({ id: msg.id, ok: true, diagnostics: out.diagnostics, frame: out.frame, timing: out.timing },
      [...transfer]);
  } catch (e) {
    self.postMessage({ id: msg.id, error: String((e && e.message) || e) });
  }
}
