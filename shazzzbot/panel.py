"""Build the status message sections. Data in, sections out; view.py draws them as text, embed or image.

Text tables are plain monospace code blocks so columns line up. Each row starts with a two-cell status slot that is
near-invisible when the row is fine. Discord timestamps do not render inside code blocks, so ages are computed here.
"""
import os
import re
import time
from collections.abc import Collection, Sequence
from typing import NamedTuple

import discord

from . import schema
from .apphistory import AppHistory
from .docker import Container, Engine
from .glyphs import CRIT, OK, WARN
from .host import Snapshot
from .unraid import Array, Disk

FINE, WARNING, CRITICAL = 0, 1, 2
CONT = -1  # a wrapped line that continues the row above it: indented, no glyph
NOTE = -3
MORE = -2  # the "not visible" summary under capped issues: a plus, not a level glyph
SLOT = (OK, WARN, CRIT)
COAL = discord.Color(0x1E1F22)  # one neutral embed border; status lives in the rows
LABEL = 5
ICON_TEXT_WIDTH = 3
WIDTH = 50  # desktop embed code blocks fit about 55 monospace columns; the status slot takes 3
ELL = chr(0x2026)
Thresholds = dict[str, tuple[float, float]]
DEFAULT_THRESHOLDS: dict[str, tuple[float, float]] = schema.threshold_defaults()
CORES = os.cpu_count() or 1  # the host's, since containers see every host CPU
Row = tuple[int, str]
ANSI = {WARNING: "\x1b[1;33m", CRITICAL: "\x1b[1;31m"}  # Discord ansi blocks: bold is the brightest


class Trend(NamedTuple):
    samples: Sequence[tuple[float, float]]
    start: float
    end: float
    gap: float
    colour: tuple[int, int, int]


class Part(NamedTuple):
    rows: Sequence[tuple[int, tuple[str, ...]]]
    heading: str = ""
    align: str = ""  # 'l' or 'r' per column; left when unset
    heads: bool = False  # the first row names the columns
    cut: int | None = 0  # the column clipped to fit a phone in text mode; None wraps instead
    on_value: bool = False  # image dot sits before the row's last value, not its label
    grid: bool = False  # embed view: one inline field per row, label above its values
    only: str = ""  # the views that show this part, space separated: text, embed, image
    marks: Sequence[tuple[int, ...]] = ()  # per row, a level per grid value line, or per table cell in the image
    icons: Sequence[str] = ()  # per row, an image state icon: run, pause, stop, warn, or ""
    block: bool = False  # embed view: one "**label:** head: value" line per row, not inline fields
    trends: Sequence[Trend | None] = ()
    colours: Sequence[tuple[int, ...]] = ()


class Section(NamedTuple):
    key: str
    title: str
    parts: list[Part]
    note: str = ""  # shown above the parts
    footer: str = ""
    stamp: bool = False  # Discord's own timestamp in the footer
    issues: Sequence[Row] = ()
    issues_limit: int | None = None


def use_ok_glyph(glyph: str) -> None:
    """Set once at startup; renderers read SLOT at call time, so never bind it by name."""
    global SLOT
    SLOT = (glyph, *SLOT[1:])


def level(value: float, th: tuple[float, float]) -> int:
    warn, crit = th
    return CRITICAL if value >= crit else WARNING if value >= warn else FINE


def glyph(lv: int) -> str:
    return "+" if lv == MORE else "  " if lv == NOTE else " " if lv == CONT else SLOT[lv]


def worst(rows: list[Row]) -> int:
    return max((lv for lv, _ in rows), default=FINE)


def table(rows: list[Row], limit: int = 1024) -> str:
    """Status glyphs only when a row has a status. Rows that would pass Discord's `limit` are cut."""
    glyphs = any(lv > FINE for lv, _ in rows)
    lines = [(f"{glyph(lv)} {text}" if glyphs or lv == MORE else text).rstrip() for lv, text in rows]
    out, cut = lines, 0
    fence = "```ansi\n" if any("\x1b" in line for line in lines) else "```\n"
    while len(text := fence + "\n".join(out) + "\n```") > limit:
        cut += 1
        out = lines[:-cut] + [f"...and {cut} more"]
    return text


def clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n - 1].rstrip() + ELL


def drive(name: str) -> str:
    return clip(name, 11)


def count(n: int) -> str:
    return str(n) if n < 100_000 else "99,999+"


