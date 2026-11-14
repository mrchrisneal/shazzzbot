// The log under the preview. The Activity feed (what the preview and bot are doing) is always on; served by a
// bot, the App feed polls its redacted /api/log every 2 s while open, and a toggle picks Combined, Activity, or App.
const POLL_MS = 2000;
export const APP_KEEP = 2000;
export const ACTIVITY_KEEP = 500;
const LEVELS = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };
const FEED_KEY = "shazzzbot-log-feed";

export function push(list, rows, cap) {
  list.push(...rows);
  if (list.length > cap) list.splice(0, list.length - cap);
}

export function view(feed, activity, app) {
  if (feed === "activity") return activity;
  if (feed === "app") return app;
  const out = [];
  let i = 0;
  let j = 0;
  while (i < activity.length || j < app.length) {
    out.push(j >= app.length || (i < activity.length && activity[i].t <= app[j].t) ? activity[i++] : app[j++]);
  }
  return out;
}

export function mountLog(api) {
  const $ = (id) => document.getElementById(id);
  const list = $("log-lines");
  const activity = [];
  const app = [];
  const state = { seq: 0, timer: 0, paused: false, open: false, failed: false, feed: "activity", dirty: false };
  const fmt = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit",
    hourCycle: "h23" });

  function row(r) {
    const li = document.createElement("li");
    li.className = "log-row" + (r.error ? " log-error" : "");
    li.dataset.lv = String(LEVELS[r.level] || 20);
    const cells = [["log-time", fmt.format(new Date(r.t * 1000))]];
    if (state.feed === "combined") cells.push(["log-src", r.src]);
    if (r.src === "app") cells.push(["log-level", r.level], ["log-name", r.logger]);
    cells.push(["log-msg", r.message]);
    for (const [c, text] of cells) {
      const span = document.createElement("span");
      span.className = c;
      span.textContent = text;
      li.append(span);
    }
    return li;
  }

  function draw() {
    state.dirty = false;
    if (!state.open || state.paused) return;
    const stick = list.scrollTop + list.clientHeight >= list.scrollHeight - 4;
    list.replaceChildren(...view(state.feed, activity, app).map(row));
    if (stick) list.scrollTop = list.scrollHeight;
  }

  function later() {
    if (!state.dirty) {
      state.dirty = true;
      requestAnimationFrame(draw);
    }
  }

  async function poll() {
    clearTimeout(state.timer);
    if (!api || !state.open || state.paused) return;
    try {
      const res = await fetch(api + "log?after=" + state.seq, { credentials: "same-origin", cache: "no-store" });
      if (!res.ok) throw new Error("log failed (" + res.status + ")");
      const body = await res.json();
      if (body.seq < state.seq) state.seq = 0;
      push(app, body.records.map((r) => ({ ...r, src: "app" })), APP_KEEP);
      state.seq = body.seq;
      state.failed = false;
      later();
    } catch (e) {
      if (!state.failed) {
        push(app, [{ t: Date.now() / 1000, src: "app", level: "ERROR", logger: "preview",
          message: e.message + "; retrying every 2 s", error: true }], APP_KEEP);
        later();
      }
      state.failed = true;
    }
    if (state.open && !state.paused) state.timer = setTimeout(poll, POLL_MS);
  }

  function pick(feed) {
    state.feed = feed;
    for (const b of document.querySelectorAll("[data-feed]")) b.setAttribute("aria-pressed", String(b.dataset.feed === feed));
    try {
      localStorage.setItem(FEED_KEY, feed);
    } catch {
      // storage may be blocked; the choice then lasts for this page only
    }
    draw();
  }

  if (api) {
    $("log-feeds").hidden = false;
    $("log-note").hidden = true;
    let saved = null;
    try {
      saved = localStorage.getItem(FEED_KEY);
    } catch {
      saved = null;
    }
    state.feed = ["combined", "activity", "app"].includes(saved) ? saved : "combined";
    for (const b of document.querySelectorAll("[data-feed]")) b.addEventListener("click", () => pick(b.dataset.feed));
    pick(state.feed);
  }
  $("log-btn").addEventListener("click", () => {
    state.open = !state.open;
    $("log-panel").hidden = !state.open;
    $("log-btn").setAttribute("aria-expanded", String(state.open));
    if (state.open) {
      draw();
      poll();
    } else clearTimeout(state.timer);
  });
  $("log-pause").addEventListener("click", () => {
    state.paused = !state.paused;
    $("log-pause").setAttribute("aria-pressed", String(state.paused));
    $("log-pause").textContent = state.paused ? "Resume" : "Pause";
    draw();
    poll();
  });
  $("log-level").addEventListener("change", () => {
    list.dataset.min = $("log-level").value;
  });
  $("log-copy").addEventListener("click", async () => {
    const min = Number($("log-level").value);
    const text = [...list.children].filter((li) => Number(li.dataset.lv || 50) >= min)
      .map((li) => [...li.children].map((c) => c.textContent).join(" ")).join("\n");
    try {
      await navigator.clipboard.writeText(text);
      $("log-copy").textContent = "Copied";
    } catch {
      $("log-copy").textContent = "Copy failed";
    }
    setTimeout(() => { $("log-copy").textContent = "Copy"; }, 1500);
  });
  return (message) => {
    push(activity, [{ t: Date.now() / 1000, src: "activity", level: "INFO", message }], ACTIVITY_KEEP);
    if (state.open) later();
  };
}
