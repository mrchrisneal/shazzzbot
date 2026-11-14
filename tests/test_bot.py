import asyncio
import json
import logging
import runpy
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import aiohttp
import discord
import pytest

from shazzzbot import bot as B
from shazzzbot import (
    charts,
    forges,
    health,
    jellyfin,
    jobs,
    netdata,
    panel,
    prep,
    relay,
    services,
    unraid,
)
from shazzzbot.config import IMAGE_FLOOR, Config, Output
from shazzzbot.docker import Container, Engine
from shazzzbot.glyphs import GREEN
from shazzzbot.host import Snapshot
from shazzzbot.services import Result, Service
from tests.test_forge import DATE, TOKEN, Response
from tests.test_jobs import NOW, Frozen
from tests.test_unraid import DISKS, VAR

GUILD, PANEL, AUDIT = 10, 20, 30
OPER, VIEWER, STRANGER, VIEW_ROLE = 1, 2, 3, 99
PERMS = {"operate": {"users": [OPER]}, "view": {"roles": [VIEW_ROLE]}}
WEB = Container("c1", "web", "running", "Up 1 hour")


def make_bot(tmp_path, *, outputs=None, **kw) -> B.Bot:
    cfg = Config(token="t", guild_id=GUILD, outputs=[Output(PANEL)] if outputs is None else outputs,
                 audit_channel_id=AUDIT, perms=PERMS,
                 state_file=str(tmp_path / "data" / "state.json"), emhttp=str(tmp_path / "emhttp"),
                 host_root=str(tmp_path / "host"), **kw)
    b = B.Bot(cfg)
    b.host.poll = MagicMock(return_value=Snapshot(load=(1.0, 1.0, 1.0)))
    return b


async def sampled(b: B.Bot, n: int = 1) -> None:
    """The first sample only sets the baseline, so n recorded samples take n + 1 polls."""
    for _ in range(n + 1):
        await b.sample()


def with_array(tmp_path) -> None:
    (tmp_path / "emhttp").mkdir()
    (tmp_path / "emhttp" / "var.ini").write_text(VAR)
    (tmp_path / "emhttp" / "disks.ini").write_text(DISKS)


def channel(*old) -> MagicMock:
    """A panel channel whose history holds old, newest first."""
    ch = MagicMock(spec=discord.TextChannel)
    ch.guild = SimpleNamespace(id=GUILD)
    ch.send = AsyncMock(return_value=SimpleNamespace(id=777))
    ch.fetch_message = AsyncMock()

    async def history(limit):
        for m in old:
            yield m
    ch.history = history
    return ch


def wire(b: B.Bot, panel_ch=None, audit_ch=None) -> None:
    chans = {PANEL: panel_ch, AUDIT: audit_ch}
    b.get_channel = lambda cid: chans.get(cid)  # type: ignore[method-assign]


def ctx(user: int, roles=(), guild: int = GUILD) -> MagicMock:
    c = MagicMock()
    c.author = SimpleNamespace(id=user, roles=[SimpleNamespace(id=r) for r in roles])
    c.guild_id = guild
    c.respond, c.defer, c.edit = AsyncMock(), AsyncMock(), AsyncMock()
    return c


def commands(b: B.Bot) -> dict:
    out = {}
    for c in b.pending_application_commands:
        for s in getattr(c, "subcommands", None) or [c]:
            out[s.qualified_name] = s
    return out


def not_found() -> discord.NotFound:
    return discord.NotFound(MagicMock(status=404, reason="Not Found"), "gone")


# --- construction and state ---

def test_state_missing_corrupt_and_valid(tmp_path):
    b = make_bot(tmp_path)
    assert b.main.message_id is None and b.window == charts.DEFAULT_WINDOW
    state = tmp_path / "data" / "state.json"
    state.write_text("{not json")
    assert make_bot(tmp_path).main.message_id is None
    state.write_text(json.dumps({"format": 1, "messages": {"20": 42, "21": 43}, "window": "6h"}))
    b = make_bot(tmp_path)
    assert (b.main.message_id, b.window) == (42, 6 * 3600)
    state.write_text(json.dumps({"format": 1, "messages": {"20": 42}, "window": "99d"}))
    assert make_bot(tmp_path).window == charts.DEFAULT_WINDOW
    for junk in ({"format": 1, "messages": {"20": "42", "x": 1}, "window": 7},
                 {"format": 1, "messages": [20]}):
        state.write_text(json.dumps(junk))
        b = make_bot(tmp_path)
        assert (b.main.message_id, b.window) == (None, charts.DEFAULT_WINDOW)


def test_save_state_round_trips_and_creates_dir(tmp_path):
    b = make_bot(tmp_path)
    b.main.message_id, b.window = 5, 1800
    b._save_state()
    assert json.loads((tmp_path / "data" / "state.json").read_text()) == {
        "format": 1, "messages": {"20": 5}, "window": "30m", "upload_at": None, "task_times": {}}
    assert not (tmp_path / "data" / "state.json.tmp").exists()
    b = make_bot(tmp_path)
    assert (b.main.message_id, b.window) == (5, 1800)


@pytest.mark.parametrize("value", [[], {}, {"format": True}, {"format": 0}, {"format": 2}])
def test_unsupported_state_format_refuses_without_replacing_state(tmp_path, value):
    from shazzzbot.helper.formats import FormatError

    make_bot(tmp_path)
    state = tmp_path / "data" / "state.json"
    state.write_text(json.dumps(value))
    before = state.read_bytes()
    with pytest.raises(FormatError):
        make_bot(tmp_path)
    assert state.read_bytes() == before


def test_init_cadences_and_green(tmp_path, monkeypatch):
    monkeypatch.setattr(panel, "SLOT", panel.SLOT)
    b = make_bot(tmp_path, interval=30, edit_every=90)
    assert (b.sample_loop.seconds, b.panel_loop.seconds) == (30, 2)
    assert b._loads.maxlen == 3 and panel.SLOT[0] != GREEN
    make_bot(tmp_path, green_ok=True)
    assert panel.SLOT[0] == GREEN


# --- guild lock ---

def test_start_begins_sampling_before_login(tmp_path, monkeypatch):
    order = []

    async def login(self, token, *, reconnect=True):
        order.append(("login", token, reconnect))
    monkeypatch.setattr(discord.Bot, "start", login)
    for proxy, docker in (("http://proxy:2375/", True), (None, False)):
        b = make_bot(tmp_path, docker_proxy=proxy)
        b.sample_loop = MagicMock(start=lambda: order.append("sample"))  # type: ignore[method-assign]

        async def go(b=b):
            await b.start("tok")
            await b.http_session.close()
        asyncio.run(go())
        assert (b.docker is not None) == docker
        assert order == ["sample", ("login", "tok", True)]
        order.clear()
    assert make_bot(tmp_path, docker_proxy="http://proxy:2375/").docker is None  # made in start, not __init__


def test_on_ready_leaves_foreign_guilds_and_starts_once(tmp_path, monkeypatch):
    home, other = SimpleNamespace(id=GUILD, leave=AsyncMock()), SimpleNamespace(id=666, leave=AsyncMock())
    monkeypatch.setattr(B.Bot, "guilds", [home, other])
    b = make_bot(tmp_path)
    b.panel_loop = MagicMock(is_running=MagicMock(side_effect=[False, True]))  # type: ignore[method-assign]

    async def go():
        await b.on_ready()
        await b.on_ready()
    asyncio.run(go())
    other.leave.assert_awaited()
    home.leave.assert_not_awaited()
    b.panel_loop.start.assert_called_once_with()


# --- sampling ---

def test_first_poll_is_only_a_baseline(tmp_path):
    b = make_bot(tmp_path)
    asyncio.run(b.sample())
    assert b._snap is None and not b.history.samples and not b._loads
    asyncio.run(b.sample())
    assert b._snap is not None and len(b.history.samples) == len(b._loads) == 1


def test_ups_is_polled_by_the_sampler_and_failures_replace_stale_data(tmp_path, monkeypatch):
    from shazzzbot import ups

    b = make_bot(tmp_path, ups="unit@nut")
    with_array(tmp_path)
    reading = ups.Reading(("OB", "LB"), 12, 2520)
    poll = AsyncMock(side_effect=[reading, ups.Reading()])
    monkeypatch.setattr(B.ups, "read", poll)

    async def go():
        await b.sample()
        sections = await b.collect()
        system = next(s for s in sections if s.key == "system")
        assert any(lv == panel.CRITICAL and "On battery, Low battery" in reason for lv, reason in system.issues)
        assert sum(reason.startswith("UPS:") for _, reason in system.issues) == 1
        alerts = next(r for r in b._system if r.label == "Alerts")
        assert any(lv == panel.CRITICAL and reason.startswith("UPS:") for lv, reason in alerts.issues)
        labels = [r.label for r in b._system]
        assert labels[labels.index("Array") + 1] == "UPS"
        await b.sample()
        sections = await b.collect()
        system = next(s for s in sections if s.key == "system")
        assert (panel.WARNING, "UPS: no data") in system.issues
        assert not any("Low battery" in reason for _, reason in system.issues)
    asyncio.run(go())
    assert poll.await_count == 2 and b._ups == ups.Reading()
    poll.reset_mock()
    unconfigured = make_bot(tmp_path)
    asyncio.run(unconfigured.sample())
    poll.assert_not_awaited()
    assert unconfigured._ups is None


def test_apps_history_uses_samples_and_the_history_window_without_growing_on_collect(tmp_path, monkeypatch):
    from shazzzbot.glyphs import NO_DATA
    from shazzzbot.spark import TREND_WIDTH

    b = make_bot(tmp_path, interval=30)
    b.window = 300
    b.host.poll.return_value = Snapshot(top_cpu=[("app", 1)])
    now = [1000.0]
    monkeypatch.setattr(B.time, "time", lambda: now[0])

    async def go():
        await b.sample()
        assert b.app_history.samples == {"cpu": {}, "ram": {}}
        await b.sample()
        apps = next(s for s in await b.collect() if s.key == "apps")
        assert apps.parts[0].rows[0][1][-1] == NO_DATA * (TREND_WIDTH - 1) + chr(0x28c0)
        now[0] = 1030.0
        b.host.poll.return_value = Snapshot(top_cpu=[("app", 2)])
        await b.sample()
        apps = next(s for s in await b.collect() if s.key == "apps")
        assert len(apps.parts[0].trends[0].samples) == 2
        await b.collect()
        assert len(b.app_history.samples["cpu"]["app"]) == 2
        now[0] = 1060.0
        await b.sample()
        apps = next(s for s in await b.collect() if s.key == "apps")
        trend = apps.parts[0].trends[0]
        assert trend.start == 760 and trend.end == 1060 and trend.samples == ((1000, 1), (1030, 2), (1060, 2))
        await b.collect()
        assert len(b.app_history.samples["cpu"]["app"]) == 3
        b.window = 600
        now[0] = 1090.0
        await b.sample()
        apps = next(s for s in await b.collect() if s.key == "apps")
        assert apps.note == "Trends, last 10m (partial data)" and apps.parts[0].trends[0].start == 490
        assert b.app_history.samples["cpu"]["app"].maxlen == 21
    asyncio.run(go())


def test_app_history_retains_a_full_window_at_the_real_sampling_period(tmp_path):
    for period in (5, 10, 60):
        b = make_bot(tmp_path / str(period), interval=period)
        assert b.app_history.interval == b.sample_loop.seconds == period
        snapshot = Snapshot(top_cpu=[("app", 1)], top_ram=[("app", 64)])
        for t in range(0, 1801, period):
            b.app_history.add(snapshot, 1800, t)
        expected = [(t, 1) for t in range(0, 1801, period)]
        assert b.app_history.values("cpu", "app", 0, 1800) == expected
        assert len(b.app_history.values("ram", "app", 0, 1800)) == len(expected)
        b.app_history.add(snapshot, 1800, 1800 + period)
        retained = [(t, 1) for t in range(period, 1801 + period, period)]
        assert b.app_history.values("cpu", "app", 0, 1800 + period) == retained
        b.history.close()


def test_samples_persist_across_a_restart(tmp_path):
    b = make_bot(tmp_path)
    asyncio.run(sampled(b, 3))
    b.history.close()
    assert len(make_bot(tmp_path).history.samples) == 3


def test_sample_loop_beats_and_swallows_errors(tmp_path, caplog):
    b = make_bot(tmp_path)
    caplog.clear()
    b.sample = AsyncMock(side_effect=[None, RuntimeError("x")])  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR, "shazzzbot"):
        asyncio.run(b.sample_loop.coro(b))
        assert not caplog.records and health.ok()
        asyncio.run(b.sample_loop.coro(b))
    assert "sampling failed" in caplog.text and b.sample.await_count == 2


def test_on_guild_join(tmp_path):
    b = make_bot(tmp_path)
    home, other = SimpleNamespace(id=GUILD, leave=AsyncMock()), SimpleNamespace(id=666, leave=AsyncMock())
    asyncio.run(b.on_guild_join(home))
    asyncio.run(b.on_guild_join(other))
    home.leave.assert_not_awaited()
    other.leave.assert_awaited_once()


# --- build ---

def titles(embeds) -> list:
    return [e.title for e in embeds]


def test_build_minimal(tmp_path):
    b = make_bot(tmp_path)
    assert titles(asyncio.run(b.build()))[0] == "Unraid System Info"  # before the first sample
    asyncio.run(sampled(b))
    embeds = asyncio.run(b.build())
    assert titles(embeds) == ["Unraid System Info", "History, last 30m", "Top Apps", "Tasks"]
    assert [e.image.url for e in embeds] == ["attachment://system.png", "attachment://history.png",
                                             "attachment://apps.png", "attachment://tasks.png"]
    assert b.array is None and len(b.history.samples) == 1
    b.host.poll.reset_mock()
    asyncio.run(b.build())
    b.host.poll.assert_not_called()
    assert [r.label for r in b._system][-2:] == ["Alerts", "Tasks"]