def grid(rows: Sequence[tuple[int, tuple[str, ...]]], align: str, cut: int = 0, width: int = WIDTH,
         marks: Sequence[tuple[int, ...]] = ()) -> list[Row]:
    """Each column as wide as its widest cell, but column `cut` (the names) is clipped so a row fits `width`, never
    below 6 characters: a rare wrapped row reads better than an unreadable name. Columns are two spaces apart, or one
    when two would not fit. align is 'l' or 'r' per column."""
    w = [max(map(len, col)) for col in zip(*(cells for _, cells in rows))]
    gap = 2 if sum(w) + 2 * (len(w) - 1) <= width else 1
    w[cut] = max(6, min(w[cut], width - (sum(w) - w[cut]) - gap * (len(w) - 1)))

    def cell(i: int, c: str, n: int, a: str) -> str:
        c = clip(c, n) if i == cut else c
        return c.rjust(n) if a == "r" else c.ljust(n)

    def paint(r: int, i: int, text: str) -> str:
        lv = marks[r][i] if r < len(marks) and i < len(marks[r]) else FINE
        return f"{ANSI[lv]}{text}\x1b[0m" if lv > FINE else text
    return [(lv, (" " * gap).join(paint(r, i, cell(i, *z)) for i, z in enumerate(zip(cells, w, align))))
            for r, (lv, cells) in enumerate(rows)]


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' * (n != 1)}"


def more(counts: Sequence[tuple[int, str]]) -> list[Row]:
    phrases = [plural(n, word) for n, word in counts if n]
    return [(MORE, " and ".join(phrases).replace(" ", " additional ", 1) + " not visible")] if phrases else []


def kv(lv: int, label: str, value: str, width: int = LABEL) -> Row:
    return lv, f"{label:<{width}} {clip(value, WIDTH - width - 1)}"


def mem(mb: float) -> str:
    return f"{mb / 1024:.1f}GB" if mb >= 1024 else f"{mb:.0f}MB"


def capacity(used: float, total: float, divisor: float, unit: str) -> str:
    return f"{used / divisor:.1f}{unit}/{total / divisor:.1f}{unit}"


def uptime(seconds: int) -> str:
    d, s = divmod(seconds, 86400)
    return f"{d}d {s // 3600:02d}h {s % 3600 // 60:02d}m"


def size(kb: float) -> str:
    for unit in "KMG":
        if kb < 1024:
            return f"{kb:.1f}{unit}B"
        kb /= 1024
    return f"{kb:.1f}TB"


def system_checks(s: Snapshot, th: Thresholds) -> list[Row]:
    rows = [
        (level(s.cpu_pct, th["resource"]), f"CPU {s.cpu_pct:.0f}%"),
        (level(s.ram_pct, th["resource"]), f"RAM {s.ram_pct:.0f}%"),
        (level(s.iowait_pct, th["iowait"]), f"I/O wait {s.iowait_pct:.0f}%"),
        *((lv, "Load " + ", ".join(f"{v:.2f} ({m})" for v, m in zip(s.load, ("1m", "5m", "15m"))
                                   if level(v / CORES, th["load"]) == lv)) for lv in (CRITICAL, WARNING)
          if any(level(v / CORES, th["load"]) == lv for v in s.load)),
        (level(s.disk_util_pct, th["disk_busy"]), f"Disk I/O {s.disk_util_pct:.0f}% busiest"),
    ]
    if s.net_speed_mbps:
        pct = 100 * max(s.net_rx_mbps, s.net_tx_mbps) / s.net_speed_mbps
        rows.append((level(pct, th["resource"]), f"Net I/O {pct:.0f}% of link"))
    if s.temp_c is not None:
        rows.append((level(s.temp_c, th["temp"]), f"CPU temp {s.temp_c:.0f}C"))
    return rows


def disk_temp(d: Disk, th: Thresholds) -> int:
    return level(d.temp, th["hdd_temp" if d.rotational else "ssd_temp"]) if d.temp is not None else FINE


