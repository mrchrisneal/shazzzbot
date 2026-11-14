// Discord message preview: draws shazzzbot frames the way the Discord client shows them, one view at a time.
//   const p = mountPreview(el, { mode: "default" });  // or "text", "rich", "image"
//   p.update({ default: [[{ embed, src }]], text: [[{ embed }]] }); p.setMode("rich"); p.destroy();
// A view is a list of messages, each a list of { embed, name?, src?, file? }; embeds are discord.Embed.to_dict().
// With { timing: true }, an editor-only line inside each embed with "at" (unix seconds, the footer's time), below
// the footer, counts down to it; update(frame, now) anchors "at" to the frame's clock. An item "lead" (seconds)
// marks the bot's pre-render window before "at". The line is never part of the embed data, so no export sees it.
// A view missing from a frame keeps its last render. Clicking an image opens it in a viewer, as Discord does.
import { discordTime, renderEmbed } from "./render.js";
import { dur, ticker } from "./ticker.js";

export const MODES = ["default", "text", "rich", "image"];

function safeSrc(src) {
  if (typeof src !== "string") return null;
  if (src.startsWith("blob:")) return src;
  if (!/^[A-Za-z0-9_][A-Za-z0-9_./-]*$/.test(src) || src.split("/").includes("..")) return null;
  const base = new URL(".", document.baseURI);
  const url = new URL(src, base);
  return url.origin === base.origin && url.pathname.startsWith(base.pathname) ? src : null;
}

function resolver(items) {
  return (url) => {
    if (typeof url !== "string" || !url.startsWith("attachment://")) return null;
    const name = url.slice("attachment://".length);
    const hit = items.find((e) => e.name === name || (typeof e.file === "string" && e.file.split("/").pop() === name));
    return hit ? safeSrc(hit.src || hit.file) : null;
  };
}

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function messages(view) {
  if (!Array.isArray(view)) return null;
  const item = (x) => (x && typeof x === "object" && x.embed && typeof x.embed === "object" ? x : { embed: x });
  return Array.isArray(view[0]) ? view.map((m) => (Array.isArray(m) ? m.map(item) : [])) : [view.map(item)];
}

function sent(items) {
  const times = items.map((i) => Date.parse(i.embed.timestamp)).filter((t) => !isNaN(t));
  return times.length ? new Date(Math.max(...times)) : new Date();
}

const ICONS = {
  open: "M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5",
  close: "M6 6l12 12M18 6L6 18",
  clock: "M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18M12 7v5l3 2",
};

function icon(name, size = 20) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(ns, "path");
  for (const [k, v] of Object.entries({ d: ICONS[name], fill: "none", stroke: "currentColor", "stroke-width": "2",
    "stroke-linecap": "round", "stroke-linejoin": "round" })) path.setAttribute(k, v);
  svg.appendChild(path);
  return svg;
}

function author(opts, when, cls) {
  const avatar = el("img", "avatar");
  avatar.alt = "";
  avatar.addEventListener("error", () => avatar.classList.add("avatar-missing"), { once: true });
  avatar.src = opts.avatar;
  const head = el("h3", cls);
  const time = el("time", "msg-time", discordTime(when, cls === "msg-head" ? new Date() : new Date(8.64e15)));
  time.dateTime = when.toISOString();
  head.append(el("span", "username", opts.name), el("span", "app-tag", "APP"), time);
  return [avatar, head];
}

function viewer(root, opts) {
  const dialog = el("dialog", "viewer");
  dialog.setAttribute("aria-label", "Image viewer");
  const who = el("div", "viewer-author");
  const tools = el("div", "viewer-tools");
  const open = el("a", "viewer-btn");
  open.target = "_blank";
  open.rel = "noopener";
  open.setAttribute("aria-label", "Open in Browser");
  open.title = "Open in Browser";
  open.appendChild(icon("open"));
  tools.appendChild(open);
  const close = el("button", "viewer-close");
  close.type = "button";
  close.setAttribute("aria-label", "Close");
  close.appendChild(icon("close"));
  close.addEventListener("click", () => dialog.close());
  const img = el("img", "viewer-image");
  dialog.append(who, tools, close, img);
  dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); });
  root.appendChild(dialog);
  return (src, alt, when) => {
    img.src = open.href = src;
    img.alt = alt;
    who.replaceChildren(...author(opts, when, "viewer-head"));
    dialog.showModal();
    close.focus();
  };
}