def test_build_full(tmp_path, monkeypatch):
    with_array(tmp_path)
    svc = [Service("Site", url="http://site")]
    b = make_bot(tmp_path, netdata_url="http://nd", jellyfin_url="http://jf",
                 jellyfin_key="k", services=svc, outputs=[Output(PANEL, {"disks": "text", "docker": "embed"})])
    b._read_tasks = AsyncMock(return_value={"generated": time.time(), "smart": ["disk1"], "os_update": "7.3.5"})
    b.http_session = MagicMock(get=MagicMock(return_value=release_reply(404, {})))
    b.docker = MagicMock(list=AsyncMock(return_value=[WEB]), info=AsyncMock(return_value=Engine("27", "overlay2", 3)))
    b.svc_results = [Result(panel.WARNING, "", "HTTP 502", "N/A", "N/A", "local HTTP 502")]
    monkeypatch.setattr(netdata, "alarms", AsyncMock(return_value=[(panel.CRITICAL, "cpu hot")]))
    monkeypatch.setattr(jellyfin, "streams", AsyncMock(return_value=Counter({"transcode": 2})))
    monkeypatch.setattr(services, "check_all", AsyncMock(return_value=b.svc_results))

    async def go():
        await sampled(b)
        embeds = await b.build()
        await b._svc_task
        return embeds
    embeds = asyncio.run(go())
    assert titles(embeds)[3:] == ["Services", "Docker Containers", "Top Apps", "Tasks"]
    assert b.containers == [WEB] and b.engine == Engine("27", "overlay2", 3) and b.array.version == "7.3.4"
    b.host.poll.assert_called_with({"c1": "web"})
    jellyfin.streams.assert_awaited_once_with(b.http_session, "http://jf", "k")
    rows = {r.label: r for r in b._system}
    assert rows["Alerts"].text == "3 critical    3 warnings"
    assert (panel.WARNING, "Site local HTTP 502") in rows["Alerts"].issues
    assert rows["Jellyfin"].text == "2 transcoding (2 total)"
    assert rows["OS"].note == "(update available!)"
    by = {e.title: e for e in embeds}
    assert "1 issue" in by["Disks"].description  # smart flag from the helper reaches the array embed
    assert by["Docker Containers"].fields[0].name == "Engine" and not by["Services"].description
    assert b.image_keys() == ("system", "history", "services", "apps", "tasks")


def test_build_failures_log_and_continue(tmp_path, monkeypatch, caplog):
    b = make_bot(tmp_path, netdata_url="http://nd", jellyfin_url="http://jf")
    b.http_session = MagicMock(get=MagicMock(return_value=release_reply(404, {})))
    b.containers = [WEB]
    b.docker = MagicMock(list=AsyncMock(side_effect=aiohttp.ClientError("down")),
                         info=AsyncMock(side_effect=ValueError("bad")))
    monkeypatch.setattr(netdata, "alarms", AsyncMock(side_effect=TimeoutError()))
    monkeypatch.setattr(jellyfin, "streams", AsyncMock(side_effect=aiohttp.ClientError("jf")))
    async def go():
        await sampled(b)
        return await b.build()
    with caplog.at_level(logging.WARNING, "shazzzbot"):
        embeds = asyncio.run(go())
    msgs = " ".join(r.getMessage() for r in caplog.records)
    for part in ("docker list failed", "docker info failed", "netdata alarms failed", "jellyfin sessions failed"):
        assert part in msgs
    assert b.containers == [WEB] and b.engine is None and "Docker Containers" in titles(embeds)
    rows = {r.label: r for r in b._system}
    assert rows["Jellyfin"].text == "unreachable" and rows["Alerts"].level == panel.WARNING


def test_sampler_polls_engine_every_five_minutes(tmp_path):
    b = make_bot(tmp_path)
    b.docker = MagicMock(list=AsyncMock(return_value=[]), info=AsyncMock(return_value=Engine("27", "o", 0)))

    async def go():
        await b.sample()
        await b.sample()
        b._engine_at -= B.ENGINE_EVERY + 1
        await b.sample()
    asyncio.run(go())
    assert b.docker.info.await_count == 2 and b.docker.list.await_count == 3


def test_first_panel_waits_for_the_first_service_check(tmp_path, monkeypatch):
    b = make_bot(tmp_path, services=[Service("A", url="http://a")])
    b.http_session = MagicMock(get=MagicMock(return_value=release_reply(404, {})))

    async def check_all(svcs, cs, http, remote, **kwargs):
        await asyncio.sleep(0.05)
        return [Result(panel.FINE, "", "5ms", "N/A", "N/A")]
    monkeypatch.setattr(services, "check_all", check_all)
    assert "Services" in titles(asyncio.run(b.build()))


def test_service_checks_run_in_background_one_at_a_time(tmp_path, monkeypatch, caplog):
    b = make_bot(tmp_path, services=[Service("A", url="http://a")])
    b.http_session = MagicMock(get=MagicMock(return_value=release_reply(404, {})))
    gate = None
    ok = [Result(panel.FINE, "", "5ms", "N/A", "N/A")]

    async def check_all(svcs, cs, http, remote, **kwargs):
        await gate.wait()
        return ok
    monkeypatch.setattr(services, "check_all", check_all)
    monkeypatch.setattr(B, "FIRST_CHECK_WAIT", 0.01)

    async def go():
        nonlocal gate
        gate = asyncio.Event()
        await b.build()
        first = b._svc_task
        await b.build()
        assert b._svc_task is first  # throttled by SERVICES_EVERY
        b._svc_at -= B.SERVICES_EVERY
        await b.build()
        assert b._svc_task is first  # still running, not doubled
        gate.set()
        await first
        assert b.svc_results == ok
        assert "Services" in titles(await b.build())
        b._svc_at -= B.SERVICES_EVERY
        monkeypatch.setattr(services, "check_all", AsyncMock(side_effect=RuntimeError("boom")))
        await b.build()
        assert b._svc_task is not first
        await b._svc_task
    with caplog.at_level(logging.ERROR, "shazzzbot"):
        asyncio.run(go())
    assert b.svc_results == ok and "service checks failed" in caplog.text


def test_build_loads_keep_a_minute(tmp_path):
    b = make_bot(tmp_path)

    asyncio.run(sampled(b, 10))
    assert len(b._loads) == 7 and len(b.history.samples) == 10


# --- images and refresh ---

def test_image_files(tmp_path, monkeypatch):
    with_array(tmp_path)
    b = make_bot(tmp_path)
    seen, bars = [], []
    real, real_bars = charts.render, charts.disk_bars

    def spy(samples, window, now, tz, gap):
        seen.append((window, gap))
        return real(samples, window, now, tz, gap)

    def spy_bars(disks, now, tz):
        bars.append(disks)
        return real_bars(disks, now, tz)
    monkeypatch.setattr(charts, "render", spy)
    monkeypatch.setattr(charts, "disk_bars", spy_bars)

    async def go():
        await sampled(b)
        await b.build()
        return await b.image_files()
    files = asyncio.run(go())
    assert [f.filename for f in files] == ["system.png", "history.png", "disks.png", "apps.png", "tasks.png"]
    assert all(f.fp.read(8) == b"\x89PNG\r\n\x1a\n" for f in files)
    assert seen == [(b.window, 30)]
    assert bars == [[("disk1", 98.0, None, 90.0, 95.0)]]  # parity has no fs, so no bar


def test_schedule_spaces_uploads_one_floor_apart_on_ticks(tmp_path, monkeypatch):
    b = make_bot(tmp_path, timezone="UTC")
    b._sections = [panel.Section("history", "H", [])]
    slow = B.Board(9, image_every=1800, modes={"history": "image"}, message_id=3)
    b.main.modes = {"history": "image"}
    b.boards += [slow, B.Board(8, modes={"history": "text"})]
    now = 1_800_000_000.0 + 7  # a whole half hour plus 7s
    monkeypatch.setattr(B.time, "time", lambda: now)
    slow.image_slot = b._slot(1800)
    plan = b.schedule(now, b.main)
    assert plan[b.main.channel_id][:2] == [now - 7 + 90, now - 7 + 165]  # floor 75, on 15s ticks
    assert plan[slow.channel_id][0] == now - 7 + 1815 and 8 not in plan
    b._upload_wall = None
    assert b.schedule(now)[b.main.channel_id][0] == now - 7 + 15
    b.main.image_wanted = True
    assert B.images_note(b) == " Images follow at 8:00 AM."
    b.main.modes = {}
    assert b.schedule(now)[slow.channel_id] == [now - 7 + 1800, now - 7 + 3600]  # idle until its slot


def test_ticks_upload_due_images_between_edit_cycles(tmp_path, monkeypatch):
    monkeypatch.setattr(B.time, "time", lambda: 1_800_000_001.0)
    b = make_bot(tmp_path)
    b._snap = Snapshot()
    stub_render(b)
    other = B.Board(31, message_id=6, msg=edited(MagicMock()))
    b.main.message_id, b.main.msg = 5, edited(MagicMock())
    b.boards.append(other)
    wire(b, channel())
    for board in b.boards:
        board.edit_slot = b._slot(board.edit_every)
    b._due = lambda board, force: board is b.main  # type: ignore[method-assign]
    asyncio.run(b.panel_loop.coro(b))
    b.main.msg.edit.assert_awaited_once()
    other.msg.edit.assert_not_awaited()
    asyncio.run(b.panel_loop.coro(b))  # same tick: nothing
    b.main.msg.edit.assert_awaited_once()

def test_panel_over_the_embed_limit_preserves_configured_modes(tmp_path, monkeypatch, caplog):
    with_array(tmp_path)
    b = make_bot(tmp_path, outputs=[Output(PANEL, {"disks": "text"})])
    monkeypatch.setattr(B, "EMBED_LIMIT", 400)
    with caplog.at_level(logging.WARNING, "shazzzbot"):
        asyncio.run(b.build())
        asyncio.run(b.build())
    assert set(b.image_keys()) == {"system", "history", "apps", "tasks"}
    assert b.main.modes["disks"] == "text" and "drawing" not in caplog.text


def stub_render(b: B.Bot, keys=("history",)):
    b.collect = AsyncMock(return_value=[])  # type: ignore[method-assign]
    b._fit = MagicMock(return_value=["E"])  # type: ignore[method-assign]
    b.image_keys = lambda board=None: keys  # type: ignore[method-assign]
    b.image_files = AsyncMock(side_effect=lambda board=None: ["F"] if keys else [])  # type: ignore[method-assign]
    if not keys:
        for board in b.boards:
            board.mode = "text"


def edited(msg) -> MagicMock:
    msg.edit = AsyncMock(return_value=msg)
    return msg


def test_refresh_without_channel(tmp_path, caplog):
    b = make_bot(tmp_path)
    stub_render(b)
    wire(b, panel_ch=MagicMock())
    with caplog.at_level(logging.ERROR, "shazzzbot"):
        asyncio.run(b.refresh())
    b._fit.assert_not_called()
    assert "panel channel 20 not found" in caplog.text


def test_refresh_posts_first_message_and_saves(tmp_path):
    b = make_bot(tmp_path)
    stub_render(b)
    ch = channel()
    wire(b, ch)
    asyncio.run(b.refresh())
    ch.send.assert_awaited_once_with(embeds=["E"], files=["F"], nonce=ANY, enforce_nonce=True)
    assert b.main.message_id == 777 and b._upload_at > 0
    assert json.loads((tmp_path / "data" / "state.json").read_text())["messages"] == {"20": 777}


def test_panel_loop_edits_once_per_local_slot_and_posts_immediately(tmp_path, monkeypatch):
    b = make_bot(tmp_path, edit_every=90, timezone="America/Los_Angeles")
    b._snap = Snapshot(load=(1.0, 1.0, 1.0))
    stub_render(b, keys=())
    ch = channel()
    msg = edited(MagicMock(id=777))
    ch.send.return_value = msg
    wire(b, ch)
    start = datetime(2026, 1, 1, 0, 0, tzinfo=ZoneInfo(b.cfg.timezone)).timestamp()
    clock = [start + 45]
    monkeypatch.setattr(B.time, "time", lambda: clock[0])

    async def go():
        await b.panel_loop.coro(b)
        await b.panel_loop.coro(b)
        clock[0] = start + 89
        await b.panel_loop.coro(b)
        clock[0] = start + 90
        await b.panel_loop.coro(b)
        await b.panel_loop.coro(b)

    asyncio.run(go())
    ch.send.assert_awaited_once()
    assert msg.edit.await_count == 1