def disk_checks(d: Disk, smart: Collection[str] = frozenset(), th: Thresholds = DEFAULT_THRESHOLDS) -> list[Row]:
    """Unraid's own health verdict: disk status, read/write errors, unacknowledged SMART warnings, temperature."""
    rows = []
    if d.status != "DISK_OK":
        rows.append((CRITICAL, f"{d.name} {d.status}"))
    if d.errors:
        rows.append((CRITICAL, f"{d.name} {count(d.errors)} read/write errors"))
    if d.name in smart:
        rows.append((WARNING, f"{d.name} SMART warning"))
    if d.used_pct is not None and (full := level(d.used_pct, th["disk_full"])):
        rows.append((full, f"{d.name} {d.used_pct:.0f}% used"))
    if temp_level := disk_temp(d, th):
        rows.append((temp_level, f"{d.name} running {'hot' if temp_level == CRITICAL else 'warm'} ({d.temp}C)"))
    return rows


def array_checks(a: Array, smart: Collection[str] = frozenset(), th: Thresholds = DEFAULT_THRESHOLDS) -> list[Row]:
    rows = [(FINE if a.state == "STARTED" else CRITICAL, f"Array {a.state}")]
    p = a.parity
    if not p.running and (p.last_exit or p.last_errors):
        rows.append((WARNING, f"Last parity check: exit {p.last_exit}, {p.last_errors} errors"))
    for d in a.disks:
        rows += disk_checks(d, smart, th)
    return rows


def alerts(s: Snapshot, a: Array | None, cs: list[Container], th: Thresholds,
           smart: frozenset[str] = frozenset()) -> list[Row]:
    rows = system_checks(s, th) + (array_checks(a, smart, th) if a else [])
    if a and a.usage:
        used, total = a.usage
        rows.append((level(100 * used / total, th["disk_full"]),
                     f"Array {capacity(used, total, 2**30, 'TB')} ({100 * used / total:.0f}%)"))
    rows += [(CRITICAL, f"{c.name} unhealthy") for c in cs if c.unhealthy]
    return sorted((r for r in rows if r[0]), key=lambda r: -r[0])


def group_usage(rows: list[Row], th: Thresholds) -> list[Row]:
    """Per-disk usage alerts, folded to one line per level when more than one disk shares it."""
    out: list[Row] = []
    kinds = {"used": "disk_full", "busy": "disk_busy"}
    groups = {(lv, kind): [t for v, t in rows if v == lv and re.fullmatch(rf"\S+ \d+% {kind}", t)]
              for lv in (CRITICAL, WARNING) for kind in kinds}
    for lv, t in rows:
        kind = t.rsplit(" ", 1)[-1]
        group = groups.get((lv, kind), [])
        if t not in group or len(group) < 2:
            out.append((lv, t))
        elif t == group[0]:
            out.append((lv, f"{len(group)} disks {th[kinds[kind]][lv - 1]:.0f}%+ {kind}"))
    return out


def flow(mega: float, base: int, unit: str, sep: str = "") -> str:
    """Whole number, scaled K/M/G so it stays at three digits."""
    n = mega * base
    for prefix in "KM":
        if round(n) < 1000:
            return f"{n:.0f}{sep}{prefix}{unit}"
        n /= base
    return f"{n:.0f}{sep}G{unit}"


def apps_section(s: Snapshot, history: AppHistory | None = None, window: int | None = None,
                 now: float | None = None) -> Section:
    from .charts import BLUE, DEFAULT_WINDOW, GREEN, span
    from .glyphs import NO_DATA
    from .spark import TREND_WIDTH, spark

    seconds = window or DEFAULT_WINDOW
    end = time.time() if now is None else now
    start = end - seconds

    def top(apps: list[tuple[str, str]], heading: str, metric: str) -> Part:
        rows = []
        trends = []
        series = {name: history.values(metric, name, start, end) if history else [] for name, _ in apps}
        for i, (name, value) in enumerate(apps, 1):
            samples = series[name]
            trend = Trend(tuple(samples), start, end, history.interval * 3,
                          BLUE if metric == "cpu" else GREEN) if history and samples else None
            text = spark(samples, TREND_WIDTH, start=start, end=end) if trend else NO_DATA * TREND_WIDTH
            bare = name not in s.grouped_apps and not name.startswith(("*", "VM:", "LXC:"))
            grouped = bare and (len(s.app_instances.get(name, ())) > 1
                                or bool(history and history.multiple(metric, name, start, end)))
            marker = " (+)" if grouped else ""
            limit = 32 - len(marker)
            label = (name if len(name) <= limit else name[:limit - 3] + "...") + marker
            rows.append((FINE, (str(i), label, value, text)))
            trends.append(trend)
        return Part(rows or [(FINE, ("none",))], heading, "llrl", cut=1 if apps else None,
                    trends=trends)
    parts = [top([(n, f"{v:.1f}%") for n, v in s.top_cpu], f"By CPU ({s.cpu_pct:.0f}%)", "cpu"),
             top([(n, mem(v)) for n, v in s.top_ram],
                 f"By RAM ({capacity(s.ram_used_mb, s.ram_total_mb, 1024, 'GB')})", "ram")]
    partial = any(NO_DATA in row[1][3] for part in parts for row in part.rows if len(row[1]) == 4)
    note = f"Trends, last {span(seconds)}{' (partial data)' if partial else ''}" if history or partial else ""
    return Section("apps", "Top Apps", parts, note=note)


