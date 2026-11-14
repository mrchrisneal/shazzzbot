import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from shazzzbot import bot as B
from shazzzbot import delivery, panel, view
from shazzzbot.config import IMAGE_FLOOR, Output
from tests.test_bot import PANEL, VIEW_ROLE, VIEWER, channel, commands, ctx, edited, make_bot, wire


def cards():
    return [panel.Section(key, title, [panel.Part([(panel.FINE, ("Value", "1%"))])])
            for key, title in (("system", "System"), ("apps", "Apps"), ("history", "History"))]


def test_every_footer_of_a_message_with_images_names_its_next_upload(tmp_path, monkeypatch):
    now = 1_800_000_001
    monkeypatch.setattr(B.time, "time", lambda: now)
    b = make_bot(tmp_path, outputs=[Output(PANEL, {"system": "text", "apps": "embed"}, edit_every=30),
                                   Output(21, edit_every=120)])
    b._sections = cards()
    for board in b.boards:
        board.modes = {s.key: board.render.get(s.key, "image") for s in b._sections}
    for board in b.boards:
        embeds = b._fit(board, b._sections)
        next_image = b.schedule(now, board)[board.channel_id][0]
        assert delivery.keys(b, board)
        for embed in embeds:
            assert embed.footer.text == view.footer(next_image, b.tz)
            assert embed.timestamp == datetime.fromtimestamp(now, UTC)
        if board is b.main:
            assert next_image > delivery.edit_boundary(b, board, now)


def test_a_message_with_images_holds_text_changes_for_its_upload(tmp_path, monkeypatch):
    clock = [1_800_000_000.0]
    monkeypatch.setattr(B.time, "time", lambda: clock[0])
    monkeypatch.setattr(B.time, "monotonic", lambda: clock[0])
    b = make_bot(tmp_path, outputs=[Output(PANEL, {"system": "text", "apps": "embed"})], edit_every=30)
    b._sections = cards()
    b.collect = AsyncMock(return_value=b._sections)
    b.image_files = AsyncMock(return_value=[MagicMock()])
    ch, msg = channel(), edited(MagicMock(id=777))
    ch.send.return_value = msg
    wire(b, ch)

    async def go():
        await b.refresh()
        original = {embed.title: embed.to_dict() for embed in ch.send.await_args.kwargs["embeds"]}
        clock[0] += 30
        b._sections = [section._replace(note="changed") for section in b._sections]
        b.collect.return_value = b._sections
        await b.refresh()
        msg.edit.assert_not_awaited()
        clock[0] += 60
        await b.refresh()
        payload = msg.edit.await_args.kwargs
        assert payload["files"] and msg.edit.await_count == 1
        updated = {embed.title: embed.to_dict() for embed in payload["embeds"]}
        assert all(updated[t]["timestamp"] != original[t]["timestamp"] for t in ("System", "Apps", "History"))
    asyncio.run(go())


def test_a_first_post_held_by_the_floor_names_the_queued_upload_on_every_part(tmp_path, monkeypatch):
    clock = [1_800_000_000.0]
    monkeypatch.setattr(B.time, "time", lambda: clock[0])
    monkeypatch.setattr(B.time, "monotonic", lambda: clock[0])
    b = make_bot(tmp_path, outputs=[Output(PANEL, {"system": "text", "apps": "embed"})])
    b._sections = cards()
    b.collect = AsyncMock(return_value=b._sections)
    b.image_files = AsyncMock(return_value=[MagicMock()])
    b._upload_at, b._upload_wall = clock[0] - 40, clock[0] - 40
    ch = channel()
    ch.send.return_value = edited(MagicMock(id=777))
    wire(b, ch)
    asyncio.run(b.refresh())
    sent = ch.send.await_args.kwargs
    assert "files" not in sent
    queued = delivery.message_schedule(b, clock[0])[delivery.identity(b.main)][0]
    assert queued < delivery.message_schedule(b, clock[0], b.main)[delivery.identity(b.main)][0]
    assert {embed.footer.text for embed in sent["embeds"]} == {view.footer(queued, b.tz)}

