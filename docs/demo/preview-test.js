// Mounts preview.js with the built demo frames and checks its contract; the result shows in #result.
import { mountPreview } from "./preview.js";

const out = [];
const check = (ok, what) => out.push((ok ? "PASS " : "FAIL ") + what);
const before = new Set(Object.keys(window));
const host = document.getElementById("mount");
const p = mountPreview(host, { mode: "default" });
const frame = (await (await fetch("frames.json")).json()).sets.helper.frames[0];
const root = host.firstElementChild.shadowRoot;
const count = (sel) => root.querySelectorAll(sel).length;
check(root.textContent.includes("Rendering...") && !count(".msg"), "placeholder before the first frame");
p.update(frame);
check(count(".msg-first") === 1 && count(".avatar") === 1 && count(".embed") > 0, "one message group drawn");
check(count("img.embed-image") > 0 && count(".embed") > count("img.embed-image"), "default view mixes images and embeds");
const timed = document.createElement("div");
document.body.append(timed);
const tp = mountPreview(timed, { mode: "default", timing: true });
tp.update(frame, frame.default.flat().find((i) => i.at).at - 50);
const troot = timed.firstElementChild.shadowRoot;
const lines = [...troot.querySelectorAll(".timing")];
check(lines.length > 0 && lines.every((l) => l.parentElement.classList.contains("embed-body")
  && l.previousElementSibling && l.previousElementSibling.classList.contains("embed-footer")),
  "TIMING sits inside the embed, right below its footer");
check(lines.some((l) => /^TIMING: (Embed|Image) updating in \d/.test(l.textContent))
  && [...troot.querySelectorAll(".embed-footer")].every((f) => !f.textContent.includes("TIMING")),
  "TIMING text counts down and never enters the footer");
tp.destroy();
timed.remove();
p.setMode("text");
check(count(".embed-image") === 0 && count(".code-block") > 0, "text view");
p.setMode("bogus");
check(count(".code-block") > 0, "unknown mode ignored");
p.update({ text: [] });
check(root.textContent.includes("No embeds in this view."), "empty view");
p.update({ image: [[{ embed: { image: { url: "https://example.com/x.png" } } }]] });
p.setMode("image");
check(!root.querySelector(".embed img[src^='http']"), "external image never fetched");
check(document.head.querySelectorAll("style, link").length === 0, "no styles outside the shadow root");
check([...Object.keys(window)].every((k) => before.has(k)), "no globals");
p.destroy();
check(host.children.length === 0, "destroy removes it");
document.getElementById("result").textContent = out.join("\n");
