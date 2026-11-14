import assert from "node:assert/strict";
import { createConfigHistory, currentMix, setRender } from "./renderedit.js";

const S = ["system", "history", "services", "tasks"];
const doc = 'title = "T"\n\n[render]\n# keep me\nsystem = "image"  # note\n"tasks" = \'text\'\n\n[tasks]\nx = 1\n';
{
  const out = setRender(doc, S, "text").text;
  assert.equal(out, 'title = "T"\n\n[render]\n# keep me\nsystem = "text"  # note\n"tasks" = "text"\n'
    + 'history = "text"\nservices = "text"\n\n[tasks]\nx = 1\n', "only [render] values change; missing keys appended");
  assert.equal(currentMix(out, S), "text");
  assert.equal(setRender(out, S, "text").text, out, "applying the same preset twice is stable");
  assert.equal(currentMix(setRender(doc, S, "mixed").text, S), "mixed");
  assert.equal(currentMix(doc, S), null, "a hand-made mix matches no preset");
}
{
  const out = setRender('title = "T"\n[tasks]\nx = 1\n', S, "rich").text;
  assert.equal(out, 'title = "T"\n[render]\nsystem = "embed"\nhistory = "embed"\nservices = "embed"\ntasks = "embed"\n\n'
    + "[tasks]\nx = 1\n", "a missing [render] goes before the first table");
  assert.equal(setRender('title = "T"\n', ["system"], "image").text, 'title = "T"\n\n[render]\nsystem = "image"\n');
  assert.equal(currentMix('title = "T"\n', S), "image", "unset sections use image mode");
}
for (const bad of ['title = """\n[render]\n"""\n', 'render = { system = "text" }\n', "render.system = 'text'\n",
  '[render]\nsystem = "text"\n[render]\n']) {
  const r = setRender(bad, S, "image");
  assert.ok(r.error && r.text === bad, "refuses without changing: " + JSON.stringify(bad));
  assert.equal(currentMix(bad, S), null);
}
{
  const h = createConfigHistory(2);
  assert.equal(h.pop("a"), undefined);
  h.push("a"); h.push("b"); h.push("c");
  assert.equal(h.can("c"), true);
  assert.equal(h.pop("x"), "c");
  assert.equal(h.pop("x"), "b");
  assert.equal(h.pop("x"), undefined, "bounded: the oldest entry was dropped");
  h.push("a"); h.push("b");
  assert.equal(h.pop("b"), "a", "entries equal to the current text are skipped");
  assert.equal(h.can("z"), false);
}
console.log("renderedit ok");
