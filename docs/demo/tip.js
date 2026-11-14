// Hover tooltips for every [data-tip] control, after the GeneratorPi tip(): a manual popover shown after the mouse
// rests OPEN_MS on the control, or on keyboard focus. Compact status can opt into touch.
const GAP = 8;
const GUTTER = 16;
const CLOSE_MS = 100;
const OPEN_MS = 500;
const POPOVER = typeof HTMLElement !== "undefined" && "showPopover" in HTMLElement.prototype;
let seq = 0;

function place(trigger, tip) {
  const r = trigger.getBoundingClientRect();
  const w = tip.offsetWidth;
  const h = tip.offsetHeight;
  const vw = document.documentElement.clientWidth;
  let top = r.top - h - GAP;
  let side = "top";
  if (top < GUTTER && r.bottom + GAP + h <= innerHeight - GUTTER) {
    top = r.bottom + GAP;
    side = "bottom";
  }
  const left = Math.min(Math.max(r.left + r.width / 2 - w / 2, GUTTER), Math.max(GUTTER, vw - GUTTER - w));
  tip.style.setProperty("top", Math.round(Math.max(GUTTER / 2, top)) + "px");
  tip.style.setProperty("left", Math.round(left) + "px");
  tip.style.setProperty("--arrow-x", Math.round(Math.min(Math.max(r.left + r.width / 2 - left, GUTTER), w - GUTTER)) + "px");
  tip.dataset.side = side;
}

// Opens only after the pointer rests `ms` on one target; leaving first cancels it.
export function rest(open, ms = OPEN_MS, later = setTimeout, cancel = clearTimeout) {
  let timer = 0;
  return {
    enter() {
      cancel(timer);
      timer = later(open, ms);
    },
    leave() {
      cancel(timer);
    },
  };
}

function tip(trigger) {
  const t = document.createElement("span");
  t.className = "tip";
  t.textContent = trigger.dataset.tip;
  t.id = "tip-" + ++seq;
  t.setAttribute("popover", "manual");
  t.setAttribute("role", "tooltip");
  if (t.textContent !== trigger.getAttribute("aria-label")) trigger.setAttribute("aria-describedby", t.id);
  document.body.append(t);
  let timer = 0;
  const tap = trigger.hasAttribute("data-tip-tap");
  if (tap) trigger.setAttribute("aria-expanded", "false");
  const open = () => t.matches(":popover-open");
  const outside = (e) => { if (!trigger.contains(e.target) && !t.contains(e.target)) hide(); };
  const esc = (e) => {
    if (e.key === "Escape" && open()) {
      e.stopPropagation();
      hide();
    }
  };
  function show() {
    clearTimeout(timer);
    t.textContent = trigger.dataset.tip;
    if (open() || !trigger.checkVisibility()) return;
    t.showPopover();
    place(trigger, t);
    document.addEventListener("keydown", esc, true);
    if (tap) {
      trigger.setAttribute("aria-expanded", "true");
      document.addEventListener("pointerdown", outside);
    }
  }
  const hover = rest(show);
  function hide() {
    clearTimeout(timer);
    hover.leave();
    if (open()) t.hidePopover();
    document.removeEventListener("keydown", esc, true);
    if (tap) {
      trigger.setAttribute("aria-expanded", "false");
      document.removeEventListener("pointerdown", outside);
    }
  }
  const later = (e) => {
    hover.leave();
    if (tap && e.pointerType === "touch") return;
    if (e.pointerType === "mouse") timer = setTimeout(hide, CLOSE_MS);
    else hide();
  };
  trigger.addEventListener("pointerenter", (e) => {
    if (e.pointerType !== "mouse") return;
    if (open()) clearTimeout(timer);
    else hover.enter();
  });
  trigger.addEventListener("pointerleave", later);
  t.addEventListener("pointerenter", () => clearTimeout(timer));
  t.addEventListener("pointerleave", later);
  trigger.addEventListener("focus", () => { if (trigger.matches(":focus-visible")) show(); });
  trigger.addEventListener("blur", hide);
  if (tap) new MutationObserver(() => {
    if (!trigger.dataset.tip) { hide(); return; }
    if (open()) {
      t.textContent = trigger.dataset.tip;
      place(trigger, t);
    }
  }).observe(trigger, { attributes: true, attributeFilter: ["data-tip"] });
  trigger.addEventListener("click", (e) => {
    const toggle = tap && matchMedia("(max-width: 899px)").matches;
    if (toggle) {
      e.preventDefault();
      if (open()) hide();
      else show();
    } else hide();
  });
}

export function tips(root = document) {
  if (POPOVER) for (const el of root.querySelectorAll("[data-tip]")) tip(el);
}
