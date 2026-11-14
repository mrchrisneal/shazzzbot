"""The System table as an image: one bar per metric, coloured on the shared green-yellow-red ramp."""
import io
import math
import re
import time
from collections import Counter
from datetime import tzinfo
from typing import NamedTuple

from PIL import Image, ImageDraw, ImageFont

from . import __version__, renderbudget, view
from . import ups as nut
from .charts import BG, GREEN, GRID, RED, TEXT, YELLOW, _fonts, _mix, ramp, text_width
from .host import Snapshot
from .jellyfin import KINDS
from .jobs import Active, dur
from .panel import (
    CORES,
    CRITICAL,
    FINE,
    WARNING,
    Part,
    Section,
    Thresholds,
    capacity,
    clip,
    level,
    plural,
    uptime,
    worst,
)
from .panel import Row as Alert
from .tasktimes import elapsed

W, ROW_H = 800, 50
K = renderbudget.SCALE
DOWN, UP, DEG, SEP = chr(0x2193), chr(0x2191), chr(0xB0), f" {chr(0xB7)} "
DOT = chr(0x25CF)  # a span of this draws a status dot in its colour
ARROWS = {DOWN: 90.0, UP: -90.0}  # degrees, clockwise from pointing right
RGB = tuple[int, int, int]
ROOM_C = 16.0  # the temperature bar starts at room temperature, about 60F


class Row(NamedTuple):
    label: str
    text: str
    fill: float | None = None  # bar fill 0..1, or no bar
    colour: tuple[int, int, int] | None = None
    dot: int = FINE
    note: str = ""  # drawn in green after the text
    spans: tuple[tuple[str | float, RGB], ...] = ()  # coloured text or arrow angles, drawn instead of text
    level: int = FINE  # for the text and embed views, which have no bar colour
    issues: tuple[Alert, ...] = ()  # the Issues footer lists these in place of this row
    marks: tuple[int, ...] = ()  # embed view: a level for each value line, in place of the row dot
    cols: tuple[str, ...] = ()  # text view: sub-values padded to columns shared with the other rows that have them
    marker: float | None = None
    colours: tuple[int, ...] = ()
    lines: tuple[str, ...] = ()  # text and embed views: one value per line, in place of the drawn text


def sig3(mega: float, base: int, unit: str) -> str:
    """A fixed three-character number, scaled K/M/G: 290 KB/s, 61 Kbps, 4.0 MB/s."""
    n = mega * base
    for prefix in "KM":
        if n < 999.5:
            break
        n /= base
    else:
        prefix = "G"
    text = f"{n:.1f}" if n < 9.95 else f"{n:.0f}"
    return f"{text:\u2007>3} {prefix}{unit}"


def swivel(now: float, then: float) -> float:
    """Arrow angle for a change: 45 degrees up at +10% a minute, clamped to 75, 0 when flat."""
    return -max(-75.0, min(75.0, math.degrees(math.atan(10 * (now - then) / max(then, 0.1)))))


def swivel_colour(angle: float) -> RGB:
    """White when level; toward yellow then red as it climbs (red by 60 degrees), toward green as it falls."""
    t = min(1.0, abs(angle) / 60)
    if angle > 0:
        return _mix(TEXT, GREEN, t)
    return _mix(TEXT, YELLOW, t * 2) if t < 0.5 else _mix(YELLOW, RED, t * 2 - 1)


def bar(label: str, text: str, value: float, warn: float, crit: float, full: float = 100.0, low: float = 0.0,
        calm: float | None = None, hot: str = "", state: int | None = None) -> Row:
    """Use resource thresholds, or an explicit device state, to colour the shared bar."""
    colour = ramp(value, warn, crit, calm) if state is None else (GREEN, YELLOW, RED)[state]
    lv = level(value, (warn, crit)) if state is None else state
    i = text.rfind(hot) if hot else -1
    spans = ((text[:i], TEXT), (hot, (TEXT, YELLOW, RED)[lv]), (text[i + len(hot):], TEXT)) if i >= 0 else ()
    return Row(label, text, (value - low) / (full - low), colour, level=lv,
               spans=tuple(x for x in spans if x[0]))


