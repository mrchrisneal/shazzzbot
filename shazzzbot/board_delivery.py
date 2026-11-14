"""Render, pack, and reconcile a channel's ordered panel messages."""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from functools import partial
from typing import TYPE_CHECKING, Any, cast

import discord

from . import delivery, discordauth, forges, panel, postcheck, view
from .boards import EMBED_COUNT, Board, Message, child, pack

if TYPE_CHECKING:
    from .bot import Bot

log = logging.getLogger("shazzzbot")
FOOTER_GROWTH = len("\nUpdate available") + 1


def _groups(part: panel.Part) -> list[list[tuple[int, tuple[str, ...]]]]:
    out: list[list[tuple[int, tuple[str, ...]]]] = []
    for row in part.rows[part.heads:]:
        if row[0] in (panel.CONT, panel.NOTE) and out:
            out[-1].append(row)
        else:
            out.append([row])
    return out


def _take(section: panel.Section, keep: int) -> panel.Section:
    parts = []
    dropped = 0
    for part in section.parts:
        groups = _groups(part)
        count = min(keep, len(groups))
        rows = list(part.rows[:part.heads]) + [row for group in groups[:count] for row in group]
        parts.append(part._replace(rows=rows))
        keep -= count
        dropped += len(groups) - count
    if dropped:
        parts.append(panel.Part([(panel.MORE, (f"...and {dropped} more",))], cut=None))
    return section._replace(parts=parts)


def render(bot: Bot, board: Board, sections: list[panel.Section], private: bool,
           limit: int, note_room: int) -> list[discord.Embed]:
    limit -= FOOTER_GROWTH + len("Image pending.\n")
    sections = [s for s in sections if board.sections is None or forges.group(s.key) in board.sections]
    now = board.render_at = time.time()
    board.modes = {s.key: board.mode or board.render.get(forges.group(s.key), "image") for s in sections}
    singletons = set()

    def draw(section: panel.Section) -> discord.Embed:
        mode = board.modes[section.key]
        meta = delivery.stamp(bot, board, discord.Embed(), now)
        footer = cast("discord.embeds.EmbedFooter", meta.footer).text or ""
        section = section._replace(title=panel.clip(section.title, 256), footer=footer, stamp=False)
        embed = view.embed(section, mode)
        if len(embed) > limit:
            singletons.add(section.key)
            if len(view.embed(_take(section, 0), mode)) > limit:
                section = section._replace(note="...and 1 more")
            low, high = 0, sum(len(_groups(part)) for part in section.parts)
            while low < high:
                mid = (low + high + 1) // 2
                if len(view.embed(_take(section, mid), mode)) <= limit:
                    low = mid
                else:
                    high = mid - 1
            embed = view.embed(_take(section, low), mode)
        embed.timestamp = meta.timestamp
        return embed

    embeds = {section.key: draw(section) for section in sections}
    left_out: list[str] = []
    if private:
        while embeds and (sum(map(len, embeds.values())) > limit - note_room
                          or len(embeds) > EMBED_COUNT - bool(left_out)):
            key = max(embeds, key=lambda key: len(embeds[key]))
            del embeds[key]
            board.modes.pop(key)
            left_out.append(next(section.title for section in sections if section.key == key))
    if left_out:
        note = f"Not shown, too long without images: {', '.join(left_out)}. The panel has them."
        if len(note) > note_room:
            note = f"Not shown: {len(left_out)} sections. The panel has them."
        embeds["left_out"] = delivery.stamp(bot, board, discord.Embed(description=note), now)
    if not embeds:
        embeds["empty"] = delivery.stamp(bot, board, discord.Embed(
            description="No data is available for the selected sections."), now)
    board.embed_keys = list(embeds)
    board.singletons = singletons
    log.debug("render channel %s: %s in %.2fs", board.channel_id, ", ".join(embeds), time.time() - now)
    return list(embeds.values())


