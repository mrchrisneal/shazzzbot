"""Rolling metric history and the 2x2 CPU / RAM / disk I/O / net I/O chart image."""
import contextlib
import io
import itertools
import logging
import math
import os
import re
import sqlite3
import threading
import time
from array import array
from collections import deque
from collections.abc import Sequence
from datetime import datetime, tzinfo
from typing import cast

from PIL import Image, ImageDraw, ImageFont

from . import renderbudget
from .helper import formats
from .host import Snapshot
from .panel import FINE, Part, Section, flow
from .spark import limits

log = logging.getLogger(__name__)
DAY = 86400
MIN_WINDOW, MAX_WINDOW, DEFAULT_WINDOW = 300, 7 * DAY, 1800
SPANS = ("5m", "15m", "30m", "1h", "2h", "6h", "12h", "24h", "3d", "7d")
# Discord shows embed images about 400px wide, so draw at 2x with large type to stay legible on a phone.
W, HEAD_H, CHART_H, DISK_H, KEY_H, KEY_W = 800, 52, 220, 220, 30, 54
FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")
BG, PANEL, GRID, TEXT, DIM = (43, 45, 49), (30, 31, 34), (58, 60, 67), (220, 221, 222), (148, 155, 164)
SHADE = (15, 15, 17)  # PANEL under 50% black: marks spans with no samples


RGB = tuple[int, int, int]
# Apple HIG system colours, dark-mode variants.
BLUE, GREEN, YELLOW, ORANGE, RED = (0, 145, 255), (48, 209, 88), (255, 214, 0), (255, 146, 48), (255, 66, 69)


def _lab(rgb: RGB) -> tuple[float, float, float]:
    r, g, b = (v / 255 / 12.92 if v / 255 <= 0.04045 else ((v / 255 + 0.055) / 1.055) ** 2.4 for v in rgb)
    lc = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    mc = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    sc = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (0.2104542553 * lc + 0.7936177850 * mc - 0.0040720468 * sc,
            1.9779984951 * lc - 2.4285922050 * mc + 0.4505937099 * sc,
            0.0259040371 * lc + 0.7827717662 * mc - 0.8086757660 * sc)