def _ups_shutdown(row: Row, setting: dict | None) -> Row:
    if not setting or setting.get("mode") not in {"batt_level", "batt_timer", "sec_timer"}:
        return row
    mode, value = setting["mode"], setting["value"]
    note = f"(off at {value:g}%)" if mode == "batt_level" else \
        f"(off at {elapsed(value)} left)" if mode == "batt_timer" else f"(off after {elapsed(value)} on battery)"
    marker = value if mode == "batt_level" else None
    if not row.spans:
        return row._replace(text=row.text + " " + note, marker=marker, lines=(row.text, note))
    spans = (*row.spans[:2], (" " + note, TEXT), *row.spans[2:])
    return row._replace(text="".join(str(text) for text, _ in spans), spans=spans, marker=marker,
                        lines=(row.lines[0], note, *row.lines[1:]), marks=(row.marks[0], FINE, *row.marks[1:]))


def ups_row(reading: nut.Reading, th: Thresholds, shutdown: dict | None = None) -> Row:
    if not reading.status:
        return _ups_shutdown(Row("UPS", "no data", dot=WARNING, issues=((WARNING, "UPS: no data"),)), shutdown)
    flags = set(reading.status)
    state = CRITICAL if flags & {"LB", "RB", "FSD", "OFF", "OVER"} else WARNING \
        if flags & {"OB", "ALARM", "BYPASS", "WAIT"} or flags - nut.STATUS.keys() else FINE
    parts = (f"{reading.charge:.0f}%" if reading.charge is not None else "charge no data",
             f"{elapsed(reading.runtime)} left" if reading.runtime is not None else "runtime no data", reading.words)
    marks = (WARNING if reading.charge is None else FINE, WARNING if reading.runtime is None else FINE, state)
    lv = max(marks)
    lines = (f"{parts[0]} - {parts[1]}", parts[2])
    row = bar("UPS", "    ".join(lines), reading.charge or 0.0, *th["resource"], state=lv)
    spans = tuple((sep + part, (TEXT, YELLOW, RED)[mark]) for sep, part, mark in zip(("", " - ", "    "), parts, marks))
    return _ups_shutdown(row._replace(fill=None if reading.charge is None else row.fill, spans=spans, lines=lines,
                                     marks=(max(marks[:2]), state),
                                     issues=((lv, "UPS: " + "  ".join(lines)),) if lv > FINE else ()), shutdown)


def rows(s: Snapshot, th: Thresholds, os_name: str = "", os_update: str = "",
         usage: tuple[float, float] | None = None, load_then: tuple[float, float, float] | None = None,
         hostname: str = "", ups: nut.Reading | None = None, ups_shutdown: dict | None = None) -> list[Row]:

    out = [Row("Hostname", hostname)] if hostname else []
    if os_name:
        out.append(Row("OS", os_name, note="(update available!)" if os_update else ""))
    out.append(Row("Uptime", uptime(s.uptime_s),
                   dot=CRITICAL if s.uptime_s < 900 else WARNING if s.uptime_s < 3600 else FINE))
    out += [bar("CPU", f"{s.cpu_pct:.0f}%    (iowait {s.iowait_pct:.0f}%)", s.cpu_pct, *th["resource"],
                hot=f"{s.cpu_pct:.0f}%"),
            bar("RAM", f"{capacity(s.ram_used_mb, s.ram_total_mb, 1024, 'GB')}    ({s.ram_pct:.0f}%)",
                s.ram_pct, *th["resource"], hot=f"{s.ram_pct:.0f}%")]
    if s.temp_c is not None:
        warn, crit = th["temp"]
        out.append(bar("Temp", f"{s.temp_c:.0f}{DEG}C", s.temp_c, warn, crit, full=crit, low=ROOM_C, calm=warn - 15,
                       hot=f"{s.temp_c:.0f}{DEG}C"))
    spans: list[tuple[str | float, RGB]] = []
    levels = [level(v / CORES, th["load"]) for v in s.load]
    for v, then, name, lv in zip(s.load, load_then or s.load, ("1m", "5m", "15m"), levels):
        angle = swivel(v, then)
        spans += [(f"{v:.2f}", (TEXT, YELLOW, RED)[lv]), (f" ({name})  ", TEXT),
                  (angle, swivel_colour(angle)), (" " * 8, TEXT)]
    out.append(Row("Load", "", spans=tuple(spans[:-1]), level=max(levels),
                   cols=tuple(f"{v:.2f} ({name})" for v, name in zip(s.load, ("1m", "5m", "15m")))))
    r, w = sig3(s.disk_read_mbs, 1024, "B/s"), sig3(s.disk_write_mbs, 1024, "B/s")
    busy = f"({s.disk_util_pct:.0f}% busiest)"
    out.append(bar("Disk I/O", f"R {r}   W {w}   {busy}", s.disk_util_pct, *th["resource"],
                   state=level(s.disk_util_pct, th["disk_busy"]),
                   hot=f"{s.disk_util_pct:.0f}% busiest")._replace(cols=(f"R {r}", f"W {w}", busy)))
    net = (f"{DOWN} {sig3(s.net_rx_mbps, 1000, 'bps')}", f"{UP} {sig3(s.net_tx_mbps, 1000, 'bps')}")
    if s.net_speed_mbps:
        pct = 100 * max(s.net_rx_mbps, s.net_tx_mbps) / s.net_speed_mbps
        link = f"({pct:.0f}% of link)"
        out.append(bar("Net I/O", f"{net[0]}   {net[1]}   {link}", pct, *th["resource"],
                       hot=f"{pct:.0f}% of link")._replace(cols=(*net, link)))
    else:
        out.append(Row("Net I/O", f"{net[0]}   {net[1]}", cols=net))
    if usage:
        pct = 100 * usage[0] / usage[1]
        out.append(bar("Array", f"{capacity(usage[0], usage[1], 2**30, 'TB')}    ({pct:.0f}%)",
                       pct, *th["disk_full"], hot=f"{pct:.0f}%"))
    if ups is not None:
        out.append(ups_row(ups, th, ups_shutdown))
    return out