def map_disk_alerts(a: Array, rows: Sequence[Row]) -> dict[str, Row]:
    names = {key: d.name for d in a.disks for key in (d.device, "/dev/" + d.device)}
    out = {}
    for lv, text in rows:
        device, space, rest = text.partition(" ")
        name = device.removeprefix("/dev/")
        if device in names or re.fullmatch(
                r"(?:[hsv]d[a-z]+|xvd[a-z]+|nvme\d+n\d+(?:p\d+)?|mmcblk\d+(?:p\d+)?|dm-\d+|md\d+)", name):
            out[text] = (lv, names.get(device, name) + space + rest)
    return out


def array_section(a: Array, smart: Collection[str] = frozenset(),
                  io: dict[str, tuple[float, float, float]] | None = None,
                  th: Thresholds = DEFAULT_THRESHOLDS) -> Section:
    io = io or {}
    cells = [(FINE, ("Disk", "Temp", "Used", "Free", "Read", "Write", "Busy", "Errors", "SMART"))]
    warned = list(smart)
    marks: list[tuple[int, ...]] = [()]
    tr = tw = tb = 0.0
    busy_rows: list[Row] = []
    for d in a.disks:
        temp = f"{d.temp}C" if d.temp is not None else "-"
        used = f"{d.used_pct:.0f}%" if d.used_pct is not None else "-"
        free = size(d.free_kb) if d.free_kb is not None else "-"
        r, w, busy = io.get(d.device, (0.0, 0.0, 0.0))
        tr, tw, tb = tr + r, tw + w, max(tb, busy)
        n = warned.count(d.name)
        busy_lv = level(busy, th["disk_busy"])
        if busy_lv:
            busy_rows.append((busy_lv, f"{d.name} {busy:.0f}% busy"))
        cells.append((max(worst(disk_checks(d, smart, th)), busy_lv),
                      (drive(d.name), temp, used, free, flow(r, 1024, "B/s"), flow(w, 1024, "B/s"),
                       f"{busy:.0f}%", count(d.errors), plural(n, "warning") if n else "ok")))
        used_lv = FINE if d.used_pct is None else level(d.used_pct, th["disk_full"])
        marks.append((FINE, disk_temp(d, th), used_lv, *(FINE,) * 3, busy_lv, CRITICAL if d.errors else FINE,
                      WARNING if n else FINE))
    if a.disks:
        temps = [d.temp for d in a.disks if d.temp is not None]
        sized = [d for d in a.disks if d.used_pct is not None]
        total_kb, free_kb = sum(d.size_kb for d in sized), sum(d.free_kb or 0 for d in sized)
        loads = [io.get(d.device, (0.0, 0.0, 0.0))[2] for d in a.disks]
        errors, n = sum(d.errors for d in a.disks), sum(warned.count(d.name) for d in a.disks)
        avg_temp = sum(temps) / len(temps) if temps else None
        used_pct = 100 * (1 - free_kb / total_kb) if total_kb else None
        cells.append((FINE, ("Total", "-" if avg_temp is None else f"~{avg_temp:.0f}C",
                             "-" if used_pct is None else f"{used_pct:.0f}%",
                             size(free_kb) if sized else "-",
                             flow(tr, 1024, "B/s"), flow(tw, 1024, "B/s"), f"~{sum(loads) / len(loads):.0f}%",
                             count(errors), plural(n, "warning") if n else "ok")))
        marks.append((FINE, FINE if avg_temp is None else level(avg_temp, th["hdd_temp"]),
                      FINE if used_pct is None else level(used_pct, th["disk_full"]), *(FINE,) * 4,
                      CRITICAL if errors else FINE, WARNING if n else FINE))
    reasons = [(lv, text) for lv, text in array_checks(a, smart, th) if lv > FINE] + busy_rows
    issues = sum(lv > FINE for lv, c in cells if c[0] != "Total")
    state = f"Array {a.state.lower()}"
    if a.state == "STARTED":
        state += f" ({issues} issue{'s' * (issues != 1)})" if issues else " (no issues)"
    head = (max((lv for lv, _ in cells), default=FINE) if a.state == "STARTED" else CRITICAL, (state,))
    totals = [(FINE, (f"Total read: {flow(tr, 1024, 'B/s')}",)), (FINE, (f"Total write: {flow(tw, 1024, 'B/s')}",)),
              (FINE, (f"Busiest disk: {tb:.0f}%",))]
    return Section("disks", "Disks", [Part([head, *totals], "Array Status", cut=None),
                                      Part(cells, align="lrrrrrrrr", heads=True, marks=marks, only="embed image"),
                                      Part([(lv, c[:-2]) for lv, c in cells], align="lrrrrrr", heads=True,
                                           marks=[m[:-2] for m in marks], only="text")],
                   issues=group_usage(reasons, th))


