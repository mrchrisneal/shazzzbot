import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from shazzzbot import board_delivery, delivery, panel
from tests.test_bot import channel, edited, make_bot, not_found


def prepared(tmp_path, media=False):
    bot = make_bot(tmp_path)
    bot.main.mode = "image" if media else "text"
    bot._sections = [panel.Section("system", "System", [])]
    bot.main.modes = {"system": "image" if media else "text"}
    bot.main.embed_keys = ["system"]
    bot.main.planned = [discord.Embed(title="System", description="original")]
    if media:
        bot.main.planned[0].set_image(url="attachment://system.png")
    bot.image_files = AsyncMock(side_effect=lambda _message: [discord.File(io.BytesIO(b"png"), "system.png")]
                                if media else [])
    return bot


@pytest.mark.parametrize("media", [False, True])
def test_accepted_post_timeout_reuses_nonce_and_restores_the_original_content(tmp_path, monkeypatch, media):
    clock = [1_800_000_000.0]
    monkeypatch.setattr("time.time", lambda: clock[0])
    bot = prepared(tmp_path, media)
    target = channel()
    accepted = {}
    calls = []

    async def send(**kwargs):
        nonce = kwargs["nonce"]
        assert kwargs["enforce_nonce"] is True and isinstance(nonce, str) and len(nonce) == 24
        calls.append(nonce)
        if nonce not in accepted:
            original = [discord.Embed.from_dict(embed.to_dict()) for embed in kwargs["embeds"]]
            accepted[nonce] = edited(SimpleNamespace(id=777, author=SimpleNamespace(id=9), embeds=original,
                                                    attachments=[]))
            raise TimeoutError("response lost after Discord accepted the post")
        return accepted[nonce]

    target.send.side_effect = send
    with pytest.raises(TimeoutError):
        asyncio.run(board_delivery._send(bot, target, bot.main, media))
    pending = bot.main.post_nonce
    assert pending and bot.main.message_id is None
    original = accepted[pending].embeds[0].to_dict()
    clock[0] += 120
    bot.main.planned[0].description = "newer sample"
    assert asyncio.run(board_delivery._send(bot, target, bot.main, media))
    assert len(accepted) == 1 and calls == [pending, pending]
    assert bot.main.message_id == 777 and bot.main.post_nonce is None
    assert bot.main.embeds[0].to_dict() == original
    if media:
        assert bot.main.images["system.png"].to_dict() == original


def test_edits_have_no_nonce_and_a_deleted_message_gets_a_new_post_nonce(tmp_path):
    bot = prepared(tmp_path)
    target = channel()
    posts = []

    async def send(**kwargs):
        posts.append(kwargs["nonce"])
        assert kwargs["enforce_nonce"] is True
        return edited(SimpleNamespace(id=700 + len(posts), author=SimpleNamespace(id=9),
                                      embeds=kwargs["embeds"], attachments=[]))

    target.send.side_effect = send
    assert asyncio.run(board_delivery._send(bot, target, bot.main, False))
    assert bot.main.post_nonce is None
    assert asyncio.run(board_delivery._send(bot, target, bot.main, False))
    assert "nonce" not in bot.main.msg.edit.await_args.kwargs
    assert "enforce_nonce" not in bot.main.msg.edit.await_args.kwargs
    bot.main.msg.edit.side_effect = not_found()
    assert asyncio.run(board_delivery._send(bot, target, bot.main, False))
    assert len(posts) == 2 and posts[0] != posts[1] and bot.main.post_nonce is None
    assert bot.main.message_id == 702


def test_edit_timeout_retries_the_same_content_without_nonce_or_a_new_post(tmp_path):
    bot = prepared(tmp_path)
    message = edited(SimpleNamespace(id=777))
    message.edit.side_effect = [TimeoutError("uncertain edit"), message]
    embeds = [discord.Embed(title="System", description="same content")]
    with pytest.raises(TimeoutError):
        asyncio.run(bot._send(bot.main, message.edit, embeds, None, (), edit=True))
    assert asyncio.run(bot._send(bot.main, message.edit, embeds, None, (), edit=True)) is message
    calls = message.edit.await_args_list
    assert calls[0].kwargs == calls[1].kwargs == {"embeds": embeds}


