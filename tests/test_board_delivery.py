import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from shazzzbot import board_delivery as bd
from shazzzbot import boards, delivery, panel
from tests.test_bot import PANEL, channel, edited, make_bot, not_found, wire


def sections(sizes):
    return [panel.Section(f"s{i}", f"Section {i}", [], note="x" * size) for i, size in enumerate(sizes)]


def setup(tmp_path, monkeypatch, sizes, mode="text"):
    bot = make_bot(tmp_path)
    bot.main.mode = mode
    bot._sections = sections(sizes)
    bot.collect = AsyncMock(side_effect=lambda: bot._sections)
    monkeypatch.setattr(bd.view, "embed", lambda s, mode: discord.Embed(title=s.title, description=s.note))
    monkeypatch.setattr(bd, "pause", AsyncMock())
    ch = channel()
    posts = []

    async def send(**kwargs):
        msg = edited(SimpleNamespace(id=100 + len(posts), author=SimpleNamespace(id=9), embeds=kwargs["embeds"],
                                    attachments=[]))
        msg.delete = AsyncMock()
        posts.append(msg)
        return msg
    ch.send.side_effect = send
    wire(bot, ch)
    return bot, ch, posts


@pytest.mark.parametrize(("sizes", "counts"), [([3300, 2000, 1800, 100], [1, 3]), ([2400] * 6, [2, 2, 2])])
def test_whole_sections_spill_balanced_without_changing_mode(tmp_path, monkeypatch, sizes, counts):
    bot, ch, posts = setup(tmp_path, monkeypatch, sizes)
    asyncio.run(bot.refresh())
    packets = [call.kwargs["embeds"] for call in ch.send.await_args_list]
    assert list(map(len, packets)) == counts
    assert [embed.title for packet in packets for embed in packet] == [s.title for s in bot._sections]
    assert all(sum(map(len, packet)) <= 6000 for packet in packets)
    assert all(message.modes[key] == "text" for message in bot.main.messages for key in message.embed_keys)
    assert json.loads((tmp_path / "data/state.json").read_text())["messages"][str(PANEL)] == [p.id for p in posts]
    assert bd.pause.await_count == len(posts) - 1


def test_unchanged_packets_are_not_edited_and_changed_packet_is(tmp_path, monkeypatch):
    bot, _ch, posts = setup(tmp_path, monkeypatch, [2400] * 6)
    async def go():
        await bot.refresh()
        await bot.refresh()
        assert all(post.edit.await_count == 0 for post in posts)
        bot._sections[2] = bot._sections[2]._replace(note="y" * 2400)
        await bot.refresh()
        assert [post.edit.await_count for post in posts] == [0, 1, 0]
        assert len(await bot.latest()) == 6
    asyncio.run(go())


def test_restart_reuses_all_saved_messages_and_shrink_removes_only_ours(tmp_path, monkeypatch):
    monkeypatch.setattr(type(make_bot(tmp_path)), "user", SimpleNamespace(id=9))
    bot, ch, posts = setup(tmp_path, monkeypatch, [2400] * 6)
    asyncio.run(bot.refresh())
    again = make_bot(tmp_path)
    assert [message.message_id for message in again.main.messages] == [100, 101, 102]
    again.main.mode = "text"
    again._sections = bot._sections
    again.collect = AsyncMock(side_effect=lambda: again._sections)
    by_id = {post.id: post for post in posts}
    ch.fetch_message.side_effect = by_id.__getitem__
    wire(again, ch)
    asyncio.run(again.refresh())
    assert ch.send.await_count == 3 and all(post.edit.await_count == 0 for post in posts)
    posts[2].author.id = 88
    again._sections = again._sections[:1]
    asyncio.run(again.refresh())
    assert posts[1].delete.await_count == 1 and posts[2].delete.await_count == 0
    assert posts[0].delete.await_count == 0
    assert json.loads((tmp_path / "data/state.json").read_text())["messages"] == {str(PANEL): 100}