def layout(bot: Bot, board: Board, sections: list[panel.Section], limit: int) -> None:
    embeds = bot._fit(board, sections)
    all_keys = list(board.embed_keys)
    alone = {i for i, key in enumerate(board.embed_keys) if key in board.singletons}
    cached = {name: embed for message in board.messages for name, embed in message.images.items()}
    weights = []
    for embed in embeds:
        name = delivery.image_name(embed) if isinstance(embed, discord.Embed) else None
        pending = len("Image pending.\n") if name else 0
        weights.append(max(len(embed) + FOOTER_GROWTH + pending, len(cached[name]) if name in cached else 0))
    packets = pack(embeds, limit, alone, weights)
    wanted = len(packets)
    if len(board.messages) > wanted:
        board.surplus += board.messages[wanted:]
        board.messages = board.messages[:wanted]
    while len(board.messages) < wanted:
        board.messages.append(child(board, len(board.messages)))
    offset = 0
    for i, (message, packet) in enumerate(zip(board.messages, packets)):
        message.planned = packet
        message.modes = dict(board.modes)
        message.render_at = board.render_at
        message.phase = board.phase + i * min(1.0, board.edit_every / wanted)
        message.embed_keys = all_keys[offset:offset + len(packet)]
        message.expected = tuple(message.embed_keys) if len(all_keys) == len(embeds) else None
        offset += len(packet)
    board.layout_ready = True


def changed(old: list[discord.Embed], new: list[discord.Embed]) -> bool:
    def content(embeds: list[discord.Embed]) -> list[dict[str, Any]] | None:
        out: list[dict[str, Any]] = []
        for embed in embeds:
            if not isinstance(embed, discord.Embed):
                return None
            data: dict[str, Any] = dict(embed.to_dict())
            data.pop("timestamp", None)
            footer = dict(data.get("footer", {}))
            if "text" in footer:
                footer["text"] = re.sub(r"Update at[^)]*", "Update at", footer["text"])
                data["footer"] = footer
            out.append(data)
        return out
    before, after = content(old), content(new)
    return before is None or after is None or before != after


async def pause(board: Board) -> None:
    await asyncio.sleep(min(1.0, board.edit_every / len(board.messages)))


async def cleanup(bot: Bot, channel: discord.TextChannel, board: Board) -> None:
    if not board.surplus:
        board.swept = True
        return
    pending = []
    for message in board.surplus:
        if message.message_id is None:
            continue
        try:
            msg = message.msg or await channel.fetch_message(message.message_id)
            if bot.user and msg.author.id == bot.user.id:
                await msg.delete()
        except discord.NotFound:
            pass
        except (discord.HTTPException, discordauth.Blocked) as error:
            log.warning("could not remove surplus panel message in %s: %s", board.channel_id, error)
            pending.append(message)
    board.surplus = pending
    board.swept = not pending
    bot._save_state()


async def _fetch(bot: Bot, channel: discord.TextChannel, message: Message) -> None:
    if message.message_id and message.msg is None:
        message.msg = await channel.fetch_message(message.message_id)
    if message.msg is not None:
        if bot.user and message.msg.author.id != bot.user.id:
            raise LookupError("another bot wrote it")
        old = getattr(message.msg, "embeds", None)
        if isinstance(old, list) and not message.images:
            delivery.remember(message, old)
            message.embeds = old
            attachments = getattr(message.msg, "attachments", None)
            if isinstance(attachments, list) and not attachments and not message.images:
                message.image_keys = ()


async def send(bot: Bot, channel: discord.TextChannel, message: Message, force: bool) -> bool | None:
    selected = delivery.keys(bot, message)
    if message.image_keys is None and message.images and {key + ".png" for key in selected} <= message.images.keys():
        message.image_keys, message.image_wanted = selected, False
        message.image_slot = bot._slot(message.image_every)
    upload = delivery.due(bot, message, force) and (
        not selected or not bot._media_lock.locked() and delivery.next_upload(bot) is message)
    if message.msg is not None and (selected or message.image_uncertain) and not upload:
        return None
    now = time.time()
    preview = [delivery.stamp(bot, message, embed.copy(), now) if isinstance(embed, discord.Embed) else embed
               for embed in message.planned]
    preview = delivery.prepare(bot, message, preview, False)
    if not upload and message.msg is not None and not changed(message.embeds, preview):
        return False
    media = upload and bool(selected)
    if delivery.refresh_at(bot, message, time.time(), media) > time.time():
        return None
    if media:
        async with bot._media_lock, bot.rate_budget.permit(delivery.route(message), True):
            return await _send(bot, channel, message, upload)
    async with bot.rate_budget.permit(delivery.route(message), media):
        return await _send(bot, channel, message, upload)