export function timingText(item, left, stage = null) {
  const what = item.name ? "Image" : "Embed";
  if (stage && stage.state === "failed") return what + " render failed: " + (stage.reason || "unknown error");
  if (stage && stage.state === "running") {
    const verb = { render: "Rendering", upload: "Uploading", edit: "Editing", send: "Sending" }[stage.stage]
      || "Updating";
    return verb + " " + what.toLowerCase() + "...";
  }
  if (!Number.isFinite(left)) return "not scheduled";
  if (left <= 0) return what.toLowerCase() + " waiting for next update";
  return (item.name ? "image" : "edit") + " in " + dur(left);
}

function compact(seconds) {
  const n = Math.max(0, Math.floor(seconds));
  if (n >= 86400) return Math.floor(n / 86400) + "d " + Math.floor(n % 86400 / 3600) + "h";
  if (n >= 3600) return Math.floor(n / 3600) + "h " + Math.floor(n % 3600 / 60) + "m";
  if (n >= 60) return Math.floor(n / 60) + "m" + (n % 60 ? " " + n % 60 + "s" : "");
  return n + "s";
}

export function timingFields(item, left, now, stage = null) {
  const timing = item.timing || {};
  const success = timing.last_success;
  const date = new Date(success * 1000);
  const known = Number.isFinite(success) && Number.isFinite(date.getTime());
  const last = known ? date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" })
    + " (" + compact(now - success) + " ago)" : timing.sent ? "never" : "not sent";
  const fields = [["Next", timingText(item, left, stage)], ["Last success", last]];
  if (timing.last_failed) fields.push(["Last attempt", "failed: " + (timing.reason || "unknown error")]);
  fields.push(["Cycle", Number.isFinite(timing.nominal) && timing.nominal > 0
    ? "nominal every " + compact(timing.nominal) : "nominal unavailable"]);
  fields.push(["Planned gap", Number.isFinite(timing.planned_gap) && timing.planned_gap >= 0
    ? compact(timing.planned_gap) : "unavailable"]);
  return fields;
}

function timingBar(line, fields, tag) {
  const parts = fields.map(([label, value]) => {
    const part = el("span", "timing-part");
    part.append(el("span", "timing-label", label + ":"), el("span", "timing-value", value));
    return part;
  });
  line.replaceChildren(icon("clock", 14), ...parts);
  if (tag) line.append(el("span", "timing-tag", "(" + tag + ")"));
}

function matchingStage(item, stage) {
  if (!stage || stage.state === "done" || stage.state === "superseded") return null;
  const targets = stage.targets || [];
  const name = (text) => String(text || "").replace(/\.png$/, "").replace(/_/g, " ").toLowerCase();
  if (targets.length) {
    const choices = [name(item.key), name(item.name), name(item.embed && item.embed.title)];
    return targets.some((target) => choices.includes(name(target))) ? stage : null;
  }
  return !!item.name === (stage.stage === "upload") ? stage : null;
}

