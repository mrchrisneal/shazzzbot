import asyncio
import io
from array import array
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from shazzzbot import board_delivery, delivery, jellyfin, panel, view
from shazzzbot import bot as B
from shazzzbot.config import IMAGE_FLOOR, Output
from shazzzbot.host import Snapshot
from tests.test_board_delivery import setup
from tests.test_bot import GUILD, PANEL, channel, edited, make_bot, with_array


@pytest.mark.parametrize("blocked", ["helper", "issues", "jellyfin", "release"])
def test_network_collection_does_not_hold_the_panel_lock(tmp_path, monkeypatch, blocked):
    bot = make_bot(tmp_path, jellyfin_url="http://jellyfin")
    bot.http_session = MagicMock()
    bot.main.mode = "text"
    checked = []

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        def check(name, value):
            async def call(*_args, **_kwargs):
                assert not bot._lock.locked(), name
                checked.append(name)
                if name == blocked:
                    entered.set()
                    await release.wait()
                return value
            return call

        monkeypatch.setattr(bot, "_read_tasks", check("helper", None))
        monkeypatch.setattr(bot, "_read_ups_shutdown", check("ups", {}))
        monkeypatch.setattr(bot, "_issues", check("issues", []))
        monkeypatch.setattr(jellyfin, "streams", check("jellyfin", Counter()))
        monkeypatch.setattr(bot, "_check_release", check("release", None))
        task = asyncio.create_task(bot.refresh())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert not bot._lock.locked()
            async with asyncio.timeout(5):
                async with bot._lock:
                    assert not task.done()
        finally:
            release.set()
            await task

    asyncio.run(run())
    assert {"helper", "ups", "issues", "jellyfin", "release"} <= set(checked)


def test_discord_send_fetch_edit_and_delete_run_outside_the_panel_lock(tmp_path, monkeypatch):
    bot, ch, posts = setup(tmp_path, monkeypatch, [3500, 3500])
    monkeypatch.setattr(type(bot), "user", SimpleNamespace(id=9))
    original = ch.send.side_effect
    calls = []

    async def send(**kwargs):
        assert not bot._lock.locked()
        calls.append("send")
        msg = await original(**kwargs)

        async def edit(**payload):
            assert not bot._lock.locked()
            calls.append("edit")
            msg.embeds = payload["embeds"]
            return msg

        async def delete():
            assert not bot._lock.locked()
            calls.append("delete")

        msg.edit.side_effect = edit
        msg.delete.side_effect = delete
        return msg

    async def fetch(mid):
        assert not bot._lock.locked()
        calls.append("fetch")
        return next(msg for msg in posts if msg.id == mid)

    ch.send.side_effect = send
    ch.fetch_message.side_effect = fetch

    async def run():
        await bot.refresh()
        assert len(posts) == 2
        bot.main.msg = None
        bot._sections = [bot._sections[0]._replace(note="changed")]
        await bot.refresh()

    asyncio.run(run())
    assert set(calls) == {"send", "fetch", "edit", "delete"}