def test_missing_middle_message_reuses_remaining_in_order_and_appends(tmp_path, monkeypatch):
    bot, ch, posts = setup(tmp_path, monkeypatch, [2400] * 6)
    asyncio.run(bot.refresh())
    bot.main.messages[1].msg = None
    ch.fetch_message.side_effect = not_found()
    asyncio.run(bot.refresh())
    assert [m.message_id for m in bot.main.messages] == [100, 102, 103]
    assert posts[2].edit.await_count == 1


@pytest.mark.parametrize("failure", ["missing", "forbidden", "blocked", "foreign", "no-user", "no-id"])
def test_surplus_cleanup_is_scoped_and_failed_delete_is_retried(tmp_path, monkeypatch, failure):
    bot = make_bot(tmp_path)
    monkeypatch.setattr(type(bot), "user", None if failure == "no-user" else SimpleNamespace(id=9))
    msg = SimpleNamespace(author=SimpleNamespace(id=8 if failure == "foreign" else 9), delete=AsyncMock())
    message = boards.Message(PANEL, message_id=None if failure == "no-id" else 1)
    bot.main.surplus = [message]
    ch = channel()
    ch.fetch_message.return_value = msg
    if failure == "missing":
        ch.fetch_message.side_effect = not_found()
    elif failure == "forbidden":
        ch.fetch_message.side_effect = discord.Forbidden(MagicMock(status=403, reason="Forbidden"), "no")
    elif failure == "blocked":
        ch.fetch_message.side_effect = bd.discordauth.Blocked()
    asyncio.run(bd.cleanup(bot, ch, bot.main))
    msg.delete.assert_not_awaited()
    assert bool(bot.main.surplus) == (failure in {"forbidden", "blocked"})
    assert bot.main.swept == (failure not in {"forbidden", "blocked"})
    if failure in {"forbidden", "blocked"}:
        ch.fetch_message.side_effect = None
        asyncio.run(bd.cleanup(bot, ch, bot.main))
        msg.delete.assert_awaited_once()


def test_foreign_saved_message_is_neither_edited_nor_deleted(tmp_path, monkeypatch):
    bot, ch, posts = setup(tmp_path, monkeypatch, [100])
    monkeypatch.setattr(type(bot), "user", SimpleNamespace(id=9))
    bot.main.message_id = 17
    foreign = SimpleNamespace(author=SimpleNamespace(id=8), edit=AsyncMock(), delete=AsyncMock())
    ch.fetch_message.return_value = foreign
    asyncio.run(bot.refresh())
    assert bot.main.message_id == posts[0].id
    foreign.edit.assert_not_awaited()
    foreign.delete.assert_not_awaited()


def test_trimming_keeps_whole_logical_rows_and_counts_them(tmp_path):
    bot = make_bot(tmp_path)
    bot.main.mode = "embed"
    rows = [(panel.FINE, ("Name", "Value"))]
    rows += [(panel.WARNING, (f"row{i}", "v" * 80)) for i in range(20)]
    section = panel.Section("tasks", "Tasks", [panel.Part(rows, heads=True, grid=True)])
    embeds = bd.render(bot, bot.main, [section], False, 650, 0)
    assert len(embeds[0]) <= 650 and bot.main.singletons == {"tasks"}
    assert "...and" in str(embeds[0].to_dict())
    grouped = panel.Section("tasks", "Tasks", [panel.Part([
        (panel.FINE, ("a",)), (panel.CONT, ("continued",)), (panel.NOTE, ("note",)),
        (panel.FINE, ("b",))])])
    taken = bd._take(grouped, 1)
    assert taken.parts[0].rows == grouped.parts[0].rows[:3]
    assert taken.parts[1].rows == [(panel.MORE, ("...and 1 more",))]
    assert bd._take(grouped, 2).parts == grouped.parts


