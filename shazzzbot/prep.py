"""Start scheduled panel work early, so each post leaves at its boundary instead of after the render."""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from . import delivery, panel
from .boards import Board, Message

if TYPE_CHECKING:
    from .bot import Bot
    from .panelwork import RenderSnapshot

DEFAULT = 5.0
LOW, HIGH = 3.0, 30.0  # LOW stays above the 2s loop, so a boundary is never skipped between two checks
ALPHA = 0.3
LATE = 0.01  # asyncio can wake a hair before the boundary, which would leave the slot unchanged
hold = asyncio.sleep


class Lead:
    """Seconds to start early per output kind: a moving average of measured prep time, plus margin."""

    def __init__(self) -> None:
        self.took: dict[str, float] = {}

    def __call__(self, kind: str) -> float:
        took = self.took.get(kind)
        return DEFAULT if took is None else min(HIGH, max(LOW, took * 1.5 + 1))

    def observe(self, kind: str, seconds: float) -> None:
        took = self.took.get(kind)
        self.took[kind] = seconds if took is None else took + ALPHA * (seconds - took)


@dataclass
class Prepared:
    at: float
    sections: list[panel.Section]
    files: dict[tuple[int, int], tuple[tuple[str, ...], list[tuple[str, bytes]]]] = field(default_factory=dict)
    snapshot: RenderSnapshot | None = None


def upcoming(bot: Bot, now: float) -> tuple[float, list[Message]] | None:
    """The next scheduled send after now, and the messages planned to upload images at that moment."""
    messages = [message for board in bot.boards for message in board.messages]
    edits = [delivery.refresh_at(bot, m, now if m.edit_slot != delivery.edit_slot(bot, m)
                                 else bot._boundary(now - m.phase, m.edit_every) + m.phase)
             for m in messages if not delivery.keys(bot, m)]
    plan = delivery.message_schedule(bot, now)
    images = [(m, times[0]) for m in messages if (times := plan.get(delivery.identity(m)))]
    future = [t for t in (*edits, *(t for _, t in images)) if now < t < math.inf]
    if not future:
        return None
    at = min(future)
    return at, [m for m, t in images if t <= at]


async def ahead(bot: Bot) -> Prepared | None:
    """Collect and draw for the next boundary once it is within the lead, then hold until it."""
    now = time.time()
    found = upcoming(bot, now)
    if found is None:
        return None
    at, uploads = found
    if at - now > bot.lead("image" if uploads else "text"):
        return None
    started = time.monotonic()
    prepared = Prepared(at, await bot.collect(at), snapshot=bot._snapshot)
    bot.lead.observe("text", time.monotonic() - started)
    for message in uploads:
        owner = cast(Board, message.owner or message)
        if owner.lock.locked():
            continue
        async with owner.lock:
            async with bot._lock:
                message.snapshot = prepared.snapshot
            prepared.files[delivery.identity(message)] = (delivery.keys(bot, message),
                                                          await bot.draw_images(message, at))
    if prepared.files:
        bot.lead.observe("image", time.monotonic() - started)
    if (delay := at - time.time()) > 0:
        await hold(delay + LATE)
    return prepared