def test_png_renderers_receive_the_same_time_as_their_embed_timestamp(tmp_path, monkeypatch):
    clock = [1_800_000_001.0]
    monkeypatch.setattr(B.time, "time", lambda: clock[0])
    b = make_bot(tmp_path)
    b._sections = cards()
    embeds = b._fit(b.main, b._sections)
    clock[0] += 25
    png = b"png"
    system = MagicMock(return_value=png)
    history = MagicMock(return_value=png)
    apps = MagicMock(return_value=png)
    monkeypatch.setattr(B.sysimage, "render", system)
    monkeypatch.setattr(B.charts, "render", history)
    monkeypatch.setattr(B.view, "image", apps)
    monkeypatch.setattr(B.view, "stack", lambda images, *args, **kwargs: images[0])
    asyncio.run(b.image_files())
    rendered = embeds[0].timestamp.timestamp()
    assert system.call_args.args[1] == rendered
    assert history.call_args.args[2] == rendered
    assert apps.call_args.kwargs["now"] == rendered
    assert all(embed.timestamp.timestamp() == rendered for embed in embeds)


@pytest.mark.parametrize("key", ["system", "history"])
def test_saved_image_only_message_is_loaded_without_a_startup_upload_or_edit(tmp_path, monkeypatch, key):
    now = 1_800_000_001.0
    monkeypatch.setattr(B.time, "time", lambda: now)
    b = make_bot(tmp_path)
    b._sections = [section for section in cards() if section.key == key]
    old = b._fit(b.main, b._sections)
    b.main.message_id = 777
    b._save_state()
    again = make_bot(tmp_path)
    again._sections = b._sections
    again.collect = AsyncMock(return_value=again._sections)
    again.image_files = AsyncMock()
    ch, msg = channel(), edited(MagicMock(id=777, embeds=old))
    ch.fetch_message.return_value = msg
    wire(again, ch)
    asyncio.run(again.refresh(scheduled=True))
    ch.fetch_message.assert_awaited_once_with(777)
    msg.edit.assert_not_awaited()
    again.image_files.assert_not_awaited()
    assert again.main.embeds[0].to_dict() == old[0].to_dict()


def test_saved_image_cache_missing_a_section_is_replaced(tmp_path):
    b = make_bot(tmp_path)
    b._sections = cards()
    old = b._fit(b.main, b._sections)[:1]
    b.main.message_id = 777
    b.collect = AsyncMock(return_value=b._sections)
    b.image_files = AsyncMock(return_value=[MagicMock()])
    ch, msg = channel(), edited(MagicMock(id=777, embeds=old))
    ch.fetch_message.return_value = msg
    wire(b, ch)
    asyncio.run(b.refresh())
    b.image_files.assert_awaited_once()
    assert "files" in msg.edit.await_args.kwargs


def test_foreign_cached_message_is_never_edited(tmp_path, monkeypatch):
    monkeypatch.setattr(B.Bot, "user", SimpleNamespace(id=9))
    b = make_bot(tmp_path)
    b._sections = cards()
    b.main.message_id, b.main.msg = 1, edited(MagicMock(author=SimpleNamespace(id=10)))
    old = b.main.msg
    b.collect = AsyncMock(return_value=b._sections)
    b.image_files = AsyncMock(return_value=[])
    ch = channel()
    wire(b, ch)
    asyncio.run(b.refresh())
    old.edit.assert_not_awaited()
    ch.send.assert_awaited_once()


def test_pending_image_has_no_broken_attachment_and_cache_keeps_other_prose():
    board = delivery.Board(1)
    picture = discord.Embed(title="Image", description="A note").set_image(url="attachment://system.png")
    prose = discord.Embed(description="Text")
    pending = delivery.outgoing(board, [picture, prose], False)
    assert pending[0].image is None and pending[0].description == "Image pending.\nA note"
    assert pending[1] is prose and picture.image.url
    empty = discord.Embed().set_image(url="attachment://apps.png")
    assert delivery.outgoing(board, [empty], False)[0].description == "Image pending."
    delivery.remember(board, [picture, prose])
    assert delivery.outgoing(board, [picture], False)[0] is picture
    assert delivery.outgoing(board, [picture], True)[0] is picture


