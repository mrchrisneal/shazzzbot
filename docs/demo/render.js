const ESC = String.fromCharCode(27);
const BRAILLE = new RegExp("([" + String.fromCharCode(0x2800) + "-" + String.fromCharCode(0x28ff) + "]+)");

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function appendText(parent, text) {
  if (!text) return;
  text.split(BRAILLE).forEach((part, i) => {
    if (!part) return;
    if (i % 2) parent.appendChild(el("span", "braille", part));
    else parent.appendChild(document.createTextNode(part));
  });
}

const INLINE_RULES = [
  { re: /\\([^A-Za-z0-9\s])/y, make: (m) => document.createTextNode(m[1]) },
  { re: /``([\s\S]+?)``|`([^`]+)`/y, make: (m) => el("code", "inline-code", m[1] !== undefined ? m[1] : m[2]) },
  { re: /\*\*([\s\S]+?)\*\*(?!\*)/y, tag: "strong" },
  { re: /__([\s\S]+?)__(?!_)/y, tag: "u" },
  { re: /\*(?=\S)([\s\S]+?)\*(?!\*)/y, tag: "em" },
  { re: /_(?=\S)([\s\S]+?)_(?![A-Za-z0-9_])/y, tag: "em" },
  { re: /~~([\s\S]+?)~~/y, tag: "s" },
];

export function renderInline(parent, src) {
  let buf = "";
  let i = 0;
  outer: while (i < src.length) {
    for (const rule of INLINE_RULES) {
      rule.re.lastIndex = i;
      const m = rule.re.exec(src);
      if (!m) continue;
      appendText(parent, buf);
      buf = "";
      if (rule.make) {
        parent.appendChild(rule.make(m));
      } else {
        const n = el(rule.tag);
        renderInline(n, m[1]);
        parent.appendChild(n);
      }
      i += m[0].length;
      continue outer;
    }
    buf += src[i];
    i += 1;
  }
  appendText(parent, buf);
}

export function renderAnsi(parent, src) {
  const state = { bold: false, underline: false, fg: null };
  const re = new RegExp(ESC + "\\[([0-9;]*)m", "g");
  let last = 0;
  const emit = (text) => {
    if (!text) return;
    if (!state.bold && !state.underline && state.fg === null) {
      parent.appendChild(document.createTextNode(text));
      return;
    }
    const s = el("span", null, text);
    if (state.bold) s.classList.add("ansi-b");
    if (state.underline) s.classList.add("ansi-u");
    if (state.fg !== null) s.style.color = "var(--ansi-" + state.fg + ")";
    parent.appendChild(s);
  };
  let m;
  while ((m = re.exec(src))) {
    emit(src.slice(last, m.index));
    last = re.lastIndex;
    for (const raw of (m[1] || "0").split(";")) {
      const c = Number(raw || 0);
      if (c === 0) Object.assign(state, { bold: false, underline: false, fg: null });
      else if (c === 1) state.bold = true;
      else if (c === 4) state.underline = true;
      else if (c >= 30 && c <= 37) state.fg = c;
      else if (c === 39) state.fg = null;
    }
  }
  emit(src.slice(last));
}

function codeBlock(raw) {
  const head = /^([A-Za-z0-9_+-]+)\n/.exec(raw);
  const lang = head ? head[1].toLowerCase() : "";
  let body = head ? raw.slice(head[0].length) : raw.replace(/^\n/, "");
  body = body.replace(/\n$/, "");
  const pre = el("pre", "code-block");
  const code = el("code");
  if (lang === "ansi") renderAnsi(code, body);
  else code.textContent = body;
  pre.appendChild(code);
  return pre;
}

export function renderMarkdown(parent, src) {
  const re = /```([\s\S]*?)```/g;
  let last = 0;
  let m;
  const text = (s) => {
    if (!s) return;
    const span = el("span");
    renderInline(span, s);
    parent.appendChild(span);
  };
  while ((m = re.exec(src))) {
    let before = src.slice(last, m.index);
    if (last > 0) before = before.replace(/^\n/, "");
    text(before.replace(/\n$/, ""));
    parent.appendChild(codeBlock(m[1]));
    last = re.lastIndex;
  }
  text(last > 0 ? src.slice(last).replace(/^\n/, "") : src.slice(last));
}

function colorHex(c) {
  if (typeof c !== "number" || !c) return null;
  return "#" + (c & 0xffffff).toString(16).padStart(6, "0");
}

export function discordTime(d, now = new Date()) {
  const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((day(now) - day(d)) / 86400000);
  if (days === 0) return "Today at " + time;
  if (days === 1) return "Yesterday at " + time;
  return d.toLocaleDateString([], { year: "2-digit", month: "numeric", day: "numeric" }) + ", " + time;
}

// Discord packs inline fields three to a row and splits a row of two in halves.
function fieldColumns(fields) {
  const spans = [];
  let i = 0;
  while (i < fields.length) {
    let run = 0;
    while (i + run < fields.length && fields[i + run].inline && run < 3) run += 1;
    if (!run) {
      spans.push("1 / 13");
      i += 1;
      continue;
    }
    const width = 12 / run;
    for (let k = 0; k < run; k += 1) spans.push(1 + k * width + " / " + (1 + (k + 1) * width));
    i += run;
  }
  return spans;
}

export function watchImage(img, onState, deadline, deps = {}) {
  const later = deps.setTimeout || ((f, ms) => setTimeout(f, ms));
  const cancel = deps.clearTimeout || ((timer) => clearTimeout(timer));
  const now = deps.now || (() => Date.now());
  let settled = false;
  let timer = 0;
  const clean = () => {
    cancel(timer);
    img.removeEventListener("load", loaded);
    img.removeEventListener("error", failed);
  };
  const finish = (state, reason) => {
    if (settled) return;
    settled = true;
    clean();
    onState({ state, ...(reason ? { reason } : {}) });
  };
  const loaded = () => finish("done");
  const failed = () => finish("failed", "image could not be loaded");
  img.addEventListener("load", loaded);
  img.addEventListener("error", failed);
  if (Number.isFinite(deadline)) {
    timer = later(() => finish("failed", "image load timed out"), Math.max(0, deadline - now()));
  }
  return () => {
    settled = true;
    clean();
  };
}

export function renderEmbed(embed, resolve, options = {}) {
  const root = el("article", "embed");
  const bar = colorHex(embed.color);
  if (bar) root.style.setProperty("--embed-bar", bar);
  const body = el("div", "embed-body");
  root.appendChild(body);

  if (embed.title) {
    const t = el("div", "embed-title");
    renderInline(t, String(embed.title));
    body.appendChild(t);
  }
  if (embed.description) {
    const d = el("div", "embed-desc md");
    renderMarkdown(d, String(embed.description));
    body.appendChild(d);
  }
  if (Array.isArray(embed.fields) && embed.fields.length) {
    const grid = el("div", "embed-fields");
    const spans = fieldColumns(embed.fields);
    embed.fields.forEach((f, i) => {
      const cell = el("div", "embed-field");
      cell.style.gridColumn = spans[i];
      const name = el("div", "field-name");
      renderInline(name, String(f.name || ""));
      const value = el("div", "field-value md");
      renderMarkdown(value, String(f.value || ""));
      cell.append(name, value);
      grid.appendChild(cell);
    });
    body.appendChild(grid);
  }
  const imgUrl = embed.image && resolve(embed.image.url);
  if (imgUrl) {
    const media = el("div", "embed-media");
    const img = el("img", "embed-image");
    const cleanup = watchImage(img, (state) => {
      if (state.state === "failed") {
        img.hidden = true;
        media.appendChild(el("p", "image-error", "Image render failed: " + state.reason));
      }
      if (options.onImageState) options.onImageState(state);
    }, options.deadline);
    if (options.track) options.track(cleanup);
    img.src = imgUrl;
    img.alt = embed.title ? String(embed.title) + " image" : "Status image";
    media.appendChild(img);
    body.appendChild(media);
  }
  const footText = embed.footer && embed.footer.text;
  const when = embed.timestamp ? new Date(embed.timestamp) : null;
  const stamp = when && !isNaN(when) ? discordTime(when) : "";
  if (footText || stamp) {
    const foot = el("div", "embed-footer");
    const text = el("span", "embed-footer-text", footText || "");
    if (footText && stamp) text.appendChild(el("span", "embed-footer-sep", "\u2022"));
    if (stamp) text.appendChild(document.createTextNode(stamp));
    foot.appendChild(text);
    body.appendChild(foot);
  }
  return root;
}
