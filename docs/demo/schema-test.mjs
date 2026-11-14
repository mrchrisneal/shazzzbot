import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { schemaCompletions, schemaLanguageData } from "./editor.js";
import { editorSchema } from "./demo.js";

const schema = { format: 1, version: "1.2.3", sections: { config: {
  title: { kind: "string", editable: true, description: "Card title." },
  green_ok: { kind: "boolean", editable: true, description: "Healthy dot." },
  order: { kind: "list", editable: true, description: "Order.",
    item: { kind: "string", choices: ["system", "tasks"], description: "Section." } },
  tasks: { kind: "table", editable: true, ref: "tasks", description: "Task emphasis." },
  thresholds: { kind: "table", editable: true, ref: "thresholds", description: "Levels." },
  services: { kind: "table", editable: false, ref: "services", description: "Services." },
  private_value: { kind: "string", editable: false, description: "Private." },
  password_hash: { kind: "string", editable: true, sensitive: true, default: "never returned" },
}, tasks: {
  long_running_minutes: { kind: "duration", description: "Emphasis threshold.", unit: "minutes" },
  heavy_kinds: { kind: "list", item: { kind: "string", choices: ["parity", "file_integrity"] } },
  secret: { kind: "string", sensitive: true, default: "never returned" },
  display: { kind: "table", ref: "display", description: "Display." },
  rename: { kind: "table", ref: "text_map", description: "Renames." },
}, display: { width: { kind: "integer", description: "Width." } },
services: { columns: { kind: "list", editable: true, description: "Labels." },
  public_host: { kind: "string", description: "Host." } }, thresholds: { resource: { kind: "threshold", description: "CPU and RAM.", unit: "percent" } } },
editor: { keys: ["title", "green_ok", "order", "tasks", "thresholds", "services", "password_hash"],
  tables: ["tasks", "thresholds"], leaves: { services: ["columns"] } } };
const complete = (text, explicit = true) => schemaCompletions(schema,
  { state: { doc: { toString: () => text } }, pos: text.length, explicit });
const labels = (text) => complete(text)?.options.map((option) => option.label);
const languageData = schemaLanguageData(schema);
const sourceFunction = languageData()[0].autocomplete;
assert.equal(languageData()[0].autocomplete, sourceFunction);
assert.deepEqual(sourceFunction({ state: { doc: { toString: () => "[tasks]\n" } }, pos: 8, explicit: true }),
  complete("[tasks]\n"));
assert.deepEqual(labels(""), ["title", "green_ok", "order", "tasks", "thresholds", "services"]);
assert.deepEqual(labels("[tasks]\n"), ["long_running_minutes", "heavy_kinds", "display", "rename"]);
assert.deepEqual(labels("[ tasks . display ]\n"), ["width"]);
assert.deepEqual(labels("[services]\n"), ["columns"]);
assert.equal(complete("[tasks]\nlong_r").from, "[tasks]\n".length);
assert.equal(complete("[tasks]\n").options[0].info, "Emphasis threshold. Unit: minutes.");
assert.deepEqual(labels("green_ok = "), ["true", "false"]);
assert.deepEqual(labels('order = ["s'), ['"system"', '"tasks"']);
assert.deepEqual(labels('[tasks]\nheavy_kinds = ["parity", "f'), ['"parity"', '"file_integrity"']);
for (const text of ["# comment", "[unknown]\n", "[[outputs]]\n", "[config]\n", "[tasks.extra]\n", "[tasks.rename]\n", "[services.checks]\n",
  "[[services.checks]]\n", "[services]\npublic_host = ", "[tasks.display.width]\n",
  'title = "inside string', 'title = """\ninside multiline', "secret = ", "green_ok = true # comment",
  "order = []", "[tasks]\nsecret = ", "key.with.dots"]) assert.equal(complete(text), null, text);
assert.equal(complete("", false), null);
const existing = 'order = ["sys"]';
const replacement = schemaCompletions(schema, { state: { doc: { toString: () => existing } },
  pos: existing.indexOf("sys") + 1, explicit: true });
assert.equal(existing.slice(0, replacement.from) + replacement.options[0].label + existing.slice(replacement.to),
  'order = ["system"]');
assert.equal(schemaCompletions({}, { state: { doc: {} } }), null);
assert.ok(!JSON.stringify(complete("")).includes("never returned"));
assert.ok(!JSON.stringify(complete("[tasks]\n")).includes("never returned"));
assert.equal(editorSchema({ live: true, schema }, { ...schema, version: "old" }), schema);
assert.equal(editorSchema({ live: false }, schema), schema);
assert.equal(editorSchema(undefined, schema).version, "1.2.3");
assert.throws(() => editorSchema({ live: true }, schema), /schema is unavailable/);
assert.throws(() => editorSchema(undefined, {}), /schema is unavailable/);
const source = readFileSync(new URL("./demo.js", import.meta.url), "utf8");
assert.match(source, /schema: editorSchema\(liveConfig, loaded.data.schema\)/);
assert.match(source, /persisted: state.live/);
const generated = JSON.parse(readFileSync(new URL("./schema.json", import.meta.url), "utf8"));
const generatedContext = (text) => ({ state: { doc: { toString: () => text } }, pos: text.length, explicit: true });
assert.deepEqual(schemaCompletions(generated, generatedContext("[tasks]\nheavy_kinds = [")).options
  .map((option) => JSON.parse(option.label)), generated.sections.tasks.heavy_kinds.item.choices);
assert.deepEqual(schemaCompletions(generated, generatedContext("")).options.map((option) => option.label),
  generated.editor.keys);
assert.deepEqual(schemaCompletions(generated, generatedContext("[services]\n")).options.map((o) => o.label),
  ["columns"]);
assert.equal(schemaCompletions(generated, generatedContext("[tasks.rename]\n")), null);
assert.equal(editorSchema({ live: true, schema: generated }, schema).version, generated.version);
console.log("PASS schema completion stays within editable metadata and preserves the selected server version");
