"""Build docs/demo/frames.json and its PNGs from invented sample data through the bot's own collect and render code.

Run from the repository root: python -m tools.demo_build [--out DIR]. A fixed seed and clock make reruns
byte-identical. The "helper" set answers the bot's helper requests with sample data; the "nohelper" set configures no
helper, so it renders what the bot really shows when the helper is not installed.
"""
import argparse
import asyncio
import hashlib
import json
import math
import os
import random
import shutil
import tempfile
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple
from unittest import mock

from shazzzbot import __version__, helperclient, panel, previewdata, schemaview, services
from shazzzbot import bot as B
from shazzzbot.config import Config, Output
from shazzzbot.docker import Container, Engine
from shazzzbot.host import Snapshot
from shazzzbot.previewvary import band
from shazzzbot.services import Service
from shazzzbot.ups import Reading

ROOT = Path(__file__).resolve().parent.parent
SEED = 5737
START = datetime(2026, 11, 9, 20, 0, tzinfo=UTC).timestamp()
STEP, INTERVAL, WINDOW = 60, 10, 1800
INTERVAL_S = 6
CORES = 8
SETS = ("helper", "nohelper")
MODES = previewdata.MODES
RENDER = {"system": "image", "history": "image", "disks": "image", "services": "image", "docker": "image",
          "apps": "image", "tasks": "embed", "forges": "image"}
KEY_ENV = "SHAZZZBOT_DEMO_HELPER_KEY"
DAY = 86400
GB = 1024 * 1024
SERVICES = [Service("Jellyfin", container="jellyfin", url="http://192.0.2.10:8096/health"),
            Service("Nextcloud", container="nextcloud", url="http://192.0.2.10:8443",
                    public_url="https://cloud.example.com"),
            Service("Home Assistant", container="home-assistant", url="http://192.0.2.10:8123",
                    vpn_url="http://192.0.2.20:8123"),
            Service("Website", public_url="https://www.example.com")]
APPS = ["jellyfin", "nextcloud", "home-assistant", "paperless-ngx", "VM: Windows 11", "mariadb", "kavita", "dockerd"]
LONG_APP = "photo-library-machine-learning-worker-with-a-long-name"


class Spec(NamedTuple):
    name: str
    mix: tuple[int, int] | None  # tenths of lines OK and warning, the rest critical; None rotates every level
    parity: str = "idle"  # idle, running, paused, or errors
    ups: int | None = None  # index into UPS; None follows the levels
    severity: int | None = None  # the highest issue level the frame must show


SPECS = (Spec("All good", (10, 0), ups=0, severity=panel.FINE),
         Spec("Mostly good", (7, 3), ups=1, severity=panel.WARNING),
         Spec("Mixed bag", (4, 3), "running", severity=panel.CRITICAL),
         Spec("Mostly bad", (1, 2), "paused", 2, severity=panel.CRITICAL),
         Spec("All bad", (0, 0), "errors", 2, severity=panel.CRITICAL),
         Spec("Max width", (0, 0), "running", severity=panel.CRITICAL),
         Spec("Parity running", None, "running", 0),
         Spec("Parity paused", None, "paused", 1),
         Spec("Parity w/ errors", None, "errors", 0),
         Spec("Parity good", (10, 0), ups=0, severity=panel.FINE),
         Spec("On battery", None, ups=2))
NAMES = [s.name for s in SPECS]
FRAMES, WORST = len(SPECS), NAMES.index("Max width")
UPS = (Reading(("OL",), 100.0, 5400.0, 18.0), Reading(("OL", "CHRG"), 78.0, 3900.0, 22.0),
       Reading(("OB", "LB"), 9.0, 240.0, 41.0))


def frame_time(n: int) -> float:
    return START + n * STEP


def frame_at(t: float) -> int:
    return math.floor((t - START) / STEP + 0.5)


