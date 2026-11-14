"""Output message state and balanced, ordered section packing."""
from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from .panelwork import RenderSnapshot

EMBED_COUNT = 10


@dataclass
class Message:
    channel_id: int
    mode: str | None = None
    message_id: int | None = None
    post_nonce: str | None = None
    post_at: float | None = None
    post_marker: str | None = None
    msg: discord.Message | None = None
    modes: dict[str, str] = field(default_factory=dict)
    embeds: list[discord.Embed] = field(default_factory=list)
    image_every: int = 60
    edit_every: int = 60
    render: dict[str, str] = field(default_factory=dict)
    sections: list[str] | None = None
    edit_slot: int | None = None
    image_at: float = -math.inf
    image_slot: int | None = None
    image_keys: tuple[str, ...] | None = None
    image_wanted: bool = False
    image_uncertain: bool = False
    swept: bool = False
    images: dict[str, discord.Embed] = field(default_factory=dict)
    render_at: float | None = None
    retry_at: float = 0.0
    phase: float = 0.0
    position: int = 0
    owner: Board | None = field(default=None, repr=False)
    expected: tuple[str, ...] | None = None
    planned: list[discord.Embed] = field(default_factory=list)
    embed_keys: list[str] = field(default_factory=list)
    snapshot: RenderSnapshot | None = field(default=None, repr=False)


@dataclass
class Board(Message):
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)
    messages: list[Message] = field(default_factory=list)
    surplus: list[Message] = field(default_factory=list)
    singletons: set[str] = field(default_factory=set)
    layout_ready: bool = False
    scheduled: bool = False

    def __post_init__(self) -> None:
        self.messages = [self]


def child(board: Board, position: int) -> Message:
    return Message(board.channel_id, mode=board.mode, image_every=board.image_every, edit_every=board.edit_every,
                   render=board.render, sections=board.sections, phase=board.phase, owner=board, position=position)


def restore_ids(value: object) -> list[int]:
    values = [value] if type(value) is int else value
    if not isinstance(values, list) or not all(type(v) is int and v > 0 for v in values):
        return []
    return list(dict.fromkeys(values))


def message_state(boards: Sequence[Board]) -> dict[str, int | list[int]]:
    out: dict[str, int | list[int]] = {}
    for board in boards:
        ids = [message.message_id for message in [*board.messages, *board.surplus] if message.message_id]
        if ids:
            out[str(board.channel_id)] = ids[0] if len(ids) == 1 else ids
    return out


def pack(embeds: Sequence[discord.Embed], limit: int = 6000,
         alone: set[int] | None = None, weights: Sequence[int] | None = None) -> list[list[discord.Embed]]:
    """Minimize message count, then squared sizes, with a larger first group winning ties."""
    if not embeds:
        return []
    prefix = [0]
    singles = [0]
    for i, embed in enumerate(embeds):
        weight = weights[i] if weights is not None else len(embed)
        if weight > limit:
            raise ValueError("section exceeds message limit")
        prefix.append(prefix[-1] + weight)
        singles.append(singles[-1] + int(alone is not None and i in alone))
    best = [(0, 0, 0, 0)]
    for end in range(1, len(prefix)):
        choices = []
        for start in range(max(0, end - EMBED_COUNT), end):
            size = prefix[end] - prefix[start]
            if size <= limit and (end - start == 1 or singles[end] == singles[start]):
                count, cost, first, _ = best[start]
                choices.append((count + 1, cost + size * size, first if count else -end, start))
        best.append(min(choices))
    groups = []
    end = len(embeds)
    while end:
        start = best[end][3]
        groups.append(list(embeds[start:end]))
        end = start
    return groups[::-1]
