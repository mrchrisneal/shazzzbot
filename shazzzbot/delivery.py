"""Panel delivery plans, attachment metadata, and observed Discord cooldowns."""
from __future__ import annotations

import asyncio
import contextvars
import logging
import math
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self
from urllib.parse import urlsplit

import discord

from . import config, discordlog, view
from .boards import Board, Message

if TYPE_CHECKING:
    from .bot import Bot

__all__ = ("Board", "Message")

TICK = 15
MAX_IMAGE_SPACING = config.MAX_IMAGE_SPACING
log = logging.getLogger("shazzzbot")
ACTIVE: contextvars.ContextVar[Watch | None] = contextvars.ContextVar("panel_delivery", default=None)


def image_name(embed: discord.Embed) -> str | None:
    if embed.image is None or not embed.image.url:
        return None
    url = embed.image.url
    return (url.removeprefix("attachment://") if url.startswith("attachment://")
            else urlsplit(url).path.rsplit("/", 1)[-1])


def outgoing(board: Message, embeds: list[discord.Embed], uploading: bool,
             pending_footer: str | None = None) -> list[discord.Embed]:
    out = []
    for embed in embeds:
        name = image_name(embed) if isinstance(embed, discord.Embed) else None
        if name and not uploading:
            if name in board.images:
                embed = board.images[name]
            else:
                embed = embed.copy()
                embed.set_image(url=None)
                embed.description = "Image pending." + ("\n" + embed.description if embed.description else "")
                if pending_footer is not None:
                    embed.set_footer(text=pending_footer)
        out.append(embed)
    return out


def prepare(bot: Bot, board: Message, embeds: list[discord.Embed], uploading: bool) -> list[discord.Embed]:
    now = time.time()
    at = message_schedule(bot, now).get(identity(board), [edit_boundary(bot, board, now)])[0]
    pending = view.footer(at if board.channel_id else None, bot.tz, bot.update_available)
    out = outgoing(board, embeds, uploading, pending)
    if uploading or not keys(bot, board):
        return out
    for i, embed in enumerate(out):
        if isinstance(embed, discord.Embed) and not image_name(embed):
            out[i] = embed.copy()
            out[i].set_footer(text=pending)
    return out


def remember(board: Message, embeds: list[discord.Embed]) -> None:
    board.images = {name: embed for embed in embeds if isinstance(embed, discord.Embed)
                    and (name := image_name(embed))}


def attachments(message: Message, embeds: list[discord.Embed]) -> dict[str, list[discord.Attachment]]:
    old = getattr(message.msg, "attachments", None)
    if not isinstance(old, list):
        return {}
    names = {name for embed in embeds if isinstance(embed, discord.Embed) and (name := image_name(embed))}
    kept = [item for item in old if item.filename in names]
    return {"attachments": kept} if len(kept) != len(old) else {}


def route(message: Message) -> str:
    method = "PATCH" if message.message_id is not None else "POST"
    tail = f"/{message.message_id}" if message.message_id is not None else ""
    return discordlog.route_key(method, f"/channels/{message.channel_id}/messages{tail}")


def refresh_at(bot: Bot, message: Message, at: float, media: bool = False,
               budget: discordlog.Budget | None = None) -> float:
    return (budget or bot.rate_budget).next(route(message), max(at, message.retry_at,
                                                              message.owner.retry_at if message.owner else 0), media)


def edit_boundary(bot: Bot, board: Message, now: float) -> float:
    budget = bot.rate_budget.clone()
    messages = [message for parent in bot.boards for message in parent.messages if not keys(bot, message)]
    if not any(message is board for message in messages):
        messages.append(board)
    planned = {}
    channel_free: dict[int, float] = {}
    for message in sorted(messages, key=lambda m: bot._boundary(now - m.phase, m.edit_every) + m.phase):
        nominal = bot._boundary(now - message.phase, message.edit_every) + message.phase
        at = refresh_at(bot, message, max(nominal, channel_free.get(message.channel_id, nominal)), budget=budget)
        planned[id(message)] = at
        channel_free[message.channel_id] = at + min(1.0, message.edit_every / len(messages))
        if math.isfinite(at):
            budget.take(route(message), at)
    return planned[id(board)]


def edit_due(bot: Bot, message: Message) -> bool:
    """A message with images changes only when its images upload; the rest edit on their own clock."""
    return not keys(bot, message) and message.edit_slot != edit_slot(bot, message)