def lv(n: int, line: int) -> int:
    """OK, warning, or critical for one line: the frame's mix spread across lines, or a rotation through all three."""
    n = min(max(n, 0), FRAMES - 1)
    mix = SPECS[n].mix
    if mix is None:
        return (n + line) % 3
    slot = (line * 7 + n * 3) % 10
    return 0 if slot < mix[0] else 1 if slot < sum(mix) else 2


def pick(r: random.Random, n: int, line: int, key: str, top: float) -> float:
    return r.uniform(*band(key, lv(n, line), top))


def snapshot(t: float) -> Snapshot:
    r = random.Random(SEED * 100003 + int(t))
    n = frame_at(t)
    bad = n == WORST
    cpu = 100.0 if bad else pick(r, n, 0, "resource", 100)
    ram = 32768.0 if bad else 32768 * pick(r, n, 1, "resource", 100) / 100
    core = pick(r, n, 3, "load", 2) * CORES
    busy = 100.0 if bad else pick(r, n, 4, "disk_busy", 100)
    rx = 1000.0 if bad else 10 * pick(r, n, 5, "resource", 100)
    apps = APPS + [LONG_APP] * bad
    weights = {name: r.uniform(0.2, 1.0) * (8 - i) for i, name in enumerate(apps)}
    if bad:
        weights[LONG_APP] = 15.0
    total = sum(weights.values())
    top_cpu = sorted(((a, round(cpu * CORES * w / total, 1)) for a, w in weights.items()), key=lambda x: -x[1])
    top_ram = sorted(((a, round(600 + 3200 * w / total * len(apps), 0)) for a, w in weights.items()),
                     key=lambda x: -x[1])
    io = {dev: (r.uniform(0, 40), r.uniform(0, 25), r.uniform(1, min(30, busy)))
          for dev in ("sdb", "sdc", "sde", "sdf")}
    io["nvme0n1"] = (r.uniform(10, 90), r.uniform(5, 60), r.uniform(2, min(20, busy)))
    io["sdd"] = (9999.9, 9999.9, busy) if bad else (r.uniform(20, 180), r.uniform(1, 20), busy)
    return Snapshot(
        uptime_s=999 * DAY + DAY - 60 if bad else int(12 * DAY + 4 * 3600 + t - START),
        cpu_pct=cpu, cpu_cores=[max(0.0, min(100.0, cpu + r.uniform(-15, 15))) for _ in range(CORES)],
        iowait_pct=99.0 if bad else pick(r, n, 2, "iowait", 50), ram_used_mb=ram,
        ram_total_mb=32768.0, load=(128.0, 96.5, 64.25) if bad else (core, core * .9, core * .8),
        disk_read_mbs=sum(v[0] for v in io.values()), disk_write_mbs=sum(v[1] for v in io.values()),
        disk_util_pct=busy, net_rx_mbps=rx, net_tx_mbps=1000.0 if bad else r.uniform(5, 30), net_speed_mbps=1000.0,
        temp_c=105.0 if bad else pick(r, n, 6, "temp", 105),
        top_cpu=top_cpu[:5], top_ram=top_ram[:5], disk_io=io,
        procs=frozenset({"bunker"} if bad else {"fstrim"} if lv(n, 4) == 1 else ()))


def ini(sections: dict[str, dict[str, object]]) -> str:
    return "".join((f'["{name}"]\n' if name else "") + "".join(f'{k}="{v}"\n' for k, v in values.items())
                   for name, values in sections.items())


def free(r: random.Random, n: int, line: int) -> float:
    return 1 - pick(r, n, line, "disk_full", 100) / 100