def status_rows(issues: list[Alert], active: list[Active], soonest: list[tuple[float, str]],
                streams: Counter[str] | None, jellyfin: bool, colours: dict[str, int] | None = None) -> list[Row]:
    crit = sum(lv == CRITICAL for lv, _ in issues)
    warn = len(issues) - crit
    counts = ([(DOT, RED), (f"{crit} critical", TEXT)] * bool(crit)
              + [("    ", TEXT)] * bool(crit and warn)
              + [(DOT, YELLOW), (plural(warn, "warning"), TEXT)] * bool(warn))
    out = [Row("Alerts", "    ".join(t for t, c in counts if c == TEXT and t.strip()) or "none", spans=tuple(counts),
               level=worst(issues), issues=tuple(issues), marks=(CRITICAL,) * bool(crit) + (WARNING,) * bool(warn))]
    if active:
        names = ", ".join(n for _, n, _ in active)
        text = f"{plural(len(active), 'task')} running:  {names if len(names) <= 44 else names[:43] + '...'}"
        hot = any(name in (colours or {}) for _, name, _ in active)
        shown = active[:3]
        out.append(Row("Tasks", text, dot=max(lv for _, _, lv in active),
                       spans=((text, YELLOW),) if hot else (), colours=(WARNING, WARNING) if hot else (),
                       lines=(f"{plural(len(active), 'task')} running:", *(clip(n, 30) for _, n, _ in shown)),
                       marks=(WARNING if any(n in (colours or {}) for _, n, _ in active[3:]) else FINE,
                              *(WARNING if n in (colours or {}) else FINE for _, n, _ in shown))))
    else:
        out.append(Row("Tasks", f"next:  {soonest[0][1]} in {dur(soonest[0][0])}" if soonest else "idle"))
    if jellyfin:
        if streams is None:
            out.append(Row("Jellyfin", "unreachable", dot=CRITICAL))
        else:
            n = sum(streams.values())
            parts = ", ".join(f"{streams[k]} {'transcoding' if k == 'transcode' else k}" for k in KINDS if streams[k])
            out.append(Row("Jellyfin", f"{parts} ({n} total)" if n else "0 streams"))
    return out


def _arrow(im: Image.Image, x: float, cy: float, deg: float, colour: RGB) -> float:
    """One solid colour through a right-pointing mask that is rotated, so edges blend only into the background."""
    h = 9 * K
    mask = Image.new("L", (2 * h, 2 * h), 0)
    m = ImageDraw.Draw(mask)
    m.line((h - 7 * K, h, h + 3 * K, h), fill=255, width=2 * K)
    m.polygon(((h + 2 * K, h - 5 * K), (h + 2 * K, h + 5 * K), (h + 8 * K, h)), fill=255)
    im.paste(colour, (round((x + 6) * K) - h, round(cy * K) - h), mask.rotate(-deg, Image.Resampling.BICUBIC))
    return x + 14


