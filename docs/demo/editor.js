import { basicSetup, EditorView, EditorState, linter, lintGutter, StreamLanguage, toml, oneDarkTheme,
  HighlightStyle, syntaxHighlighting, tags } from "./vendor/codemirror.js";
import { createConfigHistory } from "./renderedit.js";

export const palettes = {
  dark: { background: "#282c34", activeLine: "#333943", selection: "#45516b", foreground: "#efeff1",
    key: "#e5c07b", string: "#a7d88b", number: "#f0bb94", boolean: "#e5b8ff", section: "#82cfff",
    comment: "#c7cbd4", punctuation: "#d9dde7" },
};
export const tomlLanguage = StreamLanguage.define({ ...toml,
  token(stream, state) {
    const type = toml.token(stream, state);
    const text = stream.current();
    if (type === "atom") return text.startsWith("[") ? "section" : /^(true|false)$/.test(text) ? "boolean" : "number";
    return !type && /^[=,\[\]{}]+$/.test(text) ? "punctuation" : type;
  },
  tokenTable: { section: tags.heading, boolean: tags.bool, punctuation: tags.punctuation },
});
export const highlightStyles = Object.fromEntries(Object.entries(palettes).map(([name, p]) => [name,
  HighlightStyle.define([{ tag: tags.propertyName, color: p.key }, { tag: tags.string, color: p.string },
    { tag: tags.number, color: p.number }, { tag: tags.bool, color: p.boolean },
    { tag: tags.heading, color: p.section, fontWeight: "bold" }, { tag: tags.comment, color: p.comment },
    { tag: tags.punctuation, color: p.punctuation }]),
]));

function appearance() {
  const p = palettes.dark;
  return [EditorView.theme({
    "&": { color: p.foreground, backgroundColor: p.background, maxHeight: "70vh", fontSize: "14px", minHeight: "240px" },
    ".cm-scroller": { fontFamily: "\"gg mono\", \"Source Code Pro\", Consolas, \"DejaVu Sans Mono\", monospace",
      lineHeight: "20px" },
    ".cm-content": { caretColor: p.foreground },
    ".cm-gutters": { backgroundColor: p.background, color: p.comment, border: "none" },
    ".cm-activeLine, .cm-activeLineGutter": { backgroundColor: p.activeLine },
    "&.cm-focused .cm-selectionBackground, .cm-selectionBackground": { backgroundColor: p.selection },
    ".cm-tooltip": { backgroundColor: p.background, color: p.foreground, border: "1px solid " + p.comment,
      borderRadius: "4px" },
  }, { dark: true }), oneDarkTheme, syntaxHighlighting(highlightStyles.dark)];
}

export function saveState(initial, persisted) {
  let current = initial;
  let baseline = initial;
  return {
    get label() { return !persisted ? "Demo installation" : current !== baseline ? "Unsaved changes" : "Saved"; },
    changed(text) { current = text; },
    saved(text) { baseline = text; },
  };
}

export function bindSidebar(handle, sidebar, { view = window, storage } = {}) {
  const key = "shazzzbot-sidebar-width";
  let store;
  let preferred = null;
  try {
    store = storage || view.localStorage;
    const value = Number(store.getItem(key));
    if (Number.isFinite(value) && value >= 320 && value <= 720) preferred = value;
  } catch {}
  let drag = null;
  const maximum = () => Math.max(320, Math.min(720, view.innerWidth - 368));
  const width = () => Math.max(320, Math.min(maximum(), preferred ?? (view.innerWidth >= 1200 ? 420 : 380)));
  const active = () => view.innerWidth >= 900 && !handle.hidden && !sidebar.closest?.(".side-closed");
  const paint = () => {
    handle.hidden = view.innerWidth < 900;
    sidebar.style.setProperty("--sidebar-width", width() + "px");
    handle.setAttribute("aria-valuemin", "320");
    handle.setAttribute("aria-valuemax", String(maximum()));
    handle.setAttribute("aria-valuenow", String(width()));
  };
  const persist = () => {
    try {
      if (preferred === null) store.removeItem(key);
      else store.setItem(key, String(preferred));
    } catch {}
  };
  const down = (e) => {
    if (drag || !active() || e.button && e.button !== 0) return;
    e.preventDefault();
    drag = { id: e.pointerId, x: e.clientX, width: width(), previous: preferred };
    handle.setPointerCapture(e.pointerId);
  };
  const move = (e) => {
    if (!drag || drag.id !== e.pointerId) return;
    preferred = Math.max(320, Math.min(maximum(), drag.width + drag.x - e.clientX));
    paint();
  };
  const finish = (e) => {
    if (!drag || drag.id !== e.pointerId) return;
    if (e.type === "pointercancel") preferred = drag.previous;
    handle.releasePointerCapture(e.pointerId);
    drag = null;
    paint();
    if (e.type !== "pointercancel") persist();
  };
  const reset = () => { if (active()) { preferred = null; paint(); persist(); } };
  const press = (e) => {
    if (!active() || !["ArrowLeft", "ArrowRight", "Home"].includes(e.key)) return;
    e.preventDefault();
    if (e.key === "Home") reset();
    else { preferred = Math.max(320, Math.min(maximum(), width() + (e.key === "ArrowLeft" ? 16 : -16))); paint(); persist(); }
  };
  const events = { pointerdown: down, pointermove: move, pointerup: finish, pointercancel: finish,
    keydown: press, dblclick: reset };
  for (const [name, callback] of Object.entries(events)) handle.addEventListener(name, callback);
  view.addEventListener("resize", paint);
  paint();
  return () => {
    if (drag) finish({ type: "pointercancel", pointerId: drag.id });
    for (const [name, callback] of Object.entries(events)) handle.removeEventListener(name, callback);
    view.removeEventListener("resize", paint);
  };
}

