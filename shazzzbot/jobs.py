"""Host maintenance tasks and container schedules, plus parity and mover state."""
import re
import shlex
from collections.abc import Sequence
from datetime import date, datetime, timedelta, tzinfo
from fnmatch import fnmatch

from . import taskstyle, tasktimes
from .docker import Container
from .panel import FINE, NOTE, WARNING, Part, Section, clip
from .unraid import Array

SHOWN = 20
STALE = 300
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
WEEKDAYS = ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT")
# Host processes that mean a maintenance job is running.
PROCS = {"bunker": "File integrity check", "btrfs": "Btrfs scrub", "btrfs_scrub": "Btrfs scrub", "fstrim": "SSD trim",
         "ssd_trim": "SSD trim", "mover": "Mover", "move": "Mover", "xfs_fsr": "XFS defrag",
         "user-script.sh": "VM backup"}  # host command names
Active = tuple[float | None, str, int]  # (elapsed seconds or None, name, level)
# Names derived from Unraid's own cron comments.
NAMES = {
    "Btrfs balance cache": "Cache balance", "Btrfs scrub cache": "Cache scrub", "TRIM": "SSD trim",
    "Docker monitoring": "Docker update check", "Plugins version check": "Plugin update check",
    "Languages version check": "Language update check", "Unraid OS update check": "OS update check",
    "Array status check": "Array health check", "System monitoring": "System monitor",
    "Appdata.Backup": "Appdata backup", "VM Backup plugin default": "VM backup",
}


def display(name: str, rename: dict[str, str]) -> str:
    if name in rename or name in NAMES:
        return rename.get(name) or NAMES[name]
    if " " in name:
        return name
    pretty = name.removesuffix(".sh").replace("-", " ").replace("_", " ")
    return pretty[:1].upper() + pretty[1:]