def test_slow_output_does_not_block_other_outputs_or_cached_status(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    other = B.Board(21, mode="text")
    bot.boards.append(other)
    bot.main.mode = "text"
    bot._sections = [panel.Section("system", "System", [], note="current")]
    bot.collect = AsyncMock(side_effect=lambda: bot._sections)
    chans = {PANEL: channel(), 21: channel()}
    bot.get_channel = lambda cid: chans.get(cid)
    counts = {PANEL: 0, 21: 0}
    maximum = dict(counts)

    async def run():
        entered, release, other_done = asyncio.Event(), asyncio.Event(), asyncio.Event()

        def message(cid, payload):
            return edited(SimpleNamespace(id=cid + 100, author=SimpleNamespace(id=9),
                                          embeds=payload["embeds"], attachments=[]))

        async def first(**payload):
            counts[PANEL] += 1
            maximum[PANEL] = max(maximum[PANEL], counts[PANEL])
            entered.set()
            try:
                await release.wait()
                return message(PANEL, payload)
            finally:
                counts[PANEL] -= 1

        async def second(**payload):
            assert not bot._lock.locked()
            counts[21] += 1
            maximum[21] = max(maximum[21], counts[21])
            counts[21] -= 1
            other_done.set()
            return message(21, payload)

        chans[PANEL].send.side_effect = first
        chans[21].send.side_effect = second
        cached = B.discord.Embed(title="cached", description="last completed content")
        bot.main.embeds = [cached]
        first_refresh = asyncio.create_task(bot.refresh())
        second_refresh = None
        try:
            await asyncio.wait_for(entered.wait(), 5)
            second_refresh = asyncio.create_task(bot.refresh())
            await asyncio.wait_for(other_done.wait(), 5)
            assert await asyncio.wait_for(bot.latest(), 5) == [cached]
            assert not first_refresh.done()
        finally:
            release.set()
            await first_refresh
            if second_refresh is not None:
                await second_refresh

    asyncio.run(run())
    assert maximum == {PANEL: 1, 21: 1}
    assert chans[PANEL].send.await_count == chans[21].send.await_count == 1
    assert all(ch.guild.id == GUILD for ch in chans.values())


def test_cancelled_refresh_releases_board_locks_and_drains_its_tasks(tmp_path, monkeypatch):
    bot, main, _posts = setup(tmp_path, monkeypatch, [100])
    bot.boards.append(B.Board(21, mode="text"))
    other = channel()
    bot.get_channel = lambda cid: {PANEL: main, 21: other}.get(cid)

    async def run():
        entered = {PANEL: asyncio.Event(), 21: asyncio.Event()}
        stopped = set()
        hold = asyncio.Event()

        def send(cid):
            async def wait(**_payload):
                entered[cid].set()
                try:
                    await hold.wait()
                finally:
                    stopped.add(cid)
            return wait

        main.send.side_effect = send(PANEL)
        other.send.side_effect = send(21)
        task = asyncio.create_task(bot.refresh())
        try:
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered.values())), 5)
            assert not bot._lock.locked()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert stopped == {PANEL, 21}
        assert all(not board.lock.locked() for board in bot.boards)
        assert not bot._lock.locked()
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(run())


@pytest.mark.parametrize("target", ["layout", "fit"])
def test_layout_failures_release_every_acquired_board_lock(tmp_path, monkeypatch, target):
    bot, ch, _posts = setup(tmp_path, monkeypatch, [100])
    bot.boards.append(B.Board(21, mode="text"))
    error = ValueError("layout failed")
    if target == "layout":
        monkeypatch.setattr(board_delivery, "layout", MagicMock(side_effect=error))
    else:
        monkeypatch.setattr(bot, "_fit", MagicMock(side_effect=error))
    with pytest.raises(ValueError) as caught:
        asyncio.run(bot.refresh())
    assert caught.value is error
    assert not bot._lock.locked()
    assert all(not board.lock.locked() for board in bot.boards)
    ch.send.assert_not_awaited()


@pytest.mark.parametrize("busy", ["budget", "media"])
def test_private_status_rechecks_the_upload_budget_after_drawing(tmp_path, monkeypatch, busy):
    bot = make_bot(tmp_path)
    bot._sections = [panel.Section("apps", "Top Apps", [])]
    original, fallback = B.discord.Embed(title="image"), B.discord.Embed(title="embed fallback")
    bot._fit = MagicMock(side_effect=[[original], [fallback]])
    saved = MagicMock()
    monkeypatch.setattr(bot, "_save_state", saved)
    uploaded_at = []

    async def files(_board):
        assert not bot._lock.locked()
        if busy == "budget":
            bot._upload_at = B.time.monotonic()
        else:
            await bot._media_lock.acquire()
        uploaded_at.append(bot._upload_at)
        return [B.discord.File(io.BytesIO(b"image"), "apps.png")]

    bot.image_files = AsyncMock(side_effect=files)
    embeds, actual_files = asyncio.run(bot.status_reply())
    assert embeds == [fallback] and actual_files == []
    assert bot._upload_at == uploaded_at[0] and bot._upload_wall is None
    assert len(bot._fit.call_args_list) == 2
    assert bot._fit.call_args_list[-1].kwargs == {"images": False}
    assert bot._fit.call_args_list[-1].args[0].mode == "embed"
    saved.assert_not_called()
    if busy == "media":
        bot._media_lock.release()


def test_private_status_does_not_draw_while_a_media_send_is_running(tmp_path):
    bot = make_bot(tmp_path)
    bot._sections = [panel.Section("apps", "Top Apps", [])]
    bot._fit = MagicMock(return_value=[B.discord.Embed(title="embed fallback")])
    bot.image_files = AsyncMock()

    async def run():
        async with bot._media_lock:
            embeds, files = await bot.status_reply()
        assert embeds[0].title == "embed fallback" and files == []
        bot.image_files.assert_not_awaited()
        assert bot._fit.call_args.kwargs == {"images": False}

    asyncio.run(run())


