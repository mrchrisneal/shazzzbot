import asyncio
import json
import math
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from shazzzbot import board_delivery, delivery, panel, view
from tests.test_board_delivery import setup
from tests.test_bot import PANEL, channel, edited, make_bot, wire
from tests.test_discordlog_budget import observe


@pytest.mark.parametrize("mode", ["text", "embed", "image"])
def test_all_render_footers_and_actual_edits_use_the_budget(tmp_path, monkeypatch, mode):
    clock = [1_800_000_000.0]
    monkeypatch.setattr(delivery.time, "time", lambda: clock[0])
    monkeypatch.setattr(delivery.time, "monotonic", lambda: clock[0])
    bot = make_bot(tmp_path, edit_every=30, image_every=60)
    bot.main.mode = mode
    bot._sections = [panel.Section("apps", "Apps", [panel.Part([(panel.FINE, ("Name", "1%"))])])]
    bot.collect = AsyncMock(side_effect=lambda: bot._sections)
    msg = edited(MagicMock(id=42, embeds=[]))
    bot.main.message_id, bot.main.msg = 42, msg
    wire(bot, channel())
    png_notes = []
    monkeypatch.setattr(board_delivery.view, "stack", lambda images, note="", **kw: png_notes.append(note) or images[0])
    bot._release_at = delivery.time.monotonic()
    observe(bot.rate_budget, **{"X-RateLimit-Remaining": "2", "X-RateLimit-Reset-After": "90"})
    async def go():
        await bot.refresh()
        first = msg.edit.await_args.kwargs["embeds"][0]
        assert first.footer.text == view.footer(clock[0] + 90, bot.tz)
        assert first.timestamp == datetime.fromtimestamp(clock[0], UTC)
        if mode == "image":
            assert png_notes[-1] == first.footer.text.replace("\n", " | ")
        clock[0] += 30
        bot._sections[0] = bot._sections[0]._replace(note="new value")
        await bot.refresh()
        assert msg.edit.await_count == 1
        clock[0] += 60
        await bot.refresh()
        assert msg.edit.await_count == 2
        assert msg.edit.await_args.kwargs["embeds"][0].timestamp.timestamp() == clock[0]
    asyncio.run(go())


def test_bucket_reaches_buffer_between_packets_without_losing_deferred_edit(tmp_path, monkeypatch):
    clock = [1_800_000_000.0]
    monkeypatch.setattr(delivery.time, "time", lambda: clock[0])
    monkeypatch.setattr(delivery.time, "monotonic", lambda: clock[0])
    bot, _ch, _posts = setup(tmp_path, monkeypatch, [2400] * 6)
    board_delivery.layout(bot, bot.main, bot._sections, 6000)
    for i, message in enumerate(bot.main.messages):
        message.message_id = 42 + i
        message.msg = edited(SimpleNamespace(id=42 + i, embeds=[], attachments=[]))
        def update(*, embeds, msg=message.msg, **kwargs):
            msg.embeds = embeds
            return msg
        message.msg.edit.side_effect = update
        message.edit_slot = -1
    observe(bot.rate_budget, **{"X-RateLimit-Remaining": "3", "X-RateLimit-Reset-After": "90"})
    async def go():
        await bot.refresh()
        messages = bot.main.messages
        assert [message.msg.edit.await_count for message in messages] == [1, 1, 0]
        assert messages[2].edit_slot == -1
        assert delivery.refresh_at(bot, messages[2], clock[0]) == clock[0] + 90
        clock[0] += 90
        await bot.refresh()
        assert [message.msg.edit.await_count for message in messages] == [1, 1, 1]
        third = messages[2].msg.edit.await_args.kwargs["embeds"]
        assert all(embed.footer.text == view.footer(delivery.edit_boundary(bot, messages[2], clock[0]), bot.tz)
                   for embed in third)
    asyncio.run(go())


def test_budget_state_uses_existing_atomic_path_and_survives_restart(tmp_path, monkeypatch, caplog):
    clock = [1_800_000_000.0]
    monkeypatch.setattr(delivery.time, "time", lambda: clock[0])
    bot = make_bot(tmp_path)
    observe(bot.rate_budget)
    route = delivery.route(bot.main)
    bot.rate_budget.take(route, clock[0])
    bot._save_state()
    state = tmp_path / "data/state.json"
    assert json.loads(state.read_text())["rate_budget"]["format"] == 1
    again = make_bot(tmp_path)
    assert again.rate_budget.dump() == bot.rate_budget.dump()
    value = json.loads(state.read_text())
    from shazzzbot.helper.formats import FormatError

    value["rate_budget"] = {"format": 999}
    state.write_text(json.dumps(value))
    before = state.read_bytes()
    with pytest.raises(FormatError):
        make_bot(tmp_path)
    assert state.read_bytes() == before
    for corrupt in ("{broken",):
        caplog.clear()
        state.write_text(corrupt)
        assert make_bot(tmp_path).rate_budget.dump() is None
        assert "rate budget starts fresh" in caplog.text
    assert not (tmp_path / "data/state.json.tmp").exists()


