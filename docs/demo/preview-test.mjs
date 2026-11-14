import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { mountPreview, timingFields, timingText } from "./preview.js";
import { watchImage } from "./render.js";

const css = readFileSync(new URL("preview.css", import.meta.url), "utf8");
const luminance = (hex) => {
  const channels = hex.match(/\w\w/g).map((part) => Number.parseInt(part, 16) / 255)
    .map((value) => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
  return channels.reduce((sum, value, index) => sum + value * [0.2126, 0.7152, 0.0722][index], 0);
};
for (const [index, theme] of [css].entries()) {
  const colors = new Map([...theme.matchAll(/--([\w-]+):\s*#([a-fA-F0-9]{6})/g)]
    .map(([, name, value]) => [name, value]));
  const color = (name) => colors.get(name);
  const background = luminance(color("timing-bg"));
  for (const name of ["timing-label", "text"]) {
    const foreground = luminance(color(name));
    const contrast = (Math.max(background, foreground) + 0.05) / (Math.min(background, foreground) + 0.05);
    assert.ok(contrast >= 4.5, (index ? "light" : "dark") + " timing " + name + " contrast: " + contrast);
  }
}

class Node extends EventTarget {
  constructor(tag) {
    super();
    this.tagName = tag;
    this.children = [];
    this.className = "";
    this.attributes = new Map();
    this.dataset = {};
    this.style = { setProperty() {} };
    this.classList = {
      add: (...names) => { this.className = [...new Set([...this.className.split(" "), ...names])].join(" ").trim(); },
      contains: (name) => this.className.split(" ").includes(name),
    };
    this._text = "";
  }
  set textContent(text) { this._text = String(text); this.children = []; }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  append(...children) {
    children.forEach((child) => this.appendChild(typeof child === "string" ? textNode(child) : child));
  }
  replaceChildren(...children) { this.children = []; this._text = ""; this.append(...children); }
  setAttribute(name, value) { this.attributes.set(name, value); }
  getAttribute(name) { return this.attributes.get(name); }
  attachShadow() { this.shadowRoot = new Node("shadow"); return this.shadowRoot; }
  querySelectorAll(selector) {
    const matches = (node) => selector.startsWith(".") ? node.classList.contains(selector.slice(1))
      : node.tagName === selector;
    return this.children.flatMap((child) => [...(matches(child) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  remove() { this.parentNode.children = this.parentNode.children.filter((child) => child !== this); }
}

function textNode(text) { const node = new Node("text"); node.textContent = text; return node; }

const old = { document: globalThis.document, setTimeout, clearTimeout, now: Date.now };
let time = 1000;
let sequence = 0;
const timers = new Map();
const later = (callback, delay) => {
  const id = ++sequence;
  timers.set(id, { callback, at: time + delay });
  return id;
};
const cancel = (id) => timers.delete(id);
const advance = (milliseconds) => {
  const target = time + milliseconds;
  for (;;) {
    const due = [...timers].filter(([_id, task]) => task.at <= target).sort((a, b) => a[1].at - b[1].at)[0];
    if (!due) break;
    time = due[1].at;
    timers.delete(due[0]);
    due[1].callback();
  }
  time = target;
};

globalThis.document = { createElement: (tag) => new Node(tag), createElementNS: (_namespace, tag) => new Node(tag),
  createTextNode: textNode, baseURI: "https://demo.example/demo/", hidden: false };
globalThis.setTimeout = later;
globalThis.clearTimeout = cancel;
Date.now = () => time;

try {
  assert.equal(timingText({ name: "disks.png" }, -30), "image waiting for next update");
  assert.equal(timingText({}, 0), "embed waiting for next update");
  assert.equal(timingText({ name: "disks.png" }, 5, { state: "running", stage: "render" }),
    "Rendering image...");
  assert.equal(timingText({ name: "disks.png" }, 5, { state: "failed", reason: "timed out" }),
    "Image render failed: timed out");
  const success = 1700000000;
  const clock = new Date(success * 1000).toLocaleTimeString([], {
    hour: "numeric", minute: "2-digit", second: "2-digit",
  });
  assert.deepEqual(timingFields({ timing: {
    last_success: success, sent: false, last_failed: true, reason: "timeout", nominal: 60, planned_gap: 150,
  } }, 49, success + 180), [
    ["Next", "edit in 49s"], ["Last success", clock + " (3m ago)"],
    ["Last attempt", "failed: timeout"], ["Cycle", "nominal every 1m"], ["Planned gap", "2m 30s"],
  ]);
  assert.deepEqual(timingFields({}, NaN, success), [
    ["Next", "not scheduled"], ["Last success", "not sent"], ["Cycle", "nominal unavailable"],
    ["Planned gap", "unavailable"],
  ]);
  assert.equal(timingFields({ timing: { sent: true, last_success: null } }, 5, success)[1][1], "never");
  assert.equal(timingFields({ timing: { sent: true, last_success: Infinity } }, 5, success)[1][1], "never");
  assert.equal(timingFields({ timing: { last_failed: true } }, 5, success)[2][1], "failed: unknown error");
  assert.equal(timingFields({ timing: { nominal: 3600, planned_gap: 0 } }, 5, success)[2][1],
    "nominal every 1h 0m");
  assert.equal(timingFields({ timing: { nominal: 86400 } }, 5, success)[2][1], "nominal every 1d 0h");

  const img = new Node("img");
  const states = [];
  const clean = watchImage(img, (state) => states.push(state), time + 100, { now: () => time,
    setTimeout: later, clearTimeout: cancel });
  img.dispatchEvent(new Event("load"));
  img.dispatchEvent(new Event("error"));
  advance(200);
  assert.deepEqual(states, [{ state: "done" }]);
  clean();

  const failed = new Node("img");
  watchImage(failed, (state) => states.push(state), time + 100, { now: () => time,
    setTimeout: later, clearTimeout: cancel });
  failed.dispatchEvent(new Event("error"));
  assert.deepEqual(states.at(-1), { state: "failed", reason: "image could not be loaded" });
  advance(200);
  assert.equal(states.length, 2, "an image error settles once and cancels its deadline");

  const ignored = new Node("img");
  watchImage(ignored, (state) => states.push(state), time + 100, { now: () => time,
    setTimeout: later, clearTimeout: cancel })();
  advance(200);
  ignored.dispatchEvent(new Event("error"));
  assert.equal(states.length, 2, "superseded image listeners and deadlines are removed");

  const host = new Node("host");
  const events = [];
  const preview = mountPreview(host, { timing: true, onImageState: (state) => events.push(state) });
  preview.setStage({ state: "failed", stage: "render", targets: ["Disks"], reason: "worker failed" });
  const shadow = host.children[0].shadowRoot;
  assert.match(shadow.textContent, /Image render failed: worker failed/);
  const epoch = () => success + time / 1000;
  const frame = () => ({ default: [[
    { key: "disks", name: "disks.png", src: "blob:disks", at: epoch() - 60,
      timing: { last_success: epoch() - 180, sent: true, nominal: 75, planned_gap: 150 },
      embed: { title: "Disks", image: { url: "attachment://disks.png" }, timestamp: "2026-01-01T00:00:00Z" } },
    { key: "tasks", at: epoch() + 60, timing: { sent: false, nominal: 60, planned_gap: null },
      embed: { title: "Tasks", description: "Current tasks" } },
  ]] });
  preview.update(frame(), epoch());
  const lines = () => shadow.querySelectorAll(".timing").map((line) => line.textContent);
  assert.match(lines()[0], /Image render failed: worker failed/);
  assert.match(lines()[1], /edit in 1m 00s/);
  const cards = () => shadow.querySelectorAll(".embed");
  assert.equal(cards().length, 2);
  for (const card of cards()) {
    const bar = card.querySelector(".timing");
    assert.equal(bar.parentNode, card, "the timing strip attaches directly to the embed frame");
    assert.equal(card.children.at(-1), bar);
    assert.equal(card.classList.contains("has-timing"), true);
    assert.equal(card.querySelector(".embed-body").querySelector(".timing"), null);
  }
  const values = (index) => cards()[index].querySelectorAll(".timing-value").map((node) => node.textContent);
  assert.deepEqual(values(1), ["edit in 1m 00s", "not sent", "nominal every 1m", "unavailable"]);
  assert.equal(values(0)[2], "nominal every 1m 15s");
  assert.equal(values(0)[3], "2m 30s", "planned gap is separate from the configured cycle");
  preview.setTiming(false);
  assert.equal(shadow.querySelectorAll(".timing").length, 0);
  assert.equal(cards().every((card) => !card.classList.contains("has-timing")), true);
  preview.setTiming(true);
  assert.equal(shadow.querySelectorAll(".timing").length, 2);
  for (const image of shadow.querySelectorAll(".embed-image")) image.dispatchEvent(new Event("load"));
  advance(61010);
  assert.match(values(0)[1], /\(4m ago\)/, "success age follows the simulated clock, not wall time");
  preview.setStage({ state: "running", stage: "upload", targets: ["Disks"], deadline: time + 100 });
  assert.match(lines()[0], /Uploading image/);
  assert.doesNotMatch(lines()[1], /Uploading/);
  preview.setStage({ state: "done", stage: "upload" });
  assert.match(lines()[0], /waiting for next update/);
  preview.setStage({ state: "running", stage: "render", targets: ["Disks"] });
  preview.setStage({ state: "superseded" });
  assert.match(lines()[0], /waiting for next update/);

  for (let profile = 0; profile < 12; profile += 1) {
    preview.setStage({ state: "running", stage: "render", targets: ["Disks"], deadline: time + 100 });
    preview.update(frame(), epoch());
    advance(100);
    assert.match(lines()[0], /Image render failed: image load timed out/);
    assert.equal(shadow.querySelector(".embed-image").hidden, true);
    assert.equal(events.at(-1).state, "failed");
    assert.doesNotMatch(lines().join(" "), /updating now/);
  }
  preview.destroy();
  assert.equal(timers.size, 0, "destroy removes all image deadlines and ticker subscriptions");
  const initialHost = new Node("host");
  const initial = mountPreview(initialHost, { imageTimeoutMs: 100 });
  initial.update(frame(), epoch());
  assert.equal(initialHost.children[0].shadowRoot.querySelectorAll(".timing").length, 2,
    "timing strips default on");
  advance(100);
  assert.match(initialHost.children[0].shadowRoot.textContent, /Image render failed: image load timed out/);
  initial.destroy();
  assert.equal(timers.size, 0, "prebuilt images use the same supplied ceiling without an active render stage");
  const deliveryHost = new Node("host");
  const delivery = mountPreview(deliveryHost);
  const delivered = (timing) => ({ default: [[{ key: "tasks", at: epoch() + 60,
    timing: { nominal: 60, planned_gap: 60, ...timing },
    embed: { title: "Tasks", description: "Current tasks", timestamp: "2026-01-01T00:00:00Z" },
  }]] });
  const deliveryShadow = deliveryHost.children[0].shadowRoot;
  const deliveryValues = () => deliveryShadow.querySelectorAll(".timing-value").map((node) => node.textContent);
  const queued = delivered({ next: epoch() + 120 });
  delivery.update(queued, epoch());
  assert.equal(deliveryValues()[0], "edit in 2m 00s", "the real queue plan overrides the preview render time");
  assert.equal(queued.default[0][0].at, epoch() + 60, "timing does not rewrite the image or embed render plan");
  delivery.update(delivered({ next: null }), epoch());
  assert.equal(deliveryValues()[0], "not scheduled", "an explicit absent real plan cannot use a preview estimate");
  delivery.update(delivered({}), epoch());
  assert.equal(deliveryValues()[0], "edit in 1m 00s", "a prebuilt frame without queue metadata uses its own plan");
  delivery.update(delivered({ sent: true, last_success: null }), epoch());
  assert.equal(deliveryValues()[1], "never", "a render timestamp is not evidence of a successful delivery");
  delivery.update(delivered({ sent: true, last_success: epoch() - 30, last_failed: true, reason: "refused" }), epoch());
  assert.match(deliveryValues()[1], /\(30s ago\)/);
  assert.equal(deliveryValues()[2], "failed: refused", "a failed attempt retains the previous success");
  delivery.update(delivered({ sent: true, last_success: epoch(), last_failed: false }), epoch());
  assert.match(deliveryValues()[1], /\(0s ago\)/);
  assert.equal(deliveryShadow.querySelectorAll(".timing-label").some((node) => node.textContent === "Last attempt:"),
    false, "a successful delivery clears the failure field");
  const body = deliveryShadow.querySelector(".embed-body").textContent;
  delivery.setTiming(false);
  assert.equal(deliveryShadow.querySelector(".embed-body").textContent, body,
    "hiding timing leaves the Discord embed content unchanged");
  delivery.destroy();
  assert.equal(timers.size, 0);
  console.log("PASS attached timing strips, delivery metadata, toggle, image lifecycle and profile timeout soak");
} finally {
  Date.now = old.now;
  globalThis.document = old.document;
  globalThis.setTimeout = old.setTimeout;
  globalThis.clearTimeout = old.clearTimeout;
}