def test_images_use_their_captured_snapshot_after_a_new_collection(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    with_array(tmp_path)
    bot.window = 300
    bot._snap = Snapshot(cpu_pct=10, ram_used_mb=10, ram_total_mb=100)
    bot.history.samples.append(array("d", [100, 10, 20, 1, 2, 3, 4]))
    first_sections = asyncio.run(bot.collect())
    first = bot._snapshot
    assert first.disks
    bot.window = 600
    bot.tz = ZoneInfo("America/Los_Angeles")
    bot._snap = Snapshot(cpu_pct=90, ram_used_mb=90, ram_total_mb=100)
    bot.history.keep = 50
    bot.history.add(bot._snap, t=200)
    assert [row[0] for row in bot.history.samples] == [200]
    asyncio.run(bot.collect())
    assert first is not bot._snapshot and first.system != bot._snapshot.system
    assert first.sections == tuple(first_sections) and first.window == 300
    assert list(first.samples[0]) == [100, 10, 20, 1, 2, 3, 4]
    bot.main.snapshot = first
    bot.main.modes = {"system": "image", "history": "image"}
    system, history = MagicMock(return_value=b"system"), MagicMock(return_value=b"history")
    release = AsyncMock()
    monkeypatch.setattr(B.sysimage, "render", system)
    monkeypatch.setattr(B.charts, "render", history)
    bars = MagicMock(return_value=b"bars")
    monkeypatch.setattr(B.charts, "disk_bars", bars)
    monkeypatch.setattr(B.view, "stack", lambda images, *_args, **_kwargs: b"|".join(images))
    monkeypatch.setattr(bot, "_check_release", release)
    rendered = asyncio.run(bot.draw_images(bot.main, 300))
    assert rendered == [("system.png", b"system|bars"), ("history.png", b"history")]
    system.assert_called_once_with(list(first.system), 300, first.tz, first.status)
    bars.assert_called_once_with(list(first.disks), 300, first.tz)
    history.assert_called_once_with(list(first.samples), first.window, 300, first.tz, gap=first.gap)
    release.assert_not_awaited()


def test_cancel_during_a_later_board_acquire_releases_earlier_board_locks(tmp_path):
    bot = make_bot(tmp_path)
    second = B.Board(21, mode="text")
    bot.boards.append(second)

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        waiter = None

        async def hold_second():
            async with second.lock:
                entered.set()
                await release.wait()

        async def collect():
            nonlocal waiter
            await second.lock.acquire()
            waiter = asyncio.create_task(hold_second())
            await asyncio.sleep(0)
            second.lock.release()
            return []

        bot.collect = collect
        refresh = asyncio.create_task(bot.refresh())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert bot.main.lock.locked() and second.lock.locked()
            assert not refresh.done()
            refresh.cancel()
            with pytest.raises(asyncio.CancelledError):
                await refresh
            assert not bot.main.lock.locked()
            assert second.lock.locked()
        finally:
            release.set()
            await waiter

    asyncio.run(run())


def test_cancelled_sdk_child_drains_other_outputs_and_releases_their_locks(tmp_path, monkeypatch):
    bot, main, _posts = setup(tmp_path, monkeypatch, [100])
    bot.boards.append(B.Board(21, mode="text"))
    other = channel()
    bot.get_channel = lambda cid: {PANEL: main, 21: other}.get(cid)

    async def run():
        entered, hold, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def cancel(**_payload):
            await entered.wait()
            raise asyncio.CancelledError

        async def wait(**_payload):
            entered.set()
            try:
                await hold.wait()
            finally:
                stopped.set()

        main.send.side_effect = cancel
        other.send.side_effect = wait
        with pytest.raises(asyncio.CancelledError):
            await bot.refresh()
        assert stopped.is_set()
        assert not bot._lock.locked()
        assert all(not board.lock.locked() for board in bot.boards)
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(run())


def test_simultaneous_empty_status_reads_build_once(tmp_path):
    bot = make_bot(tmp_path)
    content = B.discord.Embed(title="built once")

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def build():
            entered.set()
            await release.wait()
            return [content]

        bot.build = AsyncMock(side_effect=build)
        first = asyncio.create_task(bot.latest())
        await entered.wait()
        second = asyncio.create_task(bot.latest())
        await asyncio.sleep(0)
        assert bot.main.lock.locked() and not second.done()
        release.set()
        assert await asyncio.gather(first, second) == [[content], [content]]
        bot.build.assert_awaited_once()
        assert not bot.main.lock.locked()

    asyncio.run(run())


def test_finished_output_releases_only_its_own_unused_drawn_images(tmp_path, monkeypatch):
    bot, main, _posts = setup(tmp_path, monkeypatch, [100])
    bot.boards.append(B.Board(21, mode="text"))
    other = channel()
    bot.get_channel = lambda cid: {PANEL: main, 21: other}.get(cid)
    bot._drawn = {(PANEL, 99): (("unused",), []), (21, 99): (("unused",), [])}

    async def run():
        main_done, other_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def sweep(_channel, board):
            if board.channel_id == PANEL:
                main_done.set()

        async def wait(**payload):
            other_entered.set()
            await release.wait()
            return edited(SimpleNamespace(id=121, author=SimpleNamespace(id=9),
                                          embeds=payload["embeds"], attachments=[]))

        bot._sweep = AsyncMock(side_effect=sweep)
        other.send.side_effect = wait
        task = asyncio.create_task(bot.refresh())
        try:
            await asyncio.wait_for(asyncio.gather(main_done.wait(), other_entered.wait()), 5)
            assert (PANEL, 99) not in bot._drawn and (21, 99) in bot._drawn
            assert not bot.main.lock.locked() and bot.boards[1].lock.locked()
        finally:
            release.set()
            await task
        assert bot._drawn == {}

    asyncio.run(run())


def test_slow_media_upload_never_overlaps_another_channel_upload(tmp_path, monkeypatch):
    start = 1_800_000_000.0
    clock = [start]
    fake = SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0])
    for module in (B, delivery, board_delivery):
        monkeypatch.setattr(module, "time", fake)
    bot = make_bot(tmp_path, outputs=[Output(PANEL, image_every=300), Output(21, image_every=1800)])
    bot._sections = [panel.Section("tasks", "Tasks", [panel.Part([(panel.FINE, ("Task", "idle"))])])]
    bot.collect = AsyncMock(return_value=bot._sections)
    bot.image_files = AsyncMock(return_value=[MagicMock()])
    channels = {PANEL: channel(), 21: channel()}
    monkeypatch.setattr(bot, "get_channel", channels.get)
    uploads, active, maximum = [], [0], [0]
    messages = {}

    async def run():
        entered, release, checked = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = bot._refresh
        visits = []

        async def refresh(board, *args, **kwargs):
            await original(board, *args, **kwargs)
            if board.channel_id == 21:
                visits.append(21)
                if len(visits) == 2:
                    checked.set()

        monkeypatch.setattr(bot, "_refresh", refresh)

        def sender(cid):
            async def send(**payload):
                media = bool(payload.get("files"))
                if media:
                    active[0] += 1
                    maximum[0] = max(maximum[0], active[0])
                    uploads.append((cid, clock[0]))
                try:
                    if media and cid == PANEL and len(uploads) == 1:
                        entered.set()
                        await release.wait()
                    if cid not in messages:
                        messages[cid] = edited(SimpleNamespace(id=cid + 100, author=SimpleNamespace(id=9),
                                                              embeds=payload["embeds"], attachments=[]))
                        messages[cid].edit.side_effect = send
                    messages[cid].embeds = payload["embeds"]
                    return messages[cid]
                finally:
                    if media:
                        active[0] -= 1
            return send

        for cid, ch in channels.items():
            ch.send.side_effect = sender(cid)
        first = asyncio.create_task(bot.refresh())
        second = None
        try:
            await asyncio.wait_for(entered.wait(), 5)
            clock[0] += IMAGE_FLOOR + delivery.TICK
            second = asyncio.create_task(bot.refresh(images_only=True))
            await asyncio.wait_for(checked.wait(), 5)
            assert uploads == [(PANEL, start)] and active[0] == 1
        finally:
            release.set()
            await first
            if second is not None:
                await second
        completed = bot.main.image_at
        queued = bot.schedule(clock[0])[21][0]
        assert queued >= completed + IMAGE_FLOOR
        clock[0] = queued
        await bot.refresh(images_only=True)
        assert uploads == [(PANEL, start), (21, queued)]
        assert maximum[0] == 1 and queued - completed >= IMAGE_FLOOR
        payload = messages[21].edit.await_args.kwargs
        assert all(embed.footer.text == view.footer(bot.schedule(clock[0])[21][0], bot.tz)
                   for embed in payload["embeds"])

    asyncio.run(run())