def test_pending_image_footer_names_its_first_upload_not_an_imaginary_previous_upload(tmp_path, monkeypatch):
    now = 1_800_000_020
    monkeypatch.setattr(B.time, "time", lambda: now)
    b = make_bot(tmp_path)
    b._sections = cards()[:1]
    b._upload_wall = now - 20
    embeds = b._fit(b.main, b._sections)
    prepared = delivery.prepare(b, b.main, embeds, False)
    at = b.schedule(now)[PANEL][0]
    assert at < b.schedule(now, b.main)[PANEL][0]
    assert prepared[0].footer.text == view.footer(at, b.tz)
    assert prepared[0].image is None


def test_observed_retry_after_is_scoped_to_the_active_request_and_not_retried_here():
    notify = MagicMock()
    watch = delivery.Watch(notify)
    logger = logging.getLogger("discord.http")
    logger.warning("We are being rate limited. Retrying in %.2f seconds.", 12)
    with watch:
        logger.warning("Other message")
        logger.warning("We are being rate limited.")
        logger.warning("We are being rate limited. Retrying in %s seconds.", "bad")
        logger.warning("We are being rate limited. Retrying in %s seconds.", float("inf"))
        logger.warning("We are being rate limited. Retrying in %s seconds.", 0)
        logger.warning("We are being rate limited. Retrying in %.2f seconds.", 46.27)
        logger.warning("We are being rate limited. Retrying in %.2f seconds.", 90.76)
    assert notify.call_args_list == [((46.27, True),), ((90.76, False),)]
    assert watch not in logger.handlers and delivery.ACTIVE.get() is None


@pytest.mark.parametrize("header,expected", [("123.5", 123.5), ("bad", IMAGE_FLOOR),
                                          (None, IMAGE_FLOOR), ("nan", IMAGE_FLOOR), (-1, IMAGE_FLOOR)])
def test_retry_header_is_used_or_a_conservative_policy_delay_is_kept(header, expected):
    error = discord.HTTPException(MagicMock(status=429, headers={"Retry-After": header}), "limited")
    assert delivery.retry_delay(error) == expected


def test_rate_limited_upload_moves_the_plan_and_persists_the_delay(tmp_path, monkeypatch):
    now = 1_800_000_001.0
    monkeypatch.setattr(B.time, "time", lambda: now)
    b = make_bot(tmp_path)
    b._sections = cards()
    b._fit(b.main, b._sections)
    before = b.schedule(now)[PANEL][0]
    delivery.limited(b, b.main, 120, True, True)
    assert b.schedule(now)[PANEL][0] >= now + 120 > before
    fresh = b._fit(b.main, b._sections)[0]
    assert fresh.footer.text == view.footer(b.schedule(now, b.main)[PANEL][0], b.tz)
    assert b._image_spacing >= 150 and not b._due(b.main, True)
    again = make_bot(tmp_path)
    assert again._image_spacing == b._image_spacing and again._image_retry == now + 120
    b._save_state = MagicMock(side_effect=OSError)
    delivery.limited(b, b.main, 10, False, False)


def test_startup_edits_are_spread_within_each_configured_period(tmp_path, monkeypatch):
    clock = [1_800_000_000.0]
    monkeypatch.setattr(B.time, "time", lambda: clock[0])
    b = make_bot(tmp_path, outputs=[Output(cid, {"system": "text"}) for cid in (PANEL, 21, 22)])
    b._snap = B.Snapshot()
    b._sections = cards()[:1]
    b.collect = AsyncMock(return_value=b._sections)
    b.image_files = AsyncMock(return_value=[])
    channels = {board.channel_id: channel() for board in b.boards}
    for cid, ch in channels.items():
        ch.send.return_value = edited(MagicMock(id=cid))
    monkeypatch.setattr(b, "get_channel", channels.get)

    async def go():
        for offset, cid in ((0, PANEL), (20, 21), (40, 22)):
            clock[0] = 1_800_000_000.0 + offset
            await b.panel_loop.coro(b)
            assert channels[cid].send.await_count == 1
            assert sum(ch.send.await_count for ch in channels.values()) == offset // 20 + 1
        assert all(board.phase < board.edit_every for board in b.boards)
    asyncio.run(go())