async def _send(bot: Bot, channel: discord.TextChannel, message: Message, upload: bool) -> bool:
    keys = delivery.keys(bot, message)
    now = message.render_at = time.time()
    embeds = [delivery.stamp(bot, message, embed.copy(), now) if isinstance(embed, discord.Embed) else embed
              for embed in message.planned]
    files = await bot.image_files(message) if upload else None
    if message.msg is not None:
        try:
            message.msg = await bot._send(message, message.msg.edit, embeds, files, keys, edit=True)
            return True
        except discord.NotFound:
            message.msg, message.message_id, message.image_keys = None, None, None
            message.image_uncertain = False
            message.post_nonce, message.post_at, message.post_marker = None, None, None
            message.images.clear()
            files = await bot.image_files(message) if delivery.due(bot, message, True) else None
    if message.post_nonce is None:
        message.post_nonce = secrets.token_hex(12)
        message.post_at = time.monotonic()
        message.post_marker = postcheck.marker(delivery.prepare(bot, message, embeds, bool(files)))
    send = partial(channel.send, nonce=message.post_nonce, enforce_nonce=True)
    recovered = await postcheck.recover(bot, message, channel)
    try:
        message.msg = recovered or await bot._send(message, send, embeds, files, keys, edit=False)
    except discord.HTTPException as error:
        if 400 <= error.status < 500:
            message.post_nonce, message.post_at, message.post_marker = None, None, None
        raise
    message.message_id = message.msg.id
    message.post_nonce, message.post_at, message.post_marker = None, None, None
    actual = getattr(message.msg, "embeds", None)
    if isinstance(actual, list):
        message.embeds = actual
        delivery.remember(message, actual)
        if files and any(file.filename not in message.images for file in files):
            message.image_keys, message.image_wanted, message.image_slot = None, True, None
    bot._save_state()
    return True


async def refresh(bot: Bot, board: Board, sections: list[panel.Section], force: bool, images_only: bool) -> None:
    if bot.rate_budget.auth.blocked(board.channel_id):
        return
    channel = bot.get_channel(board.channel_id)
    if not isinstance(channel, discord.TextChannel) or channel.guild.id != bot.cfg.guild_id:
        log.error("panel channel %s not found", board.channel_id)
        return
    if not board.layout_ready:
        layout(bot, board, sections, bot.embed_limit)
    board.layout_ready = False
    holes = False
    for message in board.messages:
        try:
            await _fetch(bot, channel, message)
        except (discord.NotFound, LookupError):
            message.msg, message.message_id = None, None
            holes = True
        except discordauth.Blocked:
            return
    if holes:
        live = [(message.message_id, message.msg, dict(message.images), list(message.embeds), message.image_keys,
                 message.image_slot, message.image_at) for message in board.messages if message.message_id]
        for i, message in enumerate(board.messages):
            if i < len(live):
                (message.message_id, message.msg, message.images, message.embeds, message.image_keys,
                 message.image_slot, message.image_at) = live[i]
            else:
                message.message_id, message.msg, message.image_keys = None, None, None
                message.images, message.embeds = {}, []
    sent = False
    for message in board.messages:
        if time.time() < max(board.retry_at, message.retry_at):
            continue
        if images_only and not delivery.due(bot, message, False):
            continue
        if board.scheduled and message.message_id is not None \
                and message.edit_slot == delivery.edit_slot(bot, message) \
                and not delivery.due(bot, message, force):
            continue
        if sent:
            await pause(board)
        try:
            result = await send(bot, channel, message, force)
            if result is not None:
                message.edit_slot = delivery.edit_slot(bot, message)
            sent = result or sent
        except discordauth.Blocked:
            return
        except Exception:
            log.exception("panel update in channel %s failed (message %s)", board.channel_id, message.position)
        if message.message_id is None:
            break
    await bot._sweep(channel, board)