def emhttp(path: Path, n: int) -> None:
    t, bad, r = frame_time(n), n == WORST, random.Random(SEED + 7 * n)
    size = 7814026532
    parity = 1 if bad else ("idle", "running", "paused", "errors").index(SPECS[n].parity) % 3
    errors = SPECS[n].parity == "errors"
    var: dict[str, object] = {
           "NAME": "tower", "version": "7.2.0", "mdState": "STARTED",
           "shareMoverActive": "yes" if bad or lv(n, 1) == 1 else "no",
           "mdResyncPos": int((0.37 + 0.07 * n) % 1 * size) if bad or parity else 0, "mdResyncSize": size,
           "mdResyncDt": 1, "mdResyncDb": 180000, "mdResync": 0 if parity == 2 else 1, "mdResyncAction": "check P",
           "sbSynced": int(t - 3 * 3600), "sbSynced2": int(START - 4 * DAY),
           "sbSyncExit": -4 if errors else 0, "sbSyncErrs": 3 if errors else 0}
    disks: dict[str, dict[str, object]] = {}
    disks["parity"] = {"type": "Parity", "device": "sdb", "status": "DISK_OK", "size": size, "numErrors": 0,
                       "temp": 70 if bad else round(pick(r, n, 0, "hdd_temp", 65))}
    statuses = ("DISK_OK", "DISK_INVALID", "DISK_DSBL")
    for i, (dev, kb) in enumerate([("sdc", size), ("sdd", size), ("sde", size // 2), ("sdf", size // 2)], 1):
        disks[f"disk{i}"] = {
            "type": "Data", "device": dev, "status": "DISK_OK", "numErrors": 0,
            "temp": 70 if bad else round(pick(r, n, i, "hdd_temp", 65)),
            "fsSize": kb, "fsFree": 0 if bad else int(kb * free(r, n, i + 1))}
    disks["disk2"]["status"] = "DISK_NP_DSBL" if bad else ("DISK_OK", "DISK_OK", statuses[1 + n % 2])[lv(n, 2)]
    disks["disk3"]["numErrors"] = 4294967295 if bad else (0, 3, 12)[lv(n, 3)]
    disks["cache"] = {"type": "Cache", "device": "nvme0n1", "status": "DISK_OK", "numErrors": 0, "rotational": 0,
                      "temp": 85 if bad else round(pick(r, n, 5, "ssd_temp", 85)),
                      "fsSize": 976762552, "fsFree": 0 if bad else int(976762552 * free(r, n, 6))}
    if bad:
        disks["nvme_appdata_mirror_pool_for_containers"] = {
            "type": "Cache", "device": "nvme1n1", "status": "DISK_OK", "temp": 85, "fsSize": 976762552,
            "fsFree": 0, "numErrors": 0, "rotational": 0}
    path.mkdir(parents=True, exist_ok=True)
    (path / "var.ini").write_text(ini({"": var}))
    (path / "disks.ini").write_text(ini(disks))


def smart(n: int) -> list[str]:
    return ["disk1", "disk3", "nvme_appdata_mirror_pool_for_containers"] if n == WORST else \
        ["disk1"] if lv(n, 4) else []


def containers(n: int) -> list[Container]:
    def c(i: int, name: str, state: str, status: str, project: str = "", ports: tuple[int, ...] = (),
          image: str = "", command: str = "") -> Container:
        return Container(f"c{i:02d}", name, state, status, project, (project or "bridge",), ports, image, command)
    kavita = (("running", "Up 12 days"), ("restarting", "Restarting (1) 5 seconds ago"),
              ("exited", "Exited (1) 2 minutes ago"))[lv(n, 1)]
    cloud = ("Up 5 days (healthy)", "Up 30 seconds (health: starting)", "Up 5 days (unhealthy)")[lv(n, 2)]
    home = (("running", "Up 2 days"), ("paused", "Up 2 days (Paused)"),
            ("exited", "Exited (137) 1 minute ago"))[lv(n, 0)]
    speed = (("exited", "Exited (0) 3 hours ago"), ("running", "Up 4 seconds"), ("created", "Created"))[lv(n, 1)]
    out = [c(1, "jellyfin", "running", "Up 12 days (healthy)", "media", (8096,)),
           c(2, "kavita", *kavita, "media", (5000,)), c(3, "photoprism", "running", "Up 12 days", "media", (2342,)),
           c(4, "nextcloud", "running", cloud, "cloud", (8443,)), c(5, "nextcloud-db", "running", "Up 5 days", "cloud"),
           c(6, "redis", "running", "Up 5 days", "cloud"), c(7, "home-assistant", *home, "home"),
           c(8, "mosquitto", "running", "Up 2 days", "home", (1883,)),
           c(9, "watchtower", "running", "Up 12 days", image="containrrr/watchtower",
             command="--schedule '0 0 4 * * *' --cleanup"),
           c(10, "dozzle", "running", "Up 12 days", ports=(8080,)),
           c(11, "speedtest", *speed)]
    if n == WORST:
        stack = "photo-archive-and-document-scanning"
        out += [c(12, "immich-server", "running", "Up 1 hour", stack, (2283,)),
                c(13, "immich-machine-learning-worker", "exited", "Exited (137) 4 minutes ago", stack),
                c(14, "immich-microservices", "exited", "Exited (1) 4 minutes ago", stack)]
    return out


FAILS = ("HTTP 502", "timeout", "refused", "TLS", "DNS", "no route", "bad body")


def latencies(n: int) -> dict[str, float | str]:
    r = random.Random(SEED + n)
    urls = [u for s in SERVICES for u in (s.url, s.vpn_url, s.public_url) if u]
    out: dict[str, float | str] = {}
    for i, u in enumerate(urls):
        level = lv(n, i)
        out[u] = "bad reply" if n == WORST else r.uniform(8, 60) if level == 0 else r.uniform(1600, 2400) \
            if level == 1 else FAILS[(n + i) % len(FAILS)]
    return out


def tasks(n: int, now: float) -> dict:
    jobs_ = [{"name": "Parity check", "cron": "30 6 * * 3"}, {"name": "Appdata.Backup", "cron": "30 6 * * 1"},
             {"name": "Plugins version check", "cron": "10 0 * * *"}, {"name": "nightly-photo-sync.sh",
                                                                       "cron": "0 2 * * *"}]
    running: dict[str, float] = ({}, {"Appdata.Backup": now - 420}, {"nightly-photo-sync.sh": now - 7300})[lv(n, 0)]
    finished: dict[str, float] = {"Plugins version check": now - 300} if lv(n, 1) == 0 else {}
    if n == WORST:
        long = "offsite-copy-of-family-photos-and-scanned-documents.sh"
        jobs_.append({"name": long, "cron": "45 20 * * *"})
        running = {"Appdata.Backup": now - 99 * 3600, "nightly-photo-sync.sh": now - 7300, long: now - 60}
    return {"generated": now - 5, "jobs": jobs_, "smart": smart(n),
            "os_update": "7.2.1" if lv(n, 2) else "", "running": running, "finished": finished,
            "durations": {"Parity check": {"at": START - 4 * DAY, "seconds": 52300},
                          "Appdata.Backup": {"at": START - 6 * DAY, "seconds": 1260}},
            "kinds": {name: "appdata_backup_plugin" for name in running if name == "Appdata.Backup"}}


frozen = previewdata.frozen


class Clock(previewdata.Clock):
    def __init__(self) -> None:
        super().__init__(START)


async def _frames(name: str, tmp: Path, out: Path, snaps: list[dict]) -> tuple[list[dict], list[int]]:
    clock, state = Clock(), {"n": 0}

    async def helper(_settings: helperclient.Settings, action: str) -> dict:
        clock.mono += 0.004
        if action == "hello":
            return {"version": __version__, "sha256": hashlib.sha256(b"demo").hexdigest(),
                    "protocol": helperclient.PROTOCOL, "bundle_version": __version__,
                    "entry_sha256": hashlib.sha256(b"demo entry").hexdigest()}
        if action == "tasks":
            return helperclient.tasks_data(tasks(state["n"], clock.t))
        mode = (("batt_level", 20), ("batt_timer", 300), ("sec_timer", 600))[lv(state["n"], 1)]
        return helperclient.shutdown_data(dict(zip(("mode", "value"), mode, strict=True)))

    async def probe(_http: object, url: str, _expect: str = "") -> tuple[bool, str, float]:
        got = latencies(state["n"])[url]
        return (False, got, 0.0) if isinstance(got, str) else (True, f"{got:.0f}ms", got)

    with ExitStack() as stack:
        stack.enter_context(previewdata.at(clock, CORES))
        stack.enter_context(mock.patch.object(services, "probe", probe))
        stack.enter_context(mock.patch.dict(os.environ, {KEY_ENV: "demo_helper_key_for_sample_frames_only"}))
        if name == "helper":
            stack.enter_context(mock.patch.object(helperclient, "request", helper))
        cfg = Config(token="demo", guild_id=1, outputs=[Output(2)], render=dict(RENDER), interval=INTERVAL,
                     state_file=str(tmp / "state.json"), emhttp=str(tmp / "emhttp"), host_root=str(tmp / "host"),
                     ups="ups@192.0.2.30", services=SERVICES,
                     helper=helperclient.validate({"socket": str(tmp / "helper.sock"), "key_env": KEY_ENV})
                     if name == "helper" else None)
        bot = B.Bot(cfg)
        bot.docker = object()  # type: ignore[assignment]
        bot.engine = Engine("27.5.1", "overlay2", 42)
        t, frames = START - WINDOW, []
        severity: list[int] = []
        try:
            for n in range(FRAMES):
                state["n"], end = n, frame_time(n)
                while t <= end:
                    clock.t = t
                    snap = bot._snap = snapshot(t)
                    bot._loads.append(snap.load)
                    bot.app_history.add(snap, bot.window, bot.history.add(snap, t)[0])
                    t += INTERVAL
                clock.t, clock.mono = end, 1000.0 + end - START
                emhttp(tmp / "emhttp", n)
                bot.containers = containers(n)
                ups = SPECS[n].ups
                bot._ups = UPS[lv(n, 2) if ups is None else ups] if n != WORST else \
                    Reading(("OB", "DISCHRG", "LB", "RB"), 0.0, 0.0, 100.0)
                bot.svc_results = await services.check_all(SERVICES, bot.containers, None)  # type: ignore[arg-type]
                sections = await bot.collect()
                previewdata.steady(bot, sections)
                severity.append(max((panel.worst(list(s.issues)) for s in sections), default=panel.FINE))
                frames.append(await _frame(bot, sections, out / "frames" / name / str(n), f"frames/{name}/{n}"))
                snaps.append(await previewdata.dump(bot))
        finally:
            bot.history.close()
            bot.app_history.close()
    return frames, severity


async def _frame(bot: B.Bot, sections: list, folder: Path, rel: str) -> dict:
    frame = await previewdata.frame(bot, sections)
    folder.mkdir(parents=True, exist_ok=True)
    for item in (item for messages in frame.values() for message in messages for item in message):
        if "png" in item:
            name = item.pop("name")
            (folder / name).write_bytes(item.pop("png"))
            item["file"] = f"{rel}/{name}"
    return frame


def build(out: Path) -> dict:
    data: dict = {"interval_s": INTERVAL_S, "names": NAMES, "sets": {}}
    snaps: dict = {"interval_s": INTERVAL_S, "names": NAMES, "sets": {}}
    with tempfile.TemporaryDirectory() as tmp:
        for name in SETS:
            shutil.rmtree(out / "frames" / name, ignore_errors=True)
            snaps["sets"][name] = []
            frames, severity = asyncio.run(_frames(name, Path(tmp) / name, out, snaps["sets"][name]))
            data["sets"][name] = {"frames": frames, "severity": severity}
    (out / "frames.json").write_text(json.dumps(data, indent=1) + "\n")
    (out / "snapshots.json").write_text(json.dumps(snaps, separators=(",", ":"), allow_nan=False) + "\n")
    (out / "schema.json").write_text(schemaview.dumps())
    return data


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "demo", help="demo folder (default docs/demo)")
    build(parser.parse_args(argv).out)


if __name__ == "__main__":
    main()
