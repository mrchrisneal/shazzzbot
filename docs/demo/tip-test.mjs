// Hover tips wait for the pointer to rest. Run: node docs/demo/tip-test.mjs
import assert from "node:assert/strict";
import { rest } from "./tip.js";

let t = 0;
const timers = new Map();
let id = 0;
const later = (f, ms) => (timers.set(++id, { f, at: t + ms }), id);
const cancel = (k) => timers.delete(k);
const advance = (ms) => {
  t += ms;
  for (const [k, v] of [...timers]) if (v.at <= t) { timers.delete(k); v.f(); }
};
let shown = 0;
const h = rest(() => shown++, 500, later, cancel);
h.enter();
advance(499);
assert.equal(shown, 0, "no tip at 499 ms");
advance(1);
assert.equal(shown, 1, "tip at 500 ms");
h.enter();
advance(300);
h.leave();
advance(1000);
assert.equal(shown, 1, "no tip when the pointer leaves early");
console.log("PASS hover tips open only after 500 ms of rest");

class Element extends EventTarget {
  constructor(attrs = {}) {
    super();
    this.attrs = new Map(Object.entries(attrs));
    this.dataset = { tip: "Full status details" };
    this.style = { setProperty() {} };
    this.offsetWidth = 200;
    this.offsetHeight = 50;
    this.visible = false;
  }
  setAttribute(key, value) { this.attrs.set(key, value); }
  getAttribute(key) { return this.attrs.get(key); }
  hasAttribute(key) { return this.attrs.has(key); }
  matches(selector) { return selector === ":popover-open" && this.visible; }
  checkVisibility() { return true; }
  getBoundingClientRect() { return { top: 10, bottom: 54, left: 100, width: 180 }; }
  contains(target) { return target === this; }
  showPopover() { this.visible = true; }
  hidePopover() { this.visible = false; }
}

const saved = { document: globalThis.document, HTMLElement: globalThis.HTMLElement,
  matchMedia: globalThis.matchMedia, innerHeight: globalThis.innerHeight, MutationObserver: globalThis.MutationObserver };
try {
  const compact = new Element({ "data-tip-tap": "" });
  const normal = new Element();
  const created = [];
  const document = new EventTarget();
  document.createElement = () => new Element();
  document.body = { append: (tip) => created.push(tip) };
  document.documentElement = { clientWidth: 375 };
  document.querySelectorAll = () => [compact, normal];
  globalThis.document = document;
  globalThis.HTMLElement = Element;
  globalThis.matchMedia = () => ({ matches: true });
  globalThis.innerHeight = 800;
  const updates = [];
  globalThis.MutationObserver = class {
    constructor(callback) { updates.push(callback); }
    observe(target, options) {
      assert.equal(target, compact);
      assert.deepEqual(options, { attributes: true, attributeFilter: ["data-tip"] });
    }
  };
  const { tips } = await import("./tip.js?touch-test");
  tips(document);
  const tap = (target) => {
    const down = new Event("pointerdown");
    Object.defineProperty(down, "pointerType", { value: "touch" });
    target.dispatchEvent(down);
    target.dispatchEvent(new Event("click"));
  };
  tap(compact);
  assert.equal(created[0].visible, true);
  assert.equal(compact.getAttribute("aria-expanded"), "true");
  assert.equal(updates.length, 1, "only the opted-in status tip watches changing details");
  compact.dataset.tip = "Latest full status";
  updates[0]();
  assert.equal(created[0].textContent, "Latest full status");
  const leave = new Event("pointerleave");
  Object.defineProperty(leave, "pointerType", { value: "touch" });
  compact.dispatchEvent(leave);
  assert.equal(created[0].visible, true, "touch pointerleave must not immediately hide the tip");
  tap(compact);
  assert.equal(created[0].visible, false, "second tap closes the status tip");
  tap(compact);
  document.dispatchEvent(new Event("pointerdown"));
  assert.equal(created[0].visible, false, "tap outside closes the status tip");
  tap(compact);
  const escape = new Event("keydown");
  Object.defineProperty(escape, "key", { value: "Escape" });
  document.dispatchEvent(escape);
  assert.equal(created[0].visible, false, "Escape closes the status tip");
  compact.dispatchEvent(new Event("click"));
  assert.equal(created[0].visible, true, "native keyboard activation opens the status tip");
  compact.dataset.tip = "";
  updates[0]();
  assert.equal(created[0].visible, false, "a cleared status closes its open details");
  document.dispatchEvent(escape);
  tap(normal);
  assert.equal(created[1].visible, false, "ordinary tips keep their no-touch behavior");
  console.log("PASS compact status tap toggles its tip, outside tap and Escape close it, other tips ignore touch");
} finally {
  Object.assign(globalThis, saved);
}