def test_an_actual_429_does_not_immediately_retry_the_same_message_route(tmp_path, monkeypatch):
    now = 1_800_000_001.0
    monkeypatch.setattr(B.time, "time", lambda: now)
    b = make_bot(tmp_path)
    b.main.msg = MagicMock()
    error = discord.HTTPException(MagicMock(status=429, headers={"Retry-After": "120"}), "limited")
    send = AsyncMock(side_effect=error)
    asyncio.run(b._send(b.main, send, [], [MagicMock()], (), edit=True))
    assert send.await_count == 1 and b.main.retry_at == now + 120
    send = AsyncMock(side_effect=error)
    with pytest.raises(discord.HTTPException):
        asyncio.run(b._send(b.main, send, [], None, (), edit=True))
    assert send.await_count == 1


def test_other_text_delivery_errors_propagate_without_inventing_a_cooldown(tmp_path):
    b = make_bot(tmp_path)
    error = discord.Forbidden(MagicMock(status=403), "denied")
    with pytest.raises(discord.Forbidden):
        asyncio.run(b._send(b.main, AsyncMock(side_effect=error), [], None, (), edit=True))
    assert b.main.retry_at == 0


def test_private_snapshot_footer_does_not_promise_an_automatic_update(tmp_path):
    b = make_bot(tmp_path)
    b._sections = cards()
    board = delivery.Board(0, mode="text")
    assert all("No automatic updates" in embed.footer.text for embed in b._fit(board, b._sections))


def test_future_completion_stamp_survives_restart_without_releasing_the_budget_early(tmp_path, monkeypatch):
    monkeypatch.setattr(B.time, "time", lambda: 1_800_000_001.0)
    monkeypatch.setattr(B.time, "monotonic", lambda: 1000.0)
    b = make_bot(tmp_path)
    b._upload_wall = 1_800_000_015.0
    b._save_state()
    again = make_bot(tmp_path)
    assert again._upload_at == 1014.0


@pytest.mark.parametrize("bad", [True, -1, float("nan"), float("inf"), 10**20, "bad"])
def test_invalid_saved_cooldowns_do_not_break_the_scheduler(tmp_path, bad):
    import json

    b = make_bot(tmp_path)
    with open(b.cfg.state_file, "w") as stream:
        json.dump({"format": 1, "upload_at": bad, "image_spacing": bad, "image_retry": bad}, stream)
    again = make_bot(tmp_path)
    assert again._upload_wall is None and again._image_spacing == IMAGE_FLOOR and again._image_retry == 0


def test_repeated_429s_cap_spacing_without_shortening_the_server_retry(tmp_path, monkeypatch):
    now = 1_800_000_001.0
    monkeypatch.setattr(B.time, "time", lambda: now)
    b = make_bot(tmp_path)
    for _ in range(30):
        delivery.limited(b, b.main, 90, True, True)
        assert IMAGE_FLOOR <= b._image_spacing <= delivery.MAX_IMAGE_SPACING
    assert b._image_spacing == delivery.MAX_IMAGE_SPACING
    delivery.limited(b, b.main, 1200, True, True)
    assert b._image_spacing == delivery.MAX_IMAGE_SPACING and b._image_retry == now + 1200


def test_successful_uploads_recover_the_minimum_and_persist_each_step(tmp_path):
    b = make_bot(tmp_path)
    b._image_spacing = delivery.MAX_IMAGE_SPACING
    previous = b._image_spacing
    for _ in range(11):
        asyncio.run(b._send(b.main, AsyncMock(return_value=MagicMock()), [], [MagicMock()], (), edit=False))
        assert IMAGE_FLOOR <= b._image_spacing < previous
        assert make_bot(tmp_path)._image_spacing == pytest.approx(b._image_spacing)
        previous = b._image_spacing
    assert b._image_spacing == IMAGE_FLOOR


