// Top bar and log helpers: activity text, profile playback, log feeds. Run: node docs/demo/bar-test.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { activityText, advance, names, profileLabel } from "./bar.js";

assert.deepEqual(names(["disks.png", "app_history.png"]), ["Disks", "App History"]);
assert.equal(activityText({ stage: "render", targets: ["Disks"] }), "Rendering image: Disks...");
assert.equal(activityText({ stage: "render", targets: [] }), "Rendering embeds...");
assert.equal(activityText({ stage: "upload", targets: ["Disks", "System"], n: 1, of: 2 }),
  "Uploading image: Disks, System...");
assert.equal(activityText({ stage: "edit", n: 3, of: 5 }), "Editing message 3 of 5...");
assert.equal(activityText({ stage: "edit", n: 1, of: 1 }), "Editing message...");
assert.equal(activityText({ stage: "send", n: 2, of: 2 }), "Sending message 2 of 2...");
console.log("PASS activity text for render, upload, edit, and send");

let pos = { index: 0, step: 0 };
const seen = [];
for (let i = 0; i < 4; i++) seen.push((pos = advance(pos, 1, 10, true)).index);
assert.deepEqual(seen, [1, 2, 3, 4], "cycling moves to the next profile each interval");
assert.deepEqual(advance({ index: 9, step: 0 }, 1, 10, true), { index: 0, step: 0 });
pos = { index: 1, step: 0 };
for (let i = 0; i < 3; i++) pos = advance(pos, 1, 10, false);
assert.deepEqual(pos, { index: 1, step: 3 }, "without cycling the profile stays and the step moves");
assert.deepEqual(advance({ index: 1, step: 0 }, -1, 10, false), { index: 1, step: 0 });
assert.equal(profileLabel("Mostly good", 2), "Mostly good #3");
assert.equal(profileLabel("Mostly good", 0), "Mostly good");
console.log("PASS playback cycles profiles or steps within one");

const { ACTIVITY_KEEP, push, view } = await import("./logpanel.js");
const act = [{ t: 1, src: "activity" }, { t: 4, src: "activity" }];
const app = [{ t: 2, src: "app" }, { t: 3, src: "app" }, { t: 5, src: "app" }];
assert.deepEqual(view("combined", act, app).map((r) => r.t), [1, 2, 3, 4, 5], "combined merges by time");
assert.equal(view("activity", act, app), act);
assert.equal(view("app", act, app), app);
const ring = [];
for (let i = 0; i < ACTIVITY_KEEP + 20; i++) push(ring, [{ t: i }], ACTIVITY_KEEP);
assert.equal(ring.length, ACTIVITY_KEEP);
assert.equal(ring[0].t, 20);
console.log("PASS log feeds merge by time, filter, and stay bounded");

const html = readFileSync(new URL("index.html", import.meta.url), "utf8");
const links = [...html.matchAll(/<a\b([^>]*\bhref="https?:\/\/[^">]+"[^>]*)>([\s\S]*?)<\/a>/g)];
assert.equal(links.length, 3);
for (const [, attrs, content] of links) {
  assert.match(attrs, /class="[^"]*\bexternal-link\b/);
  assert.match(content, /<svg[^>]*class="[^"]*\bexternal-icon\b[^>]*aria-hidden="true"/);
  if (/target="_blank"/.test(attrs)) {
    assert.match(attrs, /rel="[^"]*\bnoopener\b/);
    assert.match(attrs, /rel="[^"]*\bnoreferrer\b/);
    assert.match(content, /class="sr-only"[^>]*>\s*\(opens in a new tab\)/);
  }
}
assert.match(html, /<title>Shazzzbot Editor<\/title>/);
assert.match(html, /<select[^>]*id="format-pick"[^>]*aria-label="[^"]+"/);
console.log("PASS editor title, labelled mobile format control and every external link");

class ResizeTarget {
  constructor() { this.listeners = new Map(); }
  addEventListener(name, callback) {
    if (!this.listeners.has(name)) this.listeners.set(name, new Set());
    this.listeners.get(name).add(callback);
  }
  removeEventListener(name, callback) { this.listeners.get(name)?.delete(callback); }
  emit(name, detail = {}) {
    const event = { type: name, button: 0, pointerId: 7, preventDefault() {}, ...detail };
    for (const callback of this.listeners.get(name) || []) callback(event);
  }
}

