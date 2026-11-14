// Loader contract with fake fetch, clock, and Cache Storage. Run: node docs/demo/loader-test.mjs
import assert from "node:assert/strict";

globalThis.location ??= { href: "https://example.test/docs/demo/" };
const { AuthError, BUDGET, PREFIX, load, openCache, purge } = await import("./loader.js");

function clock() {
  let t = 0;
  return { now: () => t, sleep: async (ms) => { t += ms; } };
}

const ok = (bytes, len = bytes.length) => new Response(new Uint8Array(bytes), { status: 200, headers: { "Content-Length": String(len) } });

function deps(replies, extra = {}) {
  const c = clock();
  const states = [];
  let calls = 0;
  const fetch = async () => {
    const r = replies[Math.min(calls++, replies.length - 1)];
    if (r instanceof Error) throw r;
    return r();
  };
  return { d: { fetch, ...c, cache: null, on: (s) => states.push(s), ...extra }, states, calls: () => calls };
}

const tests = {
  async "retry then succeed"() {
    const { d, states, calls } = deps([new TypeError("NetworkError"), () => new Response("", { status: 503 }), () => ok([1, 2, 3])]);
    assert.deepEqual([...await load({ url: "a/b.js" }, d)], [1, 2, 3]);
    assert.equal(calls(), 3);
    const retries = states.filter((s) => s.status === "retrying");
    assert.deepEqual(retries.map((s) => s.attempt), [2, 3]);
    assert.equal(retries[1].why, "server returned 503");
  },
  async "truncated body is retried"() {
    const { d, states } = deps([() => ok([1, 2], 10), () => ok([1, 2, 3])]);
    await load({ url: "b.wasm" }, d);
    assert.match(states.find((s) => s.status === "retrying").why, /cut off at 20%/);
  },
  async "budget exhaustion fails"() {
    const { d, states } = deps([new TypeError("NetworkError")]);
    await assert.rejects(load({ url: "c.zip" }, d), /Could not load demo\/c.zip/);
    assert.ok(d.now() >= BUDGET);
    assert.equal(states.at(-1).status, "failed");
  },
  async "auth failure never retries"() {
    for (const r of [() => new Response("", { status: 401 }), () => new Response("", { status: 302 })]) {
      const { d, calls } = deps([r]);
      const e = await load({ url: "x/y.js" }, d).catch((err) => err);
      assert.ok(e instanceof AuthError && e.file === "x/y.js");
      assert.equal(calls(), 1);
    }
  },
  async "version change drops the old cache and purge drops all"() {
    const names = new Set([PREFIX + "old", PREFIX + "new", "other"]);
    const store = { keys: async () => [...names], delete: async (n) => names.delete(n), open: async (n) => (names.add(n), n) };
    assert.equal(await openCache("new", store), PREFIX + "new");
    assert.deepEqual([...names].sort(), ["other", PREFIX + "new"]);
    await purge(store);
    assert.deepEqual([...names], ["other"]);
  },
  async "cached copy skips the network"() {
    const cache = { match: async () => new Response(new Uint8Array([9])), put: async () => {} };
    const { d, calls } = deps([() => ok([1])], { cache });
    assert.deepEqual([...await load({ url: "d.js", cache: true }, d)], [9]);
    assert.equal(calls(), 0);
  },
  async "healthy byte progress has no total download deadline"() {
    let elapsed = 0;
    let chunks = 0;
    const bytes = await load({ url: "large.wasm" }, {
      now: () => elapsed, sleep: async () => {}, cache: null, on: () => {},
      fetch: async () => ({ status: 200, headers: new Headers(), body: { getReader: () => ({
        read: async () => {
          elapsed += 15000;
          return chunks++ < 20 ? { done: false, value: new Uint8Array([1]) } : { done: true };
        },
      }) } }),
    });
    assert.ok(elapsed > BUDGET);
    assert.deepEqual([...bytes], Array(20).fill(1));
  },
};

let failed = 0;
for (const [name, fn] of Object.entries(tests)) {
  try {
    await fn();
    console.log("PASS " + name);
  } catch (e) {
    failed++;
    console.log("FAIL " + name + ": " + e.message);
  }
}
process.exitCode = failed ? 1 : 0;
