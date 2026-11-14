// Update schedule breakdown: grouping, real gaps, and the sends-per-minute estimate. Run: node docs/demo/schedule-test.mjs
import assert from "node:assert/strict";
import { breakdown, equation, perMinute, span } from "./schedule.js";

const e = (title, kind) => ({ key: title.toLowerCase(), kind, title });
const mixed = { embeds: [e("System", "image"), e("Disks", "image"), e("Services", "embed"), e("Docker", "embed"),
  e("Tasks", "text")], edit_every: 60, image_every: 60, edit_at: 1000, images: [1045, 1125] };

assert.deepEqual([span(5), span(60), span(80), span(3600), span(5400)], ["5s", "1m", "1m 20s", "1h", "90m"]);

let b = breakdown(mixed, true);
assert.equal(b.count, 5);
assert.deepEqual(b.edit.groups, [], "a message with images has no separate edit");
assert.deepEqual(b.upload.groups, [{ kind: "embed", titles: ["Services", "Docker"] }, { kind: "text", titles: ["Tasks"] },
  { kind: "image", titles: ["System", "Disks"] }], "the upload carries every part");
assert.equal(b.edit.at, null);
assert.equal(b.upload.at, 1045);
assert.equal(b.upload.gap, 80, "the gap is the projected one, not image_every");
assert.equal(perMinute(b.rate), "0.75");
assert.equal(equation(b), "1 upload / 1m 20s = about 0.75 sends per minute");

b = breakdown({ ...mixed, images: [Infinity, Infinity] }, true);
assert.equal(b.upload.at, null);
assert.equal(b.upload.gap, null);
assert.equal(equation(b), "", "a blocked upload adds nothing, and there is no separate edit");

b = breakdown({ ...mixed, embeds: [e("Services", "embed"), e("Tasks", "text")], images: [] }, true);
assert.deepEqual(b.edit.groups, [{ kind: "embed", titles: ["Services"] }, { kind: "text", titles: ["Tasks"] }]);
assert.equal(equation(b), "1 edit / 1m = about 1 send per minute");

b = breakdown({ ...mixed, images: [1045] }, true);
assert.equal(b.upload.at, 1045);
assert.equal(b.upload.gap, null, "no second upload: no invented gap");

b = breakdown(mixed, false);
assert.equal(b.edit.at, null);
assert.equal(b.upload.at, null);
assert.equal(b.rate, 0);
assert.equal(equation(b), "", "on demand: nothing runs on its own");

b = breakdown({ ...mixed, embeds: [e("Services", "embed")], images: [] }, true);
assert.equal(b.upload, null);

const total = [mixed, { ...mixed, embeds: [e("Tasks", "text")], edit_every: 30, images: [] }]
  .map((m) => breakdown(m, true).rate).reduce((a, r) => a + r, 0);
assert.equal(perMinute(total), "2.75");
console.log("schedule ok");