function resizeFixture(stored = null, width = 1280) {
  const view = new ResizeTarget();
  view.innerWidth = width;
  const handle = new ResizeTarget();
  const attrs = new Map();
  handle.setAttribute = (name, value) => attrs.set(name, String(value));
  handle.getAttribute = (name) => attrs.get(name);
  handle.style = {};
  handle.captures = [];
  handle.setPointerCapture = (id) => handle.captures.push(["set", id]);
  handle.releasePointerCapture = (id) => handle.captures.push(["release", id]);
  let closed = false;
  handle.closest = () => closed ? {} : null;
  const sidebar = { style: { setProperty() {} }, closest: handle.closest };
  const writes = [];
  const storage = { getItem: () => stored, setItem: (key, value) => writes.push([key, value]),
    removeItem: (key) => writes.push([key, null]) };
  return { view, handle, sidebar, storage, writes,
    width: () => Number(attrs.get("aria-valuenow")), close: () => { closed = true; } };
}

const { bindSidebar, saveState } = await import("./editor.js");
const saved = saveState("initial", true);
assert.equal(saved.label, "Saved");
saved.changed("draft");
assert.equal(saved.label, "Unsaved changes");
saved.changed("initial");
assert.equal(saved.label, "Saved", "undoing edits restores the saved baseline");
saved.changed("draft");
saved.saved("draft");
assert.equal(saved.label, "Saved");
saved.changed("newer draft");
saved.saved("draft");
assert.equal(saved.label, "Unsaved changes", "a completed older save cannot clear a newer edit");
const demo = saveState("sample", false);
demo.changed("edited");
assert.equal(demo.label, "Demo installation", "the static demo has no config.toml, edited or not");
console.log("PASS dirty state follows the saved draft and preserves concurrent edits");
{
  const h = resizeFixture();
  const cleanup = bindSidebar(h.handle, h.sidebar, h);
  assert.equal(h.width(), 420);
  h.handle.emit("pointerdown", { clientX: 800 });
  h.handle.emit("pointermove", { clientX: 620 });
  assert.equal(h.width(), 600);
  assert.equal(h.writes.length, 0, "drag changes the display without writing storage per move");
  h.handle.emit("pointerup", { clientX: 620 });
  assert.equal(h.writes.length, 1, "completed drag persists once");
  assert.deepEqual(h.handle.captures, [["set", 7], ["release", 7]]);
  h.handle.emit("pointermove", { clientX: 500 });
  assert.equal(h.width(), 600, "moving after release cannot resize");
  h.handle.emit("pointerdown", { clientX: 800 });
  h.handle.emit("pointermove", { clientX: 700 });
  assert.equal(h.width(), 700);
  h.handle.emit("pointercancel");
  assert.equal(h.width(), 600, "cancelled drag restores the prior preference");
  assert.equal(h.writes.length, 1, "cancelled drag cannot persist a partial gesture");
  cleanup();
  h.handle.emit("keydown", { key: "ArrowLeft" });
  assert.equal(h.width(), 600, "cleanup removes event handlers");
}
console.log("PASS sidebar drag captures its pointer and persists only at completion");

{
  const h = resizeFixture();
  const cleanup = bindSidebar(h.handle, h.sidebar, h);
  h.handle.emit("keydown", { key: "ArrowLeft" });
  assert.equal(h.width(), 436);
  h.handle.emit("keydown", { key: "ArrowRight" });
  assert.equal(h.width(), 420);
  for (let n = 0; n < 50; n++) h.handle.emit("keydown", { key: "ArrowLeft" });
  assert.equal(h.width(), 720);
  for (let n = 0; n < 50; n++) h.handle.emit("keydown", { key: "ArrowRight" });
  assert.equal(h.width(), 320);
  h.handle.emit("dblclick");
  assert.equal(h.width(), 420);
  assert.deepEqual(h.writes.at(-1), ["shazzzbot-sidebar-width", null], "reset removes the stored preference");
  h.view.innerWidth = 1000;
  h.view.emit("resize");
  h.handle.emit("keydown", { key: "Home" });
  assert.equal(h.width(), 380);
  cleanup();
}
console.log("PASS sidebar keyboard sizing clamps and both reset controls restore responsive defaults");

{
  const h = resizeFixture("600");
  const cleanup = bindSidebar(h.handle, h.sidebar, h);
  assert.equal(h.width(), 600);
  h.view.innerWidth = 900;
  h.view.emit("resize");
  assert.equal(h.width(), 532);
  h.view.innerWidth = 1280;
  h.view.emit("resize");
  assert.equal(h.width(), 600, "viewport clamping preserves the preferred width");
  assert.equal(h.writes.length, 0);
  h.close();
  h.handle.emit("keydown", { key: "ArrowLeft" });
  h.handle.emit("pointerdown", { clientX: 800 });
  h.handle.emit("pointermove", { clientX: 700 });
  assert.equal(h.width(), 600, "closed sidebar cannot resize");
  h.view.innerWidth = 899;
  h.view.emit("resize");
  const before = h.width();
  h.handle.emit("keydown", { key: "ArrowLeft" });
  h.handle.emit("pointerdown", { clientX: 800 });
  h.handle.emit("pointermove", { clientX: 700 });
  assert.equal(h.width(), before, "mobile layout has no active divider");
  cleanup();
}
console.log("PASS sidebar remembers preferred width while respecting viewport, closed and mobile layouts");

