"""Recover an old uncertain panel post before creating another message."""
import asyncio
import hashlib
import json
import posixpath
import re
import time
from typing import Any
from urllib.parse import urlsplit

import discord

from .boards import Message

NONCE_WINDOW = 120.0
SCAN_LIMIT = 20
SCAN_WAIT = 20.0


def marker(embeds: list[discord.Embed]) -> str:
    data = []
    for embed in embeds:
        if not isinstance(embed, discord.Embed):
            continue
        item: dict[str, Any] = dict(embed.to_dict())
        for key in ("image", "thumbnail"):
            if key in item:
                url = item[key].get("url", "")
                path = urlsplit(url)
                item[key] = {"url": posixpath.basename(path.path) or path.netloc}
        for key in ("footer", "author"):
            if key in item:
                item[key] = {field: value for field, value in item[key].items() if field != "proxy_icon_url"}
        if "text" in item.get("footer", {}):
            item["footer"]["text"] = re.sub(r"Update at[^)]*", "Update at", item["footer"]["text"])
        data.append(item)
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=True).encode()).hexdigest() if data else ""


async def recover(bot: Any, message: Message, channel: discord.TextChannel) -> discord.Message | None:
    if message.post_at is None or time.monotonic() - message.post_at <= NONCE_WINDOW:
        return None
    if bot.user is None:
        raise TimeoutError("cannot verify the author of an uncertain panel post")
    tracked = {entry.message_id for board in bot.boards if board.channel_id == message.channel_id
               for entry in (*board.messages, *board.surplus) if entry.message_id is not None}

    async def scan() -> discord.Message | None:
        async for candidate in channel.history(limit=SCAN_LIMIT):
            if candidate.author.id != bot.user.id or candidate.id in tracked:
                continue
            if message.post_nonce and getattr(candidate, "nonce", None) == message.post_nonce \
                    or message.post_marker and marker(candidate.embeds) == message.post_marker:
                return candidate
        return None

    return await asyncio.wait_for(scan(), SCAN_WAIT)
