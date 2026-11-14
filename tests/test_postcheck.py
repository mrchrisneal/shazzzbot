import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
import pytest

from shazzzbot import boards, discordauth, postcheck

CHANNEL, OWNER = 20, 9


def embed():
    return discord.Embed(title="System", description="a panel", timestamp=datetime.fromtimestamp(1_800_000_000, UTC))


def target(*candidates):
    channel = MagicMock(spec=discord.TextChannel)

    async def history(limit):
        assert limit == 20
        for candidate in candidates:
            yield candidate

    channel.history = history
    return channel


def pending(monkeypatch):
    monkeypatch.setattr(postcheck.time, "monotonic", lambda: 1000.0)
    board = boards.Board(CHANNEL)
    board.post_at = 0.0
    board.post_nonce = "pending-nonce"
    board.post_marker = postcheck.marker([embed()])
    bot = SimpleNamespace(user=SimpleNamespace(id=OWNER), boards=[board])
    return bot, board


def posted(mid, *, owner=OWNER, nonce=None, embeds=None):
    return SimpleNamespace(id=mid, author=SimpleNamespace(id=owner), nonce=nonce,
                           embeds=[embed()] if embeds is None else embeds)


def test_marker_normalizes_discord_media_metadata_but_keeps_content_and_timestamp():
    original = embed().set_image(url="attachment://system.png").set_thumbnail(url="attachment://icon.png")
    original.set_author(name="Bot", icon_url="https://example.com/icon.png")
    original.set_footer(text="Shazzzbot v1 (Update at 10:00 AM PST)", icon_url="https://example.com/icon.png")
    original.add_field(name="CPU", value="40%")
    data = original.to_dict()
    data["image"] = {"url": "https://cdn.example.com/attachments/1/2/system.png?ex=123",
                     "proxy_url": "https://proxy.example.com/system.png", "width": 800, "height": 400}
    data["thumbnail"] = {"url": "https://cdn.example.com/attachments/1/2/icon.png", "width": 20}
    data["footer"] = {**data["footer"], "text": "Shazzzbot v1 (Update at 10:05 AM PST)",
                      "proxy_icon_url": "https://proxy.example.com/icon.png"}
    data["author"] = {**data["author"], "proxy_icon_url": "https://proxy.example.com/icon.png"}
    returned = discord.Embed.from_dict(data)
    expected = postcheck.marker([original])
    assert len(expected) == 64 and postcheck.marker([returned]) == expected
    returned.timestamp = datetime.fromtimestamp(1_800_000_001, UTC)
    assert postcheck.marker([returned]) != expected
    assert postcheck.marker([embed()]) != expected
    assert postcheck.marker(["E", original]) == expected
    assert postcheck.marker(["E"]) == postcheck.marker([]) == ""
    assert postcheck.marker([discord.Embed.from_dict({"image": {}})])


@pytest.mark.parametrize("age", [None, 0.0, 120.0])
def test_recent_uncertain_post_uses_native_nonce_without_history(monkeypatch, age):
    bot, message = pending(monkeypatch)
    message.post_at = None if age is None else 1000.0 - age
    channel = MagicMock()
    assert asyncio.run(postcheck.recover(bot, message, channel)) is None
    channel.history.assert_not_called()


def test_old_post_cannot_be_recovered_without_the_bot_identity(monkeypatch):
    bot, message = pending(monkeypatch)
    bot.user = None
    with pytest.raises(TimeoutError, match="cannot verify the author"):
        asyncio.run(postcheck.recover(bot, message, target()))


def test_old_post_matches_own_nonce_and_never_steals_tracked_messages(monkeypatch):
    bot, message = pending(monkeypatch)
    known = boards.child(message, 1)
    known.message_id = 2
    message.messages.append(known)
    retired = boards.child(message, 2)
    retired.message_id = 3
    message.surplus.append(retired)
    other = boards.Board(21, message_id=4)
    bot.boards.append(other)
    found = posted(4, nonce=message.post_nonce, embeds=[])
    candidates = [posted(1, owner=99, nonce=message.post_nonce), posted(2, nonce=message.post_nonce),
                  posted(3, nonce=message.post_nonce), posted(5, embeds=[]), found]
    assert asyncio.run(postcheck.recover(bot, message, target(*candidates))) is found


def test_old_post_matches_its_original_embed_when_nonce_is_missing(monkeypatch):
    bot, message = pending(monkeypatch)
    message.post_nonce = None
    found = posted(5)
    assert asyncio.run(postcheck.recover(bot, message, target(posted(4, embeds=[]), found))) is found
    assert asyncio.run(postcheck.recover(bot, message, target(posted(6, embeds=[])))) is None
    message.post_marker = None
    assert asyncio.run(postcheck.recover(bot, message, target(posted(7, embeds=[])))) is None


@pytest.mark.parametrize("error", [TimeoutError("slow history"), discordauth.Blocked(),
                                 discord.Forbidden(SimpleNamespace(status=403, reason="denied"), "denied")])
def test_failed_history_scan_never_permits_a_blind_resend(monkeypatch, error):
    bot, message = pending(monkeypatch)

    async def history(limit):
        assert limit == 20
        raise error
        yield

    channel = SimpleNamespace(history=history)
    with pytest.raises(type(error)) as caught:
        asyncio.run(postcheck.recover(bot, message, channel))
    assert caught.value is error


def test_history_scan_has_one_total_deadline(monkeypatch):
    bot, message = pending(monkeypatch)
    actual = asyncio.wait_for
    calls = []

    async def deadline(awaitable, seconds):
        calls.append(seconds)
        return await actual(awaitable, 0)

    monkeypatch.setattr(postcheck.asyncio, "wait_for", deadline)
    with pytest.raises(TimeoutError):
        asyncio.run(postcheck.recover(bot, message, target()))
    assert calls == [20]