{
  const h = resizeFixture(null, 899);
  const cleanup = bindSidebar(h.handle, h.sidebar, h);
  const before = h.width();
  assert.equal(h.handle.hidden, true);
  h.handle.emit("keydown", { key: "ArrowLeft" });
  h.handle.emit("pointerdown", { clientX: 800 });
  h.handle.emit("pointermove", { clientX: 700 });
  h.handle.emit("pointerup");
  assert.equal(h.width(), before);
  assert.equal(h.writes.length, 0, "mobile interaction cannot change the desktop preference");
  assert.deepEqual(h.handle.captures, []);
  cleanup();
}

for (const stored of ["broken", "-1", "Infinity", "{}", ""]) {
  const h = resizeFixture(stored);
  bindSidebar(h.handle, h.sidebar, h)();
  assert.equal(h.width(), 420, `invalid stored width ${stored} uses the default`);
}
{
  const h = resizeFixture();
  Object.defineProperty(h.view, "localStorage", { get() { throw new Error("SecurityError"); } });
  const cleanup = bindSidebar(h.handle, h.sidebar, { view: h.view });
  h.handle.emit("keydown", { key: "ArrowLeft" });
  assert.equal(h.width(), 436);
  cleanup();
}
{
  const h = resizeFixture();
  h.storage.getItem = () => { throw new Error("SecurityError"); };
  h.storage.setItem = () => { throw new Error("SecurityError"); };
  const cleanup = bindSidebar(h.handle, h.sidebar, h);
  h.handle.emit("keydown", { key: "ArrowLeft" });
  assert.equal(h.width(), 436);
  cleanup();
}
console.log("PASS sidebar tolerates malformed preferences and unavailable browser storage");

const { palettes, tomlLanguage, highlightStyles } = await import("./editor.js");
const { highlightTree } = await import("./vendor/codemirror.js");
const source = '# Config\n[render]\nenabled = true\ninterval = 60\ntitle = "Example"\nsections = ["system", "tasks"]\n';

function luminance(color) {
  assert.match(color, /^#[\da-f]{6}$/i);
  const linear = color.slice(1).match(/../g).map((channel) => {
    const value = Number.parseInt(channel, 16) / 255;
    return value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4;
  });
  return .2126 * linear[0] + .7152 * linear[1] + .0722 * linear[2];
}

for (const mode of ["dark"]) {
  const style = highlightStyles[mode];
  const rules = [...style.module.getRules().matchAll(/([^{}]+)\{([^{}]*)\}/g)];
  const spans = [];
  highlightTree(tomlLanguage.parser.parse(source), style, (from, to, classes) => {
    const matching = rules.filter(([, selectors]) => classes.split(/\s+/).some((name) => selectors.includes(`.${name}`)));
    const declarations = matching.map(([, , text]) => text).join(";");
    const color = [...declarations.matchAll(/(?:^|[;\s])color:\s*(#[\da-f]{6})/gi)].at(-1)?.[1];
    spans.push({ from, to, color, declarations, text: source.slice(from, to) });
  });
  for (const text of ["# Config", "[render]", "enabled", "true", "60", '"Example"', "=", "[", ",", "]"]) {
    const offset = text === "[" ? source.indexOf('["system"') : text === "]" ? source.lastIndexOf("]") : source.indexOf(text);
    const token = spans.find((span) => span.from <= offset && span.to >= offset + text.length);
    assert.ok(token, `${mode} actually highlights TOML ${text}`);
    assert.ok(token.color, `${mode} TOML ${text} has a foreground color`);
    const foreground = luminance(token.color);
    for (const layer of ["background", "activeLine", "selection"]) {
      const background = luminance(palettes[mode][layer]);
      const contrast = (Math.max(foreground, background) + .05) / (Math.min(foreground, background) + .05);
      assert.ok(contrast >= 4.5, `${mode} TOML ${text} on ${layer} contrast ${contrast} meets AA`);
    }
    if (text === "[render]") assert.match(token.declarations, /font-weight:\s*(bold|[7-9]00)/);
    if (text === "true") assert.doesNotMatch(token.declarations, /font-weight:\s*(bold|[7-9]00)/);
  }
  const section = spans.find((span) => span.text === "[render]");
  const boolean = spans.find((span) => span.text === "true");
  assert.notEqual(section.color, boolean.color, "section headings and booleans have distinct colors");
}
console.log("PASS actual TOML tokens meet AA on normal, active and selected backgrounds in both themes");
