"""The /ratelimits command: the learned Discord rate budget, shown privately."""
from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING, Any

import discord

from . import view

if TYPE_CHECKING:
    from .bot import Bot

LIMIT = 1900
MAJOR = re.compile(r"/(channels|guilds|webhooks)/(\d+)$")


def rate(value: float | None) -> str:
    if value is None or not math.isfinite(value) or value <= 0:
        return "none"
    return f"{value:.2f}/s" if value >= 1 else f"1 per {1 / value:.0f}s"


def where(route: str) -> str:
    match = MAJOR.search(route)
    if not match:
        return route
    kind, num = match.groups()
    label = f"<#{num}>" if kind == "channels" else kind[:-1]
    return f"{route[:match.start()]} in {label}"


def line(key: str, row: dict[str, Any], tz: Any) -> str:
    route = key.split("|", 1)[0]
    last = row.get("last_429") or 0
    parts = [f"target {rate(row.get('target_rate'))}"]
    if row.get("header_limit"):
        parts.append(f"limit {row['header_limit']} per {row.get('window') or 0:g}s")
    if row.get("buffer") is not None:
        parts.append(f"buffer {row['buffer']}, {row.get('remaining')} left")
    parts.append(f"{row.get('429_count', 0)} x 429" + (f", last {view.clock(last, tz)}" if last else ""))
    if row.get("scope"):
        parts.append(f"scope {row['scope']}")
    if row.get("divergent"):
        parts.append("media and text differ")
    return f"**{row.get('class', '?')}** `{where(route)}`\n" + ", ".join(parts)


def report(summary: dict[str, dict[str, Any]], tz: Any) -> str:
    if not summary:
        return "No rate data yet. The budget learns from Discord replies as the panel updates."
    rows = sorted(summary.items(), key=lambda kv: (-(kv[1].get("429_count") or 0), kv[0]))
    out = ["Discord rate budget (keeps a 10% buffer under each learned limit):"]
    for i, (key, row) in enumerate(rows):
        text = line(key, row, tz)
        if sum(len(s) + 1 for s in out) + len(text) > LIMIT:
            out.append(f"...and {len(rows) - i} more.")
            break
        out.append(text)
    return "\n".join(out)


def register(bot: Bot) -> None:
    from .bot import denied

    @bot.slash_command(description="Show the learned Discord rate budget")
    async def ratelimits(ctx: discord.ApplicationContext) -> None:
        if await denied(bot, ctx, "view"):
            return
        await ctx.respond(report(bot.rate_budget.summary(), bot.tz), ephemeral=True,
                          allowed_mentions=discord.AllowedMentions.none())