def down(c: Container) -> bool:
    return c.state != "running"


def stack_state(cs: list[Container]) -> Row:
    if not any(c.state == "running" for c in cs):
        return FINE, "stopped"
    if any(down(c) for c in cs):
        return CRITICAL, "degraded"
    return (WARNING, "unhealthy") if any(c.unhealthy for c in cs) else (FINE, "up")


def stack_icon(state: Row) -> str:
    lv, name = state
    return f"dot{lv}" if lv else "run" if name == "up" else "stop"


def docker_section(cs: list[Container], engine: Engine | None) -> Section:
    stacks: dict[str, list[Container]] = {}
    for c in cs:
        stacks.setdefault(c.project, []).append(c)
    loose = stacks.pop("", [])
    states = {name: stack_state(members) for name, members in stacks.items()}
    running = sum(c.state == "running" for c in cs)
    sick = [c for c in cs if c.state == "running" and c.unhealthy]
    up = sum(t == "up" for _, t in states.values())
    summary: list[tuple[int, tuple[str, ...]]] = []
    if engine:
        summary += [(FINE, ("Engine", f"{engine.version} {engine.driver}")), (FINE, ("Images", str(engine.images)))]
    summary += [(WARNING if sick else FINE, ("Running", f"{running} of {len(cs)}")),
                (worst(list(states.values())),
                 ("Stacks", f"{up} of {plural(len(states), 'stack')} up" if states else "none")),
                (FINE, ("Networks", f"{plural(len({n for c in cs for n in c.networks}), 'network')} in use"))]
    cells: list[tuple[int, tuple[str, ...]]] = [(FINE, ("Stack", "Up"))]
    icons = [""]

    def line(lv: int, name: str, members: list[Container]) -> tuple[int, tuple[str, ...]]:
        return lv, (name, f"{sum(c.state == 'running' for c in members)}/{len(members)}")
    for name in sorted(stacks, key=lambda n: (-states[n][0], n)):
        cells.append(line(states[name][0], name, stacks[name]))
        icons.append(stack_icon(states[name]))
    if loose:  # standalone containers are not a stack: stopped ones are normal, never degraded
        cells.append(line(FINE, "(no stack)", loose))
        icons.append("")
    problems = [(WARNING, (c.name, "unhealthy")) for c in sick]
    def degraded(members: list[Container]) -> str:
        lines = [f"{c.name} {c.state} ({c.exit_code})" if c.exit_code is not None else f"{c.name} {c.state}"
                 for c in members if down(c)]
        return "degraded:" + "".join("\n" + line for line in lines[:5]) + (
            f"\n...and {len(lines) - 5} more" if len(lines) > 5 else "")
    problems += [(CRITICAL, (name, degraded(members))) for name, members in stacks.items()
                 if states[name][0] == CRITICAL]
    return Section("docker", "Docker Containers", [Part(summary, "Platform", cut=1, on_value=True, grid=True),
                                                  Part(cells, "Stacks", align="lr", heads=True, on_value=True,
                                                       icons=icons, block=True)],
                   issues=[(lv, " ".join(cells)) for lv, cells in problems])