def _linear(light: float, a: float, b: float) -> tuple[float, float, float]:
    lc = (light + 0.3963377774 * a + 0.2158037573 * b) ** 3
    mc = (light - 0.1055613458 * a - 0.0638541728 * b) ** 3
    sc = (light - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (4.0767416621 * lc - 3.3077115913 * mc + 0.2309699292 * sc,
            -1.2684380046 * lc + 2.6097574011 * mc - 0.3413193965 * sc,
            -0.0041960863 * lc - 0.7034186147 * mc + 1.7076147010 * sc)


def _rgb(lin: tuple[float, float, float]) -> RGB:
    out = [12.92 * v if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055 for v in lin]
    r, g, b = (round(255 * min(1.0, max(0.0, v))) for v in out)
    return r, g, b


def oklch(hue: float, light: float, chroma: float) -> RGB:
    """OKLCH to sRGB, lowering chroma until it fits sRGB so the lightness never shifts."""
    while True:
        lin = _linear(light, chroma * math.cos(math.radians(hue)), chroma * math.sin(math.radians(hue)))
        if chroma <= 0 or all(-1e-4 <= v <= 1 + 1e-4 for v in lin):
            return _rgb(lin)
        chroma -= 0.005


def _mix(c0: RGB, c1: RGB, t: float) -> RGB:
    a, b = _lab(c0), _lab(c1)
    return _rgb(_linear(*(x + (y - x) * t for x, y in zip(a, b))))


def ramp(value: float, warn: float, crit: float, calm: float | None = None) -> RGB:
    """Green up to calm (default half of warn), yellow at warn, orange midway, red at crit; blended in OKLab."""
    stops = [(warn / 2 if calm is None else calm, GREEN), (warn, YELLOW), ((warn + crit) / 2, ORANGE), (crit, RED)]
    if value >= crit:
        return RED
    if value <= stops[0][0]:
        return GREEN
    return next(_mix(c0, c1, (value - v0) / ((v1 - v0) or 1))
                for (v0, c0), (v1, c1) in itertools.pairwise(stops) if value <= v1)


def core_colour(k: int, n: int) -> RGB:
    """n hues spread evenly round the wheel from Apple blue, at the Apple palette's average lightness and chroma."""
    return oklch(251 + 360 * k / n, 0.73, 0.19)


SS = renderbudget.SCALE
Font = ImageFont.FreeTypeFont
# (name, used percent, temp C or None, warn %, crit %)
DiskBar = tuple[str, float, int | None, float, float]
# (title, [(sample index, colour, legend)], unit, fixed top of scale or None for auto). Samples are
# (t, cpu, ram, read, write, rx, tx, *per-core cpu).
CHARTS = [
    ("CPU", [(1, BLUE, "")], "%", 100.0),
    ("RAM", [(2, GREEN, "")], "%", 100.0),
    ("Disk I/O", [(3, BLUE, "R"), (4, GREEN, "W")], "MB/s", None),
    ("Net I/O", [(5, BLUE, "DL"), (6, GREEN, "UL")], "Mbps", None),
]
_SPAN = re.compile(r"^\s*(\d+)\s*([mhd])\s*$", re.IGNORECASE)
_UNIT = {"m": 60, "h": 3600, "d": DAY}


def parse_span(text: str) -> int | None:
    """'45m', '6h' or '3d' -> seconds, or None when malformed or outside 5m..7d."""
    m = _SPAN.match(text)
    if not m:
        return None
    s = int(m.group(1)) * _UNIT[m.group(2).lower()]
    return s if MIN_WINDOW <= s <= MAX_WINDOW else None


def span(seconds: int) -> str:
    if seconds > DAY and seconds % DAY == 0:
        return f"{seconds // DAY}d"
    return f"{seconds // 3600}h" if seconds % 3600 == 0 else f"{seconds // 60}m"


def span_choices(typed: str, keep: int) -> list[str]:
    q = typed.strip()
    return [s for s in SPANS if s.startswith(q) and cast(int, parse_span(s)) <= keep] or [
        span(min(parse_span(q) or DEFAULT_WINDOW, keep))]


Row = array  # array('d'): t, cpu, ram, read, write, rx, tx, *per-core cpu


class History:
    """A rolling cache of the last keep seconds of samples, mirrored to SQLite so a restart keeps it."""

    def __init__(self, path: str, keep: int):
        self.keep = keep
        self.samples: deque[Row] = deque()
        self._lock = threading.Lock()
        self._unsaved: list[Row] = []  # rows a failed write left behind, saved with the next one that works
        self._closed = False
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        try:
            self._db, rows = self._open(path)
        except sqlite3.DatabaseError as e:
            log.error("history %s is unreadable (%s); moved it to %s.bad and started a new one", path, e, path)
            for sfx in ("-wal", "-shm"):
                with contextlib.suppress(FileNotFoundError):
                    os.remove(path + sfx)
            os.replace(path, path + ".bad")
            self._db, rows = self._open(path)
        for (blob,) in rows:
            if isinstance(blob, bytes) and len(blob) >= 56 and len(blob) % 8 == 0:
                self.samples.append(array("d", blob))

    def _open(self, path: str) -> tuple[sqlite3.Connection, list]:
        db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        try:
            formats.sqlite(db, "chart history", 1)
            db.execute("PRAGMA journal_mode=WAL")  # a crash mid-write never corrupts the file
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("CREATE TABLE IF NOT EXISTS samples (t REAL PRIMARY KEY, v BLOB NOT NULL)")
            since = time.time() - self.keep
            return db, db.execute("SELECT v FROM samples WHERE t >= ? ORDER BY t", (since,)).fetchall()
        except (sqlite3.DatabaseError, formats.FormatError):
            db.close()
            raise

    def add(self, s: Snapshot, t: float | None = None) -> Row:
        """Append to the cache and return the row; store() then writes it to disk."""
        row = array("d", (t or time.time(), s.cpu_pct, s.ram_pct, s.disk_read_mbs, s.disk_write_mbs,
                          s.net_rx_mbps, s.net_tx_mbps, *s.cpu_cores))
        self.samples.append(row)
        while self.samples[0][0] < row[0] - self.keep:
            self.samples.popleft()
        return row

    def store(self, row: Row) -> None:
        """Blocking: call it from a worker thread so a slow disk never delays sampling."""
        with self._lock:
            if self._closed:
                return
            rows = [r for r in self._unsaved if r[0] >= row[0] - self.keep] + [row]
            try:
                self._db.execute("BEGIN")
                self._db.executemany("INSERT OR REPLACE INTO samples VALUES (?, ?)",
                                     [(r[0], r.tobytes()) for r in rows])
                self._db.execute("DELETE FROM samples WHERE t < ?", (row[0] - self.keep,))
                self._db.execute("COMMIT")
            except sqlite3.Error as e:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                if not self._unsaved:
                    log.error("history write failed, the charts keep the samples in memory only: %s", e)
                self._unsaved = rows
                return
            if self._unsaved:
                log.info("history writes work again; saved %d held samples", len(rows) - 1)
                self._unsaved = []

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._db.close()


def gaps(times: list[float], start: float, end: float, longest: float) -> list[tuple[float, float]]:
    """Spans longer than longest seconds with no sample, including before the first and after the last."""
    return [(a, b) for a, b in itertools.pairwise([start, *times, end]) if b - a > longest]


def num(v: float) -> str:
    """At most four digits: one decimal below 1000, whole numbers above."""
    return f"{v:.1f}" if abs(v) < 999.95 else f"{v:.0f}"


def _measure(value: float, unit: str, *, axis: bool = False) -> str:
    text = f"{value:g}" if axis else num(value)
    if len(text) > 9:
        text = f"{value:.1e}".replace("e+", "e")
    return text + ("" if unit == "%" else " ") + unit


def _nice(v: float) -> float:
    """Smallest 1/2/5 x 10^k at or above v."""
    step = 10.0 ** math.floor(math.log10(v)) if v > 0 else 1.0
    for m in (1, 2, 5):
        if v <= m * step:
            return float(m * step)
    return float(10 * step)


def _columns(samples: Sequence, idx: int, start: float, window: float, width: int) -> list:
    """Peak per pixel column, so short spikes survive a long window."""
    cols: list[float | None] = [None] * width
    for s in samples:
        c = min(width - 1, int((s[0] - start) / window * width))
        prev = cols[c] if c >= 0 else None
        if c >= 0 and idx < len(s) and (prev is None or s[idx] > prev):  # rows from before a CPU change lack cores
            cols[c] = s[idx]
    return cols


def _runs(cols: list[int], cuts: list[tuple[int, int]]) -> list[list[int]]:
    """Split the columns that hold data wherever a gap lies between two of them, so no line crosses it."""
    out: list[list[int]] = []
    for c in cols:
        if out and not any(out[-1][-1] <= a and b <= c for a, b in cuts):
            out[-1].append(c)
        else:
            out.append([c])
    return out


def _smooth(pts: list[tuple[float, float]], steps: int = 8) -> list[tuple[float, float]]:
    """Monotone cubic (Fritsch-Carlson) through the points: curved, but never overshoots a sample."""
    n = len(pts)
    if n < 3:
        return pts
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    h = [xs[i + 1] - xs[i] or 1e-9 for i in range(n - 1)]
    sl = [(ys[i + 1] - ys[i]) / h[i] for i in range(n - 1)]
    m = [sl[0]] + [0.0 if sl[i - 1] * sl[i] <= 0 else (sl[i - 1] + sl[i]) / 2 for i in range(1, n - 1)] + [sl[-1]]
    for i in range(n - 1):
        if sl[i] == 0:
            m[i] = m[i + 1] = 0.0
        else:
            a, b = m[i] / sl[i], m[i + 1] / sl[i]
            if a * a + b * b > 9:
                k = 3 / (a * a + b * b) ** 0.5
                m[i], m[i + 1] = k * a * sl[i], k * b * sl[i]
    out = []
    for i in range(n - 1):
        for j in range(steps):
            t = j / steps
            t2, t3 = t * t, t * t * t
            out.append((xs[i] + t * h[i], (2 * t3 - 3 * t2 + 1) * ys[i] + (t3 - 2 * t2 + t) * h[i] * m[i]
                        + (-2 * t3 + 3 * t2) * ys[i + 1] + (t3 - t2) * h[i] * m[i + 1]))
    out.append(pts[-1])
    return out


def _fonts() -> tuple[Font, Font, Font]:
    """Noto Sans: Discord's own fallback for its proprietary gg sans."""
    reg, bold = os.path.join(FONT_DIR, "NotoSans-Regular.ttf"), os.path.join(FONT_DIR, "NotoSans-SemiBold.ttf")
    return ImageFont.truetype(bold, 26), ImageFont.truetype(bold, 22), ImageFont.truetype(reg, 20)


def text_width(font: Font, text: str) -> float:
    return max(font.getlength(text), font.getbbox(text, anchor="lm")[2])


def plot(d: ImageDraw.ImageDraw, box: tuple[float, float, float, float], samples: Sequence, idx: int,
         start: float, end: float, top: float | None, colour: RGB,
         holes: Sequence[tuple[float, float]] = (), stroke: int = 2, *, floor: bool = False) -> None:
    x0, y0, x1, y1 = box
    width = max(1, round(x1 - x0))
    layer = Image.new("L", (width, max(1, round(y1 - y0) + 1))) if floor else None
    target = ImageDraw.Draw(layer) if layer is not None else d
    left, bottom = (0.0, y1 - y0) if floor else (x0, y1)
    cuts = [(min(width - 1, int((a - start) / (end - start) * width)),
             min(width - 1, int((b - start) / (end - start) * width))) for a, b in holes]
    cols = _columns(samples, idx, start, end - start, width)
    lo, top = limits([(s[0], s[idx]) for s in samples], start, end) if top is None else (0.0, top)
    span = top - lo
    for run in _runs([c for c, value in enumerate(cols) if value is not None], cuts):
        pts = [(left + c, bottom - max(0.0, min(1.0, (cols[c] - lo) / span if span else 0.0)) * (y1 - y0)) for c in run]
        ink = 255 if layer is not None else colour
        if len(pts) == 1:
            x, y = pts[0]
            radius = stroke / 2
            target.ellipse((x - radius, y - radius, x + radius, y + radius), fill=ink)
        else:
            target.line(_smooth(pts), fill=ink, width=stroke, joint="curve")
    if layer is not None:
        d.bitmap((int(x0), int(y0)), layer, fill=colour)


def _legend(samples: list, spec: tuple) -> list[tuple[RGB | None, str, str]]:
    series, unit = spec[1:3]
    last = samples[-1] if samples else None
    return [(colour, name, f"{name} {_measure(last[i], unit)}".strip() if last else name)
            for i, colour, name in (spec[4] if len(spec) > 4 else series)]


def chart_width(samples: list, spec: tuple, fonts: tuple[Font, Font, Font]) -> int:
    _, head, small = fonts
    cap = -int(small.getbbox("D", anchor="ls")[1])
    legend = sum(text_width(small, label) + 8 + (cap + 12 if name and colour else 0)
                 for colour, name, label in _legend(samples, spec))
    return math.ceil(44 + text_width(head, spec[0]) + 12 + legend)


def disk_width(disks: list[DiskBar], font: Font) -> int:
    cell = max((max(text_width(font, name), text_width(font, f"{pct:.0f}%")) + 10
                for name, pct, _, _, _ in disks), default=0)
    return math.ceil(44 + len(disks) * cell)


def _chart(d: ImageDraw.ImageDraw, ld: ImageDraw.ImageDraw, fonts: tuple[Font, Font, Font],
           box: tuple[int, int, int, int], samples: list, now: float, window: int,
           spec: tuple, holes: list[tuple[float, float]]) -> None:
    """Frame and text go on d; the data lines go on ld, the SS-times supersampled line layer."""
    title, series, unit, fixed = spec[:4]
    _, head, small = fonts
    x0, y0, x1, y1 = box
    d.rounded_rectangle((x0 + 6, y0 + 6, x1 - 6, y1 - 6), 12, fill=PANEL)
    px0, py0, px1, py1 = x0 + 22, y0 + 50 + small.size + 6, x1 - 22, y1 - 22
    d.text((px0, y0 + 16), title, font=head, fill=DIM)
    base = y0 + 18 + small.getmetrics()[0]
    cap = -int(small.getbbox("D", anchor="ls")[1])  # swatches are exactly as tall as a capital, on its baseline
    labels = _legend(samples, spec)
    x: float = px1
    for colour, name, label in reversed(labels):
        x -= d.textlength(label, small) + 8
        d.text((x, base), label, font=small, fill=TEXT, anchor="ls")
        if name and colour:
            d.rounded_rectangle((x - 6 - cap, base - cap, x - 6, base), 3, fill=colour)
            x -= cap + 12
    top = fixed or _nice(max((s[i] for s in samples for i, _, _ in series), default=0) or 1)
    width = px1 - px0

    def at(t: float) -> float:
        return px0 + max(0.0, min(1.0, (t - now + window) / window)) * width
    for a, b in holes:
        d.rectangle((at(a), py0, at(b), py1), fill=SHADE)
    for q in range(5):
        y = py1 - (py1 - py0) * q // 4
        d.line((px0, y, px1, y), fill=GRID, width=2)
    d.text((px0 + 4, py0 - 6), _measure(top, unit, axis=True), font=small, fill=DIM, anchor="ld")
    for i, colour, _ in series:
        plot(ld, (px0 * SS, py0 * SS, px1 * SS, py1 * SS), samples, i, now - window, now,
             top, colour, holes, SS if len(series) > 2 else 3 * SS // 2)


def _disks(d: ImageDraw.ImageDraw, fonts: tuple[Font, Font, Font], box: tuple[int, int, int, int],
           disks: list[DiskBar]) -> None:
    _, head, small = fonts
    x0, y0, x1, y1 = box
    d.rounded_rectangle((x0 + 6, y0 + 6, x1 - 6, y1 - 6), 12, fill=PANEL)
    d.text((x0 + 22, y0 + 16), "Disk usage", font=head, fill=DIM)
    top, bot = y0 + 56, y1 - 66
    step = (x1 - x0 - 44) / len(disks)
    for n, (name, pct, temp, warn, crit) in enumerate(disks):
        bx0, bx1 = x0 + 22 + n * step + 5, x0 + 22 + (n + 1) * step - 5
        d.rounded_rectangle((bx0, top, bx1, bot), 6, fill=GRID)
        fill_top = bot - (bot - top) * max(0.0, min(pct, 100.0)) / 100
        if bot - fill_top >= 4:
            d.rounded_rectangle((bx0, fill_top, bx1, bot), 6, fill=ramp(pct, warn, crit))
        cx = (bx0 + bx1) / 2
        d.text((cx, bot + 8), name, font=small, fill=TEXT, anchor="ma")
        d.text((cx, bot + 34), f"{pct:.0f}%", font=small, fill=DIM, anchor="ma")


def disk_bars(disks: list[DiskBar], now: float | None = None, tz: tzinfo | None = None) -> bytes | None:
    """The "Disk usage" bars as their own panel, stacked under the System summary; None without disks."""
    return _bars(disks, now, tz) if disks else None


@renderbudget.serialized
def _bars(disks: list[DiskBar], now: float | None, tz: tzinfo | None, *, min_width: int = 0) -> bytes:
    fonts = _fonts()
    width = max(min_width, W, disk_width(disks, fonts[2]))
    if not renderbudget.check("disk_usage", 4 * width * DISK_H + 131072):
        from .view import placeholder

        return placeholder("disk_usage", "Disk usage", width, now, tz)
    im = Image.new("RGB", (width, DISK_H), BG)
    _disks(ImageDraw.Draw(im), fonts, (0, 0, width, DISK_H), disks)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    from .view import Rendered

    return Rendered(buf.getvalue(), lambda width: _bars(disks, now, tz, min_width=width), key="disk_usage",
                    section_title="Disk usage", now=now, tz=tz)


@renderbudget.serialized
def render(samples: list, window: int, now: float | None = None, tz: tzinfo | None = None, gap: float = 30.0, *,
           min_width: int = 0) -> bytes:
    """Timestamp header and 2x2 line charts. Spans with no sample for over gap seconds are shaded."""
    now = now or time.time()
    recent = [s for s in samples if now - window <= s[0] <= now]
    holes = gaps([s[0] for s in recent], now - window, now, gap)
    cores = len(recent[-1]) - 7 if recent else 0
    fonts = _fonts()
    specs: list[tuple] = list(CHARTS)
    if cores > 1:
        specs[0] = ("CPU", [(7 + k, core_colour(k, cores), "") for k in range(cores)], "%", 100.0,
                    [(1, None, "avg")])
    stamp = datetime.fromtimestamp(now, tz).strftime("%a %b %d, %I:%M:%S %p %Z").replace(" 0", " ")
    title, label = f"History ({span(window)})", f"Updated {stamp}"
    width = max(min_width, W, 2 * max(chart_width(recent, spec, fonts) for spec in specs),
                math.ceil(70 + text_width(fonts[1], title) + text_width(fonts[2], label)))
    key_w = max(KEY_W, math.ceil(24 + max((text_width(fonts[2], str(k + 1)) for k in range(cores)), default=0)))
    per_row = (width - 44) // key_w
    key_h = KEY_H * -(-cores // per_row) + 8 if cores > 1 else 0
    charts_h = HEAD_H + 2 * CHART_H + key_h
    height = charts_h
    if not renderbudget.check("history", 4 * (SS * SS + 4) * width * height + 131072):
        from .view import placeholder

        return placeholder("history", title, width, now, tz)
    im = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(im)
    lines = Image.new("RGBA", (width * SS, charts_h * SS), (0, 0, 0, 0))
    ld = ImageDraw.Draw(lines)
    d.text((22, 14), title, font=fonts[1], fill=DIM)
    d.text((width - 24, 18), label, font=fonts[2],
           fill=DIM, anchor="ra")
    cw = width // 2
    if cores > 1:
        ky = HEAD_H + CHART_H
        d.rounded_rectangle((6, ky, width - 6, ky + key_h - 6), 12, fill=PANEL)
        for k in range(cores):
            x, y = 22 + k % per_row * key_w, ky + 8 + k // per_row * KEY_H
            d.rounded_rectangle((x, y, x + 14, y + 14), 3, fill=core_colour(k, cores))
            d.text((x + 20, y + 7), str(k + 1), font=fonts[2], fill=TEXT, anchor="lm")
    for n, spec in enumerate(specs):
        x, y = n % 2 * cw, HEAD_H + n // 2 * (CHART_H + key_h)
        _chart(d, ld, fonts, (x, y, width if n % 2 else x + cw, y + CHART_H), recent, now, window, spec, holes)
    small = lines.reduce(SS)
    im.paste(small, (0, 0), small)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    from .view import Rendered

    return Rendered(buf.getvalue(), lambda width: render(samples, window, now, tz, gap, min_width=width),
                    key="history", section_title=title, now=now, tz=tz)


def section(samples: list, window: int, now: float | None = None) -> Section:
    """Current values and time-bucketed graphs over the window."""
    from .panel import grid
    cols = 28
    from .spark import TREND_WIDTH, bucket_layout, spark

    def number(value: float, base: int, sep: str = "") -> str:
        text = flow(value, base, "B/s" if base == 1024 else "bps", sep) if base else f"{value:.0f}%"
        if len(text) <= 9:
            return text
        unit = "MB/s" if base == 1024 else "Mbps"
        return f"{value:.0e}{sep}{unit}".replace("e+", "e") if base else f"{value:.0e}%".replace("e+", "e")

    now = now or time.time()
    got = [s for s in samples if now - window <= s[0] <= now]
    card_bucket, card_width = bucket_layout(window, 8)
    graph_bucket, graph_width = bucket_layout(window, cols)
    tables = []
    cards: dict[int, tuple[int, tuple[str, ...]]] = {}
    graphs = []
    for heading, base, metrics in (("Load (%)", 0, ((1, "CPU"), (2, "RAM"))),
                                   ("Disk (B/s)", 1024, ((3, "Read"), (4, "Write"))),
                                   ("Net (bit/s)", 1000, ((5, "Down"), (6, "Up")))):
        rows: list[tuple[int, tuple[str, ...]]] = [(FINE, ("", "Now"))]
        series = []
        for i, name in metrics:
            vals = [(s[0], s[i]) for s in got]
            current = number(vals[-1][1], base) if vals else "-"
            rows.append((FINE, (name, current)))
            trend = spark(vals, TREND_WIDTH if base == 0 else card_width, start=now - window, end=now,
                          bucket=None if base == 0 else card_bucket)
            label = name if i <= 2 else f"{heading.split()[0]} {name.lower()}"
            cards[i] = (FINE, (label, f"**{number(vals[-1][1], base, ' ')}**  {trend}"
                               if vals else "-"))
            series.append(vals)
            if i <= 2:
                graph = spark(vals, graph_width, start=now - window, end=now, bucket=graph_bucket)
                graphs.append(Part([(FINE, (graph,))], f"{name} {span(window)}", cut=None,
                                   only="text"))
        width = cols - 6 - sum(max(len(cells[c]) for _, cells in rows) for c in range(2))
        table_bucket, table_width = bucket_layout(window, width)
        table_width = TREND_WIDTH if base == 0 else table_width
        plotted: list[tuple[int, tuple[str, ...]]] = [(FINE, ("", "Now", "Trend"))]
        for (lv, cells), vals in zip(rows[1:], series):
            trend = spark(vals, table_width, start=now - window, end=now,
                          bucket=None if base == 0 else table_bucket)
            plotted.append((lv, (*cells, trend)))
        room = max(cols, sum(max(len(cells[c]) for _, cells in plotted) for c in range(3)) + 4) if base == 0 else cols
        tables.append(Part([(lv, (line,)) for lv, line in grid(plotted, "lrl", cut=2, width=room)],
                           heading, cut=None, only="text"))
    tiles = Part([cards[i] for i in (1, 3, 5, 2, 4, 6)], grid=True, only="embed")
    return Section("history", f"History, last {span(window)}", [*tables, *graphs, tiles])