def test_tiny_allowance_queues_after_reset_and_invalid_predictions_remain_readable(tmp_path, monkeypatch):
    clock = [1_800_000_000.0]
    monkeypatch.setattr(delivery.time, "time", lambda: clock[0])
    bot = make_bot(tmp_path)
    bot.main.message_id = 42
    bot._sections = [panel.Section("apps", "Apps", [])]
    bot.main.modes = {"apps": "image"}
    bot.main.image_wanted = True
    observe(bot.rate_budget, **{"X-RateLimit-Limit": "1", "X-RateLimit-Remaining": "0"})
    assert bot.schedule(clock[0])[PANEL] == [clock[0] + 15, clock[0] + 90]
    assert delivery.edit_boundary(bot, bot.main, clock[0]) == clock[0] + bot.main.edit_every
    bot.rate_budget.global_until = math.inf
    assert bot.schedule(clock[0])[PANEL] == [math.inf, math.inf]
    assert "Waiting for rate budget" in view.footer(math.inf, bot.tz)
    from shazzzbot.bot import images_note
    assert images_note(bot) == " Images wait for the rate budget."
    assert "Waiting for rate budget" in view.footer(delivery.edit_boundary(bot, bot.main, clock[0]), bot.tz)
    assert all(len(line) <= panel.WIDTH + 3 for line in view.footer(math.inf, bot.tz).splitlines())


@pytest.mark.parametrize("existing", [True, False])
def test_upload_timeout_preserves_content_and_the_bucket_deferral(tmp_path, monkeypatch, existing):
    clock = [1_800_000_000.0]
    monkeypatch.setattr(delivery.time, "time", lambda: clock[0])
    monkeypatch.setattr(delivery.time, "monotonic", lambda: clock[0])
    bot = make_bot(tmp_path)
    bot.main.message_id = 42
    previous = MagicMock()
    bot.main.msg = previous if existing else None
    async def delayed(**kwargs):
        observe(bot.rate_budget, **{"X-RateLimit-Remaining": "1", "X-RateLimit-Reset-After": "300"})
        raise TimeoutError
    send = AsyncMock(side_effect=delayed)
    with pytest.raises(TimeoutError):
        asyncio.run(bot._send(bot.main, send, [], [MagicMock()], (), True))
    assert send.await_count == 1 and bot.main.retry_at == clock[0] + 300
    assert bot.main.msg is (previous if existing else None)
    assert bot.main.image_wanted and bot.main.image_slot is None


@pytest.mark.parametrize("content", ["{broken"])
def test_invalid_whole_state_logs_one_warning_and_missing_state_logs_none(tmp_path, caplog, content):
    bot = make_bot(tmp_path)
    assert not any("state file is invalid" in record.getMessage() for record in caplog.records)
    state = tmp_path / "data/state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(content)
    caplog.clear()
    bot._load_state()
    assert sum("state file is invalid" in record.getMessage() for record in caplog.records) == 1


@pytest.mark.parametrize("content", ["[]", "null", "42"])
def test_headerless_state_refuses_without_a_fresh_state_or_overwrite(tmp_path, caplog, content):
    from shazzzbot.helper.formats import FormatError

    bot = make_bot(tmp_path)
    state = tmp_path / "data/state.json"
    state.write_text(content)
    with pytest.raises(FormatError):
        bot._load_state()
    assert state.read_text() == content
    assert "starts fresh" not in caplog.text


@pytest.mark.parametrize("fails", [False, True])
def test_shutdown_forces_one_state_write_and_disk_failure_does_not_stop_cleanup(tmp_path, monkeypatch, caplog, fails):
    import discord
    monkeypatch.setattr(discord.Bot, "close", AsyncMock())
    bot = make_bot(tmp_path)
    save = MagicMock(side_effect=OSError("full") if fails else None)
    monkeypatch.setattr(bot, "_save_state", save)
    asyncio.run(bot.close())
    save.assert_called_once_with()
    assert ("could not persist the Discord rate budget on shutdown" in caplog.text) == fails
    discord.Bot.close.assert_awaited_once_with()