def test_saved_panel_waits_for_next_local_slot(tmp_path, monkeypatch):
    start = datetime(2026, 1, 1, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()
    clock = [start + 45]
    monkeypatch.setattr(B.time, "time", lambda: clock[0])
    state = tmp_path / "data" / "state.json"
    state.parent.mkdir()
    state.write_text(json.dumps({"format": 1, "messages": {str(PANEL): 5}}))
    b = make_bot(tmp_path, edit_every=90, timezone="America/Los_Angeles")
    b._snap = Snapshot(load=(1.0, 1.0, 1.0))
    stub_render(b, keys=())
    msg = edited(MagicMock())
    ch = channel()
    ch.fetch_message.return_value = msg
    wire(b, ch)

    async def go():
        await b.panel_loop.coro(b)
        clock[0] = start + 90
        await b.panel_loop.coro(b)

    asyncio.run(go())
    msg.edit.assert_awaited_once()


def test_a_message_with_images_edits_only_when_they_upload(tmp_path, monkeypatch):
    b = make_bot(tmp_path)
    b.main.message_id = 5
    stub_render(b)
    ch = channel()
    msg = edited(MagicMock())
    ch.fetch_message.return_value = msg
    wire(b, ch)
    wall = [datetime(2026, 1, 1, tzinfo=ZoneInfo("UTC")).timestamp()]
    monkeypatch.setattr(B.time, "time", lambda: wall[0])

    async def go():
        await b.refresh()  # images due: never sent
        stamp = b._upload_at
        await b.refresh()
        await b.refresh(force_images=True)  # held: the last upload was under IMAGE_FLOOR ago
        assert b._upload_at == stamp and b.main.image_wanted
        b._upload_at -= IMAGE_FLOOR
        wall[0] += b.cfg.image_every
        await b.refresh()
        assert b._upload_at > stamp - IMAGE_FLOOR and not b.main.image_wanted
    asyncio.run(go())
    ch.fetch_message.assert_awaited_once_with(5)
    assert [c.kwargs for c in msg.edit.await_args_list] == [
        {"embeds": ["E"], "files": ["F"], "attachments": []}, {"embeds": ["E"], "files": ["F"], "attachments": []}]
    ch.send.assert_not_awaited()


def test_images_follow_image_every_but_never_beat_the_floor(tmp_path, monkeypatch):
    b = make_bot(tmp_path, image_every=600)
    stub_render(b)
    b._upload_at = b.main.image_at = 0.0
    b.main.image_keys = ("history",)
    b.main.message_id, b.main.msg = 5, edited(MagicMock())
    wire(b, channel())
    clock = [IMAGE_FLOOR + 1.0]
    monkeypatch.setattr(B.time, "monotonic", lambda: clock[0])
    wall = [datetime(2026, 1, 1, tzinfo=ZoneInfo("UTC")).timestamp() + 301]
    monkeypatch.setattr(B.time, "time", lambda: wall[0])
    b.main.image_slot = b._slot(b.cfg.image_every)
    asyncio.run(b.refresh())
    assert b.image_files.await_count == 0  # past the floor, not yet image_every
    clock[0] = 600.0
    wall[0] += 299
    asyncio.run(b.refresh())
    assert b.image_files.await_count == 1
    b._upload_at = -B.math.inf
    asyncio.run(b.refresh())
    assert b.image_files.await_count == 1


def limited() -> discord.HTTPException:
    return discord.HTTPException(MagicMock(status=429, reason="Too Many Requests"),
                                 {"code": 400009, "message": "upload limit"})


def test_refused_upload_defers_the_route_and_other_errors_are_logged(tmp_path, monkeypatch, caplog):
    b = make_bot(tmp_path)
    b.main.message_id = 5
    stub_render(b)
    msg = MagicMock()
    msg.edit = AsyncMock(side_effect=[limited(), msg])
    ch = channel()
    ch.fetch_message.return_value = msg
    wire(b, ch)
    wall = [datetime(2026, 1, 1, tzinfo=ZoneInfo("UTC")).timestamp()]
    monkeypatch.setattr(B.time, "time", lambda: wall[0])
    with caplog.at_level(logging.WARNING, "shazzzbot"):
        asyncio.run(b.refresh(force_images=True))
    assert [c.kwargs for c in msg.edit.await_args_list] == [
        {"embeds": ["E"], "files": ["F"], "attachments": []}]
    assert "400009" in caplog.text and b.main.image_wanted and b._upload_at > 0
    msg.edit = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500, reason="err"), "boom"))
    wall[0] += b._image_spacing
    b._upload_at = -B.math.inf
    b._image_retry = b.main.retry_at = 0
    with caplog.at_level(logging.ERROR, "shazzzbot"):
        asyncio.run(b.refresh())
    assert "panel update in channel 20 failed" in caplog.text
    assert b.main.image_wanted and b.main.image_slot is None  # the next free slot retries the upload


def test_refused_first_post_waits_for_the_cooldown_before_posting(tmp_path, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(B.time, "time", lambda: clock[0])
    monkeypatch.setattr(B.time, "monotonic", lambda: clock[0])
    b = make_bot(tmp_path)
    stub_render(b)
    ch = channel()
    ch.send.side_effect = [limited(), SimpleNamespace(id=777)]
    wire(b, ch)
    asyncio.run(b.refresh())
    assert [c.kwargs for c in ch.send.await_args_list] == [
        {"embeds": ["E"], "files": ["F"], "nonce": ANY, "enforce_nonce": True}]
    assert b.main.message_id is None and b.main.image_wanted
    clock[0] += b._image_spacing + B.TICK
    asyncio.run(b.refresh())
    assert ch.send.await_count == 2 and b.main.message_id == 777 and not b.main.image_wanted


def test_repost_when_message_gone_respects_the_floor(tmp_path):
    b = make_bot(tmp_path)
    b.main.message_id, b._upload_at = 5, B.time.monotonic()
    stub_render(b)
    ch = channel()
    ch.fetch_message.side_effect = not_found()
    wire(b, ch)
    asyncio.run(b.refresh())
    ch.send.assert_awaited_once_with(embeds=["E"], nonce=ANY, enforce_nonce=True)
    assert b.main.message_id == 777 and b.main.image_wanted


def test_repost_after_a_failed_image_edit_renders_fresh_files(tmp_path, monkeypatch):
    b = make_bot(tmp_path)
    b.main.message_id = 5
    stub_render(b)
    msg = MagicMock(edit=AsyncMock(side_effect=not_found()))
    ch = channel()
    ch.fetch_message.return_value = msg
    wire(b, ch)
    b._upload_at = -B.math.inf
    asyncio.run(b.refresh(force_images=True))
    assert b.image_files.await_count == 1
    assert ch.send.await_args.kwargs == {"embeds": ["E"], "nonce": ANY, "enforce_nonce": True}
    b2 = make_bot(tmp_path / "2")
    b2.main.message_id = 5
    stub_render(b2)
    monkeypatch.setattr(B.delivery, "due", lambda *args: True)
    wire(b2, ch)
    ch.send.reset_mock()
    asyncio.run(b2.refresh())
    assert b2.image_files.await_count == 2
    assert ch.send.await_args.kwargs == {"embeds": ["E"], "files": ["F"], "nonce": ANY, "enforce_nonce": True}


def test_text_only_panels_clear_old_images_once_and_never_spend_the_budget(tmp_path):
    b = make_bot(tmp_path)
    stub_render(b, keys=())
    b.main.message_id, b.main.msg = 5, edited(MagicMock())
    wire(b, channel())
    asyncio.run(b.refresh())
    asyncio.run(b.refresh())
    assert [c.kwargs for c in b.main.msg.edit.await_args_list] == [
        {"embeds": ["E"], "files": [], "attachments": []}, {"embeds": ["E"]}]
    assert b._upload_at == -B.math.inf


def test_mirrors_get_their_own_message_and_one_failure_spares_the_rest(tmp_path, caplog):
    outputs = [Output(PANEL), Output(21, dict.fromkeys(B.config.SECTIONS, "text")),
               Output(22, dict.fromkeys(B.config.SECTIONS, "embed"))]
    b = make_bot(tmp_path, outputs=outputs)
    asyncio.run(sampled(b))
    chans = {PANEL: channel(), 21: channel(), 22: channel()}
    chans[21].send.side_effect = RuntimeError("boom")
    b.get_channel = chans.get  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR, "shazzzbot"):
        asyncio.run(b.refresh())
    assert "panel update in channel 21 failed" in caplog.text
    main, rich = (chans[k].send.await_args.kwargs for k in (PANEL, 22))
    assert len(main["files"]) == 4 and rich["files"] == []
    assert set(b.boards[2].modes.values()) == {"embed"} and set(b.boards[1].modes.values()) == {"text"}
    assert json.loads((tmp_path / "data" / "state.json").read_text())["messages"] == {"20": 777, "22": 777}
    assert [x.message_id for x in make_bot(tmp_path, outputs=outputs).boards] == [777, None, 777]


def test_sweep_deletes_only_this_bots_older_panels(tmp_path, monkeypatch):
    monkeypatch.setattr(B.Bot, "user", SimpleNamespace(id=9))

    def post(mid, author, embeds=(), attachments=()):
        return SimpleNamespace(id=mid, author=SimpleNamespace(id=author), embeds=list(embeds),
                               attachments=list(attachments), delete=AsyncMock())
    old, old_pic, audit, other, live = (post(1, 9, ["e"]), post(2, 9, attachments=["a"]), post(3, 9),
                                        post(4, 8, ["e"]), post(5, 9, ["e"]))
    b = make_bot(tmp_path)
    stub_render(b)
    b.main.message_id, b.main.msg = 5, edited(MagicMock(author=live.author))
    b.main.surplus = [B.Message(PANEL, message_id=m.id, msg=m) for m in (old, old_pic, other)]
    ch = channel(old, old_pic, audit, other, live)
    wire(b, ch)
    asyncio.run(b.refresh())
    asyncio.run(b.refresh())
    assert [m.delete.await_count for m in (old, old_pic, audit, other, live)] == [1, 1, 0, 0, 0]


def test_sweep_failure_is_logged(tmp_path, caplog):
    b = make_bot(tmp_path)
    stub_render(b)
    ch = channel()

    b.main.surplus = [B.Message(PANEL, message_id=5)]
    ch.fetch_message.side_effect = discord.Forbidden(MagicMock(status=403, reason="Forbidden"), "no access")
    wire(b, ch)
    with caplog.at_level(logging.WARNING, "shazzzbot"):
        asyncio.run(b.refresh())
    assert "could not remove surplus panel message" in caplog.text and b.main.message_id == 777


def test_status_reply_uploads_only_within_the_budget(tmp_path):
    b = make_bot(tmp_path)
    asyncio.run(sampled(b))
    embeds, files = asyncio.run(b.status_reply())
    assert len(files) == 4 and b._upload_at > 0
    embeds, files = asyncio.run(b.status_reply())
    assert files == [] and not any(e.image for e in embeds)
    b2 = make_bot(tmp_path / "2", outputs=[Output(PANEL, {
        k: "text" for k in ("system", "history", "apps", "tasks")})])
    asyncio.run(sampled(b2))
    assert asyncio.run(b2.status_reply())[1] == [] and b2._upload_at == -B.math.inf


def test_boards_take_turns_at_the_upload_budget(tmp_path):
    b = make_bot(tmp_path, outputs=[Output(PANEL), Output(21, dict.fromkeys(B.config.SECTIONS, "image"))])
    stub_render(b)
    msgs = {cid: edited(MagicMock(id=cid)) for cid in (PANEL, 21)}
    chans = {cid: channel() for cid in msgs}
    for cid, ch in chans.items():
        ch.send.return_value = msgs[cid]
    b.get_channel = chans.get  # type: ignore[method-assign]
    asyncio.run(b.refresh())  # main goes first and takes the upload
    assert "files" in chans[PANEL].send.await_args.kwargs and "files" not in chans[21].send.await_args.kwargs
    b._upload_at -= IMAGE_FLOOR + B.TICK
    asyncio.run(b.refresh())  # the mirror has waited longest
    assert msgs[21].edit.await_args.kwargs["files"] == ["F"]
    msgs[PANEL].edit.assert_not_awaited()


def test_status_without_the_budget_leaves_out_what_only_fits_as_an_image(tmp_path):
    b = make_bot(tmp_path)
    big = panel.Section("docker", "Docker", [panel.Part([(0, ("x" * 1000,))] * 5, cut=None)])
    small = panel.Section("tasks", "Tasks", [panel.Part([(0, ("ok",))])])
    b._sections = [big, big._replace(key="services", title="Services"), small]
    b._upload_at = B.time.monotonic()
    embeds, files = asyncio.run(b.status_reply())
    assert files == [] and not any(e.image for e in embeds) and sum(map(len, embeds)) <= B.EMBED_LIMIT
    assert "Not shown" in embeds[-1].description and ("Docker" in embeds[-1].description) != (
        "Services" in embeds[-1].description)


def test_refreshes_share_board_delivery_and_latest_reuses_the_panel(tmp_path):
    b = make_bot(tmp_path)
    stub_render(b)
    ch = channel()
    ch.send.return_value = MagicMock(id=777, edit=AsyncMock())
    wire(b, ch)
    async def collect():
        await asyncio.sleep(0.01)
        return []
    b.collect = collect  # type: ignore[method-assign]

    async def go():
        await asyncio.gather(b.refresh(), b.refresh(), b.latest())
        return await b.latest()
    assert asyncio.run(go()) == ["E"]
    ch.send.assert_awaited_once()
    fresh = make_bot(tmp_path)
    fresh.build = AsyncMock(return_value=["X"])  # type: ignore[method-assign]
    assert asyncio.run(fresh.latest()) == ["X"] and asyncio.run(fresh.latest()) == ["X"]
    fresh.build.assert_awaited_once()


def test_panel_loop_waits_for_a_sample_and_swallows_errors(tmp_path, monkeypatch, caplog):
    b = make_bot(tmp_path)
    b.refresh = AsyncMock(side_effect=[None, RuntimeError("x")])  # type: ignore[method-assign]
    clock = [datetime(2026, 1, 1, tzinfo=ZoneInfo("UTC")).timestamp()]
    monkeypatch.setattr(B.time, "time", lambda: clock[0])
    asyncio.run(b.panel_loop.coro(b))
    b.refresh.assert_not_awaited()
    b._snap = Snapshot()
    with caplog.at_level(logging.ERROR, "shazzzbot"):
        asyncio.run(b.panel_loop.coro(b))
        assert not caplog.records
        clock[0] += b.cfg.edit_every
        asyncio.run(b.panel_loop.coro(b))
    assert "panel update failed" in caplog.text and b.refresh.await_count == 2


# --- audit ---

def test_audit_posts_without_pings(tmp_path, caplog):
    b = make_bot(tmp_path)
    ch = channel()
    wire(b, audit_ch=ch)
    with caplog.at_level(logging.INFO, "shazzzbot"):
        asyncio.run(b.audit(SimpleNamespace(id=7), "did a thing"))
    (args, kw), = [(c.args, c.kwargs) for c in ch.send.await_args_list]
    assert args == ("<@7> did a thing",) and kw["allowed_mentions"].to_dict()["parse"] == []
    assert "did a thing" in caplog.text


def test_audit_reports_whether_it_landed(tmp_path, caplog):
    b = make_bot(tmp_path)
    ch = channel()
    wire(b, audit_ch=MagicMock())
    assert asyncio.run(b.audit(SimpleNamespace(id=7), "x")) is False
    b.cfg.audit_channel_id = None
    b.get_channel = MagicMock()  # type: ignore[method-assign]
    assert asyncio.run(b.audit(SimpleNamespace(id=7), "x")) is False
    b.get_channel.assert_not_called()
    ch.send.assert_not_awaited()
    b.cfg.audit_channel_id = AUDIT
    ch.send.side_effect = discord.Forbidden(MagicMock(status=403, reason="Forbidden"), "no perms")
    wire(b, audit_ch=ch)
    with caplog.at_level(logging.ERROR, "shazzzbot"):
        assert asyncio.run(b.audit(SimpleNamespace(id=7), "x")) is False
    assert "audit post failed" in caplog.text


# --- permissions ---