def _field(spec: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        rng, _, step = part.partition("/")
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            a, b = map(int, rng.split("-"))
        else:
            a = b = int(rng)
            b = hi if step else b
        out.update(range(a, b + 1, int(step or 1)))
    return out


def next_run(expr: str, after: datetime) -> datetime | None:
    """Next fire time strictly after `after`, in after's zone. Day of month and day of week OR when both are set."""
    mi, hr, dom, mon, dow = expr.split()
    mins, hours = sorted(_field(mi, 0, 59)), sorted(_field(hr, 0, 23))
    days, months = _field(dom, 1, 31), _field(mon, 1, 12)
    wdays = {d % 7 for d in _field(dow, 0, 7)}
    both = dom != "*" and dow != "*"
    start: date = after.date()
    for i in range(400):
        d = start + timedelta(days=i)
        on_dom, on_dow = d.day in days, (d.weekday() + 1) % 7 in wdays
        if d.month not in months or not ((on_dom or on_dow) if both else (on_dom and on_dow)):
            continue
        for h in hours:
            for m in mins:
                t = datetime(d.year, d.month, d.day, h, m, tzinfo=after.tzinfo)
                if t > after:
                    return t
    return None


def dur(seconds: float) -> str:
    m = max(0, int(seconds)) // 60
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d{h:02d}h"


def _cron(fields: list[str]) -> str | None:
    if len(fields) not in (5, 6):
        return None
    bounds = [(0, 59)] * (len(fields) - 5) + [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
    normalized = []
    for i, (spec, (lo, hi)) in enumerate(zip(fields, bounds)):
        names = MONTHS if i == len(fields) - 2 else WEEKDAYS if i == len(fields) - 1 else ()
        for number, name in enumerate(names, 1 if i == len(fields) - 2 else 0):
            spec = spec.upper().replace(name, str(number))
        if not all(re.fullmatch(r"(?:\*|[0-9]{1,2})(?:-[0-9]{1,2})?(?:/[0-9]{1,2})?", part)
                   for part in spec.split(",")):
            return None
        try:
            values = _field(spec, lo, hi)
        except ValueError:
            return None
        if not values or min(values) < lo or max(values) > hi:
            return None
        normalized.append(spec)
    return " ".join(normalized[-5:])


def container_jobs(containers: Sequence[Container]) -> list[dict[str, str]]:
    out = []
    for c in containers:
        if "watchtower" not in c.image.lower() or len(c.command) > 16384:
            continue
        try:
            words = shlex.split(c.command)
        except ValueError:
            continue
        flags = [i for i, word in enumerate(words) if word == "--schedule" or word.startswith("--schedule=")]
        if len(flags) != 1:
            continue
        i = flags[0]
        _, equals, value = words[i].partition("=")
        args = ([value] if equals else []) + words[i + 1:]
        if not args or not args[0]:
            continue
        fields = args[0].split()
        if len(fields) == 1:
            fields = []
            for arg in args:
                if arg.startswith("--") or re.match(r"-[A-Za-z]", arg):
                    break
                fields.append(arg)
            if len(fields) > 6 and (re.fullmatch(r"[0-9*/,-]+", fields[6]) or fields[6].upper() in MONTHS + WEEKDAYS):
                continue
            fields = fields[:6]
        if cron := _cron(fields):
            out.append({"name": c.name, "cron": cron})
    return out


def upcoming(jobs: list[dict], now: datetime) -> list[tuple[datetime, str]]:
    """Soonest run per job. Jobs that fire more than once an hour are noise and are left out."""
    out = []
    for j in jobs:
        try:
            first = next_run(j["cron"], now)
            second = next_run(j["cron"], first) if first else None
        except (KeyError, ValueError):
            continue
        if first and not (second and second - first < timedelta(hours=1)):
            out.append((first, j["name"]))
    return sorted(out)


def _rows(items: list[tuple[float | None, str, int]],
          last: dict[str, float] | None = None) -> list[tuple[int, tuple[str, ...]]]:
    rows = []
    for t, name, lv in items:
        cells: tuple[str, ...] = ("" if t is None else dur(t), clip(name, 30))
        if last is not None:
            cells += (f"(last took {tasktimes.elapsed(last[name])})" if name in last else "",)
        rows.append((lv, cells))
    return rows


def _last_names(last: dict[str, float], rename: dict[str, str]) -> dict[str, float]:
    names: dict[str, list[float]] = {}
    for raw, seconds in last.items():
        names.setdefault(display(raw, rename), []).append(seconds)
    return {name: seconds[0] for name, seconds in names.items() if len(seconds) == 1}


def _shown(name: str, ignore: list[str], rename: dict[str, str]) -> bool:
    return not any(fnmatch(n, pat) for pat in ignore for n in (name, display(name, rename)))


def active(data: dict | None, a: Array | None, procs: frozenset[str], ts: float, ignore: list[str],
           rename: dict[str, str]) -> list[Active]:
    out: list[Active] = []
    if a and a.parity.running:
        p = a.parity
        out.append((ts - p.started, f"Parity - {p.pct:.0f}% {'paused' if p.paused else 'done'}",
                    WARNING if p.paused else FINE))
    names = ["Mover"] * bool(a and a.mover) + [name for proc, name in PROCS.items() if proc in procs]
    out += [(None, name, FINE) for name in dict.fromkeys(names)]
    if data:
        out += [(ts - t, display(n, rename), FINE)
                for n, t in sorted(data.get("running", {}).items(), key=lambda x: x[1]) if _shown(n, ignore, rename)]
    return out


def colours(data: dict | None, a: Array | None, procs: frozenset[str], ts: float, ignore: list[str],
            rename: dict[str, str], last: dict[str, float], settings: taskstyle.Settings) -> dict[str, int]:
    kinds = {display(name, rename): kind for name, kind in (data or {}).get("kinds", {}).items()}
    if "bunker" in procs:
        kinds[PROCS["bunker"]] = "file_integrity"
    previous = _last_names(last, rename)
    running = active(data, a, procs, ts, ignore, rename)
    if a and a.parity.running:
        name = running[0][1]
        kinds[name] = "parity"
        if "Parity check" in last:
            previous[name] = last["Parity check"]
    return {name: WARNING for elapsed, name, _ in running
            if settings.yellow(elapsed, previous.get(name), kinds.get(name, ""))}


def soonest(data: dict | None, now: datetime, ignore: list[str],
            rename: dict[str, str], containers: Sequence[Container] = ()) -> list[tuple[float, str]]:
    """(seconds until, display name) of upcoming jobs, soonest first."""
    jobs = [j for j in [*(data or {}).get("jobs", []), *container_jobs(containers)]
            if _shown(j.get("name", ""), ignore, rename)]
    return [((t - now).total_seconds(), display(n, rename)) for t, n in upcoming(jobs, now)]


def tasks_section(data: dict | None, a: Array | None, tz: tzinfo, ignore: list[str],
                  rename: dict[str, str], procs: frozenset[str] = frozenset(), shown: int = SHOWN,
                  containers: Sequence[Container] = (), last: dict[str, float] | None = None,
                  required: bool = True, style: taskstyle.Settings | None = None) -> Section:
    now = datetime.now(tz)
    ts = now.timestamp()
    if data is not None and ts - data.get("generated", 0) > STALE:
        data = None
    known = last or {}
    durations = _last_names(known, rename)
    if a and a.parity.running and "Parity check" in known:
        durations[f"Parity - {a.parity.pct:.0f}% {'paused' if a.parity.paused else 'done'}"] = known["Parity check"]
    running = []
    tones: list[tuple[int, ...]] = []
    yellow = colours(data, a, procs, ts, ignore, rename, known, style or taskstyle.Settings())
    for elapsed, name, lv in active(data, a, procs, ts, ignore, rename):
        running.append((lv, (dur(elapsed) if elapsed is not None else "", clip(name, 30))))
        tones.append((yellow.get(name, FINE),) * 2)
        if name in durations:
            running.append((NOTE, ("", "Last took " + tasktimes.elapsed(durations[name]))))
            tones.append(())
    head: list[tuple[int, tuple[str, ...]]] = []
    soon = _rows([(t, n, FINE) for t, n in soonest(data, now, ignore, rename, containers)[:shown]], durations)
    if data is None:
        head.append((WARNING if required else FINE, ("needs the shazzzbot helper",)))
    return Section("tasks", "Tasks", [
        Part(head, cut=None),
        Part(running, "Running (elapsed)", "rl", cut=1, colours=tones) if running
        else Part([(FINE, ("idle",))], "Running (elapsed)", cut=None),
        Part(soon, "Upcoming (starts in)", "rll", cut=1) if soon
        else Part([(FINE, ("none",))], "Upcoming (starts in)", cut=None)],
                   issues=[(lv, " ".join(cells).strip()) for lv, cells in head + running if lv > FINE])