def _text(im: Image.Image, d: ImageDraw.ImageDraw, x: float, cy: float, text: str, font: ImageFont.FreeTypeFont,
          colour: tuple[int, int, int] = TEXT) -> float:
    """Draw text at K scale, with the arrow characters drawn as shapes (Noto Sans has no arrow glyphs).
    Returns the end x in unscaled pixels."""
    for ch in text:
        if ch in ARROWS:
            x = _arrow(im, x, cy, ARROWS[ch], colour)
        elif ch == DOT:
            d.ellipse((x * K, (cy - 8) * K, (x + 16) * K, (cy + 8) * K), fill=colour)
            x += 26
        else:
            d.text((x * K, cy * K), ch, font=font, fill=colour, anchor="lm")
            x += d.textlength(ch, font) / K
    return x


IN_FEED = {"CPU", "RAM", "Temp", "Load", "Disk I/O", "Net I/O", "Array"}  # the Alerts row's feed already lists these


def section(title: str, subtitle: str, upcoming: str, table: list[Row], status: tuple[int, str] | None) -> Section:
    """The System section for the text and embed views; the image view draws `table` with render()."""
    widths = [max((len(r.cols[i]) for r in table if len(r.cols) > i), default=0) for i in range(3)]

    def plain(r: Row) -> str:
        if r.cols:
            return "  ".join(c.ljust(w) for c, w in zip(r.cols, widths)).rstrip()
        if r.lines:
            return "  ".join(r.lines)
        text = "".join(t for t, _ in r.spans if isinstance(t, str) and t != DOT) if r.spans else r.text
        return re.sub(" {2,}", "  ", text).strip() + (f" {r.note}" if r.note else "")
    first = ("Hostname", "OS", "Uptime", "Temp")  # Temp up front, so CPU, RAM and Load share an embed row
    table = sorted(table, key=lambda r: first.index(r.label) if r.label in first else len(first))
    rows = [(max(r.dot, r.level), (r.label, plain(r))) for r in table]
    issues = [i for r, (lv, cells) in zip(table, rows) if lv > FINE and r.label not in IN_FEED
              for i in ([a for a in r.issues if a[0] > FINE] or [(lv, f"{cells[0]}: {cells[1]}")])]
    if status:
        rows.append((status[0], ("Nodes", status[1])))
    def marks(r: Row, value: str) -> tuple[int, ...]:
        hot = {t.strip(): (YELLOW, RED).index(c) + 1 for t, c in r.spans if isinstance(t, str) and c in (YELLOW, RED)}
        found = tuple(max((lv for t, lv in hot.items() if t and t in v), default=FINE)
                      for v in re.split(" {2,}", value))
        return r.marks or (found if any(found) else ())
    return Section("system", title, [Part(rows, cut=None, grid=True,
                                          marks=[() if r.colours and not r.lines else marks(r, cells[1])
                                                 for r, (_, cells) in zip(table, rows)],
                                          colours=[() if r.lines else r.colours for r in table])], subtitle,
                   f"Shazzzbot v{__version__} (Update at {upcoming})",
                   stamp=True, issues=list(dict.fromkeys(
                       issues + [(lv, f"{cells[0]}: {cells[1]}") for lv, cells in rows[len(table):] if lv > FINE])))


def node_status(nodes: list[tuple[int, float | None]], outdated: bool = False) -> tuple[int, str]:
    """(level, latency ms or None) per node -> the header line. Latency is informational and never sets the level."""
    lv, n = max(lv for lv, _ in nodes), len(nodes)
    ms = [m for _, m in nodes if m is not None]
    tail = f" ({'~' * (len(ms) > 1)}{sum(ms) / len(ms):.0f}ms)" if ms else ""
    text = (f"{sum(v == FINE for v, _ in nodes)} of {plural(n, 'node')} ok (see logs)" if lv
            else f"{plural(n, 'node')} ok{tail}")
    return lv, text + ("  (update available!)" if outdated else "")


def image_order(table: list[Row]) -> list[Row]:
    """The image reads Load before CPU; text and embed keep CPU, RAM and Load on one row."""
    load = [r for r in table if r.label == "Load"]
    rest = [r for r in table if r.label != "Load"]
    cpu = next((i for i, r in enumerate(rest) if r.label == "CPU"), None)
    return rest[:cpu] + load + rest[cpu:] if load and cpu is not None else table


class Layout(NamedTuple):
    width: int
    bx0: float
    bx1: float
    tx: float
    right_edges: tuple[float, ...]


def row_width(row: Row, font: ImageFont.FreeTypeFont) -> float:
    x = edge = 26.0 if row.dot else 0.0
    for text, _ in row.spans or ((row.text, TEXT),):
        for value in text if isinstance(text, str) else (text,):
            if not isinstance(value, str) or value in ARROWS:
                edge = max(edge, x + 16)
                x += 14
            elif value == DOT:
                x += 26
                edge = max(edge, x)
            else:
                edge = max(edge, x + text_width(font, value) / K)
                x += font.getlength(value) / K
    return max(edge, x + 12 + text_width(font, row.note) / K) if row.note else edge