def test_can(tmp_path):
    b = make_bot(tmp_path)
    assert B.can(b, ctx(OPER), "operate") and B.can(b, ctx(OPER), "view")
    assert B.can(b, ctx(VIEWER, [VIEW_ROLE]), "view") and not B.can(b, ctx(VIEWER, [VIEW_ROLE]), "operate")
    assert not B.can(b, ctx(STRANGER), "view")
    assert not B.can(b, ctx(OPER, guild=666), "view")
    user = ctx(OPER)
    user.author = SimpleNamespace(id=OPER)  # a User outside a guild has no roles
    assert B.can(b, user, "operate")


def cmd_bot(tmp_path) -> tuple[B.Bot, dict]:
    b = make_bot(tmp_path, restart_allow=["w*"])
    B.register(b)
    b.refresh = AsyncMock()  # type: ignore[method-assign]
    b.build = AsyncMock(return_value=["E"])  # type: ignore[method-assign]
    b.image_files = AsyncMock(return_value=["F"])  # type: ignore[method-assign]
    b.status_reply = AsyncMock(return_value=(["E"], ["F"]))  # type: ignore[method-assign]
    b.audit = AsyncMock(return_value=True)  # type: ignore[method-assign]
    b.docker = MagicMock(restart=AsyncMock(), list=AsyncMock(return_value=[WEB]))
    b.containers = [WEB]
    wire(b, audit_ch=channel())
    return b, commands(b)


def test_every_command_denies_by_default(tmp_path):
    b, cmds = cmd_bot(tmp_path)
    assert set(cmds) == {"panel refresh", "status", "about", "update", "charts window", "docker restart", "blowup",
                         "docker restart-stack", "docker restart-group", "ratelimits", "chartgen"}
    calls = {"panel refresh": (), "status": (), "update": (), "charts window": ("6h",),
             "docker restart": ("web",), "blowup": (), "docker restart-stack": ("apps",),
             "docker restart-group": ("apps",), "ratelimits": (), "chartgen": ("cpu",)}
    for name, args in calls.items():
        viewer = () if name in {"status", "ratelimits", "chartgen"} else (ctx(VIEWER, [VIEW_ROLE]),)
        for who in (ctx(STRANGER), ctx(OPER, guild=666)) + viewer:
            asyncio.run(cmds[name].callback(who, *args))
            who.respond.assert_awaited_once_with("Not allowed.", ephemeral=True)
    b.refresh.assert_not_awaited()
    b.build.assert_not_awaited()
    b.docker.restart.assert_not_awaited()
    b.audit.assert_not_awaited()
    assert b.window == charts.DEFAULT_WINDOW


def test_panel_refresh(tmp_path):
    b, cmds = cmd_bot(tmp_path)
    c = ctx(OPER)
    asyncio.run(cmds["panel refresh"].callback(c))
    c.defer.assert_awaited_once_with(ephemeral=True)
    b.refresh.assert_awaited_once_with(force_images=True)
    c.respond.assert_awaited_once_with("Refreshed.", ephemeral=True)
    b.main.image_wanted = True
    c = ctx(OPER)
    asyncio.run(cmds["panel refresh"].callback(c))
    assert c.respond.await_args.args[0] == "Refreshed."


def test_about_answers_anyone_with_the_version_license_and_source(tmp_path):
    _, cmds = cmd_bot(tmp_path)
    c = ctx(STRANGER)
    asyncio.run(cmds["about"].callback(c))
    text = c.respond.await_args.args[0]
    assert B.__version__ in text and "AGPL-3.0-or-later" in text and f"<{B.SOURCE_URL}>" in text
    assert c.respond.await_args.kwargs == {"ephemeral": True}


def test_update_requires_configured_admin_and_the_configured_guild(tmp_path, monkeypatch):
    b, cmds = cmd_bot(tmp_path)
    check = AsyncMock()
    monkeypatch.setattr(b, "_check_release", check)
    for caller in (ctx(OPER), ctx(VIEWER, [VIEW_ROLE]), ctx(STRANGER)):
        caller.author.guild_permissions = SimpleNamespace(administrator=True)
        asyncio.run(cmds["update"].callback(caller))
        caller.respond.assert_awaited_once_with("Not allowed.", ephemeral=True)
        caller.defer.assert_not_awaited()
    b.cfg.perms = {**b.cfg.perms, "admin": {"users": [OPER]}}
    caller = ctx(OPER, guild=666)
    asyncio.run(cmds["update"].callback(caller))
    caller.respond.assert_awaited_once_with("Not allowed.", ephemeral=True)
    check.assert_not_awaited()
    b.audit.assert_not_awaited()


def release_reply(status, body):
    async def chunks(size):
        yield json.dumps(body).encode()
    response = MagicMock(status=status, headers={})
    response.content.iter_chunked = chunks
    return MagicMock(__aenter__=AsyncMock(return_value=response), __aexit__=AsyncMock(return_value=False))


def test_update_defers_audits_and_reports_current_and_latest_versions(tmp_path):
    b, cmds = cmd_bot(tmp_path)
    b.cfg.perms = {**b.cfg.perms, "admin": {"users": [OPER], "roles": [101]}}
    events = []
    async def audit(user, text):
        assert user is caller.author and text == "requested bot update check"
        assert events == ["defer"]
        events.append("audit")
        return True
    b.audit.side_effect = audit
    def get(url):
        assert url == f"https://api.github.com/repos/{B.SOURCE_URL.removeprefix('https://github.com/')}/releases/latest"
        assert events == ["defer", "audit"]
        events.append("release")
        return release_reply(200, {"tag_name": tag})
    b.http_session = MagicMock(get=MagicMock(side_effect=get))
    for tag, caller, expected in (("v99.0.0", ctx(OPER), "Update available."),
                                   (f"v{B.__version__}", ctx(VIEWER, [101]), "No update available.")):
        events.clear()
        caller.defer.side_effect = lambda **kwargs: events.append("defer")
        b._release_at = -B.RELEASE_EVERY
        asyncio.run(cmds["update"].callback(caller))
        assert events == ["defer", "audit", "release"]
        caller.defer.assert_awaited_once_with(ephemeral=True)
        text = caller.respond.await_args_list[0].args[0]
        stamp = datetime.fromtimestamp(b._release_checked, b.tz).strftime("%Y-%m-%d %I:%M %p %Z")
        assert text == (f"**NODE:** bot on `unknown`\nCurrent version: v{B.__version__}\n"
                        f"Latest public release: {discord.utils.escape_markdown(tag)}\n{expected}"
                        f"\nRelease check: {stamp} - ok."
                        "\nUpdate command not configured (node_updates).")
        options = caller.respond.await_args.kwargs
        assert options["ephemeral"] and not any(options["allowed_mentions"].to_dict()["parse"])
    events.clear()
    b.http_session.get.reset_mock()
    caller = ctx(OPER)
    caller.defer.side_effect = lambda **kwargs: events.append("defer")
    asyncio.run(cmds["update"].callback(caller))
    assert events == ["defer", "audit"]
    b.http_session.get.assert_not_called()
    latest = "Latest public release: " + discord.utils.escape_markdown(f"v{B.__version__}")
    assert latest in caller.respond.await_args_list[0].args[0]


def test_update_fails_closed_when_audit_is_unavailable(tmp_path, monkeypatch):
    b, cmds = cmd_bot(tmp_path)
    b.cfg.perms = {**b.cfg.perms, "admin": {"users": [OPER]}}
    check = AsyncMock()
    monkeypatch.setattr(b, "_check_release", check)
    b.audit.return_value = False
    caller = ctx(OPER)
    asyncio.run(cmds["update"].callback(caller))
    caller.defer.assert_awaited_once_with(ephemeral=True)
    caller.respond.assert_awaited_once_with(B.NO_AUDIT, ephemeral=True)
    b.audit.assert_awaited_once_with(caller.author, "requested bot update check")
    monkeypatch.setattr(b, "audit", B.Bot.audit.__get__(b, B.Bot))
    wire(b)
    caller = ctx(OPER)
    asyncio.run(cmds["update"].callback(caller))
    caller.respond.assert_awaited_once_with(B.NO_AUDIT, ephemeral=True)
    check.assert_not_awaited()


def test_update_reports_unknown_release_without_claiming_it_is_current(tmp_path):
    b, cmds = cmd_bot(tmp_path)
    b.cfg.perms = {**b.cfg.perms, "admin": {"users": [OPER]}}
    for http in (None, MagicMock(get=MagicMock(side_effect=aiohttp.ClientError("unreachable")))):
        b.http_session = http
        caller = ctx(OPER)
        asyncio.run(cmds["update"].callback(caller))
        text = caller.respond.await_args_list[0].args[0]
        prefix = (
            f"**NODE:** bot on `unknown`\nCurrent version: v{B.__version__}\nLatest public release: unavailable."
        )
        assert text.startswith(prefix) and text.endswith("\nUpdate command not configured (node_updates).")
        assert ("network error" in text) == (http is not None)
        assert caller.respond.await_args.kwargs["ephemeral"]


def test_update_escapes_and_bounds_release_tags_without_mentions(tmp_path, monkeypatch):
    b, cmds = cmd_bot(tmp_path)
    b.cfg.perms = {**b.cfg.perms, "admin": {"users": [OPER]}}
    monkeypatch.setattr(b, "_check_release", AsyncMock())
    b.latest_release = "v99.0.0_** @everyone <@123> " + "x" * 1000
    b.update_available = True
    caller = ctx(OPER)
    asyncio.run(cmds["update"].callback(caller))
    text = caller.respond.await_args_list[0].args[0]
    assert discord.utils.escape_markdown(b.latest_release[:64]) in text and len(text) < 1000
    mentions = caller.respond.await_args.kwargs["allowed_mentions"]
    assert mentions.everyone is False and mentions.users is False and mentions.roles is False


def test_status_for_viewer_role(tmp_path):
    _, cmds = cmd_bot(tmp_path)
    c = ctx(VIEWER, [VIEW_ROLE])
    asyncio.run(cmds["status"].callback(c))
    c.defer.assert_awaited_once_with(ephemeral=True)
    c.respond.assert_awaited_once_with(embeds=["E"], files=["F"], ephemeral=True)


def test_charts_window(tmp_path):
    b, cmds = cmd_bot(tmp_path)
    bad = ctx(OPER)
    asyncio.run(cmds["charts window"].callback(bad, "8d"))
    assert "Use a span" in bad.respond.await_args.args[0]
    assert b.window == charts.DEFAULT_WINDOW and not (tmp_path / "data" / "state.json").exists()
    c = ctx(OPER)
    asyncio.run(cmds["charts window"].callback(c, "45m"))
    c.defer.assert_awaited_once_with(ephemeral=True)
    assert b.window == 2700 and json.loads((tmp_path / "data" / "state.json").read_text())["window"] == "45m"
    b.refresh.assert_awaited_once_with(force_images=True)
    c.respond.assert_awaited_once_with("Charts now cover the last 45m.", ephemeral=True)
    b.audit.assert_awaited_once_with(c.author, "set chart window to 45m")
    wire(b)
    c = ctx(OPER)
    asyncio.run(cmds["charts window"].callback(c, "6h"))
    c.respond.assert_awaited_once_with(B.NO_AUDIT, ephemeral=True)
    assert b.window == 2700


def test_chart_windows_are_capped_by_retention_on_restore_and_command(tmp_path):
    b = make_bot(tmp_path, history_days=1)
    b.window = 7 * charts.DAY
    b._save_state()
    b = make_bot(tmp_path, history_days=1)
    assert b.window == b.history.keep == charts.DAY
    B.register(b)
    b.refresh = AsyncMock()
    b.audit = AsyncMock()
    wire(b, channel(), channel())
    c = ctx(OPER)
    asyncio.run(commands(b)["charts window"].callback(c, "7d"))
    assert b.window == charts.DAY
    assert json.loads((tmp_path / "data" / "state.json").read_text())["window"] == "24h"
    c.respond.assert_awaited_once_with("Charts now cover the last 24h.", ephemeral=True)
    b.refresh.assert_awaited_once_with(force_images=True)
    b.audit.assert_awaited_once_with(c.author, "set chart window to 24h")
    spans = commands(b)["charts window"].options[0].autocomplete
    assert asyncio.run(spans(SimpleNamespace(value=""))) == list(charts.SPANS[:-2])
    assert asyncio.run(spans(SimpleNamespace(value="7d"))) == ["24h"]
    assert asyncio.run(spans(SimpleNamespace(value="invalid"))) == ["30m"]


def test_autocomplete(tmp_path):
    b, cmds = cmd_bot(tmp_path)
    b.containers = [Container(str(i), f"app{i}", "running", "") for i in range(30)] + [WEB]
    names = cmds["docker restart"].options[0].autocomplete
    spans = cmds["charts window"].options[0].autocomplete
    assert asyncio.run(names(SimpleNamespace(value="WE"))) == ["web"]
    assert len(asyncio.run(names(SimpleNamespace(value=None)))) == 25
    assert asyncio.run(spans(SimpleNamespace(value="1"))) == ["15m", "1h", "12h"]
    assert asyncio.run(spans(SimpleNamespace(value=" 45m "))) == ["45m"]


def test_restart_unknown_or_no_docker(tmp_path, monkeypatch):
    b, cmds = cmd_bot(tmp_path)
    monkeypatch.setattr(B, "confirm", AsyncMock(return_value=True))
    def say(name):
        c = ctx(OPER)
        asyncio.run(cmds["docker restart"].callback(c, name))
        return c.respond.await_args.args[0]
    assert say("wolf") == "No container named `wolf`."
    assert say("db") == "`db` is not in restarts.allow."
    wire(b)
    assert say("web") == B.NO_AUDIT
    restart, b.docker = b.docker.restart, None
    assert say("web") == "Docker is not configured."
    B.confirm.assert_not_awaited()
    restart.assert_not_awaited()
    b.audit.assert_not_awaited()


def test_restart_fails_closed_when_the_fresh_list_disagrees(tmp_path, monkeypatch):
    b, cmds = cmd_bot(tmp_path)
    monkeypatch.setattr(B, "confirm", AsyncMock(return_value=True))
    b.containers = []  # nothing cached yet, so the fresh list decides
    for fresh in (aiohttp.ClientError("down"), [Container("c1", "db", "running", "")]):
        b.docker.list = AsyncMock(side_effect=fresh) if isinstance(fresh, Exception) else AsyncMock(return_value=fresh)
        c = ctx(OPER)
        asyncio.run(cmds["docker restart"].callback(c, "web"))
        c.edit.assert_awaited_once_with(content="`web` could not be confirmed. Nothing changed.")
    b.docker.restart.assert_not_awaited()
    b.audit.assert_not_awaited()


