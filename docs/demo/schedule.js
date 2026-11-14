// The update schedule's per-message breakdown. A send is one Discord request. A message with images changes only when
// they upload, and that one request carries all its parts; a message without images edits on its own cycle, skipped
// when nothing changed.
const KINDS = ["embed", "text", "image"];

export function span(seconds) {
  const s = Math.round(seconds);
  if (s < 60) return s + "s";
  if (s % 3600 === 0) return s / 3600 + "h";
  return Math.floor(s / 60) + "m" + (s % 60 ? " " + (s % 60) + "s" : "");
}

function groups(embeds, pick) {
  return KINDS.filter(pick).map((kind) => ({ kind, titles: embeds.filter((e) => e.kind === kind).map((e) => e.title) }))
    .filter((g) => g.titles.length);
}

// edit/upload: what each request carries and when it runs; at and gap are null when not scheduled. rate is the
// estimated scheduled sends per minute, 0 when nothing runs on its own.
export function breakdown(message, auto) {
  const finite = (t) => (Number.isFinite(t) ? t : null);
  const images = message.embeds.some((e) => e.kind === "image");
  const edit = { groups: images ? [] : groups(message.embeds, () => true), every: message.edit_every,
    at: auto && !images ? finite(message.edit_at) : null };
  const [first, second] = (message.images || []).map(finite);
  const upload = images ? { groups: groups(message.embeds, () => true), at: auto ? first ?? null : null,
    gap: auto && first !== null && second != null && second > first ? second - first : null } : null;
  const terms = [];
  if (edit.at !== null && edit.groups.length) terms.push({ what: "edit", every: edit.every });
  if (upload && upload.gap !== null) terms.push({ what: "upload", every: upload.gap });
  return { count: message.embeds.length, edit, upload, terms,
    rate: terms.reduce((sum, t) => sum + 60 / t.every, 0) };
}

export function perMinute(rate) {
  return String(Math.round(rate * 100) / 100);
}

export function equation(b) {
  if (!b.terms.length) return "";
  return b.terms.map((t) => "1 " + t.what + " / " + span(t.every)).join(" + ")
    + " = about " + perMinute(b.rate) + " send" + (b.rate === 1 ? "" : "s") + " per minute";
}