def test_huge_note_is_trimmed_without_converting_mode(tmp_path):
    bot = make_bot(tmp_path)
    bot.main.mode = "text"
    embed = bd.render(bot, bot.main, [panel.Section("tasks", "Tasks", [], note="x" * 2000)], False, 300, 0)[0]
    assert len(embed) <= 300 and "...and 1 more" in embed.description
    assert bot.main.modes == {"tasks": "text"}


def test_private_status_stays_one_message_and_reports_whole_sections_omitted(tmp_path):
    bot = make_bot(tmp_path)
    private = boards.Board(0, mode="text")
    cards = [panel.Section(f"s{i}", f"Section {i}", [], note="x" * 900) for i in range(10)]
    embeds = bd.render(bot, private, cards, True, 6000, 500)
    assert sum(map(len, embeds)) <= 6000 and len(embeds) <= 10
    assert "Not shown" in embeds[-1].description
    assert "No automatic updates" in embeds[-1].footer.text


def test_attachment_retention_and_changed_comparison():
    message = boards.Message(PANEL)
    keep, drop = SimpleNamespace(filename="disk.png"), SimpleNamespace(filename="old.png")
    message.msg = SimpleNamespace(attachments=[keep, drop])
    picture = discord.Embed().set_image(url="attachment://disk.png")
    assert delivery.attachments(message, [picture]) == {"attachments": [keep]}
    message.msg.attachments = [keep]
    assert delivery.attachments(message, [picture]) == {}
    assert not bd.changed([discord.Embed(description="x")], [discord.Embed(description="x")])
    assert bd.changed([discord.Embed(description="x")], [discord.Embed(description="y")])


def test_footer_time_change_is_not_a_change_even_when_it_rewraps(monkeypatch):
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(delivery.view, "__version__", "1234567890.12345")
    zone = ZoneInfo("Pacific/Chatham")
    early, late = (delivery.view.footer(t, zone, True) for t in (1_800_000_000, 1_800_000_000 + 4 * 3600))
    def embed(text):
        return discord.Embed(description="x").set_footer(text=text)
    assert not bd.changed([embed(early)], [embed(late)])
    assert bd.changed([embed(early)], [embed(early.replace("Update available", "x"))])


def test_frames_use_real_image_queue_and_preserve_cached_image_metadata(tmp_path, monkeypatch):
    clock = [1_800_000_000.0]
    monkeypatch.setattr(delivery.time, "time", lambda: clock[0])
    monkeypatch.setattr(delivery.time, "monotonic", lambda: clock[0])
    bot, _ch, posts = setup(tmp_path, monkeypatch, [2400] * 6, mode="image")
    monkeypatch.setattr(bd.view, "embed", lambda s, mode: discord.Embed(
        title=s.title, description=s.note).set_image(url=f"attachment://{s.key}.png"))
    bot.image_files = AsyncMock(return_value=[MagicMock()])
    async def go():
        await bot.refresh()
        assert bot.image_files.await_count == 1
        frames = bot.main.messages
        plan = delivery.message_schedule(bot, clock[0])
        assert plan[(PANEL, 1)][0] + bot._image_spacing <= plan[(PANEL, 2)][0]
        first = [embed.to_dict() for embed in frames[0].embeds]
        clock[0] += bot._image_spacing
        await bot.refresh(images_only=True)
        assert bot.image_files.await_count == 2
        assert [embed.to_dict() for embed in frames[0].embeds] == first
        assert posts[1].edit.await_count == 1 and posts[0].edit.await_count == 0
        payload = posts[1].edit.await_args.kwargs["embeds"]
        assert all(embed.timestamp.timestamp() == clock[0] for embed in payload)
        assert all(embed.footer.text == bd.view.footer(plan[(PANEL, 1)][1], bot.tz) for embed in payload)
    asyncio.run(go())