def test_restart_needs_confirm(tmp_path, monkeypatch):
    b, cmds = cmd_bot(tmp_path)
    monkeypatch.setattr(B, "confirm", AsyncMock(return_value=False))
    c = ctx(OPER)
    asyncio.run(cmds["docker restart"].callback(c, "web"))
    B.confirm.assert_awaited_once_with(c, "Restart `web`?")
    b.docker.restart.assert_not_awaited()
    b.audit.assert_not_awaited()


def test_restart_success_and_failure_are_audited(tmp_path, monkeypatch):
    b, cmds = cmd_bot(tmp_path)
    monkeypatch.setattr(B, "confirm", AsyncMock(return_value=True))
    c = ctx(OPER)
    asyncio.run(cmds["docker restart"].callback(c, "web"))
    b.docker.restart.assert_awaited_once_with("c1")
    c.edit.assert_awaited_once_with(content="Restarted `web`.")
    assert [a.args for a in b.audit.await_args_list] == [(c.author, "is restarting `web`"),
                                                         (c.author, "restarted `web`")]
    for err, text in ((aiohttp.ClientError("refused"), "refused"), (TimeoutError(), "")):
        b.docker.restart.side_effect = err
        c = ctx(OPER)
        asyncio.run(cmds["docker restart"].callback(c, "web"))
        c.edit.assert_awaited_once_with(content=B.FAILED)
        b.audit.assert_awaited_with(c.author, f"restart of `web` FAILED: {text}")


def test_restart_refuses_when_the_audit_post_fails(tmp_path, monkeypatch):
    b, cmds = cmd_bot(tmp_path)
    monkeypatch.setattr(B, "confirm", AsyncMock(return_value=True))
    b.audit.return_value = False
    c = ctx(OPER)
    asyncio.run(cmds["docker restart"].callback(c, "web"))
    c.edit.assert_awaited_once_with(content=B.NO_AUDIT)
    b.docker.restart.assert_not_awaited()


# --- confirm view ---

def press(index: int | None, user: int = OPER):
    async def respond(prompt, view, ephemeral):
        assert ephemeral
        hit = SimpleNamespace(user=SimpleNamespace(id=user), response=SimpleNamespace(edit_message=AsyncMock()))
        if index is None:
            view._dispatch_timeout()  # py-cord's own timeout path: no button was pressed
        else:
            assert await view.interaction_check(hit)
            await view.children[index].callback(hit)
            hit.response.edit_message.assert_awaited_once()
    return respond


def test_only_the_panel_channel_is_answered(tmp_path, monkeypatch):
    seen = []

    async def base(self, interaction, auto_sync=None):
        seen.append(interaction.channel_id)
    monkeypatch.setattr(discord.Bot, "process_application_commands", base)
    b = make_bot(tmp_path)
    for ch in (PANEL, PANEL + 1):
        asyncio.run(b.process_application_commands(SimpleNamespace(channel_id=ch, guild_id=GUILD)))
    assert seen == [PANEL]


def test_commands_are_answered_in_each_output_and_the_commands_channel(tmp_path, monkeypatch):
    seen = []
    async def base(self, interaction, auto_sync=None):
        seen.append(interaction.channel_id)
    monkeypatch.setattr(discord.Bot, "process_application_commands", base)
    b = make_bot(tmp_path, outputs=[Output(41), Output(42)], commands_channel_id=43)
    for target in (PANEL, 41, 42, 43, 44):
        asyncio.run(b.process_application_commands(SimpleNamespace(channel_id=target, guild_id=GUILD)))
    asyncio.run(b.process_application_commands(SimpleNamespace(channel_id=41, guild_id=666)))
    assert seen == [41, 42, 43]


def test_output_never_posts_to_a_channel_in_another_guild(tmp_path):
    b = make_bot(tmp_path, outputs=[Output(PANEL)])
    ch = channel()
    ch.guild.id = 666
    wire(b, ch)
    asyncio.run(b.refresh())
    ch.send.assert_not_awaited()


def test_output_formats_sections_and_periods_are_independent(tmp_path):
    outputs = [Output(PANEL, {"system": "embed", "apps": "text"}, ["system", "apps"], 600, 30),
               Output(21, {"history": "text"}, ["history"])]
    b = make_bot(tmp_path, outputs=outputs, order=["apps", "system", "history"], image_every=180, edit_every=45)
    async def go():
        await sampled(b)
        first = await b.build()
        second = b._fit(b.boards[1], b._sections)
        return first, second
    first, second = asyncio.run(go())
    assert titles(first) == ["Top Apps", "Unraid System Info"]
    assert len(second) == 1 and second[0].title.startswith("History, last ")
    assert b.main.modes == {"apps": "text", "system": "embed"} and b.boards[1].modes == {"history": "text"}
    assert [(o.image_every, o.edit_every) for o in b.boards] == [(600, 30), (180, 45)]
    b.main.render["system"] = "text"
    assert outputs[0].render["system"] == "embed" and "system" not in b.boards[1].render


def test_output_with_no_available_sections_keeps_a_nonempty_message(tmp_path):
    b = make_bot(tmp_path, outputs=[Output(PANEL, sections=["disks"])])
    embeds = asyncio.run(b.build())
    assert [e.description for e in embeds] == ["No data is available for the selected sections."]
    assert b.image_keys() == ()
    embeds, files = asyncio.run(b.status_reply())
    assert len(embeds) == 1 and not files


def test_history_can_be_a_standalone_output_or_precede_system(tmp_path):
    b = make_bot(tmp_path, outputs=[Output(PANEL, sections=["history"])], order=["history", "system"])
    async def go():
        await sampled(b)
        embeds = await b.build()
        files = await b.image_files()
        assert len(embeds) == 1 and embeds[0].title.startswith("History, last ")
        assert [f.filename for f in files] == ["history.png"]
        b.main.sections = ["system", "history"]
        embeds = await b.build()
        files = await b.image_files()
        assert titles(embeds) == ["History, last 30m", "Unraid System Info"]
        assert [f.filename for f in files] == ["history.png", "system.png"]
        assert files[0].fp.read(8) == b"\x89PNG\r\n\x1a\n"
    asyncio.run(go())


def test_each_output_edits_on_its_own_wall_clock_period(tmp_path, monkeypatch):
    start = 1_800_000_000
    clock = [start + 1]
    monkeypatch.setattr(B.time, "time", lambda: clock[0])
    b = make_bot(tmp_path, outputs=[Output(PANEL, edit_every=30), Output(21, edit_every=120)])
    b._snap = Snapshot()
    stub_render(b, keys=())
    msgs = {cid: edited(MagicMock(id=cid)) for cid in (PANEL, 21)}
    chans = {cid: channel() for cid in msgs}
    for cid, ch in chans.items():
        ch.send.return_value = msgs[cid]
    monkeypatch.setattr(b, "get_channel", chans.get)
    async def go():
        for elapsed in (1, 29, 30, 30, 60, 120, 135):
            clock[0] = start + elapsed
            await b.panel_loop.coro(b)
    asyncio.run(go())
    assert all(ch.send.await_count == 1 for ch in chans.values())
    assert msgs[PANEL].edit.await_count == 3 and msgs[21].edit.await_count == 1


def test_saved_outputs_restore_by_channel_and_wait_for_their_own_period(tmp_path, monkeypatch):
    start = 1_800_000_000
    clock = [start + 45]
    monkeypatch.setattr(B.time, "time", lambda: clock[0])
    state = tmp_path / "data" / "state.json"
    state.parent.mkdir()
    state.write_text(json.dumps({"format": 1, "messages": {str(PANEL): 5, "21": 6}}))
    b = make_bot(tmp_path, outputs=[Output(21, edit_every=120), Output(PANEL, edit_every=30)])
    assert b.main.channel_id == 21 and [o.message_id for o in b.boards] == [6, 5]
    b._snap = Snapshot()
    stub_render(b, keys=())
    msgs = {cid: edited(MagicMock(id=cid)) for cid in (PANEL, 21)}
    chans = {cid: channel() for cid in msgs}
    for cid, ch in chans.items():
        ch.fetch_message.return_value = msgs[cid]
    monkeypatch.setattr(b, "get_channel", chans.get)
    async def go():
        await b.panel_loop.coro(b)
        assert not any(m.edit.await_count for m in msgs.values())
        clock[0] = start + 60
        await b.panel_loop.coro(b)
        assert not any(m.edit.await_count for m in msgs.values())
        clock[0] = start + 75
        await b.panel_loop.coro(b)
        assert msgs[PANEL].edit.await_count == 1 and not msgs[21].edit.await_count
        clock[0] = start + 120
        await b.panel_loop.coro(b)
    asyncio.run(go())
    assert msgs[PANEL].edit.await_count == 2 and msgs[21].edit.await_count == 1
    assert not any(ch.send.await_count for ch in chans.values())