def test_restart_clamps_a_previously_persisted_large_spacing(tmp_path):
    b = make_bot(tmp_path)
    b._image_spacing = 1_000_000
    b._save_state()
    assert make_bot(tmp_path)._image_spacing == delivery.MAX_IMAGE_SPACING
    assert delivery.recovered_spacing(1_000_000) == delivery.MAX_IMAGE_SPACING * 0.8


def test_future_upload_plan_uses_the_same_recovery_as_successful_uploads(tmp_path, monkeypatch):
    now = 1_800_000_000.0
    monkeypatch.setattr(B.time, "time", lambda: now)
    b = make_bot(tmp_path)
    b._sections = cards()[:1]
    b.main.modes = {"system": "image"}
    b._image_spacing = delivery.MAX_IMAGE_SPACING
    assert b.schedule(now, b.main)[PANEL][0] == now + delivery.recovered_spacing(b._image_spacing)


@pytest.mark.parametrize("uploaded", [False, True])
def test_private_reply_recovers_spacing_only_when_it_uploaded_images(tmp_path, uploaded):
    b = make_bot(tmp_path)
    b._image_spacing = delivery.MAX_IMAGE_SPACING
    b.status_reply = AsyncMock(return_value=([], [MagicMock()] if uploaded else []))
    b._save_state = MagicMock()
    B.register(b)
    context = ctx(VIEWER, [VIEW_ROLE])
    asyncio.run(commands(b)["status"].callback(context))
    assert b._image_spacing == delivery.MAX_IMAGE_SPACING * (0.8 if uploaded else 1)
    assert b._save_state.call_count == int(uploaded)


def test_failed_private_upload_does_not_decay_or_persist_spacing(tmp_path):
    b = make_bot(tmp_path)
    b._image_spacing = delivery.MAX_IMAGE_SPACING
    b.status_reply = AsyncMock(return_value=([], [MagicMock()]))
    b._save_state = MagicMock()
    B.register(b)
    context = ctx(VIEWER, [VIEW_ROLE])
    context.respond.side_effect = discord.HTTPException(MagicMock(status=500), "failed")
    with pytest.raises(discord.HTTPException):
        asyncio.run(commands(b)["status"].callback(context))
    assert b._image_spacing == delivery.MAX_IMAGE_SPACING
    b._save_state.assert_not_called()


def test_footer_is_ascii_and_fits_the_phone_even_with_a_long_version_and_zone(monkeypatch):
    monkeypatch.setattr(view, "__version__", "1234567890.1234567890.1234567890")
    text = view.footer(1_800_000_000, B.ZoneInfo("Pacific/Chatham"), True)
    assert text.isascii() and all(len(line) <= panel.WIDTH + 3 for line in text.splitlines())
    assert "Update available" in text and "Update at" in text
    assert not any(f"| {mode}" in text for mode in view.MODES)


def test_footer_names_version_and_next_update_only():
    utc = B.ZoneInfo("UTC")
    assert view.footer(1_800_000_000, utc) == f"Shazzzbot v{view.__version__[:16]} (Update at 8:00 AM UTC)"
    assert view.footer(None, utc).endswith("(No automatic updates)")


def test_saved_upload_timestamps_cannot_escape_safe_wall_clock_bounds(tmp_path, monkeypatch):
    import json

    from shazzzbot import bot as B
    from tests.test_bot import make_bot

    monkeypatch.setattr(B.time, "time", lambda: 1700000000.0)
    b = make_bot(tmp_path)
    with open(b.cfg.state_file, "w") as stream:
        json.dump({"format": 1, "upload_at": 10**12, "image_retry": 10**12}, stream)
    state = b._load_state()
    assert state["upload_at"] is None and state["image_retry"] == 0
    with open(b.cfg.state_file, "w") as stream:
        json.dump({"format": 1, "upload_at": 1700000015, "image_retry": 1700001200}, stream)
    state = b._load_state()
    assert state["upload_at"] == 1700000015 and state["image_retry"] == 1700001200
    b.history.close()