def test_direct_refresh_layout_and_stagger_sleep(tmp_path, monkeypatch):
    real_pause = bd.pause
    bot, _ch, posts = setup(tmp_path, monkeypatch, [100])
    asyncio.run(bot._refresh(bot.main, bot._sections, False))
    assert len(posts) == 1 and bot.main.layout_ready is False
    sleeper = AsyncMock()
    monkeypatch.setattr(bd.asyncio, "sleep", sleeper)
    asyncio.run(real_pause(bot.main))
    sleeper.assert_awaited_once_with(1.0)
    message = boards.child(bot.main, 1)
    message.image_keys = ()
    assert not bot._due(message, False)


def test_per_message_cooldowns_and_current_slots_are_skipped(tmp_path, monkeypatch):
    bot, ch, posts = setup(tmp_path, monkeypatch, [2400] * 6)
    asyncio.run(bot.refresh())
    for message in bot.main.messages:
        message.image_keys = ()
    bot.main.scheduled = True
    bot.main.messages[1].retry_at = delivery.time.time() + 100
    asyncio.run(bot._refresh(bot.main, bot._sections, False))
    assert all(post.edit.await_count == 0 for post in posts)
    assert ch.send.await_count == 3


def test_fetch_failure_isolated_at_board_boundary(tmp_path, monkeypatch, caplog):
    bot, ch, _posts = setup(tmp_path, monkeypatch, [100])
    bot.main.message_id = 18
    ch.fetch_message.side_effect = discord.Forbidden(MagicMock(status=403, reason="Forbidden"), "no access")
    asyncio.run(bot.refresh())
    assert "panel update in channel" in caplog.text
    ch.send.assert_not_awaited()


@pytest.mark.parametrize(("count", "shown"), [(10, 10), (11, 9)])
def test_private_reply_fits_exactly_ten_embeds_and_keeps_a_slot_for_the_note(tmp_path, monkeypatch, count, shown):
    bot = make_bot(tmp_path)
    monkeypatch.setattr(bd.view, "embed", lambda s, mode: discord.Embed(title=s.title, description="x"))
    private = boards.Board(0, mode="text")
    embeds = bd.render(bot, private, sections([1] * count), True, 6000, 500)
    assert len(embeds) == min(count, 10) and len(private.modes) == shown


def test_cooldown_persistence_failure_is_logged(tmp_path, caplog):
    bot = make_bot(tmp_path)
    bot._save_state = MagicMock(side_effect=OSError("read only"))
    message = boards.child(bot.main, 1)
    delivery.limited(bot, message, 20, True, False)
    assert bot.main.retry_at == message.retry_at
    assert "could not persist" in caplog.text


def test_private_omission_list_with_long_titles_fits_reply(tmp_path):
    bot = make_bot(tmp_path)
    private = boards.Board(0, mode="text")
    cards = [panel.Section(f"s{i}", "title " * 40, [], note="x" * 900) for i in range(20)]
    embeds = bd.render(bot, private, cards, True, 6000, 500)
    assert sum(map(len, embeds)) <= 6000 and len(embeds) <= 10
    assert embeds[-1].description.startswith("Not shown: ")


def test_packing_reserves_pending_images_and_preserved_attachment_content(tmp_path, monkeypatch):
    bot, ch, _posts = setup(tmp_path, monkeypatch, [2850, 2850], mode="image")
    monkeypatch.setattr(bd.view, "embed", lambda s, mode: discord.Embed(
        title=s.title, description=s.note).set_image(url=f"attachment://{s.key}.png"))
    bot.main.images = {"s0.png": discord.Embed(title="Section 0", description="x" * 4000).set_image(
        url="attachment://s0.png")}
    bot._upload_at = delivery.time.monotonic()
    asyncio.run(bot.refresh())
    assert ch.send.await_count == 2
    assert all(sum(map(len, call.kwargs["embeds"])) <= 6000 for call in ch.send.await_args_list)
