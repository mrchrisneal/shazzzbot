"""Gather external panel inputs before committing a coherent render snapshot."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import Counter
from dataclasses import dataclass
from datetime import tzinfo
from typing import TYPE_CHECKING

import aiohttp

from . import SOURCE_URL, charts, config, forges, jellyfin, jobs, panel, services, sysimage, tasktimes, unraid, view
from .host import Snapshot

if TYPE_CHECKING:
    from .bot import Bot

log = logging.getLogger("shazzzbot")


@dataclass(frozen=True)
class RenderSnapshot:
    sections: tuple[panel.Section, ...]
    system: tuple[sysimage.Row, ...]
    status: tuple[int, str] | None
    samples: tuple[memoryview, ...]
    disks: tuple[charts.DiskBar, ...]
    window: int
    tz: tzinfo
    gap: float
    update_available: bool


@dataclass
class _Gathered:
    snap: Snapshot
    arr: unraid.Array | None
    task_data: dict | None
    ups_shutdown: dict
    smart_list: list[str]
    issues: list[panel.Row]
    disk_alerts: dict[str, panel.Row]
    streams: Counter[str] | None


async def _gather(bot: Bot) -> _Gathered:
    snap = bot._snap or Snapshot()
    arr = await asyncio.to_thread(unraid.read, bot.cfg.emhttp)
    async with bot._lock:
        bot.array = arr
    task_data = await bot._read_tasks()
    ups_shutdown = await bot._read_ups_shutdown()
    smart_list = list((task_data or {}).get("smart", []))
    smart = frozenset(smart_list)
    issues = await bot._issues(snap, arr, smart)
    disk_alerts = panel.map_disk_alerts(arr, issues) if arr else {}
    issues = [disk_alerts.get(text, (lv, text)) for lv, text in issues]
    streams = None
    if bot.cfg.jellyfin_url and bot.http_session:
        try:
            streams = await jellyfin.streams(bot.http_session, bot.cfg.jellyfin_url, bot.cfg.jellyfin_key)
        except (TimeoutError, aiohttp.ClientError, ValueError) as e:
            log.warning("jellyfin sessions failed: %s", e)
    await bot._check_release(SOURCE_URL.removeprefix("https://github.com/"))
    return _Gathered(snap, arr, task_data, ups_shutdown, smart_list, issues, disk_alerts, streams)


def _compose(bot: Bot, data: _Gathered, at: float | None) -> list[panel.Section]:
    from . import bot as runtime

    snap, arr, task_data = data.snap, data.arr, data.task_data
    ups_shutdown, smart_list = data.ups_shutdown, data.smart_list
    issues, disk_alerts, streams = data.issues, data.disk_alerts, data.streams
    th = bot.cfg.thresholds
    now = runtime.datetime.now(bot.tz)
    previous_times = bot._task_times.copy()
    if tasktimes.merge(bot._task_times, (task_data or {}).get("durations"), now.timestamp()):
        try:
            bot._save_state()
        except OSError:
            bot._task_times = previous_times
            raise
    ignore, rename = bot.cfg.tasks_ignore, bot.cfg.tasks_rename
    last = {n: r["seconds"] for n, r in bot._task_times.items() if r["seconds"] is not None}
    bot._system = sysimage.rows(snap, th, f"Unraid {arr.version}" if arr else "",
                                 (task_data or {}).get("os_update", ""), arr.usage if arr else None,
                                 bot._loads[0] if bot._loads else snap.load, hostname=arr.name if arr else "",
                                 ups=bot._ups, ups_shutdown=ups_shutdown)
    for monitor in bot.forges:
        if monitor.settings.system_row:
            lv, text = monitor.summary()
            bot._system.append(sysimage.Row(monitor.section().title, text, dot=lv, level=lv))
    rest = [panel.apps_section(snap, bot.app_history, bot.window)]
    if arr:
        disks = panel.array_section(arr, smart_list, snap.disk_io, th)
        rest.append(disks._replace(issues=list(dict.fromkeys([*disks.issues, *disk_alerts.values()]))))
    if bot.docker:
        rest.append(panel.docker_section(bot.containers, bot.engine)._replace(note=bot._inventory.note()))
    if bot.svc_results or bot.exposed is not None:
        rest.extend(services.sections(bot.cfg.services, bot.svc_results, bot.exposed,
                                      tuple(bot.cfg.service_columns)))
    rest.append(jobs.tasks_section(task_data, arr, bot.tz, ignore, rename, snap.procs,
                                  bot.cfg.tasks_shown, containers=bot.containers,
                                  last=last, required=bot.cfg.helper is not None, style=bot.cfg.tasks))
    rest.extend(monitor.section() for monitor in bot.forges)
    feed = {t: lv for lv, t in issues}
    checks = [*(i for sec in rest for i in sec.issues), *(i for row in bot._system for i in row.issues)]
    for lv, t in (i for i in checks if i[0] > panel.FINE):
        feed[t] = max(lv, feed.get(t, lv))
    issues = list(dict.fromkeys(panel.group_usage(sorted(((lv, t) for t, lv in feed.items()),
                                                         key=lambda r: -r[0]), th)))
    bot._system += sysimage.status_rows(
        issues, jobs.active(task_data, arr, snap.procs, now.timestamp(), ignore, rename),
        jobs.soonest(task_data, now, ignore, rename, bot.containers), streams, bool(bot.cfg.jellyfin_url),
        jobs.colours(task_data, arr, snap.procs, now.timestamp(), ignore, rename, last, bot.cfg.tasks))
    bot_ms = bot.latency * 1000 if math.isfinite(bot.latency) else None
    nodes = bot.node_inventory()
    outdated = bot.latest_release is not None and any(
        n.update_needed(bot.latest_release) is True for n in nodes)
    bot._status = None if bot.nodes is None and bot.helper_node is None else sysimage.node_status(
        [(panel.FINE, bot_ms), *((lv, ms) for lv, ms, _ in bot.nodes or []),
         *([(bot.helper_node.level, bot.helper_node.ms)] if bot.helper_node else [])], outdated)
    upcoming = view.clock(bot._boundary(time.time() if at is None else at, bot.main.edit_every), bot.tz)
    sections = [sysimage.section(bot.cfg.title, bot.cfg.subtitle, upcoming, bot._system,
                                 bot._status),
                charts.section(list(bot.history.samples), bot.window), *rest]
    rank = {k: i for i, k in enumerate(dict.fromkeys([*bot.cfg.order, *config.SECTIONS]))}
    sections.sort(key=lambda s: rank.get(forges.group(s.key), len(rank)))
    bot._sections = sections
    bot._snapshot = RenderSnapshot(
        tuple(sections), tuple(bot._system), bot._status,
        tuple(memoryview(row).toreadonly() for row in bot.history.samples),
        tuple((panel.drive(disk.name), disk.used_pct, disk.temp, *th["disk_full"])
              for disk in (arr.disks if arr else []) if disk.used_pct is not None),
        bot.window, bot.tz, 3 * bot.cfg.interval, bot.update_available)
    return sections


async def collect(bot: Bot, at: float | None = None) -> list[panel.Section]:
    async with bot._collect_lock:
        data = await _gather(bot)
        async with bot._lock:
            return _compose(bot, data, at)
