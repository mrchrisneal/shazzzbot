// Loads one asset with retries: backoff 1, 2, 4, 8, then every 15 s, within 120 s per asset. Assets marked cache
// go into Cache Storage under a name carrying the build stamp, so a new build starts clean.
// on(state) gets {file, status: "loading"|"retrying"|"failed"|"done", pct, attempt, why, until, auth}.
export const BACKOFF = [1000, 2000, 4000, 8000, 15000];
export const BUDGET = 120000;
export const STALL = 20000;
export const PREFIX = "shazzzbot-preview-";

export class AuthError extends Error {
  constructor(file) {
    super("sign-in required");
    this.file = file;
  }
}

export function label(url) {
  return new URL(url, location.href).pathname.split("/").filter(Boolean).slice(-2).join("/");
}

export async function openCache(stamp, store = globalThis.caches) {
  if (!store) return null;
  for (const name of await store.keys()) if (name.startsWith(PREFIX) && name !== PREFIX + stamp) await store.delete(name);
  return store.open(PREFIX + stamp);
}

export async function purge(store = globalThis.caches) {
  if (store) for (const name of await store.keys()) if (name.startsWith(PREFIX)) await store.delete(name);
}

function why(e) {
  if (e && e.name === "AbortError") return "timed out";
  const m = String((e && e.message) || e || "network error");
  return /network|fetch|body|stream/i.test(m) ? "connection failed or was cut off" : m;
}

async function body(res, file, on, stall) {
  if (res.status === 401 || res.type === "opaqueredirect" || (res.status >= 300 && res.status < 400)) {
    throw new AuthError(file);
  }
  if (res.status !== 200 || res.redirected || res.type === "opaque") throw new Error("server returned " + res.status);
  const size = Number(res.headers.get("Content-Length")) || 0;
  const parts = [];
  let got = 0;
  let shown = -1;
  if (res.body && res.body.getReader) {
    const reader = res.body.getReader();
    for (;;) {
      stall();
      const { done, value } = await reader.read();
      if (done) break;
      parts.push(value);
      got += value.length;
      const pct = size ? Math.min(99, Math.floor((got / size) * 100)) : 0;
      if (pct !== shown) on({ file, status: "loading", pct: (shown = pct) });
    }
  } else {
    const all = new Uint8Array(await res.arrayBuffer());
    parts.push(all);
    got = all.length;
  }
  if (!got) throw new Error("empty body");
  if (size && got < size) throw new Error("cut off at " + Math.floor((got / size) * 100) + "%");
  const out = new Uint8Array(got);
  let at = 0;
  for (const p of parts) {
    out.set(p, at);
    at += p.length;
  }
  return out;
}

// asset: {url, cache?, type?}. deps: {fetch, sleep, now, cache, on}; cache may be null.
export async function load(asset, deps) {
  const file = label(asset.url);
  const { on } = deps;
  const url = new URL(asset.url, location.href).href;
  if (asset.cache && deps.cache) {
    const held = await deps.cache.match(url);
    if (held) {
      const bytes = new Uint8Array(await held.arrayBuffer());
      if (bytes.length) {
        on({ file, status: "done", pct: 100 });
        return bytes;
      }
    }
  }
  const first = deps.now();
  for (let attempt = 1; ; attempt++) {
    const abort = new AbortController();
    let timer = 0;
    const stall = () => {
      clearTimeout(timer);
      timer = setTimeout(() => abort.abort(), STALL);
    };
    try {
      on({ file, status: "loading", pct: 0, attempt });
      stall();
      const res = await deps.fetch(url, { cache: "no-store", redirect: "manual", credentials: "same-origin",
        signal: abort.signal });
      const bytes = await body(res, file, on, stall);
      if (asset.cache && deps.cache) {
        await deps.cache.put(url, new Response(bytes, {
          headers: { "Content-Type": asset.type || "application/octet-stream" } }));
      }
      on({ file, status: "done", pct: 100 });
      return bytes;
    } catch (e) {
      if (e instanceof AuthError) {
        on({ file, status: "failed", why: e.message, auth: true });
        throw e;
      }
      const left = BUDGET - (deps.now() - first);
      if (left <= 0) {
        on({ file, status: "failed", why: why(e), attempt });
        throw new Error("Could not load " + file + " -- " + why(e));
      }
      const wait = Math.min(BACKOFF[Math.min(attempt - 1, BACKOFF.length - 1)], left);
      on({ file, status: "retrying", attempt: attempt + 1, why: why(e), until: deps.now() + wait });
      await deps.sleep(wait);
    } finally {
      clearTimeout(timer);
    }
  }
}