def test_late_unknown_post_is_recovered_before_a_second_send(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    bot = prepared(tmp_path)
    monkeypatch.setattr(type(bot), "user", SimpleNamespace(id=9))
    target = channel()
    accepted = []

    async def send(**kwargs):
        accepted.append(edited(SimpleNamespace(id=777, author=SimpleNamespace(id=9),
                                               nonce=kwargs["nonce"], embeds=kwargs["embeds"], attachments=[])))
        raise TimeoutError("accepted but response lost")

    async def history(limit):
        assert limit == 20
        yield accepted[0]

    target.send.side_effect = send
    target.history = history
    with pytest.raises(TimeoutError):
        asyncio.run(board_delivery._send(bot, target, bot.main, False))
    clock[0] = 121
    assert asyncio.run(board_delivery._send(bot, target, bot.main, False))
    target.send.assert_awaited_once()
    assert bot.main.message_id == 777 and bot.main.post_nonce is None
    assert bot.main.embeds == accepted[0].embeds


def test_deduped_text_ack_does_not_claim_the_retry_uploaded_an_image(tmp_path):
    bot = prepared(tmp_path, True)
    target = channel()
    accepted = []

    async def send(**kwargs):
        if not accepted:
            accepted.append(edited(SimpleNamespace(id=777, author=SimpleNamespace(id=9),
                                                   embeds=kwargs["embeds"], attachments=[])))
            raise TimeoutError("accepted but response lost")
        return accepted[0]

    target.send.side_effect = send
    with pytest.raises(TimeoutError):
        asyncio.run(board_delivery._send(bot, target, bot.main, False))
    assert asyncio.run(board_delivery._send(bot, target, bot.main, True))
    assert bot.main.image_wanted and bot.main.image_keys is None and bot.main.image_slot is None
    assert not bot.main.images and "Image pending." in bot.main.embeds[0].description


def test_uncertain_image_edit_keeps_the_existing_panel_until_a_full_retry(tmp_path, monkeypatch):
    bot = prepared(tmp_path, True)
    target = channel()
    old = discord.Embed(title="System", description="last acknowledged image")
    old.set_image(url="attachment://system.png")
    bot.main.message_id = 777
    bot.main.msg = edited(SimpleNamespace(id=777))
    bot.main.embeds = [old]
    bot.main.images = {"system.png": old}
    bot.main.msg.edit.side_effect = TimeoutError("uncertain image edit")
    with pytest.raises(TimeoutError):
        asyncio.run(board_delivery._send(bot, target, bot.main, True))
    assert bot.main.embeds == [old] and bot.main.images == {"system.png": old}
    assert bot.main.image_uncertain
    monkeypatch.setattr(delivery, "due", lambda *_: False)
    assert asyncio.run(board_delivery.send(bot, target, bot.main, False)) is None
    assert bot.main.msg.edit.await_count == 1
    bot.main.msg.edit.side_effect = None
    bot.main.msg.edit.return_value = bot.main.msg
    monkeypatch.setattr(delivery, "due", lambda *_: True)
    monkeypatch.setattr(delivery, "next_upload", lambda *_: bot.main)
    monkeypatch.setattr(delivery, "refresh_at", lambda *_, **_kwargs: 0)
    assert asyncio.run(board_delivery.send(bot, target, bot.main, False))
    assert bot.main.msg.edit.await_count == 2 and not bot.main.image_uncertain
    assert bot.main.msg.edit.await_args.kwargs["files"]
    target.send.assert_not_awaited()


@pytest.mark.parametrize("status", [403, 429, 500])
def test_rejected_posts_clear_the_nonce_but_server_errors_keep_it_for_recovery(tmp_path, status):
    bot = prepared(tmp_path)
    target = channel()
    error = discord.HTTPException(SimpleNamespace(status=status, reason="failed", headers={}), "failed")
    target.send.side_effect = error
    with pytest.raises(discord.HTTPException) as caught:
        asyncio.run(board_delivery._send(bot, target, bot.main, False))
    assert caught.value is error
    assert bool(bot.main.post_nonce) == (status == 500)
    assert (bot.main.post_at is not None) == (status == 500)
