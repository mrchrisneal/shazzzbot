// Pure helpers for the top bar: the activity line's words (live, the stage comes from previewactivity.py; in the
// demo, from the mirrored schedule) and how playback moves through the sample profiles.
export function names(files) {
  return files.map((f) => f.replace(/\.png$/, "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()));
}

export function activityText(a) {
  const which = a.of > 1 ? " " + a.n + " of " + a.of : "";
  const list = (a.targets || []).join(", ");
  switch (a.stage) {
    case "render": return list ? "Rendering image: " + list + "..." : "Rendering embeds...";
    case "upload": return list ? "Uploading image: " + list + "..." : "Uploading images...";
    case "edit": return "Editing message" + which + "...";
    case "send": return "Sending message" + which + "...";
    default: return "Working...";
  }
}

// One playback move. Cycling walks the profiles; otherwise the profile stays and its variety step moves.
export function advance(pos, delta, count, cycling) {
  if (cycling) return { index: (pos.index + delta + count) % count, step: 0 };
  return { index: pos.index, step: Math.max(0, pos.step + delta) };
}

export function profileLabel(name, step) {
  return step ? name + " #" + (step + 1) : name;
}