def test_outputs_share_the_upload_floor_and_slow_edits_do_not_starve_images(tmp_path, monkeypatch):
    start = 1_800_000_000
    clock = [start + 1]
    monkeypatch.setattr(B.time, "time", lambda: clock[0])
    monkeypatch.setattr(B.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(prep, "hold", AsyncMock())  # a frozen monotonic clock never ends a real sleep
    b = make_bot(tmp_path, outputs=[Output(PANEL, image_every=60, edit_every=5),
                                   Output(21, image_every=1800, edit_every=600)])
    b._snap = Snapshot()
    stub_render(b)
    msgs = {cid: edited(MagicMock(id=cid)) for cid in (PANEL, 21)}
    chans = {cid: channel() for cid in msgs}
    for cid, ch in chans.items():
        ch.send.return_value = msgs[cid]
    monkeypatch.setattr(b, "get_channel", chans.get)
    async def go():
        await b.panel_loop.coro(b)
        assert chans[PANEL].send.await_args.kwargs["files"] == ["F"]
        chans[21].send.assert_not_awaited()
        for clock[0] in (start + 4, start + IMAGE_FLOOR - 1):
            await b.panel_loop.coro(b)
            chans[21].send.assert_not_awaited()
        clock[0] = start + IMAGE_FLOOR + B.TICK
        await b.panel_loop.coro(b)
        assert chans[21].send.await_args.kwargs["files"] == ["F"]
        msgs[PANEL].edit.assert_not_awaited()
    asyncio.run(go())


def test_saved_output_loads_image_modes_before_its_slow_text_edit(tmp_path, monkeypatch):
    start = 1_800_000_000
    clock = [start + 45]
    monkeypatch.setattr(B.time, "time", lambda: clock[0])
    monkeypatch.setattr(B.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(prep, "hold", AsyncMock())  # a frozen monotonic clock never ends a real sleep
    state = tmp_path / "data" / "state.json"
    state.parent.mkdir()
    state.write_text(json.dumps({"format": 1, "messages": {str(PANEL): 5}, "upload_at": start + 30}))
    b = make_bot(tmp_path, outputs=[Output(PANEL, sections=["history"], image_every=60, edit_every=600)])
    b._snap = Snapshot()
    send = AsyncMock()
    monkeypatch.setattr(b, "_refresh", send)
    async def go():
        await b.panel_loop.coro(b)
        assert b.image_keys() == ("history",)
        send.assert_not_awaited()
        clock[0] = start + 105
        await b.panel_loop.coro(b)
        send.assert_awaited_once()
    asyncio.run(go())


def test_new_section_appears_on_image_period_without_waiting_for_slow_edits(tmp_path, monkeypatch):
    start = 1_800_000_000
    clock = [start + 1]
    monkeypatch.setattr(B.time, "time", lambda: clock[0])
    monkeypatch.setattr(B.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(prep, "hold", AsyncMock())  # a frozen monotonic clock never ends a real sleep
    b = make_bot(tmp_path, outputs=[Output(PANEL, sections=["services"], image_every=60, edit_every=600)],
                 services=[Service("Outside", tcp="host:22")])
    b._snap = Snapshot()
    msg = edited(MagicMock(id=777))
    ch = channel()
    ch.send.return_value = msg
    wire(b, ch)
    async def go():
        await b.panel_loop.coro(b)
        assert ch.send.await_args.kwargs["embeds"][0].description == (
            "No data is available for the selected sections.")
        assert ch.send.await_args.kwargs["files"] == []
        clock[0] = start + 60
        await b.panel_loop.coro(b)
        msg.edit.assert_not_awaited()
        b.svc_results = [Result(panel.FINE, "", "1ms", "N/A", "N/A")]
        clock[0] = start + 120
        await b.panel_loop.coro(b)
        assert [f.filename for f in msg.edit.await_args.kwargs["files"]] == ["services.png"]
        assert msg.edit.await_args.kwargs["embeds"][0].title == "Services"
        upload = b._upload_at
        b.svc_results = []
        await b.refresh()
        assert msg.edit.await_args.kwargs["files"] == [] and msg.edit.await_args.kwargs["attachments"] == []
        assert b._upload_at == upload
    asyncio.run(go())


def test_output_footer_and_private_status_follow_primary_output_options(tmp_path, monkeypatch):
    now = 1_800_000_001
    monkeypatch.setattr(B.time, "time", lambda: now)
    b = make_bot(tmp_path, outputs=[Output(PANEL, {"system": "text"}, ["system"], edit_every=30),
                                   Output(21, {"system": "embed"}, ["system"], edit_every=120)])
    sections = asyncio.run(b.collect())
    first = b._fit(b.main, sections)[0]
    second = b._fit(b.boards[1], sections)[0]
    assert f"Update at {B.view.clock(b._boundary(now, 30), b.tz)}" in first.footer.text
    assert f"Update at {B.view.clock(B.delivery.edit_boundary(b, b.boards[1], now), b.tz)}" in second.footer.text
    embeds, files = asyncio.run(b.status_reply())
    assert len(embeds) == 1 and not files and embeds[0].description
    b._upload_at = B.time.monotonic()
    embeds, files = asyncio.run(b.status_reply())
    assert len(embeds) == 1 and not files and embeds[0].fields


def test_confirm_buttons_and_timeout():
    for index, want in ((0, True), (1, False), (None, False)):
        c = ctx(OPER)
        c.respond.side_effect = press(index)
        assert asyncio.run(B.confirm(c, "Sure?")) is want
        assert c.edit.await_count == (index is None)


def test_confirm_only_the_asker_may_press():
    async def go():
        v = B.Confirm(OPER)
        assert [b.label for b in v.children] == ["Confirm", "Cancel"]
        for user in (SimpleNamespace(id=STRANGER), None):
            hit = SimpleNamespace(user=user, response=SimpleNamespace(send_message=AsyncMock()))
            assert not await v.interaction_check(hit)
            hit.response.send_message.assert_awaited_once_with("This prompt is not yours.", ephemeral=True)
        assert await v.interaction_check(SimpleNamespace(user=SimpleNamespace(id=OPER)))
        for done in (False, True):
            hit = SimpleNamespace(response=SimpleNamespace(is_done=lambda d=done: d, send_message=AsyncMock()),
                                  followup=SimpleNamespace(send=AsyncMock()))
            await v.on_error(ValueError("x"), v.children[0], hit)
            (hit.followup.send if done else hit.response.send_message).assert_awaited_once_with(
                B.FAILED, ephemeral=True)
        hit.followup.send.side_effect = discord.HTTPException(MagicMock(status=404), "gone")
        await v.on_error(ValueError("x"), v.children[0], hit)
        assert v.is_finished()
    asyncio.run(go())


def test_a_failed_confirm_press_is_not_consent():
    async def go():
        v = B.Confirm(OPER)
        hit = SimpleNamespace(user=SimpleNamespace(id=OPER),
                              response=SimpleNamespace(edit_message=AsyncMock(side_effect=discord.HTTPException(
                                  MagicMock(status=404), "gone"))))
        try:
            await v.children[0].callback(hit)
        except discord.HTTPException:
            pass
        assert v.ok is None and not v.is_finished()
    asyncio.run(go())


def test_a_failed_command_is_logged_and_answered(tmp_path, caplog):
    b = make_bot(tmp_path)
    for fails in (False, True):
        c = ctx(OPER)
        if fails:
            c.respond.side_effect = discord.HTTPException(MagicMock(status=404), "expired")
        asyncio.run(b.on_application_command_error(c, discord.DiscordException("boom")))
        c.respond.assert_awaited_once_with(B.FAILED, ephemeral=True)
    assert caplog.text.count("failed") == 2


def test_close_closes_the_http_session(tmp_path, monkeypatch):
    closed = []

    async def base(self):
        closed.append(True)
    monkeypatch.setattr(discord.Bot, "close", base)
    b = make_bot(tmp_path)
    asyncio.run(b.close())
    b.http_session = MagicMock(close=AsyncMock())
    b._svc_task = MagicMock()
    monitor = MagicMock(close=AsyncMock())
    b.forges = [monitor]
    asyncio.run(b.close())
    monitor.close.assert_awaited_once_with()
    b.http_session.close.assert_awaited_once_with()
    b._svc_task.cancel.assert_called_once_with()
    assert closed == [True, True]


def test_forges_feed_alerts_and_follow_output_selection_modes_and_order(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_FORGE_TOKEN", TOKEN)
    setting = forges.Settings("forgejo", "https://forge.example", "TEST_FORGE_TOKEN", ("owner/repo",), ("ci",))
    b = make_bot(tmp_path, forges=[setting], order=["forges", "system"])
    b._check_release = AsyncMock()
    b.http_session = MagicMock()
    b.http_session.get.return_value = Response({"total_count": 1,
                                               "workflow_runs": [{"status": "failure", "updated": DATE}]})

    async def go():
        sections = await b.collect()
        again = await b.collect()
        assert [s.key for s in sections] == [s.key for s in again]
        return sections

    sections = asyncio.run(go())
    assert sections[0].key == "forge_0"
    b.http_session.get.assert_called_once()
    issue = sections[0].issues[0]
    assert issue[0] == panel.CRITICAL and "owner/repo fail" in issue[1]
    system = next(s for s in sections if s.key == "system")
    assert issue[1] in str(system)
    b.main.sections = ["forges"]
    for mode in ("text", "embed", "image"):
        b.main.render = {"forges": mode}
        embeds = b._fit(b.main, sections)
        assert len(embeds) == 1 and embeds[0].title == sections[0].title
        assert b.main.modes == {"forge_0": mode}
        assert TOKEN not in str(embeds[0].to_dict())
    assert b._wants_images(b.main)
    b.forges = []
    b.main.modes = {}
    assert not b._wants_images(b.main)


# --- entry points ---

def test_main(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(f"config_format = 1\nguild_id = {GUILD}\nstate_file = \"{tmp_path}/s.json\"\n"
                    f"[[outputs]]\nchannel = {PANEL}\n")
    monkeypatch.setenv("SHAZZZBOT_CONFIG", str(path))
    monkeypatch.setenv("DISCORD_TOKEN", "secret")
    seen = []
    monkeypatch.setattr(B.Bot, "run", lambda self, token: seen.append((self, token)))
    B.main()
    (b, token), = seen
    assert token == "secret" and b.cfg.guild_id == GUILD and "docker restart" in commands(b)


@pytest.mark.parametrize("header", ["", "config_format = 2\n"])
def test_main_exits_cleanly_on_an_unsupported_config_format(tmp_path, monkeypatch, header):
    path = tmp_path / "config.toml"
    path.write_text(f"{header}guild_id = {GUILD}\n[[outputs]]\nchannel = {PANEL}\n")
    monkeypatch.setenv("SHAZZZBOT_CONFIG", str(path))
    monkeypatch.setenv("DISCORD_TOKEN", "secret")
    with pytest.raises(SystemExit) as e:
        B.main()
    assert str(e.value.code).startswith(f"{path}: ")

def test_dunder_main_calls_main(monkeypatch):
    main = MagicMock()
    monkeypatch.setattr(B, "main", main)
    monkeypatch.delenv("SHAZZZBOT_ROLE", raising=False)
    runpy.run_module("shazzzbot", run_name="__main__")
    main.assert_called_once_with()


def test_dunder_main_runs_the_relay_role(monkeypatch):
    main = MagicMock()
    monkeypatch.setattr(relay, "main", main)
    monkeypatch.setenv("SHAZZZBOT_ROLE", "relay")
    runpy.run_module("shazzzbot", run_name="__main__")
    main.assert_called_once_with()


def test_dunder_main_refuses_an_unknown_role(monkeypatch):
    monkeypatch.setenv("SHAZZZBOT_ROLE", "evil")
    with pytest.raises(SystemExit, match="must be bot or relay"):
        runpy.run_module("shazzzbot", run_name="__main__")


def test_service_checks_use_the_relay_when_configured(tmp_path, monkeypatch):
    b = make_bot(tmp_path, services=[Service("A", public_url="https://a/")],
                 relays=[relay.Relay("https://r", "k", "0" * 64)])
    b.http_session = MagicMock(get=MagicMock(return_value=release_reply(404, {})))
    seen = []

    async def ping(http, r):
        return panel.FINE, 5.0, "", B.__version__
    monkeypatch.setattr(relay, "ping", ping)

    async def probe_via(http, r, url, expect):
        seen.append((http, r, url, expect))
        return True, "1ms", 1.0

    got = []

    async def check_all(svcs, cs, http, remote, **kwargs):
        got.append(await remote("https://a/", ""))
        return []
    monkeypatch.setattr(relay, "probe_via", probe_via)
    monkeypatch.setattr(services, "check_all", check_all)

    async def go():
        await b.build()
        await b._svc_task
    asyncio.run(go())
    assert seen == [(b.http_session, b.cfg.relays[0], "https://a/", "")] and got == [(True, "1ms", 1.0)]


def test_services_and_external_checks_share_one_section_and_both_feed_alerts(tmp_path):
    svcs = [Service("Internal", "web"), Service("Outside", tcp="host:22")]
    b = make_bot(tmp_path, services=svcs, outputs=[Output(PANEL, {"services": "text"})])
    b.svc_results = [Result(panel.CRITICAL, "missing", "N/A", "N/A", "N/A", "container missing"),
                     Result(panel.WARNING, "", "2000ms", "N/A", "N/A", "slow")]
    sections = asyncio.run(b.collect())
    keys = [s.key for s in sections]
    assert "external" not in keys and keys.count("services") == 1
    combined = next(s for s in sections if s.key == "services")
    assert combined.parts[0].rows[1][1][0] == "Internal" and len(combined.parts[0].rows) == 2
    external = next(p for p in combined.parts if p.heading == "External Checks" and p.only == "image")
    assert external.rows[1][1][0] == "Outside" and external.icons == ["", "dot1"]
    alerts = next(r for r in b._system if r.label == "Alerts")
    assert list(alerts.issues) == [(panel.CRITICAL, "Internal container missing"), (panel.WARNING, "Outside slow")]
    embeds = b._fit(b.main, sections)
    services_embed = next(e for e in embeds if e.title == "Services")
    assert "External Checks" in services_embed.description
    assert b.main.modes["services"] == "text" and "external" not in b.main.modes
    b.main.render = {}
    b._fit(b.main, sections)
    assert b.main.modes["services"] == "image"


def test_container_schedules_feed_tasks_and_system_with_ignore_and_rename(tmp_path, monkeypatch):
    b = make_bot(tmp_path, timezone="America/Los_Angeles",
                 tasks_rename={"auto-updater": "Container maintenance"})
    b._read_tasks = AsyncMock(return_value={"generated": NOW.timestamp(), "jobs": []})
    b.containers = [Container("w", "auto-updater", "running", "Up", image="watchtower",
                              command="/watchtower --schedule '0 0 22 * * *'")]
    monkeypatch.setattr(B, "datetime", Frozen)
    monkeypatch.setattr(jobs, "datetime", Frozen)
    sections = asyncio.run(b.collect())
    tasks = next(s for s in sections if s.key == "tasks")
    assert "Container maintenance" in str(tasks.parts[-1].rows)
    assert "Container maintenance" in next(r.text for r in b._system if r.label == "Tasks")
    b.cfg.tasks_ignore = ["Container maintenance"]
    sections = asyncio.run(b.collect())
    tasks = next(s for s in sections if s.key == "tasks")
    assert tasks.parts[-1].rows == [(panel.FINE, ("none",))]
    assert next(r.text for r in b._system if r.label == "Tasks") == "idle"


def test_completed_task_durations_persist_and_are_not_rewritten_or_guessed(tmp_path, monkeypatch):
    data = {"generated": NOW.timestamp(), "jobs": [{"name": "backup", "cron": "0 0 * * *"}],
            "durations": {"backup": {"at": NOW.timestamp() - 60, "seconds": 3900},
                          "invalid": {"at": True, "seconds": "bad"}}}
    monkeypatch.setattr(B, "datetime", Frozen)
    monkeypatch.setattr(jobs, "datetime", Frozen)
    b = make_bot(tmp_path, tasks_rename={"backup": "Nightly backup"})
    b._read_tasks = AsyncMock(return_value=data)
    b._save_state = MagicMock(wraps=b._save_state)
    sections = asyncio.run(b.collect())
    tasks = next(s for s in sections if s.key == "tasks")
    assert tasks.parts[-1].rows[0][1][1:] == ("Nightly backup", "(last took 1h05m)")
    b._save_state.assert_called_once_with()
    asyncio.run(b.collect())
    b._save_state.assert_called_once_with()
    stored = json.loads((tmp_path / "data" / "state.json").read_text())
    assert stored["task_times"] == {"backup": {"at": NOW.timestamp() - 60, "seconds": 3900.0}}
    restarted = make_bot(tmp_path, tasks_rename={"backup": "Renamed"})
    restarted._read_tasks = AsyncMock(return_value=data)
    data.pop("durations")
    sections = asyncio.run(restarted.collect())
    tasks = next(s for s in sections if s.key == "tasks")
    assert tasks.parts[-1].rows[0][1][1:] == ("Renamed", "(last took 1h05m)")
    restarted._read_tasks.return_value = None
    asyncio.run(restarted.collect())
    assert restarted._task_times == stored["task_times"]
    data["durations"] = {"backup": {"at": NOW.timestamp() - 30, "seconds": None}}
    restarted._read_tasks.return_value = data
    sections = asyncio.run(restarted.collect())
    tasks = next(s for s in sections if s.key == "tasks")
    assert tasks.parts[-1].rows[0][1][1:] == ("Renamed", "")
    assert make_bot(tmp_path)._task_times["backup"]["seconds"] is None


def test_task_duration_save_failure_is_retried_on_the_next_collection(tmp_path):
    import pytest

    b = make_bot(tmp_path)
    b._read_tasks = AsyncMock(return_value={"durations": {"backup": {"at": 1, "seconds": 45}}})
    save = b._save_state
    b._save_state = MagicMock(side_effect=OSError("disk full"))
    with pytest.raises(OSError, match="disk full"):
        asyncio.run(b.collect())
    assert b._task_times == {}
    b._save_state = save
    asyncio.run(b.collect())
    assert make_bot(tmp_path)._task_times == {"backup": {"at": 1.0, "seconds": 45.0}}


def test_service_check_cache_belongs_to_each_bot_and_retains_last_results(tmp_path, monkeypatch):
    svcs = [Service("Box", "web"), Service("External", tcp="host:22", every=120)]
    b = make_bot(tmp_path, services=svcs)
    now = [0.0]
    monkeypatch.setattr(services.time, "monotonic", lambda: now[0])
    fine = Result(panel.FINE, "", "1ms", "N/A", "N/A")
    failed = Result(panel.CRITICAL, "", "refused", "N/A", "N/A", "local refused")
    check = AsyncMock(return_value=fine)
    monkeypatch.setattr(services, "check", check)
    async def go():
        await b._check_services(MagicMock())
        assert b.svc_results == [fine, fine] and check.await_count == 2
        check.return_value = failed
        now[0] = B.SERVICES_EVERY
        await b._check_services(MagicMock())
        assert b.svc_results == [failed, fine] and check.await_count == 3
        now[0] = 2 * B.SERVICES_EVERY
        await b._check_services(MagicMock())
        assert b.svc_results == [failed, failed] and check.await_count == 5
        other = make_bot(tmp_path, services=svcs)
        await other._check_services(MagicMock())
        assert other._svc_cache is not b._svc_cache and check.await_count == 7
    asyncio.run(go())


def test_public_tcp_checks_use_any_relay_and_never_fall_back_locally(tmp_path, monkeypatch):
    rs = [relay.Relay("https://a", "k", "0" * 64), relay.Relay("https://b", "k", "0" * 64)]
    b = make_bot(tmp_path, services=[Service("SSH", public_tcp="host:22")], relays=rs)
    monkeypatch.setattr(relay, "ping", AsyncMock(return_value=(panel.FINE, 1.0, "", B.__version__)))
    direct = AsyncMock()
    monkeypatch.setattr(services, "probe_tcp", direct)
    rounds = [[[(False, "timeout", 0.0)], [(True, "2ms", 2.0)]],
              [[(False, "closed", 0.0)], None], [None, None]]
    async def ask(http, r, targets):
        assert targets == [{"tcp": "host:22"}]
        return rounds[0][rs.index(r)]
    monkeypatch.setattr(relay, "ask", ask)
    async def go():
        await b._check_services(MagicMock())
        assert b.svc_results[0].level == panel.FINE and b.svc_results[0].public == "2ms"
        b._svc_cache.clear()
        rounds.pop(0)
        await b._check_services(MagicMock())
        assert b.svc_results[0].level == panel.CRITICAL and b.svc_results[0].why == "public closed"
        b._svc_cache.clear()
        rounds.pop(0)
        await b._check_services(MagicMock())
        assert b.svc_results[0].level == panel.WARNING and b.svc_results[0].why == "no relay"
    asyncio.run(go())
    direct.assert_not_awaited()


def test_published_ports_are_checked_through_the_relay(tmp_path, monkeypatch):
    b = make_bot(tmp_path, public_host="x.example.com", relays=[relay.Relay("https://r", "k", "0" * 64)])
    b.http_session = MagicMock()

    async def ping(http, r):
        return panel.FINE, 5.0, "", B.__version__
    monkeypatch.setattr(relay, "ping", ping)
    b.containers = [Container("1", "web", "running", "Up", ports=(80, 443)), Container("2", "db", "running", "Up")]
    answers = [[443], None]

    async def open_ports(http, r, host, ports):
        assert (host, sorted(ports)) == ("x.example.com", [80, 443])
        return answers.pop(0)
    monkeypatch.setattr(relay, "open_ports", open_ports)

    async def go():
        await b._check_services(b.http_session)
        first = b.exposed
        await b._check_services(b.http_session)
        return first
    assert asyncio.run(go()) == [("web", 443)] and b.exposed is None


def test_nodes_are_the_bot_plus_each_relay_and_changes_are_logged(tmp_path, monkeypatch, caplog):
    rs = [relay.Relay("https://a:8443", "k", "0" * 64), relay.Relay("https://b:8443", "k", "0" * 64)]
    b = make_bot(tmp_path, relays=rs)
    b.http_session = MagicMock(get=MagicMock(return_value=release_reply(404, {})))
    rounds = [{rs[0]: (panel.FINE, 10.0, "", B.__version__), rs[1]: (panel.WARNING, 0.0, "unreachable (x)", None)},
              {rs[0]: (panel.FINE, 20.0, "", B.__version__), rs[1]: (panel.FINE, 40.0, "", B.__version__)}]

    async def ping(http, r):
        return rounds[0][r]
    monkeypatch.setattr(relay, "ping", ping)
    monkeypatch.setattr(services, "check_all", AsyncMock(return_value=[]))
    monkeypatch.setattr(B.Bot, "latency", property(lambda _: 0.03))

    async def go():
        await b.build()
        first = b._status
        await b._svc_task
        await b.build()
        second = b._status
        rounds.pop(0)
        await b._check_services(b.http_session)
        await b.build()
        return first, second, b._status
    with caplog.at_level(logging.INFO, "shazzzbot"):
        first, second, third = asyncio.run(go())
    assert first == second == (panel.WARNING, "2 of 3 nodes ok (see logs)")  # the first build waits for the check
    assert third == (panel.FINE, "3 nodes ok (~30ms)")
    assert "relay b:8443: unreachable (x)" in caplog.text and "relay b:8443: ok" in caplog.text
    assert "relay a:8443" not in caplog.text
    monkeypatch.setattr(B.Bot, "latency", property(lambda _: float("nan")))
    asyncio.run(b.build())
    assert b._status == (panel.FINE, "3 nodes ok (~30ms)")


def test_blowup_unconfigured_and_denied(tmp_path):
    b, cmds = cmd_bot(tmp_path)
    c = ctx(OPER)
    asyncio.run(cmds["blowup"].callback(c))
    c.respond.assert_awaited_once_with("Blowup is not configured.", ephemeral=True)
    c.defer.assert_not_awaited()
    b.audit.assert_not_awaited()
    b.cfg.blowup_user_id = 42
    for user, guild in ((STRANGER, GUILD), (OPER, GUILD + 1)):
        c = ctx(user, guild=guild)
        asyncio.run(cmds["blowup"].callback(c, "help"))
        c.respond.assert_awaited_once_with("Not allowed.", ephemeral=True)
        c.defer.assert_not_awaited()
    b.audit.assert_not_awaited()


def test_blowup_sends_five_times_with_only_owner_mentions(tmp_path, monkeypatch):
    b, cmds = cmd_bot(tmp_path)
    b.cfg.blowup_user_id = 42
    sleep = AsyncMock()
    monkeypatch.setattr(B.asyncio, "sleep", sleep)
    c = ctx(OPER)
    c.author.display_name = "Caller @everyone"
    c.channel = channel()
    reason = "@everyone <@77> <@&88> " + "x" * 210
    events = []

    async def defer(**kwargs):
        assert kwargs == {"ephemeral": True}
        events.append("defer")

    async def audit(user, text):
        assert user is c.author and text == f"requested blowup: {reason[:200]}"
        events.append("audit")
        return True

    async def respond(text, **kwargs):
        assert text == "Sending five attention pings." and kwargs == {"ephemeral": True}
        events.append("respond")

    async def send(text, allowed_mentions):
        assert text == f"<@42> -- Caller @everyone: {reason[:200]}"
        assert allowed_mentions.to_dict()["users"] == [42]
        assert allowed_mentions.to_dict()["parse"] == []
        assert allowed_mentions.replied_user is False
        events.append("send")

    c.defer.side_effect = defer
    c.respond.side_effect = respond
    c.channel.send.side_effect = send
    b.audit.side_effect = audit
    asyncio.run(cmds["blowup"].callback(c, reason))
    assert events == ["defer", "audit", "respond"] + ["send"] * 5
    assert c.channel.send.await_count == 5
    assert [call.args for call in sleep.await_args_list] == [(2,)] * 4


def test_blowup_default_reason_and_global_cooldown(tmp_path, monkeypatch):
    b, cmds = cmd_bot(tmp_path)
    b.cfg.blowup_user_id = 42
    monkeypatch.setattr(B.asyncio, "sleep", AsyncMock())
    clock = [100.0]
    monkeypatch.setattr(B.time, "monotonic", lambda: clock[0])
    first = ctx(OPER)
    first.author.display_name = "Caller"
    first.channel = channel()
    asyncio.run(cmds["blowup"].callback(first))
    assert first.channel.send.await_args.args == ("<@42> -- Caller: ",)
    clock[0] = 101.1
    b.cfg.perms = {"operate": {"users": [OPER, VIEWER]}}
    second = ctx(VIEWER)
    second.channel = channel()
    asyncio.run(cmds["blowup"].callback(second, "again"))
    second.respond.assert_awaited_once_with("Blowup is on cooldown for 599 seconds.", ephemeral=True)
    second.channel.send.assert_not_awaited()
    second.defer.assert_not_awaited()
    b.audit.assert_awaited_once()
    clock[0] = 700.0
    second.author.display_name = "Another caller"
    asyncio.run(cmds["blowup"].callback(second, "again"))
    assert second.channel.send.await_count == 5 and b.audit.await_count == 2


def test_blowup_fails_closed_without_audit_or_channel(tmp_path):
    b, cmds = cmd_bot(tmp_path)
    b.cfg.blowup_user_id = 42
    wire(b)
    c = ctx(OPER)
    asyncio.run(cmds["blowup"].callback(c))
    c.respond.assert_awaited_once_with(B.NO_AUDIT, ephemeral=True)
    c.defer.assert_not_awaited()
    wire(b, audit_ch=channel())
    c = ctx(OPER)
    c.channel = None
    asyncio.run(cmds["blowup"].callback(c))
    c.respond.assert_awaited_once_with(B.FAILED, ephemeral=True)
    b.audit.assert_not_awaited()
    b.audit.return_value = False
    c = ctx(OPER)
    c.channel = channel()
    asyncio.run(cmds["blowup"].callback(c, "help"))
    c.defer.assert_awaited_once_with(ephemeral=True)
    c.respond.assert_awaited_once_with(B.NO_AUDIT, ephemeral=True)
    c.channel.send.assert_not_awaited()


@pytest.mark.parametrize("blocked", [False, True])
def test_blowup_discord_error_stops_sequence(tmp_path, monkeypatch, caplog, blocked):
    b, cmds = cmd_bot(tmp_path)
    b.cfg.blowup_user_id = 42
    sleep = AsyncMock()
    monkeypatch.setattr(B.asyncio, "sleep", sleep)
    c = ctx(OPER)
    c.author.display_name = "Caller"
    c.channel = channel()
    error = B.discordauth.Blocked() if blocked else discord.HTTPException(MagicMock(status=403), "forbidden")
    c.channel.send.side_effect = [None, error]
    asyncio.run(cmds["blowup"].callback(c, "help"))
    assert c.channel.send.await_count == 2
    sleep.assert_awaited_once_with(2)
    assert "blowup send failed" in caplog.text
    c.respond.assert_awaited_with(B.FAILED, ephemeral=True)
    b.audit.assert_awaited_once_with(c.author, "requested blowup: help")


def test_upload_budget_survives_a_restart(tmp_path):
    import time as t
    b = make_bot(tmp_path)
    b._upload_wall = t.time() - 100
    b._save_state()
    again = make_bot(tmp_path)
    assert 90 < t.monotonic() - again._upload_at < 200


def test_a_timed_out_upload_preserves_the_unknown_outcome_without_a_second_send(tmp_path, monkeypatch):
    b = make_bot(tmp_path)
    monkeypatch.setattr(B, "IMAGE_WAIT", 0.01)
    calls = []

    async def send(**kw):
        calls.append(sorted(kw))
        if "files" in kw:
            await asyncio.sleep(1)
        return MagicMock()
    with pytest.raises(TimeoutError):
        asyncio.run(b._send(b.main, send, [], [MagicMock()], ("x",), edit=False))
    assert calls == [["embeds", "files"]] and b.main.image_wanted and b.main.image_slot is None


def test_slots_follow_local_wall_clock_across_dst_end(tmp_path, monkeypatch):
    b = make_bot(tmp_path, timezone="America/Los_Angeles")
    before, after = 1793523600 - 60, 1793523600 + 60  # 01:59 PDT, then 01:01 PST, as DST ends
    monkeypatch.setattr(B.time, "time", lambda: before)
    first = b._slot(60)
    monkeypatch.setattr(B.time, "time", lambda: after)
    assert b._slot(60) != first and b._slot(300) == b._slot(300)


def test_upload_completion_reserves_a_full_floor_after_a_slow_send(tmp_path, monkeypatch):
    b = make_bot(tmp_path, timezone="UTC")
    b._sections = [panel.Section("history", "H", [])]
    b.main.modes = {"history": "image"}
    clock = [1800000007.0, 1000.0]
    monkeypatch.setattr(B.time, "time", lambda: clock[0])
    monkeypatch.setattr(B.time, "monotonic", lambda: clock[1])
    b._save_state = MagicMock()
    other = B.Board(21, modes={"history": "image"})
    b.boards.append(other)
    msg = MagicMock()

    async def send(**kwargs):
        assert b._upload_wall == clock[0] and b._upload_at == clock[1]
        assert not b._due(other, False)
        clock[0] += 100.0
        clock[1] += 100.0
        return msg

    async def go():
        assert await b._send(b.main, send, [], [MagicMock()], ("history",), edit=False) is msg
    asyncio.run(go())
    expected = B.math.ceil(clock[0] / B.TICK) * B.TICK
    assert b._upload_wall == expected
    assert b._upload_at == clock[1] + expected - clock[0]
    assert not b._due(other, False)
    plan = b.schedule(clock[0])
    assert min(times[0] for times in plan.values()) == expected + IMAGE_FLOOR
    assert expected + IMAGE_FLOOR - clock[0] >= IMAGE_FLOOR
    assert b.main.image_slot == b._slot(b.main.image_every)
    elapsed = expected + IMAGE_FLOOR - clock[0]
    clock[0] += elapsed
    clock[1] += elapsed
    assert b._due(other, False)
    assert b._save_state.call_count == 2
    assert all(call.args == () and call.kwargs == {} for call in b._save_state.call_args_list)


def test_collect_merges_disk_io_into_disks(tmp_path):
    with_array(tmp_path)
    b = make_bot(tmp_path)
    b._snap = Snapshot(disk_io={"sdb": (4.0, 0.5, 25.0)})
    sections = asyncio.run(b.collect())
    assert "disk_io" not in {s.key for s in sections}
    disks = next(s for s in sections if s.key == "disks")
    row = next(cells for _, cells in disks.parts[1].rows if cells[0] == "disk1")
    assert row[-5:] == ("4MB/s", "512KB/s", "25%", "0", "ok")
    assert disks.parts[0].rows[1:] == [(panel.FINE, ("Total read: 4MB/s",)), (panel.FINE, ("Total write: 512KB/s",)),
                                     (panel.FINE, ("Busiest disk: 25%",))]


def test_newer_compares_release_tags():
    assert B.newer("v0.2.0", "0.1.0") and B.newer("1.0.0", "0.9.9")
    assert not B.newer("v0.1.0", "0.1.0") and not B.newer("v0.0.9", "0.1.0") and not B.newer("latest", "0.1.0")
    for invalid in ("1.0", "1.0.0-rc.1", "01.0.0", "1.0.0+build"):
        assert not B.newer(invalid, "0.9.9") and not B.newer("1.0.0", invalid)


@pytest.mark.parametrize("known", [None, B.__version__, "v99.0.0"])
def test_failed_release_checks_retain_cache_and_retry_after_completion(tmp_path, monkeypatch, known):
    b = make_bot(tmp_path)
    b.http_session = MagicMock()
    b.latest_release = known
    b.update_available = known == "v99.0.0"
    clock = [1000.0]
    monkeypatch.setattr(B.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(B.time, "time", lambda: 1700000000.0)
    async def fetch(http, repo):
        assert http is b.http_session and repo == "o/r"
        clock[0] += 10
        return B.releases.Check(None, "HTTP 429", 900)
    probe = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(B.releases, "fetch", probe)
    asyncio.run(b._check_release("o/r"))
    assert b.latest_release == known and b.update_available == (known == "v99.0.0")
    assert b._release_checked == 1700000000.0 and b._release_error == "HTTP 429"
    assert b._release_at == 1010 and b._release_wait == 900
    report = b.update_report(b.node_inventory()[0])
    assert "Release check:" in report and "HTTP 429" in report
    assert "No update available." not in report
    assert ("Latest known public release:" in report) == (known is not None)
    assert ("No newer release in last-known result." in report) == (known == B.__version__)
    clock[0] = 1909
    asyncio.run(b._check_release("o/r"))
    assert probe.await_count == 1
    clock[0] = 1910
    probe.side_effect = None
    probe.return_value = B.releases.Check("v99.0.0", None)
    asyncio.run(b._check_release("o/r"))
    assert probe.await_count == 2 and b.update_available
    assert b._release_wait == B.RELEASE_EVERY and b._release_error is None
    assert "Latest known" not in b.update_report(b.node_inventory()[0])


def test_release_check_flags_newer_and_keeps_flag_on_failure(tmp_path):
    b = make_bot(tmp_path)
    b.http_session = MagicMock(get=MagicMock(return_value=release_reply(200, {"tag_name": "v99.0.0"})))
    asyncio.run(b._check_release("o/r"))
    assert b.update_available and b.latest_release == "v99.0.0"
    b.http_session.get.reset_mock()
    asyncio.run(b._check_release("o/r"))
    assert not b.http_session.get.called  # cached until RELEASE_EVERY passes
    for status, body in ((404, {}), (200, ["x"]), (200, {"tag_name": 5}), (200, {"tag_name": ""}),
                         (200, {"tag_name": "latest"}), (200, {"tag_name": "9" * 5000})):
        b._release_at = -B.RELEASE_EVERY
        b.http_session.get.return_value = release_reply(status, body)
        asyncio.run(b._check_release("o/r"))
        assert b.update_available and b.latest_release == "v99.0.0"
        assert b._release_checked is not None and b._release_error
    b.update_available, b.latest_release, b._release_at = True, "v99.0.0", -B.RELEASE_EVERY
    b.http_session.get.side_effect = aiohttp.ClientError("down")
    asyncio.run(b._check_release("o/r"))
    assert b.update_available and b.latest_release == "v99.0.0"


def test_repost_when_the_stored_message_belongs_to_another_bot(tmp_path, monkeypatch):
    monkeypatch.setattr(B.Bot, "user", SimpleNamespace(id=1))
    b = make_bot(tmp_path)
    b.main.message_id = 5
    stub_render(b)
    ch = channel()
    ch.fetch_message.return_value = MagicMock(author=SimpleNamespace(id=2), edit=AsyncMock())
    wire(b, ch)
    asyncio.run(b.refresh())
    ch.fetch_message.return_value.edit.assert_not_awaited()
    assert ch.send.await_count == 1 and b.main.message_id == 777


def test_helper_tasks_and_identity_are_fresh_and_failure_discards_old_schedules(tmp_path, monkeypatch, caplog):
    from shazzzbot import helperclient

    b = make_bot(tmp_path, helper=helperclient.Settings("/run/helper.sock", "TEST_HELPER_KEY"))
    with_array(tmp_path)
    b.array = unraid.read(str(tmp_path / "emhttp"))
    data = {"generated": time.time(), "jobs": [{"name": "Backup", "cron": "0 2 * * *"}], "smart": [],
            "os_update": "", "running": {"Old task": time.time() - 60}, "finished": {}, "durations": {}}
    ask = AsyncMock(side_effect=[{"version": "0.1.0", "sha256": "a" * 64, "protocol": 1,
                                 "bundle_version": "0.1.0", "entry_sha256": "b" * 64}, data])
    monkeypatch.setattr(helperclient, "request", ask)
    sections = asyncio.run(b.collect())
    task = next(s for s in sections if s.key == "tasks")
    assert "Backup" in str(task.parts) and "Old task" in str(task.parts)
    assert [c.args[1] for c in ask.await_args_list] == ["hello", "tasks"]
    assert b.helper_node.version == "0.1.0" and b.helper_node.level == panel.FINE
    assert b.helper_node.hostname == b.array.name and b.helper_node.ms >= 0
    assert "2 nodes ok" in b._status[1]
    ask.side_effect = helperclient.Error("secret remote detail")
    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            sections = asyncio.run(b.collect())
            task = next(s for s in sections if s.key == "tasks")
            assert "needs the shazzzbot helper" in str(task.parts)
            assert "Old task" not in str(task.parts) and "Backup" not in str(task.parts)
            assert (panel.WARNING, "needs the shazzzbot helper") in task.issues
    assert caplog.text.count("helper is unavailable") == 1 and "secret" not in caplog.text
    assert b.helper_node.level == panel.WARNING and b.helper_node.version is None
    ask.side_effect = [{"version": "0.2.0", "sha256": "b" * 64, "protocol": 1,
                       "bundle_version": "0.2.0", "entry_sha256": "c" * 64}, helperclient.Error("refused")]
    assert asyncio.run(b._read_tasks()) is None and b.helper_node.version == "0.2.0"
    b.cfg.helper = None
    sections = asyncio.run(b.collect())
    task = next(s for s in sections if s.key == "tasks")
    assert b.helper_node is None
    assert (panel.WARNING, "needs the shazzzbot helper") not in task.issues
    assert "needs the shazzzbot helper" in str(task.parts)


def test_netdata_block_device_alarms_reach_disks_and_system_once_in_all_views(tmp_path, monkeypatch):
    from unittest.mock import patch

    from shazzzbot import view

    with_array(tmp_path)
    a = unraid.read(str(tmp_path / "emhttp"))
    device, name = a.disks[0].device, a.disks[0].name
    b = make_bot(tmp_path, netdata_url="http://netdata")
    b.http_session = MagicMock(get=MagicMock(return_value=release_reply(404, {})))
    text = "10min_disk_backlog 8000 ms"
    monkeypatch.setattr(netdata, "alarms", AsyncMock(return_value=[(panel.WARNING, f"{device} {text}")]))
    sections = asyncio.run(b.collect())
    disks = next(s for s in sections if s.key == "disks")
    system = next(r for r in b._system if r.label == "Alerts")
    expected = (panel.WARNING, f"{name} {text}")
    assert disks.issues.count(expected) == list(system.issues).count(expected) == 1
    assert not any(f"{device} {text}" == t for _, t in system.issues)
    assert text in view.text(disks).description and any(view._md(text) in f.value for f in view.rich(disks).fields)
    with patch.object(view, "_rows", wraps=view._rows) as draw:
        assert view.image(disks).startswith(b"\x89PNG")
    assert any(f"{name} {text}" in str(call.args[1].rows) for call in draw.call_args_list)


def test_opted_in_forge_system_row_uses_cached_results_in_every_view_and_fits_worst_case(tmp_path, monkeypatch):
    from PIL import ImageDraw

    from shazzzbot import sysimage, view
    from shazzzbot.forge import Entry

    name = "Forge " + "W" * 58
    cfg = forges.Settings("forgejo", "https://forge.example", "TEST_FORGE_TOKEN",
                          ("owner/one", "owner/two", "owner/three"), ("ci",), name, True)
    b = make_bot(tmp_path, forges=[cfg])
    monitor = b.forges[0]
    monitor.ready = True
    monitor.data = {"ci": (Entry("owner/one", ("pass",)), Entry("owner/two", ("fail",), level=panel.CRITICAL),
                            Entry("owner/three", ("fail",), level=panel.CRITICAL))}
    ask = AsyncMock()
    monkeypatch.setattr(forges, "poll", ask)
    sections = asyncio.run(b.collect())
    ask.assert_not_awaited()
    row = next(r for r in b._system if r.label == name)
    assert row.text == "3 repos: 1 passing, 2 failing" and row.level == row.dot == panel.CRITICAL
    system = next(s for s in sections if s.key == "system")
    assert "3 repos: 1 passing, 2 failing" in view.text(system).description
    assert "3 repos: 1 passing, 2 failing" in str(view.rich(system).to_dict())
    drawn = []
    original = ImageDraw.ImageDraw.text
    def capture(draw, xy, text, **kwargs):
        drawn.append((xy, text, kwargs))
        return original(draw, xy, text, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    values = []
    original_value = sysimage._text
    def capture_value(*args):
        end = original_value(*args)
        values.append((args[4], end))
        return end
    monkeypatch.setattr(sysimage, "_text", capture_value)
    png = sysimage.render(b._system)
    assert png.startswith(b"\x89PNG") and any(text == name for _, text, _ in drawn)
    end = next(end for text, end in values if text == row.text)
    import io

    from PIL import Image
    width = Image.open(io.BytesIO(png)).width
    assert end <= width - 24
    monitor.data = {"ci": (Entry("Status", ("could not read (unreachable)",), level=panel.WARNING),)}
    asyncio.run(b.collect())
    row = next(r for r in b._system if r.label == name)
    assert row.text == "unreachable" and row.dot == row.level == panel.CRITICAL


def test_app_samples_persist_on_the_minute_and_shutdown_then_reload_with_the_window(tmp_path, monkeypatch):
    from shazzzbot import apphistory

    now, monotonic = [1000.0], [0.0]
    monkeypatch.setattr(B.time, "time", lambda: now[0])
    monkeypatch.setattr(apphistory.time, "monotonic", lambda: monotonic[0])
    monkeypatch.setattr(discord.Bot, "close", AsyncMock())
    b = make_bot(tmp_path)
    b.window = 300
    b._save_state()
    b.host.poll.return_value = Snapshot(top_cpu=[("app", 1)], top_ram=[("app", 64)])
    async def go():
        await sampled(b)
        path = tmp_path / "data" / "apps.json"
        assert json.loads(path.read_text())["samples"]["cpu"]["app"] == [[1000, 1]]
        now[0], monotonic[0] = 1010.0, 10.0
        b.host.poll.return_value = Snapshot(top_cpu=[("app", 2)], top_ram=[("app", 128)])
        await b.sample()
        assert json.loads(path.read_text())["samples"]["cpu"]["app"] == [[1000, 1]]
        await b.close()
        assert json.loads(path.read_text())["samples"]["cpu"]["app"] == [[1000, 1], [1010, 2]]
    asyncio.run(go())
    restarted = make_bot(tmp_path)
    assert restarted.window == 300
    assert restarted.app_history.values("cpu", "app", 0, 2000) == [(1000, 1), (1010, 2)]
    assert restarted.app_history.values("ram", "app", 0, 2000) == [(1000, 64), (1010, 128)]
    restarted.history.close()
    now[0] = 2000
    expired = make_bot(tmp_path)
    assert expired.app_history.samples == {"cpu": {}, "ram": {}}
    expired.history.close()


@pytest.mark.parametrize("cancel_first", [False, True])
def test_draw_workers_keep_the_shared_budget_lock_until_real_work_finishes(tmp_path, monkeypatch, cancel_first):
    b = make_bot(tmp_path)
    b._sections = [panel.Section("apps", "Apps", [])]
    b._check_release = AsyncMock()
    monkeypatch.setattr(B.delivery, "keys", lambda *_args: ("apps",))
    monkeypatch.setattr(B.delivery, "message_schedule", lambda *_args: {})
    monkeypatch.setattr(B.view, "stack", lambda pngs, *_args, **_kwargs: pngs[0])
    release = threading.Event()
    calls = []
    counters = threading.Lock()

    async def go():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=2))
        first_started, second_attempted = asyncio.Event(), asyncio.Event()
        worker_lock = threading.RLock()
        attempts = 0

        class ObservedLock:
            def __enter__(self):
                nonlocal attempts
                with counters:
                    attempts += 1
                    if attempts == 2:
                        loop.call_soon_threadsafe(second_attempted.set)
                worker_lock.acquire()
                return self

            def __exit__(self, *_args):
                worker_lock.release()

        monkeypatch.setattr(B.renderbudget, "LOCK", ObservedLock())
        def image(*_args, **_kwargs):
            with counters:
                calls.append("allocation")
                first = len(calls) == 1
            if first:
                loop.call_soon_threadsafe(first_started.set)
                if not release.wait(5):
                    raise TimeoutError("first render was not released")
            return b"rendered"
        monkeypatch.setattr(B.view, "image", image)
        async def locked_draw():
            return await b.draw_images(b.main, 1_800_000_001)
        tasks = [asyncio.create_task(locked_draw())]
        try:
            await asyncio.wait_for(first_started.wait(), 2)
            if cancel_first:
                tasks[0].cancel()
                with pytest.raises(asyncio.CancelledError):
                    await tasks[0]
                assert not b._lock.locked()
                tasks.append(asyncio.create_task(locked_draw()))
            else:
                tasks.append(asyncio.create_task(b.draw_images(b.main, 1_800_000_001)))
            if cancel_first:
                await asyncio.wait_for(second_attempted.wait(), 2)
            else:
                await asyncio.sleep(0)
            assert calls == ["allocation"] and not tasks[1].done()
            if cancel_first:
                assert b._lock.locked()
            release.set()
            assert await asyncio.wait_for(tasks[1], 2) == [("apps.png", b"rendered")]
            if not cancel_first:
                assert await asyncio.wait_for(tasks[0], 2) == [("apps.png", b"rendered")]
            assert calls == ["allocation", "allocation"]
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(go())


def test_only_messages_without_images_wake_the_edit_clock(tmp_path):
    b = make_bot(tmp_path, outputs=[Output(PANEL), Output(21)])
    image, text = b.boards[0].messages[0], b.boards[1].messages[0]
    b.image_keys = lambda board=None: ("history",) if board is image else ()  # type: ignore[method-assign]
    image.edit_slot = text.edit_slot = -1
    assert not B.delivery.edit_due(b, image) and B.delivery.edit_due(b, text)
    assert b._pending()
    text.edit_slot = B.delivery.edit_slot(b, text)
    assert not b._pending()