def edit_slot(bot: Bot, board: Message) -> int:
    now = time.time() - board.phase
    offset = datetime.fromtimestamp(now, bot.tz).utcoffset()
    return int((now + (offset.total_seconds() if offset else 0)) // board.edit_every)


def bounded_spacing(spacing: float) -> float:
    return max(float(config.IMAGE_FLOOR), min(MAX_IMAGE_SPACING, math.ceil(spacing / TICK) * TICK))


def recovered_spacing(spacing: float) -> float:
    return bounded_spacing(min(MAX_IMAGE_SPACING, spacing) * 0.8)


def identity(message: Message) -> tuple[int, int]:
    return message.channel_id, message.position


def keys(bot: Bot, message: Message) -> tuple[str, ...]:
    return tuple(key for key in bot.image_keys(message) if message.expected is None or key in message.expected)


def message_schedule(bot: Bot, now: float, uploaded: Message | None = None) -> dict[tuple[int, int], list[float]]:
    budget = bot.rate_budget.clone()
    boards = [message for board in bot.boards for message in board.messages if keys(bot, message)]
    last = math.ceil(now / TICK) * TICK if uploaded else (bot._upload_wall or -math.inf)
    spacing = recovered_spacing(bot._image_spacing) if uploaded else bot._image_spacing
    free = max(now, last + spacing, bot._image_retry)
    due, order = {}, {}
    for n, board in enumerate(sorted(boards, key=lambda b: b.image_at)):
        fresh = board is not uploaded and (board.image_wanted or board.message_id is None
                                          or board.image_slot != bot._slot(board.image_every))
        due[identity(board)] = max(now if fresh else bot._boundary(now, board.image_every), board.retry_at,
                                   board.owner.retry_at if board.owner else 0)
        order[identity(board)] = len(boards) if board is uploaded else n
    out: dict[tuple[int, int], list[float]] = {identity(board): [] for board in boards}
    while out and min(map(len, out.values())) < 2:
        at = math.ceil(free / TICK) * TICK
        ready = [channel for channel in due if due[channel] <= at]
        if not ready:
            free = min(due.values())
            continue
        channel = min(ready, key=order.__getitem__)
        message = next(b for b in boards if identity(b) == channel)
        allowed = refresh_at(bot, message, at, True, budget)
        if not math.isfinite(allowed):
            out[channel] = [math.inf, math.inf]
            del due[channel]
            continue
        if allowed > at:
            due[channel] = allowed
            continue
        out[channel].append(at)
        budget.take(route(message), at, True)
        order[channel] = max(order.values()) + 1
        due[channel] = bot._boundary(at, next(b.image_every for b in boards if identity(b) == channel))
        spacing = recovered_spacing(spacing)
        free = at + spacing
    return out


def schedule(bot: Bot, now: float, uploaded: Message | None = None) -> dict[int, list[float]]:
    out: dict[int, list[float]] = {}
    for (channel, _), times in message_schedule(bot, now, uploaded).items():
        out.setdefault(channel, []).extend(times)
    return {channel: sorted(times)[:2] for channel, times in out.items()}


def due(bot: Bot, message: Message, force: bool) -> bool:
    selected = keys(bot, message)
    wants = bool(selected) if message.expected is not None else bot._wants_images(message)
    if not wants or not selected and bool(message.image_keys):
        return message.image_keys != ()
    message.image_wanted |= force or selected != message.image_keys
    retry_at = max(message.retry_at, message.owner.retry_at if message.owner else 0)
    return time.time() >= max(bot._image_retry, retry_at) \
        and time.monotonic() - bot._upload_at >= bot._image_spacing \
        and (force or message.image_wanted or message.message_id is None
             or message.image_slot != bot._slot(message.image_every))


def next_upload(bot: Bot) -> Message | None:
    pending = [message for board in bot.boards for message in board.messages
               if keys(bot, message) and due(bot, message, False)]
    return min(pending, key=lambda message: message.image_at) if pending else None


def stamp(bot: Bot, board: Message, embed: discord.Embed, now: float) -> discord.Embed:
    """Every embed of a message with images names its next upload, since the whole message changes only then."""
    plan = message_schedule(bot, now, board) if keys(bot, board) else {}
    at = plan.get(identity(board), [edit_boundary(bot, board, now)])[0]
    embed.set_footer(text=view.footer(at if board.channel_id else None, bot.tz, bot.update_available))
    embed.timestamp = datetime.fromtimestamp(now, UTC)
    return embed


class Watch(logging.Handler):
    def __init__(self, notify: Callable[[float, bool], None]):
        super().__init__(logging.WARNING)
        self.notify, self.seen = notify, False

    def emit(self, record: logging.LogRecord) -> None:
        if ACTIVE.get() is self and str(record.msg).startswith("We are being rate limited.") \
                and isinstance(record.args, tuple) and record.args:
            delay = record.args[0]
            if isinstance(delay, (int, float)) and math.isfinite(delay) and delay > 0:
                self.notify(float(delay), not self.seen)
                self.seen = True

    def __enter__(self) -> Self:
        self.token = ACTIVE.set(self)
        logging.getLogger("discord.http").addHandler(self)
        return self

    def __exit__(self, *exc: object) -> None:
        logging.getLogger("discord.http").removeHandler(self)
        ACTIVE.reset(self.token)


def limited(bot: Bot, board: Message, delay: float, first: bool, uploading: bool, *, body: bool = False) -> None:
    delay = min(discordlog.DAY, max(0.0, delay))
    bot.rate_budget.defer(route(board), delay, uploading, body=body)
    board.retry_at = max(board.retry_at, time.time() + delay)
    if board.owner:
        board.owner.retry_at = max(board.owner.retry_at, board.retry_at)
    if uploading:
        bot._image_retry = max(bot._image_retry, board.retry_at)
        bot._image_spacing = bounded_spacing(max(bot._image_spacing * (2 if first else 1), config.IMAGE_FLOOR + delay))
    try:
        bot._save_state()
    except OSError:
        logging.getLogger("shazzzbot").warning("could not persist the Discord cooldown")


def retry_delay(error: discord.HTTPException) -> float:
    value = error.response.headers.get("Retry-After")
    try:
        delay = float(value) if isinstance(value, (str, int, float)) else 0.0
    except ValueError:
        delay = 0.0
    return delay if math.isfinite(delay) and delay > 0 else float(config.IMAGE_FLOOR)


async def transmit(bot: Bot, board: Message, send, embeds: list[discord.Embed], files: list[discord.File] | None,
                   keys: tuple[str, ...], edit: bool, *, upload_wait: float, image_wait: float) -> discord.Message:
    """Deliver once, preserving metadata when Discord leaves the outcome uncertain."""
    payload = prepare(bot, board, embeds, bool(files))
    if files is None:
        try:
            with Watch(lambda delay, first: limited(bot, board, delay, first, False, body=True)):
                msg = await asyncio.wait_for(
                    send(embeds=payload, **attachments(board, payload)), upload_wait)
        except discord.HTTPException as e:
            if e.status == 429:
                bot.rate_budget.observe(e.response, "PATCH" if edit else "POST",
                                        f"/channels/{board.channel_id}/messages/{board.message_id}")
                limited(bot, board, retry_delay(e), False, False)
            raise
        board.embeds = payload
        return msg
    now = time.monotonic()
    board.image_at = now
    if files:
        wall = time.time()
        bot._upload_wall, bot._upload_at = wall, now
        board.image_slot = bot._slot(board.image_every)
        bot._save_state()
    extra: dict[str, list] = {"attachments": []} if edit else {}  # an edit drops the old images
    watch = Watch(lambda delay, first: limited(bot, board, delay, first, bool(files), body=True))
    try:
        with watch:
            msg = await asyncio.wait_for(send(embeds=payload, files=files, **extra), image_wait)
    except TimeoutError:
        board.image_wanted, board.image_slot = True, None
        board.image_uncertain = edit
        board.retry_at = max(board.retry_at, bot.rate_budget.next(route(board), time.time(), bool(files)))
        raise
    except discord.HTTPException as e:
        board.image_wanted, board.image_slot = True, None
        if e.status != 429:
            raise
        log.warning("Discord refused or held the image upload in %s (%s); rescheduling the upload",
                    board.channel_id, f"{e.code} {e.text}")
        board.image_wanted = True
        board.image_slot = None
        bot.rate_budget.observe(e.response, "PATCH" if edit else "POST",
                                f"/channels/{board.channel_id}/messages/{board.message_id}", bool(files))
        delay = retry_delay(e)
        limited(bot, board, delay, not watch.seen, bool(files))
        if board.msg is not None:
            return board.msg
        raise
    board.embeds = payload
    remember(board, payload)
    board.image_keys, board.image_wanted = keys, False
    board.image_uncertain = False
    board.image_slot = bot._slot(board.image_every)
    if files:
        bot._image_spacing = recovered_spacing(bot._image_spacing)
        wall, now = time.time(), time.monotonic()
        bot._upload_wall = math.ceil(wall / TICK) * TICK
        bot._upload_at = now + bot._upload_wall - wall
        board.image_at = now
        board.image_slot = bot._slot(board.image_every)
        bot._save_state()
    return msg

