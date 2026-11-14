// Shared ticker with a fake clock. Run: node docs/demo/ticker-test.mjs
import assert from "node:assert/strict";
import { createTicker, dur } from "./ticker.js";

let t = 1000;
let pending = null;
const tk = createTicker({ now: () => t, setTimeout: (f, ms) => (pending = { f, at: t + ms }), clearTimeout: () => {} });
const target = t + 50000;
const shown = [];
tk.every(() => shown.push(dur((target - t) / 1000)));
const step = () => {
  t = pending.at;
  pending.f();
};
step();
step();
assert.deepEqual(shown, ["50s", "49s", "48s"]);
t += 10000;
tk.wake();
assert.equal(shown.at(-1), "38s");
assert.equal(dur(723), "12m 03s");
assert.equal(dur(0), "0s");
console.log("PASS ticker steps each second and recovers after a jump");

const errors = [];
let active = null;
let calls = 0;
const isolated = createTicker({ now: () => t, setTimeout: (f) => (active = f), clearTimeout: () => {},
  onError: (error) => errors.push(error.message) });
isolated.every(() => { throw new Error("broken subscriber"); });
let brokenCalls = 0;
isolated.every(() => { if (++brokenCalls > 1) throw new Error("failed during a tick"); });
const unsubscribe = isolated.every(() => { calls += 1; });
active();
active();
assert.deepEqual(errors, ["broken subscriber", "failed during a tick"]);
assert.equal(brokenCalls, 2, "a broken subscriber is removed rather than retried forever");
assert.equal(calls, 3, "one failing subscriber cannot freeze the others or future ticks");
unsubscribe();
console.log("PASS ticker isolates subscriber exceptions and keeps rearming");
