"""New, unique sample data within one demo profile: every metric is re-drawn inside the band its current level sits
in, from the bot's own thresholds, so a "Mostly good" profile stays mostly good. Deterministic per base and step."""
import copy
import json
import math
import random

from . import panel
from .helper import formats


def band(key: str, level: int, top: float) -> tuple[float, float]:
    """A value range at level for threshold key, from the bot's own default thresholds."""
    warn, crit = panel.DEFAULT_THRESHOLDS[key]
    if math.isinf(crit):
        level, crit = min(level, 1), top
    return ((warn * .4, warn * .9), (warn, warn + (crit - warn) * .9), (crit, crit + (top - crit) * .9))[level]


class _Draw:
    def __init__(self, seed: str) -> None:
        self.r = random.Random(seed)

    def level(self, value: float, key: str, top: float) -> float:
        """A new value at value's level; a value at or past top (a worst-case width) stays as it is."""
        if value >= top:
            return value
        lo, hi = band(key, panel.level(value, panel.DEFAULT_THRESHOLDS[key]), top)
        lo, hi = max(lo, 0.0), min(hi, top)
        new = self.r.uniform(lo, hi)
        if abs(new - value) < (hi - lo) * .05:
            new = lo + (new - lo + (hi - lo) / 2) % (hi - lo)
        return new

    def jitter(self, value: float, cap: float = math.inf) -> float:
        return min(cap, value * self.r.uniform(.7, 1.3) + .1)


def vary(snapshot: str, step: int, seed: str = "") -> str:
    """snapshot with every metric re-drawn for step of profile seed; step 0 returns it unchanged."""
    data = formats.load(json.loads(snapshot), "preview snapshot", 1)
    if not step:
        return snapshot
    d = _Draw(f"{seed}:{step}")
    out = copy.deepcopy(data)
    s = out["snap"]
    s["cpu_pct"] = d.level(s["cpu_pct"], "resource", 100)
    s["cpu_cores"] = [max(0.0, min(100.0, s["cpu_pct"] + d.r.uniform(-15, 15))) for _ in s["cpu_cores"]]
    s["iowait_pct"] = d.level(s["iowait_pct"], "iowait", 50)
    total = s["ram_total_mb"]
    s["ram_used_mb"] = d.level(s["ram_used_mb"] / total * 100, "resource", 100) * total / 100
    cores = data["cores"]
    core = d.level(s["load"][0] / cores, "load", 2) * cores
    s["load"] = [core, core * .9, core * .8] if core != s["load"][0] else s["load"]
    s["disk_util_pct"] = d.level(s["disk_util_pct"], "disk_busy", 100)
    s["temp_c"] = d.level(s["temp_c"], "temp", 105)
    s["net_rx_mbps"] = d.jitter(s["net_rx_mbps"], s["net_speed_mbps"])
    s["net_tx_mbps"] = d.jitter(s["net_tx_mbps"], s["net_speed_mbps"])
    s["disk_io"] = {dev: [d.jitter(rd), d.jitter(wr), busy] for dev, (rd, wr, busy) in s["disk_io"].items()}
    s["disk_read_mbs"] = sum(v[0] for v in s["disk_io"].values())
    s["disk_write_mbs"] = sum(v[1] for v in s["disk_io"].values())
    for key in ("top_cpu", "top_ram"):
        s[key] = sorted(([name, round(d.jitter(v), 1)] for name, v in s[key]), key=lambda x: -x[1])
    s["uptime_s"] += step * 60
    if out.get("history"):
        last = out["history"][-1]
        out["history"][-1] = [last[0], s["cpu_pct"], s["ram_used_mb"] / total * 100, s["disk_read_mbs"],
                              s["disk_write_mbs"], s["net_rx_mbps"], s["net_tx_mbps"], *s["cpu_cores"]][:len(last)]
    for disk in (out.get("array") or {}).get("disks", []):
        if disk.get("temp") is not None:
            key, top = ("hdd_temp", 70) if disk["rotational"] else ("ssd_temp", 90)
            disk["temp"] = round(d.level(disk["temp"], key, top))
    return json.dumps(out)
