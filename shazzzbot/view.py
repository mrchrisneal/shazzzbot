"""One section, three looks: a monospace text table, a rich embed of fields, or a PNG drawn here."""
import io
import math
import os
import re
import time
from collections.abc import Callable, Sequence
from datetime import datetime, tzinfo
from textwrap import wrap

import discord
from PIL import Image, ImageDraw, ImageFont

from . import __version__, charts, panel, renderbudget
from .charts import BG, DIM, FONT_DIR, GREEN, PANEL, RED, TEXT, YELLOW, _fonts, text_width
from .glyphs import PAUSE, PLAY, STOP
from .panel import (
    ANSI,
    COAL,
    CONT,
    CRITICAL,
    FINE,
    ICON_TEXT_WIDTH,
    MORE,
    NOTE,
    WARNING,
    WIDTH,
    Part,
    Section,
    clip,
    grid,
    table,
)

MODES = ("text", "embed", "image")
EMPTY = ("N/A", "-", "\u2014")
FAINT = tuple((t + b) // 2 for t, b in zip(TEXT, PANEL))  # placeholder cells at half strength
SEP = f" {chr(0xB7)} "
GAP2 = "\u00a0 "  # Discord collapses plain double spaces in embeds
K = renderbudget.SCALE
W, TOP, ROW_H, GAP = 800, 16, 36, 28
PAD, BOX_GAP = 6, 8  # inside each box above and below its content; between boxes
MIN_W = 360  # a narrow table still gets a card wide enough to read as one
ICON_W = 22
MAX_ROWS = 40  # per part, so a huge host cannot make a huge image


ISSUES = "Issues & Alerts"
SHOWN_ISSUES = 8
FOOT_PAD = 16
ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")
ICONS = {"github": os.path.join(ICON_DIR, "github.png")}


class Rendered(bytes):
    draw: Callable[[int], bytes]
    key: str
    section_title: str
    now: float | None
    tz: tzinfo | None

    def __new__(cls, data: bytes, draw: Callable[[int], bytes], *, key: str = "image", section_title: str = "Image",
                now: float | None = None, tz: tzinfo | None = None):
        image = super().__new__(cls, data)
        image.draw, image.key, image.section_title, image.now, image.tz = draw, key, section_title, now, tz
        return image


def filename(s: Section) -> str:
    return f"{s.key}.png"


def _base(s: Section, description: str | None) -> discord.Embed:
    e = discord.Embed(title=s.title or None, description=description or None, color=COAL)
    if s.footer:
        e.set_footer(text=s.footer)
    if s.stamp:
        e.timestamp = discord.utils.utcnow()
    return e


def embed(s: Section, mode: str) -> discord.Embed:
    if mode == "image":
        e = _base(s, s.note)
        e.set_image(url=f"attachment://{filename(s)}")
        return e
    return text(s) if mode == "text" else rich(s)


def _shown(issues: Sequence[tuple[int, str]], limit: int | None = None) -> list[tuple[int, str]]:
    limit = SHOWN_ISSUES if limit is None else limit
    rows = sorted(issues, key=lambda r: -r[0])
    if len(rows) <= limit:
        return rows
    errors = sum(lv >= CRITICAL for lv, _ in rows[limit:])
    return rows[:limit] + panel.more(((len(rows) - limit - errors, "warning"), (errors, "error")))


def parts(s: Section) -> list[Part]:
    issues = [(CONT if i else lv, (line,)) for lv, text in _shown(s.issues, s.issues_limit)
              for i, line in enumerate(text.split("\n"))]
    return s.parts + ([Part(issues, ISSUES, cut=None)] if issues else [])


def _live(s: Section, view: str) -> list[Part]:
    """Parts with something to show in this view: a table needs a row besides its header."""
    return [p for p in parts(s) if len(p.rows) > p.heads and (not p.only or view in p.only.split())]


def _lines(p: Part, status: bool) -> list[tuple[int, str]]:
    if p.heading != ISSUES and any(lv == MORE for lv, _ in p.rows):
        keep = [i for i, (lv, _) in enumerate(p.rows) if lv != MORE]
        normal = p._replace(rows=[p.rows[i] for i in keep],
                            marks=[p.marks[i] for i in keep if i < len(p.marks)],
                            colours=[p.colours[i] for i in keep if i < len(p.colours)],
                            icons=[p.icons[i] for i in keep if i < len(p.icons)],
                            trends=[p.trends[i] for i in keep if i < len(p.trends)])
        normal_lines = iter(_lines(normal, status) if keep else [])
        return [(lv, cells[0]) if lv == MORE else next(normal_lines) for lv, cells in p.rows]
    if p.colours:
        p = p._replace(marks=[_levels(p, i) for i in range(len(p.rows))])
    levels = _logical_levels([(lv, _row_levels(p, i)) for i, (lv, _) in enumerate(p.rows)])
    p = p._replace(rows=[(lv, cells) for lv, (_, cells) in zip(levels, p.rows)])
    n = len(p.rows[0][1])
    if p.heading == ISSUES:
        width = WIDTH if status else WIDTH + 3
        return [(lv if i == 0 else CONT, line) for lv, cells in p.rows
                for i, line in enumerate(wrap(re.sub(" {3,}", "  ", cells[0]), width))]
    if n == 1 and p.cut is None:
        return [(lv, cells[0]) for lv, cells in p.rows]
    if p.icons:
        lines = grid(p.rows, p.align or "l" * n, p.cut or 0, WIDTH - ICON_TEXT_WIDTH, p.marks)
        return [(lv, (_state_icon(p.icons[i], lv) + " " if p.icons[i] in ("run", "pause", "stop")
                      else " " * ICON_TEXT_WIDTH) + line) for i, (lv, line) in enumerate(lines)]
    width = (WIDTH if status else WIDTH + 3) if p.cut is not None else 999
    if p.grid:  # grid marks colour the value's double-space separated pieces, never the label
        def paint(r: int, value: str) -> str:
            marks = p.marks[r] if r < len(p.marks) else ()
            return "  ".join(f"{ANSI[marks[j]]}{v}\x1b[0m" if j < len(marks) and marks[j] > FINE else v
                             for j, v in enumerate(re.split(" {2,}", value)))
        rows = [(lv, (*cells[:-1], paint(r, cells[-1]))) for r, (lv, cells) in enumerate(p.rows)]
        return grid(rows, p.align or "l" * n, p.cut or 0, width)
    return grid(p.rows, p.align or "l" * n, p.cut or 0, width, p.marks)


def _levels(p: Part, row: int) -> tuple[int, ...]:
    marks = p.marks[row] if row < len(p.marks) else ()
    colours = p.colours[row] if row < len(p.colours) else ()
    return tuple(max(marks[i] if i < len(marks) else FINE, colours[i] if i < len(colours) else FINE)
                 for i in range(max(len(marks), len(colours))))


def _ansi_levels(text: str) -> set[int]:
    return {WARNING if code in ("33", "93") else CRITICAL
            for match in re.finditer(r"\x1b\[([0-9;]*)m", text)
            for code in match[1].split(";") if code in ("31", "33", "91", "93")}


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _md(text: str) -> str:
    """Escape data so names like DISK_INVALID or my_app never open Discord markdown."""
    return re.sub(r"([\\*_~|`\[\]])", r"\\\1", text)


def _md_bold(text: str) -> str:
    """_md, keeping the panel's own **bold** pairs (History values)."""
    return "".join(f"**{_md(x)}**" if i % 2 else _md(x) for i, x in enumerate(re.split(r"\*\*([^*\n]+)\*\*", text)))


def _grid(label: str, values: list[str], levels: tuple[int, ...], lv: int) -> str:
    """One value per line, each flagged value followed by its dot; Alerts and Tasks lead with theirs."""
    lvls = [max(levels[i] if i < len(levels) else FINE, max(_ansi_levels(v), default=FINE))
            for i, v in enumerate(values)]
    flagged = any(level > FINE for level in lvls)
    dots = [panel.glyph(level) if flagged and level > FINE else mark(lv).strip() if not flagged and i == 0
            and lv not in (CONT, NOTE) else "" for i, level in enumerate(lvls)]
    lead = label in ("Alerts", "Tasks")
    shown = [_md_bold(_plain(v)) for v in values]
    return "\n".join((f"{d} {v}" if lead else f"{v} {d}") if d else v for v, d in zip(shown, dots))


def _prose(text: str, lv: int = FINE) -> str:
    if lv in (CONT, NOTE):
        return _plain(text)
    return mark(max(lv, max(_ansi_levels(text), default=lv))) + _plain(text)


def _row_levels(p: Part, i: int) -> set[int]:
    lv, cells = p.rows[i]
    icon = p.icons[i] if i < len(p.icons) else ""
    icon_level = int(icon[3:]) if icon.startswith("dot") else FINE
    return {level for level in (lv, icon_level, *_levels(p, i)) if level > FINE} | _ansi_levels(" ".join(cells))


def _logical_levels(rows: Sequence[tuple[int, set[int]]]) -> list[int]:
    out: list[int] = []
    parent = None
    for lv, levels in rows:
        highest = max(lv, max(levels, default=lv))
        if lv == CONT and parent is not None:
            if highest > FINE:
                out[parent] = max(out[parent], highest)
            out.append(CONT)
        elif lv == NOTE:
            out.append(NOTE)
        else:
            parent = len(out)
            out.append(highest)
    return out


def _ansi_wrap(text: str, width: int, indent: int) -> list[str]:
    text = text.translate({9: 32, 10: 32, 11: 32, 12: 32, 13: 32})
    styled: list[tuple[str, str]] = []
    at, state = 0, ""
    for match in re.finditer(r"\x1b\[[0-9;]*m", text):
        styled.extend((ch, state) for ch in text[at:match.start()])
        state = "" if match[0] in ("\x1b[m", "\x1b[0m") else state + match[0]
        at = match.end()
    styled.extend((ch, state) for ch in text[at:])
    plain = "".join(ch for ch, _ in styled).rstrip()
    if len(plain) <= width:
        return [text]
    out, offset = [], 0
    for line in wrap(plain, width, subsequent_indent=" " * indent):
        content = line.lstrip()
        start = plain.index(content, offset)
        offset = start + len(content)
        out.append(line[:len(line) - len(content)] + _styled(styled[start:offset]))
    return out


def _styled(chars: list[tuple[str, str]]) -> str:
    out, current = [], ""
    for char, state in chars:
        if state != current:
            out.append(("\x1b[0m" if current else "") + state)
            current = state
        out.append(char)
    return "".join(out) + ("\x1b[0m" if current else "")


def _text_rows(p: Part, rows: list[tuple[int, str]], status: bool) -> list[tuple[int, str]]:
    width = WIDTH if status else WIDTH + 3
    indent = max(6, max((len(cells[0]) for lv, cells in p.rows if lv != MORE), default=0)) + 2 \
        if p.grid else ICON_TEXT_WIDTH if p.icons else 0
    indent = min(indent, max(0, width - 6))
    expanded = [(lv if i == 0 else CONT, line, n if i == 0 else -1)
                for n, (lv, text) in enumerate(rows)
                for i, line in enumerate(_ansi_wrap(text, width - (2 if lv == MORE and not status else 0),
                                                    2 if lv == MORE and not status else indent))]
    levels = _logical_levels([(lv, _ansi_levels(line)) for lv, line, _ in expanded])
    return [(lv, (" " if lv == CONT and status else "") + line.rstrip()) for lv, (_, line, _) in zip(levels, expanded)]


def _groups(s: Section) -> list[list[Part]]:
    """Each headed part starts a group; parts without a heading join the group before them."""
    groups: list[list[Part]] = []
    for p in _live(s, "text"):
        if p.heading or not groups or groups[-1][0].heading:
            groups.append([p])
        else:
            groups[-1].append(p)
    return groups


def _system_text(p: Part) -> Part:
    rows = []
    marks = []
    colours = []
    for i, (lv, cells) in enumerate(p.rows):
        levels = p.marks[i] if i < len(p.marks) else ()
        tones = p.colours[i] if i < len(p.colours) else ()
        if cells[0] not in {"Disk I/O", "Net I/O", "Load", "UPS", "Tasks"}:
            rows.append((lv, cells))
            marks.append(levels)
            colours.append(tones)
            continue
        for j, value in enumerate(re.split(" {2,}", cells[1])):
            rows.append((lv if j == 0 else CONT,
                         (cells[0] if j == 0 else "", value)))
            marks.append((levels[j] if j < len(levels) else FINE,))
            colours.append((tones[j] if j < len(tones) else FINE,))
    return p._replace(rows=rows, marks=marks, colours=colours)


def _app_text(parts: list[Part]) -> dict[int, list[tuple[int, str]]]:
    ranked = [p for p in parts if p.trends]
    if not ranked:
        return {}
    rows = [row for p in ranked for row in p.rows]
    width = max(6, WIDTH + 4 - 6 - sum(max(len(cells[i]) for _, cells in rows) for i in (0, 2, 3)))
    rows = [(lv, (rank, clip(name[:-4], width - 4) + " (+)" if name.endswith(" (+)") else clip(name, width),
                  value, trend)) for lv, (rank, name, value, trend) in rows]
    lines = grid(rows, "llrl", cut=1, width=WIDTH + 4)
    out = {}
    offset = 0
    for p in ranked:
        out[id(p)] = [(lv, line[:-len(cells[-1]) - 2] + " " + cells[-1])
                      for (lv, line), (_, cells) in zip(lines[offset:], p.rows)]
        offset += len(p.rows)
    return out


def text(s: Section) -> discord.Embed:
    if s.key == "system":
        s = s._replace(parts=[_system_text(p) if p.grid else p for p in s.parts])
    groups = _groups(s)
    app_lines = _app_text([p for g in groups for p in g]) if s.key == "apps" else {}
    note = _prose(s.note, max(_ansi_levels(s.note), default=CONT)) if s.note else ""
    room = (4096 - len(note) - 2) // max(1, len(groups))
    blocks = [note] if note else []
    for g in groups:
        status = panel.SLOT[FINE] != panel.OK or any(_row_levels(p, i) for p in g for i in range(len(p.rows)))
        rows: list[tuple[int, str]] = []
        for p in g:
            lines = app_lines[id(p)] if id(p) in app_lines else _lines(p, status)
            rows += [(FINE, "")] * bool(rows) + _text_rows(p, lines, status)
        head = f"**{g[0].heading}**\n" if g[0].heading else ""
        blocks.append(head + table(rows, room - len(head)))
    return _base(s, "\n".join(blocks))


def _fit(lines: list[str], limit: int) -> str:
    out = lines = [clip(line, limit - 20) for line in lines]
    cut = 0
    while len(value := "\n".join(out)) > limit:
        cut += 1
        out = lines[:-cut] + [f"...and {cut} more"]
    return value


def mark(lv: int) -> str:
    """A dot for a problem, or for every row when the OK glyph is set to show (green_ok)."""
    return f"{panel.glyph(lv)} " if lv > FINE or lv == MORE or panel.SLOT[FINE] != panel.OK else ""


def _state_icon(kind: str, lv: int = panel.WARNING) -> str:
    if not kind:
        return "  "
    if kind.startswith("dot") or kind == "warn":
        return panel.SLOT[lv if kind == "warn" else int(kind[3:])]
    if kind == "run":
        return f"\x1b[32m{PLAY}\x1b[0m "
    return {"stop": STOP, "pause": PAUSE}[kind] + " "


def _paint(text: str, lv: int) -> str:
    """Reserve the trailing status slot before clipping an aligned value."""
    text = clip(text, WIDTH)
    label, value = (re.findall(r"^(.*?:\s+|)(.*)$", text) or [("", text)])[0]
    return f"{label}{_tint(value, lv)} {mark(lv).strip()}"


def _tint(text: str, lv: int) -> str:
    return f"{ANSI[lv]}{text}\x1b[0m"


def _cell(name: str, items: list[tuple[str, int]], level: int = FINE) -> tuple[str, str]:
    """One value is prose; multiple aligned metrics use a bounded code block."""
    items = [(_plain(t).replace("`", "'"), max(lv, max(_ansi_levels(t), default=lv))) for t, lv in items]
    level = max(level, max(lv for _, lv in items))
    if len(items) == 1:
        return clip(name, 256), _prose(_md(items[0][0]), level) + "\n\u00a0"
    d = mark(level).strip()
    texts = [_paint(t, lv) if lv > FINE else clip(t, WIDTH + 3) for t, lv in items]
    fence = "```ansi\n" if any(lv > FINE for _, lv in items) else "```\n"
    return clip(f"{d} {name}" if d else name, 256), fence + _fit(texts, 1020 - len(fence)) + "\n```"


def rich(s: Section) -> discord.Embed:
    """Inline values under row labels, with flat tables when a grid would exceed Discord's limits."""
    loose: list[str] = [_prose(s.note, max(_ansi_levels(s.note), default=CONT))] if s.note else []
    options: list[tuple[list[tuple[str, str, bool]], list[str], list[tuple[str, str, bool]]]] = []
    for p in _live(s, "embed"):
        if p.block:
            heads, rows = (p.rows[0][1], p.rows[1:]) if p.heads else ((), p.rows)
            levels = _logical_levels([(lv, _row_levels(p, i + p.heads)) for i, (lv, _) in enumerate(rows)])
            table = []
            for i, ((_, cells), lv) in enumerate(zip(rows, levels)):
                if lv == MORE:
                    table.append(_prose(f"_{_plain(cells[0])}_", lv))
                    continue
                kind = p.icons[i + p.heads] if p.icons else ""
                icon = _plain(_state_icon(kind, lv)).strip() if kind in ("run", "stop", "pause", "") else ""
                values = SEP.join(f"{h}: {v}" for h, v in zip(heads[1:], cells[1:])) if heads else SEP.join(cells[1:])
                value = (icon + " " if icon else "") + f"**{_md(cells[0])}:** " + _md(values)
                table.append(_prose(value, lv))
            value = _fit(table, 1024)
            field = [(clip(p.heading, 256), value, False)]
            options.append((field, [], field))
            continue
        heads, rows = (p.rows[0][1], p.rows[1:]) if p.heads else ((), p.rows)
        lines: list[str] = []
        inline: list[tuple[str, str, bool]] = []
        detailed = any(lv == NOTE for lv, _ in rows)
        levels = _logical_levels([(lv, _row_levels(p, n + p.heads)) for n, (lv, _) in enumerate(rows)])
        for n, ((_, cells), lv) in enumerate(zip(rows, levels)):
            if lv == CONT:
                continuation = _md(_plain(GAP2.join(cell for cell in cells if cell)))
                if inline:
                    name, value, side = inline[-1]
                    inline[-1] = (name, value.removesuffix("\n\u00a0") + "\n" + continuation + "\n\u00a0", side)
                if lines:
                    lines[-1] += "\n" + continuation
                else:
                    lines.append(continuation)
                continue
            if lv == MORE:
                value = _prose(f"_{_plain(cells[0])}_", lv)
                lines.append(value)
                if p.grid or heads:
                    inline.append(("\u200b", value, False))
                continue
            if p.grid:
                grid_values = [v for v in re.split(" {2,}", cells[1]) if v.strip()] or ["-"]
                value = _fit([_grid(cells[0], grid_values, _levels(p, n + p.heads), lv)], 1022)
                inline.append((clip(cells[0], 256), value + "\n\u00a0", True))
            elif heads:
                marks = _levels(p, n + 1)
                hw = min(max(map(len, heads[1:]), default=0) + 1, WIDTH // 2)
                items = [(f"{clip(h, hw - 1) + ':':<{hw}} {v}",
                          marks[j] if j < len(marks) else lv if j == 1 else FINE)
                         for j, (h, v) in enumerate(zip(heads[1:], cells[1:]), 1) if v and v != "N/A"]
                if items:
                    inline.append((*_cell(cells[0], items, lv), True))
            if detailed:
                lines.append(f"*{_md(_plain(cells[1]))}*" if lv == NOTE
                             else _prose(f"**{_md(cells[1])}**{GAP2}{_md(cells[0])}", lv))
                continue
            if p.grid:
                lines.append(f"**{_md(cells[0])}:**{GAP2}" + value.replace("\n", GAP2))
                continue
            if p.colours and not p.grid and not heads:
                lines.append(_prose(_md(GAP2.join(cell for cell in cells if cell)), lv))
                continue
            ranked = len(cells) == 3 or len(cells) == 4 and bool(p.trends)
            if not heads and ranked and cells[0].isdigit():
                renamed = cells[1].startswith("*")  # renamed apps show in italics
                name = f"_{_md(cells[1][1:])}:_" if renamed else f"{_md(cells[1])}:"
                trend = "`" + cells[3] + "`" + GAP2 if len(cells) == 4 and cells[3] else ""
                lines.append(_prose(f"{trend}**{name}**{GAP2}{cells[2]}", lv))
                continue
            rest = SEP.join(f"{_md(h)} {_md(c)}" for h, c in zip(heads[1:], cells[1:]) if c not in ("", "-")) if heads \
                else re.sub(" {2,}", GAP2, GAP2.join(_md(c) for c in cells[1:] if c))
            label = f"**{_md(cells[0])}**" if heads else f"**{_md(cells[0])}:**" if rest and len(cells) == 2 \
                else re.sub(" {2,}", GAP2, _md(cells[0]))
            lines.append(_prose(f"{label}{GAP2}{rest}" if rest else label, lv))
        flat = [(clip(p.heading, 256), _fit(lines, 1024), False)] if p.heading else []
        complete = len(inline) == sum(lv != CONT for lv in levels)
        if complete and p.heading and options:  # a full-width heading starts this part on its own row
            inline.insert(0, (clip(p.heading, 256), "\u200b", False))
        options.append((flat, [] if p.heading else lines, inline if complete else []))
    fields: list[tuple[str, str, bool]] = []
    reserved_count = sum(len(flat) for flat, _, _ in options)
    reserved_size = sum(len(n) + len(v) for flat, _, _ in options for n, v, _ in flat)
    reserved_size += sum(len("\n".join(lines)) for _, lines, _ in options)
    field_size, loose_size = 0, len("\n".join(loose))
    for flat, lines, inline in options:
        flat_size = sum(len(n) + len(v) for n, v, _ in flat)
        line_size = len("\n".join(lines))
        reserved_count -= len(flat)
        reserved_size -= flat_size + line_size
        inline_size = sum(len(n) + len(v) for n, v, _ in inline)
        size = len(s.title) + len(s.footer) + loose_size + line_size + 2
        size += field_size + inline_size + reserved_size
        if inline and len(fields) + len(inline) + reserved_count <= 25 and size <= 6000:
            fields += inline
            field_size += inline_size
        else:
            fields += flat
            field_size += flat_size
            loose_size += line_size + (2 if loose and lines else 0)
            loose += ["", *lines] if loose and lines else lines
    if len(fields) > 25:
        loose += [f"{n}\n{v}" for n, v, _ in fields[24:-1]]
        fields = fields[:24] + fields[-1:]
    e = _base(s._replace(title=clip(s.title, 256), footer=clip(s.footer, 2048)), None)
    budget = 6000 - len(e) - 4 * len(fields)
    e.description = _fit(loose, min(4096, budget)) or None
    budget = 6000 - len(e)
    for i, (name, value, side) in enumerate(fields):
        room = budget - 4 * (len(fields) - i - 1)
        name = clip(name, min(256, room - (3 if side else 1)))
        room -= len(name)
        if value.startswith("```") and value.endswith("\n```"):
            fence, _, body = value.partition("\n")
            value = f"{fence}\n" + clip(body[:-4], min(1020, room - 4) - len(fence)) + "\n```"
        else:
            value = clip(value[:-2], min(1022, room - 2)) + "\n\u00a0" if side else clip(value, min(1024, room))
        e.add_field(name=name, value=value, inline=side)
        budget -= len(name) + len(value)
    return e


def clock(t: float, tz: tzinfo | None) -> str:
    return datetime.fromtimestamp(t, tz).strftime("%I:%M %p").lstrip("0")


def footer(at: float | None, tz: tzinfo | None, update: bool = False) -> str:
    when = ("No automatic updates" if at is None else "Waiting for rate budget" if not math.isfinite(at) else
            f"Update at {clock(at, tz)} {datetime.fromtimestamp(at, tz).strftime('%Z')}")
    text = f"Shazzzbot v{__version__[:16]} ({when})"
    return "\n".join([*wrap(text, WIDTH + 3, break_long_words=False, break_on_hyphens=False),
                      *(["Update available"] if update else [])])


def stamp(now: float, tz: tzinfo | None) -> str:
    when = datetime.fromtimestamp(now, tz).strftime("%a %b %d, %I:%M:%S %p %Z").replace(" 0", " ")
    return f"Updated {when}"


def boxes(parts: list[Part], y: int) -> list[tuple[int, int]]:
    """(top, bottom) of each part's box, stacked from y: optional subtitle, rows, padding."""
    out = []
    for p in parts:
        bottom = y + PAD + ROW_H * (len(p.rows) + bool(p.heading)) + PAD
        out.append((y, bottom))
        y = bottom + BOX_GAP
    return out


def draw_box(d: ImageDraw.ImageDraw, p: Part, top: int, head: ImageFont.FreeTypeFont,
             small: ImageFont.FreeTypeFont, columns: Sequence[float] = ()) -> None:
    y = top + PAD
    if p.heading:
        d.text((24 * K, (y + ROW_H // 2) * K), p.heading, font=head, fill=DIM, anchor="lm")
        y += ROW_H
    _rows(d, p, y, small, columns)


def frame(d: ImageDraw.ImageDraw, spans: list[tuple[int, int]], width: int) -> None:
    for top, bottom in spans:
        d.rounded_rectangle((6 * K, top * K, (width - 6) * K, bottom * K), 12 * K, fill=PANEL)


def title_line(d: ImageDraw.ImageDraw, title: str, label: str, width: int, head: ImageFont.FreeTypeFont,
               small: ImageFont.FreeTypeFont, offset: int = 0) -> None:
    cy = (TOP + ROW_H // 2 - offset) * K
    d.text((24 * K, cy), title, font=head, fill=DIM, anchor="lm")
    right = max(0, small.getbbox(label, anchor="rm")[2])
    d.text(((width - 24) * K - right, cy), label, font=small, fill=DIM, anchor="rm")


def title_width(title: str, label: str, head: ImageFont.FreeTypeFont, small: ImageFont.FreeTypeFont) -> int:
    return math.ceil(72 + (text_width(head, title) + text_width(small, label)) / K)


def _image_cell(p: Part, lv: int, column: int, text: str,
                font: ImageFont.FreeTypeFont) -> tuple[str, ImageFont.FreeTypeFont]:
    renamed = bool(p.trends) and column == 1 and text.startswith("*")
    return (text[1:] if renamed else text), _italic(font) if renamed or lv in (MORE, NOTE) else font


def part_right(p: Part, font: ImageFont.FreeTypeFont, columns: Sequence[float] = ()) -> float:
    x0, widths, gap = _layout(p, font, columns)
    edge = float(x0)
    align = p.align or "l" * len(widths)
    for lv, cells in p.rows:
        if lv == MORE:
            text, actual = _image_cell(p, lv, 0, cells[0], font)
            edge = max(edge, 50 + text_width(actual, text) / K)
            continue
        x = float(x0)
        for c, (cell, cw, a) in enumerate(zip(cells, widths, align)):
            cell, actual = _image_cell(p, lv, c, cell, font)
            right = x + cw + 1 if p.trends and c == len(widths) - 1 else \
                x + (cw if a == "r" else 0) + actual.getbbox(cell, anchor="rm" if a == "r" else "lm")[2] / K
            edge = max(edge, x + cw, right)
            x += cw + _column_gap(p, c, font, gap)
    return edge


def _band_height(width: int, height: int) -> int:
    available = max(0, (renderbudget.available() - 12 * width * height) // (4 * (K * K + 1) * width))
    return height if available >= height else min(256, available)


def _image_bytes(width: int, height: int) -> int:
    return 12 * width * height + 4 * (K * K + 1) * width * max(1, _band_height(width, height))


def _stack_bytes(width: int, sizes: Sequence[tuple[int, int]], foot: int, encoded: int) -> int:
    area = width * (sum(h for _, h in sizes) + foot)
    return max(12 * area, 4 * area + 8 * max(w * h for w, h in sizes)) + encoded


@renderbudget.serialized
def placeholder(key: str, title: str, width: int, now: float | None = None,
                tz: tzinfo | None = None, note: str = "") -> bytes:
    when = time.time() if now is None else now
    _, head, small = (font.font_variant(size=font.size * K) for font in _fonts())
    caption = clip(title or "Image", 80)
    message = caption + " image is too large to render. " + (
        "Lower latest or track fewer repos for this forge." if "forge_" in key else
        "Reduce entries in this section's config.")
    label = stamp(when, tz)
    floor = max(W, title_width(caption, label, head, small))
    width = max(MIN_W, width, title_width(caption, label, head, small))
    footer = clip(note, 512)
    for candidate in (width, floor):
        width = candidate
        body = _wrap(Part([(FINE, (message,))]), small, width).rows
        tail = _wrap(Part([(FINE, (line,)) for line in footer.splitlines()]), small, width).rows
        top = TOP + ROW_H + 4
        bottom = top + 2 * PAD + ROW_H * len(body)
        height = bottom + (BOX_GAP + ROW_H * len(tail) if tail else 0) + 6
        if renderbudget.fits(4 * (K * K + 3) * width * height):
            break
    else:
        raise ValueError("render budget cannot hold a placeholder")
    out = Image.new("RGB", (width * K, height * K), BG)
    draw = ImageDraw.Draw(out)
    frame(draw, [(top, bottom)], width)
    title_line(draw, caption, label, width, head, small)
    for i, (_, cells) in enumerate(body):
        draw.text((24 * K, (top + PAD + ROW_H * (i + .5)) * K), cells[0], font=small, fill=DIM, anchor="lm")
    for i, (_, cells) in enumerate(tail):
        draw.text((width * K / 2, (bottom + BOX_GAP + ROW_H * (i + .5)) * K), cells[0],
                  font=small, fill=DIM, anchor="mm")
    buf = io.BytesIO()
    out.reduce(K).save(buf, "PNG", optimize=True)
    return Rendered(buf.getvalue(), lambda size: placeholder(key, title, max(width, size), when, tz, note),
                    key=key, section_title=title, now=when, tz=tz)


@renderbudget.serialized
def image(s: Section, now: float | None = None, tz: tzinfo | None = None, *, min_width: int = 0) -> bytes:
    """Measure the full card before drawing its text and plots at their normal sizes."""
    now = now or time.time()
    _, head, small = (f.font_variant(size=f.size * K) for f in _fonts())
    parts = [_wrap(p, small) if p.heading == ISSUES else _cap(p) for p in _live(s, "image")]
    app_widths = [_layout(p, small)[1] for p in parts if p.trends] if s.key == "apps" else []
    columns = [max(col) for col in zip(*app_widths)]
    label = stamp(now, tz)
    notes = s.note.splitlines()
    width = max(min_width, MIN_W, title_width(s.title, label, head, small),
                math.ceil(48 + max((text_width(small, note) / K for note in notes), default=0)),
                math.ceil(max((max(part_right(p, small, columns if p.trends else ()),
                                   24 + text_width(head, p.heading) / K) for p in parts), default=0) + 24))
    spans = boxes(parts, TOP + ROW_H * (1 + len(notes)) + 4)
    h = (spans[-1][1] if spans else TOP + ROW_H * (1 + len(notes))) + 6
    if not renderbudget.check(s.key, _image_bytes(width, h)):
        return placeholder(s.key, s.title, width, now, tz)
    out = Image.new("RGB", (width, h), BG)
    band_height = _band_height(width, h)
    for offset in range(0, h, band_height):
        bottom = min(h, offset + band_height)
        band = Image.new("RGB", (width * K, (bottom - offset) * K), BG)
        try:
            d = ImageDraw.Draw(band)
            visible = [(top - offset, end - offset) for top, end in spans if top < bottom and end > offset]
            frame(d, visible, width)
            for p, (top, end) in zip(parts, spans):
                if top < bottom and end > offset:
                    draw_box(d, p, top - offset, head, small, columns if p.trends else ())
            if offset < TOP + ROW_H:
                title_line(d, s.title, label, width, head, small, offset)
            for i, note in enumerate(notes, 1):
                cy = TOP + ROW_H * i + ROW_H // 2
                if offset - ROW_H < cy < bottom + ROW_H:
                    d.text((24 * K, (cy - offset) * K), note, font=small, fill=DIM, anchor="lm")
            reduced = band.reduce(K)
            try:
                out.paste(reduced, (0, offset))
            finally:
                reduced.close()
        finally:
            band.close()
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    return Rendered(buf.getvalue(), lambda width: image(s, now, tz, min_width=width),
                    key=s.key, section_title=s.title, now=now, tz=tz)


def _wrap(p: Part, font: ImageFont.FreeTypeFont, width: int | None = None) -> Part:
    rows: list[tuple[int, tuple[str, ...]]] = []
    for lv, cells in p.rows:
        first = len(rows)
        line = ""
        for ch in cells[0]:
            if line and font.getlength(line + ch) > ((W if width is None else width) - 74) * K:
                before, space, after = line.rpartition(" ")
                if space and before:
                    rows.append((lv if len(rows) == first else CONT, (before + space,)))
                    line = after
                else:
                    rows.append((lv if len(rows) == first else CONT, (line,)))
                    line = ""
            line += ch
        rows.append((lv if len(rows) == first else CONT, (line,)))
    return p._replace(rows=rows)


def _cap(p: Part) -> Part:
    if len(p.rows) <= MAX_ROWS:
        return p
    blank = ("",) * (len(p.rows[0][1]) - 1)
    return p._replace(rows=[*p.rows[:MAX_ROWS - 1], (FINE, (f"...and {len(p.rows) - MAX_ROWS + 1} more", *blank))],
                      trends=[*p.trends[:MAX_ROWS - 1], None] if p.trends else ())


def _layout(p: Part, font: ImageFont.FreeTypeFont, columns: Sequence[float] = ()) -> tuple[int, list[float], int]:
    """Left edge, natural column widths and column gap for a part."""
    n = len(p.rows[0][1])
    dotted = not p.icons and any(lv > FINE for lv, _ in p.rows)
    x0 = 50 if n == 1 and (dotted or p.rows[0][0] == MORE) else 24
    w = [max((text_width(actual, text) / K for lv, cells in p.rows if lv != MORE
              for text, actual in [_image_cell(p, lv, i, cells[i], font)]), default=0) for i in range(n)]
    if p.trends:
        w[-1] = 120.0
    if columns:
        w = list(columns)
    return x0 + ICON_W * bool(p.icons), w, GAP + 16 * (n > 1 and dotted and p.grid)  # tables colour text, no dot


def _column_gap(p: Part, column: int, font: ImageFont.FreeTypeFont, gap: int) -> float:
    return font.getlength(" ") / K if p.trends and column == len(p.rows[0][1]) - 2 else gap


def _icon(d: ImageDraw.ImageDraw, kind: str, x: float, cy: float, *, muted: bool = False) -> None:
    """A 14px state icon centred on (x, cy)."""
    def box(*xy: float) -> tuple[float, ...]:
        return tuple(v * K for v in xy)
    if kind == "run":
        d.polygon([(px * K, py * K) for px, py in ((x - 5, cy - 7), (x - 5, cy + 7), (x + 7, cy))], fill=GREEN)
    elif kind == "pause":
        d.rectangle(box(x - 6, cy - 7, x - 2, cy + 7), fill=TEXT)
        d.rectangle(box(x + 2, cy - 7, x + 6, cy + 7), fill=TEXT)
    elif kind.startswith("dot"):
        d.ellipse(box(x - 7, cy - 7, x + 7, cy + 7), fill=(GREEN, YELLOW, RED)[int(kind[3:])])
    elif kind == "stop":
        d.rectangle(box(x - 6, cy - 6, x + 6, cy + 6), fill=DIM if muted else RED)
    elif kind == "plus":
        d.rectangle(box(x - 7, cy - 1, x + 7, cy + 1), fill=DIM)
        d.rectangle(box(x - 1, cy - 7, x + 1, cy + 7), fill=DIM)
    else:  # warn
        d.polygon([(px * K, py * K) for px, py in ((x, cy - 7), (x - 8, cy + 7), (x + 8, cy + 7))], fill=YELLOW)
        d.rectangle(box(x - 1, cy - 2, x + 1, cy + 2), fill=PANEL)
        d.rectangle(box(x - 1, cy + 4, x + 1, cy + 5), fill=PANEL)


def _italic(font: ImageFont.FreeTypeFont) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(os.path.join(FONT_DIR, "NotoSans-Italic.ttf"), font.size)


def _rows(d: ImageDraw.ImageDraw, p: Part, y: int, font: ImageFont.FreeTypeFont,
          columns: Sequence[float] = ()) -> int:
    def px(t: str) -> float:
        return d.textlength(t, font) / K
    x0, w, gap = _layout(p, font, columns)
    n = len(w)
    align = p.align or "l" * n
    for i, (lv, cells) in enumerate(p.rows):
        cy = y + ROW_H // 2
        if lv == MORE:
            text, actual = _image_cell(p, lv, 0, cells[0], font)
            d.text((50 * K, cy * K), text, font=actual, fill=DIM, anchor="lm")
            _icon(d, "plus", 31, cy)
            y += ROW_H
            continue
        last = max((c for c, v in enumerate(cells) if v and v not in EMPTY), default=0) if n > 1 else -1
        x = dot = float(x0)
        table = n > 1 and not p.grid  # tables colour the problem value; lists keep the dot
        cell_lv = list(_levels(p, i)) if table else []
        if table and lv > FINE and max(cell_lv, default=FINE) == FINE:
            cell_lv = [FINE] * n
            cell_lv[last if p.on_value else 0] = lv
        for c, (cell, cw, a) in enumerate(zip(cells, w, align)):
            if p.trends and c == n - 1:
                trend = p.trends[i] if i < len(p.trends) else None
                if trend:
                    holes = charts.gaps([t for t, _ in trend.samples], trend.start, trend.end, trend.gap)
                    baseline = cy * K + font.getbbox("0", anchor="lm")[3] - font.getbbox("0", anchor="ls")[3]
                    charts.plot(d, (x * K, baseline - 20 * K, (x + cw) * K, baseline), trend.samples, 1,
                                trend.start, trend.end, None, trend.colour, holes, 2 * K, floor=True)
                x += cw + gap
                continue
            if c == last:
                dot = (x + cw - px(cell) if a == "r" else x) + 4
            bad = cell_lv[c] if c < len(cell_lv) else FINE
            colour = DIM if p.heads and i == 0 else (YELLOW, RED)[bad - 1] if bad > FINE \
                else FAINT if cell in EMPTY else TEXT
            muted = lv in (MORE, NOTE)
            cell, actual = _image_cell(p, lv, c, cell, font)
            d.text(((x + cw if a == "r" else x) * K, cy * K), cell, font=actual,
                   fill=DIM if muted else colour,
                   anchor="rm" if a == "r" else "lm")
            x += cw + _column_gap(p, c, font, gap)
        if i < len(p.icons) and p.icons[i]:
            _icon(d, p.icons[i], x0 - ICON_W / 2 - 4, cy, muted=p.block and p.icons[i] == "stop" and lv == FINE)
        if lv > FINE and not p.icons and not table:
            d.ellipse(((dot - 26) * K, (cy - 7) * K, (dot - 12) * K, (cy + 7) * K), fill=(YELLOW, RED)[lv - 1])
        y += ROW_H
    return y


def _sizes(pngs: Sequence[bytes]) -> list[tuple[int, int]]:
    sizes = []
    for png in pngs:
        with Image.open(io.BytesIO(png)) as source:
            sizes.append(source.size)
    return sizes


def _encoded_size(pngs: Sequence[bytes]) -> int:
    return sum(len(png) for png in {id(png): png for png in pngs}.values())


@renderbudget.serialized
def stack(pngs: list[bytes], note: str | Sequence[tuple[str, tuple[int, int, int]]] = "", icon: str = "",
          *, min_width: int = 0) -> bytes:
    """Stack measured sections at one width, retaining decoded pixels only after redraws finish."""
    icon_path = ICONS[icon] if icon else None
    first = pngs[0] if isinstance(pngs[0], Rendered) else None
    key = "stack:" + ",".join(png.key if isinstance(png, Rendered) else "image" for png in pngs)
    title, now, tz = (first.section_title, first.now, first.tz) if first is not None else ("Image", None, None)
    font = _fonts()[2]
    spans = [(note, DIM)] if isinstance(note, str) else list(note)
    footer = "".join(text for text, _ in spans)
    text_w = sum(text_width(font, text) for text, _ in spans)
    foot = 30 if note else 0
    width = max(min_width, math.ceil(text_w + 24 * bool(icon) + 2 * FOOT_PAD) if note else 0)
    encoded = _encoded_size(pngs)
    if not renderbudget.fits(encoded):
        renderbudget.check(key, encoded)
        return placeholder(key, title, max(W, width), now, tz, footer)
    try:
        sizes = _sizes(pngs)
    except Image.DecompressionBombError:
        renderbudget.check(key, renderbudget.RENDER_BUDGET + 1)
        return placeholder(key, title, max(W, width), now, tz, footer)
    width = max(width, max(w for w, _ in sizes))
    required = _stack_bytes(width, sizes, foot, encoded)
    if not renderbudget.fits(required):
        renderbudget.check(key, required)
        return placeholder(key, title, width, now, tz, footer)
    normalized: list[bytes] = []
    for png, (w, _) in zip(pngs, sizes):
        with renderbudget.retained(_encoded_size([*pngs, *normalized])):
            normalized.append(png.draw(width) if w < width and isinstance(png, Rendered) else png)
    try:
        sizes = _sizes(normalized)
    except Image.DecompressionBombError:
        renderbudget.check(key, renderbudget.RENDER_BUDGET + 1)
        return placeholder(key, title, width, now, tz, footer)
    width = max(width, max(w for w, _ in sizes))
    height = sum(h for _, h in sizes) + foot
    required = _stack_bytes(width, sizes, foot, _encoded_size([*pngs, *normalized]))
    if not renderbudget.check(key, required):
        return placeholder(key, title, width, now, tz, footer)
    out = Image.new("RGB", (width, height), BG)
    y = 0
    for png in normalized:
        with Image.open(io.BytesIO(png)) as source:
            converted = source.convert("RGB")
            try:
                out.paste(converted, (0, y))
                y += source.height
            finally:
                converted.close()
    if note:
        d = ImageDraw.Draw(out)
        side = 18
        mark = Image.open(icon_path).resize((side, side), Image.Resampling.LANCZOS) if icon_path else None
        lead = mark.width + 6 if mark else 0
        x = (out.width - lead - text_w) / 2
        if mark:
            out.paste(Image.new("RGB", mark.size, spans[0][1]), (round(x), y + foot // 2 - 3 - mark.height // 2), mark)
            x += lead
        for text, colour in spans:
            d.text((x, y + foot // 2 - 3), text, font=font, fill=colour, anchor="lm")
            x += font.getlength(text)
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    return Rendered(buf.getvalue(), lambda width: stack(pngs, note, icon, min_width=width),
                    key=key, section_title=title, now=now, tz=tz)
