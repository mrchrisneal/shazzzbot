import assert from "node:assert/strict";
import { boot, boundedCache, fail, finish, within } from "./boot.js";
import { BUDGET } from "./loader.js";

const old = { document: globalThis.document, fetch: globalThis.fetch, setTimeout, clearTimeout,
  navigator: Object.getOwnPropertyDescriptor(globalThis, "navigator"), window: globalThis.window,
  location: globalThis.location, caches: globalThis.caches };
let sequence = 0;
const timers = new Map();
const later = (callback, milliseconds) => {
  const id = ++sequence;
  timers.set(id, { callback, milliseconds });
  return id;
};
const cancel = (id) => timers.delete(id);
const flush = async () => { for (let turn = 0; turn < 8; turn += 1) await Promise.resolve(); };

try {
  assert.equal(await within(() => "done", "Operation", 100, { setTimeout: later, clearTimeout: cancel }), "done");
  assert.equal(timers.size, 0);
  await assert.rejects(within(() => { throw new Error("failed"); }, "Operation", 100,
    { setTimeout: later, clearTimeout: cancel }), /failed/);
  assert.equal(timers.size, 0);
  const hung = within(() => new Promise(() => {}), "Operation", 100, { setTimeout: later, clearTimeout: cancel });
  assert.equal([...timers.values()][0].milliseconds, 100);
  [...timers.values()][0].callback();
  await assert.rejects(hung, /Operation timed out/);
  assert.equal(timers.size, 0);

  globalThis.setTimeout = later;
  globalThis.clearTimeout = cancel;
  globalThis.document = { getElementById: () => ({ addEventListener() {} }) };
  let signal;
  globalThis.fetch = (_url, options) => {
    signal = options.signal;
    return new Promise(() => {});
  };
  const pending = boot();
  await flush();
  assert.equal([...timers.values()][0].milliseconds, BUDGET);
  [...timers.values()][0].callback();
  await assert.rejects(pending, /Loading manifest.json timed out/);
  assert.equal(signal.aborted, true);
  assert.equal(timers.size, 0);
  const cached = boundedCache({ match: async (url) => url === "hit" ? { arrayBuffer: async () => "bytes" } : undefined,
    put: async () => "saved" });
  assert.equal(await cached.match("miss"), undefined);
  assert.equal(await (await cached.match("hit")).arrayBuffer(), "bytes");
  assert.equal(await cached.put("url", {}), "saved");
  for (const operation of ["match", "read", "put"]) {
    const stuck = boundedCache({ match: () => operation === "match" ? new Promise(() => {})
      : Promise.resolve({ arrayBuffer: () => new Promise(() => {}) }), put: () => new Promise(() => {}) });
    const request = operation === "put" ? stuck.put("url", {})
      : operation === "read" ? (await stuck.match("url")).arrayBuffer() : stuck.match("url");
    await flush();
    [...timers.values()][0].callback();
    await assert.rejects(request, /timed out/);
    assert.equal(timers.size, 0);
  }
  const nodes = new Map();
  globalThis.document = { getElementById: (id) => {
    if (!nodes.has(id)) nodes.set(id, { classList: { add() {} }, getAttribute: () => "0", setAttribute() {},
      addEventListener() {} });
    return nodes.get(id);
  } };
  finish();
  assert.equal(timers.size, 1);
  fail("Rendering failed");
  assert.equal(timers.size, 0, "a stale finish callback cannot hide a later failure");
  assert.equal(nodes.get("splash-text").textContent, "Rendering failed");
  assert.equal(nodes.get("splash-actions").hidden, false);
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { serviceWorker: {
    register: async () => {}, ready: new Promise(() => {}), controller: {},
  } } });
  globalThis.window = { isSecureContext: true };
  globalThis.location = { href: "https://demo.example/demo/" };
  globalThis.caches = undefined;
  const fetched = [];
  const versionURL = new URL("../../VERSION", location.href).href;
  const packageURL = new URL("../../shazzzbot/__init__.py", location.href).href;
  globalThis.fetch = async (url, options) => {
    fetched.push(url);
    if (options.method === "HEAD") return { ok: false };
    if (url === "manifest.json") return { ok: true, json: async () => ({ stamp: "test", vendor: [],
      runtime: [{ url: "https://cdn.example/pyodide.mjs", name: "pyodide.mjs" }] }) };
    if (url === "sources.json") return { ok: true, json: async () => ({ files: ["VERSION", "__init__.py"] }) };
    const text = url.endsWith("/schema.json")
      ? '{"format":1,"version":"test-version","sections":{"config":{}},"editor":{}}'
      : url.endsWith(".json") ? '{"sets":{}}' : url === versionURL ? "9.8.7\n" : "data";
    return { status: 200, headers: new Headers(), arrayBuffer: async () => new TextEncoder().encode(text).buffer };
  };
  const serviceWorker = boot();
  await flush();
  assert.equal([...timers.values()][0].milliseconds, BUDGET);
  [...timers.values()][0].callback();
  const loaded = await serviceWorker;
  assert.deepEqual(loaded.data.snapshots, { sets: {} });
  assert.equal(loaded.data.schema.version, "test-version");
  assert(fetched.includes(versionURL));
  assert(fetched.includes(packageURL));
  assert(!fetched.includes(new URL("../../shazzzbot/VERSION", location.href).href));
  assert.equal(new TextDecoder().decode(loaded.files.VERSION), "9.8.7\n");
  assert.equal(timers.size, 0, "a stalled service worker falls back to uncached loading");
  console.log("PASS boot metadata requests have a bounded deadline and abort on failure");
} finally {
  globalThis.document = old.document;
  globalThis.fetch = old.fetch;
  globalThis.setTimeout = old.setTimeout;
  globalThis.clearTimeout = old.clearTimeout;
  if (old.navigator) Object.defineProperty(globalThis, "navigator", old.navigator);
  else delete globalThis.navigator;
  globalThis.window = old.window;
  globalThis.location = old.location;
  globalThis.caches = old.caches;
}
