"""Unraid array, disk and parity state from the read-only emhttp ini files."""
import os
import re
from dataclasses import dataclass, field

_SECTION = re.compile(r'^\["?(.+?)"?\]$')


def parse_ini(text: str) -> dict[str, dict[str, str]]:
    """emhttp writes quoted section names and values, which configparser mangles."""
    out: dict[str, dict[str, str]] = {"": {}}
    cur = out[""]
    for line in text.splitlines():
        line = line.strip()
        m = _SECTION.match(line)
        if m:
            cur = out.setdefault(m.group(1), {})
        elif "=" in line:
            k, _, v = line.partition("=")
            cur[k] = v.strip('"')
    return out


@dataclass
class Disk:
    name: str
    kind: str
    device: str
    status: str
    temp: int | None
    size_kb: int
    free_kb: int | None
    errors: int
    rotational: bool = True

    @property
    def used_pct(self) -> float | None:
        return 100 * (1 - self.free_kb / self.size_kb) if self.free_kb is not None and self.size_kb else None


@dataclass
class Parity:
    action: str
    running: bool
    paused: bool
    pct: float
    speed_mbs: float
    started: int
    last_end: int
    last_exit: int
    last_errors: int


@dataclass
class Array:
    name: str
    version: str
    state: str
    mover: bool
    parity: Parity
    disks: list[Disk] = field(default_factory=list)

    @property
    def usage(self) -> tuple[float, float] | None:
        """(used KB, total KB) across data disks; parity, pools and flash are left out."""
        data = [d for d in self.disks if d.kind == "Data" and d.free_kb is not None]
        total = sum(d.size_kb for d in data)
        return (total - sum(d.free_kb or 0 for d in data), total) if total else None


def _int(v: str | None) -> int | None:
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


# Path comes from the operator's config, not from a Discord user or the network. reviewed 2026-11
# bearer:disable python_lang_path_traversal
def read(emhttp: str) -> Array | None:
    try:
        with open(os.path.join(emhttp, "var.ini")) as f:
            var = parse_ini(f.read())[""]
        with open(os.path.join(emhttp, "disks.ini")) as f:
            disks = parse_ini(f.read())
    except OSError:
        return None
    return build(var, disks)


def build(var: dict[str, str], disks: dict[str, dict[str, str]]) -> Array:
    pos, size = _int(var.get("mdResyncPos")) or 0, _int(var.get("mdResyncSize")) or 0
    dt, db = _int(var.get("mdResyncDt")) or 0, _int(var.get("mdResyncDb")) or 0
    parity = Parity(
        action=var.get("mdResyncAction", ""),
        running=pos > 0,
        paused=pos > 0 and (_int(var.get("mdResync")) or 0) == 0,
        pct=100.0 * pos / size if size else 0.0,
        speed_mbs=db / dt / 1024 if dt else 0.0,
        started=_int(var.get("sbSynced")) or 0,
        last_end=_int(var.get("sbSynced2")) or 0,
        last_exit=_int(var.get("sbSyncExit")) or 0,
        last_errors=_int(var.get("sbSyncErrs")) or 0,
    )
    out = Array(
        name=var.get("NAME", "unraid"),
        version=var.get("version", "?"),
        state=var.get("mdState", "?"),
        mover=var.get("shareMoverActive") == "yes",
        parity=parity,
    )
    for name, d in disks.items():
        status = d.get("status", "")
        if not name or status.startswith("DISK_NP"):
            continue
        out.disks.append(Disk(
            name=name,
            kind=d.get("type", ""),
            device=d.get("device", ""),
            status=status,
            temp=_int(d.get("temp")),
            size_kb=_int(d.get("fsSize")) or _int(d.get("size")) or 0,
            free_kb=_int(d.get("fsFree")),
            errors=_int(d.get("numErrors")) or 0,
            rotational=d.get("rotational") != "0",
        ))
    return out