function position(doc, line, col) {
  const l = doc.line(Math.min(Math.max(line, 1), doc.lines));
  return Math.min(l.from + Math.max(col - 1, 0), l.to);
}

export function schemaCompletions(schema, context) {
  if (schema?.format !== 1 || !schema.sections?.config || !schema.editor) return null;
  const full = context.state.doc.toString();
  const before = full.slice(0, context.pos);
  const multiline = before.match(/"""|'''/g) || [];
  if (multiline.length % 2) return null;
  const lines = before.split("\n");
  const line = lines.pop();
  let section = null;
  for (const previous of lines) {
    const header = previous.match(/^\s*\[([^\]]+)\]\s*(?:#.*)?$/);
    if (header) section = header[1].trim();
    else if (/^\s*\[/.test(previous)) section = "";
  }
  const root = schema.sections.config;
  const leaves = schema.editor.leaves || {};
  const editable = schema.editor.keys.filter((key) => (root[key]?.editable || leaves[key]) && !root[key]?.sensitive);
  let fields;
  const [top, ...path] = section ? section.split(".").map((part) => part.trim()) : [];
  if (section === null) fields = Object.fromEntries(editable.map((key) => [key, root[key]]));
  else if (leaves[top] && !path.length) {
    const table = schema.sections[root[top].ref];
    fields = Object.fromEntries(leaves[top].map((key) => [key, table[key]]));
  } else if (schema.editor.tables.includes(top) && editable.includes(top)) {
    fields = schema.sections[root[top].ref];
    for (const part of path) fields = fields?.[part]?.kind === "table" ? schema.sections[fields[part].ref] : undefined;
  }
  if (!fields || /^\s*#/.test(line)) return null;
  const assignment = line.match(/^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(.*)$/);
  if (assignment) {
    let field = fields[assignment[1]];
    if (!field || field.sensitive) return null;
    const value = assignment[2];
    let prefix;
    if (field.kind === "list") {
      if (!/^\s*\[[^\]]*$/.test(value)) return null;
      field = field.item;
      prefix = value.match(/(?:^|[,\[])\s*("?[A-Za-z0-9_-]*)$/);
    } else prefix = value.match(/^\s*("?[A-Za-z0-9_-]*)$/);
    if (!field || field.sensitive || !prefix) return null;
    const choices = field.kind === "boolean" ? [true, false] : field.choices;
    if (!Array.isArray(choices) || !choices.length) return null;
    return { from: context.pos - prefix[1].length,
      to: context.pos + full.slice(context.pos).match(/^"?[A-Za-z0-9_-]*"?/)[0].length,
      options: choices.map((value) => ({ label: JSON.stringify(value), type: "enum", info: field.description })),
      validFor: /^"?[A-Za-z0-9_-]*$/ };
  }
  const prefix = line.match(/^\s*([A-Za-z_][A-Za-z0-9_-]*|)$/);
  if (!prefix || !prefix[1] && !context.explicit) return null;
  return { from: context.pos - prefix[1].length,
    to: context.pos + full.slice(context.pos).match(/^[A-Za-z0-9_-]*/)[0].length,
    options: Object.entries(fields).filter(([, field]) => !field.sensitive)
      .map(([label, field]) => ({ label, type: field.kind === "table" ? "namespace" : "property",
        info: field.description + (field.unit ? " Unit: " + field.unit + "." : "") })),
    validFor: /^[A-Za-z0-9_-]*$/ };
}

export function schemaLanguageData(schema) {
  const source = (context) => schemaCompletions(schema, context);
  return () => [{ autocomplete: source }];
}

export function mountEditor(host, { doc, lint, schema, persisted = true, onChange = () => {} }) {
  const history = createConfigHistory();
  const root = host.attachShadow({ mode: "open" });
  const parent = document.createElement("div");
  root.appendChild(parent);
  const saved = saveState(doc, persisted);
  const paint = () => {
    const badge = document.getElementById("save-state");
    if (badge.textContent !== saved.label) badge.textContent = saved.label;
  };
  const source = async (view) => {
    const doc = view.state.doc;
    const found = await lint(doc.toString());
    return found.map((d) => {
      const from = position(doc, d.line, d.col);
      return { from, to: Math.max(from, position(doc, d.end_line, d.end_col)), message: d.message,
        severity: d.severity === "warning" ? "warning" : "error" };
    });
  };
  const view = new EditorView({ root, parent, state: EditorState.create({ doc,
    extensions: [basicSetup, tomlLanguage, lintGutter(), linter(source, { delay: 300 }),
      EditorState.languageData.of(schemaLanguageData(schema)),
      appearance(), EditorView.lineWrapping,
      EditorView.updateListener.of((update) => {
        if (!update.docChanged) return;
        saved.changed(update.state.doc.toString());
        paint();
        onChange(update.state.doc.toString());
      }), EditorView.contentAttributes.of({ "aria-label": "Display settings (TOML)" })],
  }) });
  const set = (text) => view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: text } });
  const unbind = bindSidebar(document.getElementById("sidebar-resize"), document.getElementById("sidebar"));
  paint();
  return {
    text: () => view.state.doc.toString(),
    set,
    replace: (text) => {
      if (text === view.state.doc.toString()) return;
      history.push(view.state.doc.toString());
      set(text);
    },
    undo: () => {
      const text = history.pop(view.state.doc.toString());
      if (text !== undefined) set(text);
    },
    canUndo: () => history.can(view.state.doc.toString()),
    saved: (text) => { saved.saved(text); paint(); },
    destroy: () => { unbind(); view.destroy(); },
  };
}