def layout(table: list[Row], head: ImageFont.FreeTypeFont, small: ImageFont.FreeTypeFont) -> Layout:
    bx0 = 48 + max((text_width(head, row.label) / K for row in table), default=0)
    bx1, tx = bx0 + 250, bx0 + 270
    edges = tuple((tx if row.fill is not None else bx0) + row_width(row, small) for row in table)
    return Layout(max(W, math.ceil(max(edges, default=bx0) + 24)), bx0, bx1, tx, edges)


@renderbudget.serialized
def render(table: list[Row], now: float | None = None, tz: tzinfo | None = None,
           status: tuple[int, str] | None = None, *, min_width: int = 0) -> bytes:
    """status is (level, text) for the Nodes row, drawn last as in the text and embed views."""
    now = now or time.time()
    source = table
    footer = view.parts(section("", "", "", table, status))[1:]
    if status:
        nodes_text, flag, _ = status[1].partition("  (update available!)")
        table = [*table, Row("Nodes", nodes_text, dot=status[0], note="(update available!)" if flag else "")]
    table = image_order(table)
    _, head, small = (f.font_variant(size=f.size * K) for f in _fonts())
    positions = layout(table, head, small)
    width = max(min_width, positions.width, view.title_width("System", view.stamp(now, tz), head, small))
    footer = [view._wrap(p, small) for p in footer]
    rows_top = view.TOP + view.ROW_H + 4
    spans = [(rows_top, rows_top + view.PAD + ROW_H * len(table) + view.PAD)]
    spans += view.boxes(footer, spans[0][1] + view.BOX_GAP)
    h = spans[-1][1] + 6
    required = 4 * (K * K + 3) * width * h + 6 * (18 * K) ** 2 + 131072
    if not renderbudget.check("system", required):
        return view.placeholder("system", "System", width, now, tz)
    im = Image.new("RGB", (width * K, h * K), BG)
    d = ImageDraw.Draw(im)
    view.frame(d, spans, width)
    view.title_line(d, "System", view.stamp(now, tz), width, head, small)
    bx0, bx1, tx = positions.bx0, positions.bx1, positions.tx
    for n, r in enumerate(table):
        cy = rows_top + view.PAD + n * ROW_H + ROW_H // 2
        d.text((24 * K, cy * K), r.label, font=head, fill=TEXT, anchor="lm")
        x = bx0
        if r.fill is not None:
            d.rounded_rectangle((bx0 * K, (cy - 9) * K, bx1 * K, (cy + 9) * K), 9 * K, fill=GRID)
            end = bx0 + (bx1 - bx0) * max(0.0, min(1.0, r.fill))
            if end - bx0 >= 18:
                d.rounded_rectangle((bx0 * K, (cy - 9) * K, end * K, (cy + 9) * K), 9 * K, fill=r.colour)
            elif end > bx0:
                d.ellipse((bx0 * K, (cy - 9) * K, (bx0 + 18) * K, (cy + 9) * K), fill=r.colour)
            if r.marker is not None:
                mark = bx0 + (bx1 - bx0) * max(0.0, min(1.0, r.marker / 100))
                d.line((mark * K, (cy - 11) * K, mark * K, (cy + 11) * K), fill=(255, 255, 255), width=2 * K)
            x = tx
        if r.dot:
            d.ellipse((x * K, (cy - 8) * K, (x + 16) * K, (cy + 8) * K), fill=(YELLOW, RED)[r.dot - 1])
            x += 26
        end_x: float = x
        for text, colour in r.spans or ((r.text, TEXT),):
            end_x = (_text(im, d, end_x, cy, text, small, colour) if isinstance(text, str)
                     else _arrow(im, end_x, cy, text, colour))
        if r.note:
            d.text(((end_x + 12) * K, cy * K), r.note, font=small, fill=GREEN, anchor="lm")
    for p, (top, _) in zip(footer, spans[1:]):
        view.draw_box(d, p, top, head, small)
    buf = io.BytesIO()
    im.reduce(K).save(buf, "PNG", optimize=True)
    return view.Rendered(buf.getvalue(), lambda width: render(source, now, tz, status, min_width=width),
                         key="system", section_title="System", now=now, tz=tz)