function message(items, first, opts, clocks) {
  const msg = el("div", "msg" + (first ? " msg-first" : ""));
  if (first) msg.append(...author(opts, sent(items), "msg-head"));
  const box = el("div", "accessories");
  const resolve = resolver(items);
  for (const item of items) {
    const card = renderEmbed(item.embed, resolve, {
      deadline: opts.activity && opts.activity.deadline || (Number.isFinite(opts.imageTimeoutMs)
        ? Date.now() + opts.imageTimeoutMs : undefined),
      track: (cleanup) => opts.media.push(cleanup),
      onImageState: (state) => {
        if (state.state === "failed") {
          opts.activity = { ...state, stage: "render", targets: [item.name] };
          opts.tick();
        }
        if (opts.onImageState) opts.onImageState({ ...state, name: item.name, targets: [item.name] });
      },
    });
    box.appendChild(card);
    if (opts.timing) {
      const line = el("div", "timing");
      card.classList.add("has-timing");
      card.appendChild(line);
      const planned = item.timing && "next" in item.timing ? item.timing.next : item.at;
      const target = Number.isFinite(planned) ? opts.anchor + (planned - opts.now) * 1000 : NaN;
      clocks.push({ item, line, target });
    }
  }
  for (const img of box.querySelectorAll(".embed-image")) {
    img.tabIndex = 0;
    img.setAttribute("role", "button");
    img.dataset.sent = sent(items).toISOString();
  }
  msg.append(box, el("span", "edited", "(edited)"));
  return msg;
}

export function mountPreview(host, options = {}) {
  const opts = { mode: "default", name: "shazzzbot", timing: true,
    avatar: new URL("icon.svg", import.meta.url).href, ...options };
  const box = document.createElement("div");
  host.appendChild(box);
  const root = box.attachShadow({ mode: "open" });
  const css = document.createElement("link");
  css.rel = "stylesheet";
  css.href = new URL("preview.css", import.meta.url).href;
  const chat = el("div", "chat");
  chat.setAttribute("role", "log");
  chat.setAttribute("aria-label", "Panel preview");
  root.append(css, chat);
  const show = viewer(root, opts);
  const pick = (e) => {
    const img = e.target.closest && e.target.closest(".embed-image");
    if (!img || e.type === "keydown" && e.key !== "Enter" && e.key !== " ") return;
    e.preventDefault();
    show(img.src, img.alt, new Date(img.dataset.sent));
  };
  chat.addEventListener("click", pick);
  chat.addEventListener("keydown", pick);
  const last = {};
  const clocks = [];
  opts.media = [];
  let stop = () => {};

  function tick() {
    const t = Date.now();
    for (const c of clocks) {
      timingBar(c.line, timingFields(c.item, (c.target - t) / 1000,
        opts.now + (t - opts.anchor) / 1000, matchingStage(c.item, opts.activity)), c.item.tag);
    }
  }
  opts.tick = tick;

  function draw() {
    stop();
    for (const cleanup of opts.media) cleanup();
    opts.media = [];
    clocks.length = 0;
    const list = last[opts.mode];
    if (!list) {
      const pending = opts.activity;
      const text = pending ? timingText({ name: pending.targets && pending.targets.length }, 0, pending)
        : "Waiting for preview.";
      chat.replaceChildren(el("p", "empty", text));
      return;
    }
    const shown = list.filter((m) => m.length);
    chat.replaceChildren(...(shown.length ? shown.map((m, i) => message(m, i === 0, opts, clocks))
      : [el("p", "empty", "No embeds in this view.")]));
    if (clocks.length) {
      stop = ticker.every(tick);
    }
  }

  draw();
  return {
    update(frame, now) {
      opts.now = Number.isFinite(now) ? now : Date.now() / 1000;
      opts.anchor = Date.now();
      for (const m of MODES) {
        const list = frame && messages(frame[m]);
        if (list) last[m] = list;
      }
      draw();
    },
    setMode(mode) {
      if (MODES.includes(mode)) opts.mode = mode;
      draw();
    },
    setTiming(on) {
      opts.timing = !!on;
      draw();
    },
    setStage(stage) {
      if (stage && stage.state === "superseded") {
        for (const cleanup of opts.media) cleanup();
        opts.media = [];
      }
      opts.activity = stage && !["done", "superseded"].includes(stage.state) ? stage : null;
      if (!last[opts.mode]) draw();
      else tick();
    },
    destroy() {
      stop();
      for (const cleanup of opts.media) cleanup();
      box.remove();
    },
  };
}
