// One page-wide 1 s ticker aligned to the wall-clock second. Subscribers keep an absolute target and render
// target minus now, so a sleeping tab or a skipped tick never leaves a wrong count.
export function dur(seconds) {
  const s = Math.max(0, Math.ceil(seconds));
  return s < 60 ? s + "s" : Math.floor(s / 60) + "m " + String(s % 60).padStart(2, "0") + "s";
}

export function createTicker(deps = {}) {
  const now = deps.now || (() => Date.now());
  const later = deps.setTimeout || ((f, ms) => setTimeout(f, ms));
  const cancel = deps.clearTimeout || ((t) => clearTimeout(t));
  const hidden = deps.hidden || (() => typeof document !== "undefined" && document.hidden);
  const onError = deps.onError || ((error) => console.error(error));
  const subs = new Set();
  let timer = 0;

  function invoke(f) {
    try {
      f();
    } catch (error) {
      subs.delete(f);
      onError(error);
    }
  }
  function run() {
    for (const f of [...subs]) if (subs.has(f)) invoke(f);
  }
  function arm() {
    cancel(timer);
    if (hidden() || !subs.size) return;
    timer = later(() => {
      try {
        run();
      } finally {
        arm();
      }
    }, 1000 - (now() % 1000) + 5);
  }
  return {
    every(f) {
      subs.add(f);
      invoke(f);
      arm();
      return () => {
        subs.delete(f);
        if (!subs.size) cancel(timer);
      };
    },
    wake() {
      if (!hidden()) run();
      arm();
    },
  };
}

export const ticker = createTicker();
if (typeof document !== "undefined") document.addEventListener("visibilitychange", () => ticker.wake());
