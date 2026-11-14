// Mode tabs edit only the values in the top-level [render] table of the editor text; everything else stays as typed.
export const PRESETS = {
  mixed: (key) => (key === "tasks" ? "embed" : "image"),
  text: () => "text",
  rich: () => "embed",
  image: () => "image",
};

const HEADER = /^\s*\[/;
const RENDER = /^\s*\[\s*render\s*\]\s*(#.*)?$/;
const ENTRY = /^(\s*)(["']?)([A-Za-z0-9_-]+)\2(\s*=\s*)("[^"\n]*"|'[^'\n]*')(\s*(#.*)?)$/;

function locate(text) {
  if (text.includes("'''") || text.includes('"""')) return { error: "multi-line strings in the config" };
  const lines = text.split("\n");
  const first = lines.findIndex((l) => HEADER.test(l));
  const top = first < 0 ? lines : lines.slice(0, first);
  if (top.some((l) => /^\s*["']?render["']?\s*[.=]/.test(l))) return { error: "render is not written as a [render] table" };
  const head = lines.findIndex((l) => RENDER.test(l));
  if (head >= 0 && lines.findIndex((l, i) => i > head && RENDER.test(l)) >= 0) return { error: "more than one [render]" };
  let end = head < 0 ? -1 : lines.findIndex((l, i) => i > head && HEADER.test(l));
  if (head >= 0 && end < 0) end = lines.length;
  return { lines, first, head, end };
}

// Returns { text } with every section's mode set by preset, or { text, error } with the text unchanged.
export function setRender(text, sections, preset) {
  const at = locate(text);
  if (at.error) return { text, error: "Cannot change [render]: " + at.error + ". Edit it by hand." };
  const { lines, first, head, end } = at;
  const value = (key) => '"' + PRESETS[preset](key) + '"';
  if (head < 0) {
    const block = ["[render]", ...sections.map((k) => k + " = " + value(k)), ""];
    if (first < 0) return { text: text.replace(/\n*$/, "") + "\n\n" + block.join("\n").replace(/\n$/, "") + "\n" };
    lines.splice(first, 0, ...block);
    return { text: lines.join("\n") };
  }
  const seen = new Set();
  let last = head;
  for (let i = head + 1; i < end; i++) {
    const m = lines[i].match(ENTRY);
    if (m && sections.includes(m[3])) {
      lines[i] = m[1] + m[2] + m[3] + m[2] + m[4] + value(m[3]) + m[6];
      seen.add(m[3]);
    }
    if (lines[i].trim()) last = i;
  }
  lines.splice(last + 1, 0, ...sections.filter((k) => !seen.has(k)).map((k) => k + " = " + value(k)));
  return { text: lines.join("\n") };
}

// The preset the [render] table matches, or null for any other mix or an unreadable table.
export function currentMix(text, sections) {
  const at = locate(text);
  if (at.error) return null;
  const set = {};
  for (let i = at.head + 1; at.head >= 0 && i < at.end; i++) {
    const m = at.lines[i].match(ENTRY);
    if (m) set[m[3]] = m[5].slice(1, -1);
  }
  return Object.keys(PRESETS).find((p) => sections.every((k) => (set[k] || "image") === PRESETS[p](k))) || null;
}

// Whole-document snapshots for the Undo button; Ctrl-Z stays the editor's own history.
export function createConfigHistory(limit = 20) {
  const stack = [];
  return {
    push(text) { stack.push(text); if (stack.length > limit) stack.shift(); },
    pop(current) {
      while (stack.length && stack[stack.length - 1] === current) stack.pop();
      return stack.pop();
    },
    can: (current) => stack.some((t) => t !== current),
  };
}
