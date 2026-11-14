"""The /chartgen command: up to four history charts, one per row, sent to the caller's DMs."""
from __future__ import annotations

import asyncio
import io
import logging
import math
import time
from collections.abc import Sequence
from datetime import datetime, tzinfo
from typing import TYPE_CHECKING, Any, NamedTuple

import discord
from PIL import Image, ImageDraw

from . import charts, discordauth, renderbudget, view
from .panel import FINE, Part, Section
from .spark import bucket_layout, spark

if TYPE_CHECKING:
    from .bot import Bot

log = logging.getLogger(__name__)
COOLDOWN = 60.0  # seconds between requests from one user
MAX_SERIES = 4
MODES = ("image", "rich", "text")
GRAPH_COLS = 28  # as charts.section's text graphs
KEY = "chartgen"
LIST = "list"


class Series(NamedTuple):
    key: str
    label: str
    unit: str
    col: int  # column in a history row
    top: float | None  # fixed top of scale, or None for auto
    colour: charts.RGB = charts.BLUE

    @property
    def name(self) -> str:
        return f"{self.label} ({self.unit})"


BASE = (Series("cpu", "CPU", "%", 1, 100.0), Series("ram", "RAM", "%", 2, 100.0, charts.GREEN),
        Series("disk-read", "Disk read", "MB/s", 3, None),
        Series("disk-write", "Disk write", "MB/s", 4, None, charts.GREEN),
        Series("net-down", "Net down", "Mbps", 5, None), Series("net-up", "Net up", "Mbps", 6, None, charts.GREEN))


def available(samples: Sequence[charts.Row]) -> list[Series]:
    """The series the store holds data for; per-core CPU only on a multi-core host, as in charts.render."""
    if not samples:
        return []
    cores = len(samples[-1]) - 7
    return [*BASE, *(Series(f"core-{k + 1}", f"CPU core {k + 1}", "%", 7 + k, 100.0, charts.core_colour(k, cores))
                     for k in range(cores if cores > 1 else 0))]


def matches(series: Sequence[Series], typed: str) -> list[discord.OptionChoice]:
    q = typed.strip().lower()
    hits = [discord.OptionChoice(s.name, s.key) for s in series if q in s.key or q in s.name.lower()]
    return hits[:24] + ([discord.OptionChoice("list -- show every option", LIST)] if LIST.startswith(q) else [])


def options(series: Sequence[Series], window: int) -> str:
    base = [f"`{s.key}` {s.name}" for s in series if not s.key.startswith("core-")]
    cores = len(series) - len(base)
    lines = ["Series with data: " + (", ".join(base) if base else "none yet, the bot is still sampling") + "."]
    if cores:
        lines.append(f"Per-core CPU: `core-1` to `core-{cores}` (%).")
    lines += [f"Spans: 5m to 7d, like 45m, 6h or 3d. Default: {charts.span(window)}.",
              "Modes: image (default), rich, text.",
              f"Up to {MAX_SERIES} series, one chart per row, sent to your DMs. One request per {COOLDOWN:.0f}s."]
    return "\n".join(lines)


def rows(samples: Sequence[charts.Row], s: Series, window: int, now: float) -> list[charts.Row]:
    return [r for r in samples if now - window <= r[0] <= now and len(r) > s.col]


def spec(s: Series) -> tuple:
    return s.label, [(s.col, s.colour, "")], s.unit, s.top


@renderbudget.serialized
def image(picked: Sequence[tuple[Series, list]], window: int, now: float, tz: tzinfo | None, gap: float) -> bytes:
    """The header of charts.render, then each series as one full-width chart row."""
    fonts = charts._fonts()
    stamp = datetime.fromtimestamp(now, tz).strftime("%a %b %d %Y, %I:%M:%S %p %Z").replace(" 0", " ")
    title, label = f"Charts ({charts.span(window)})", f"Updated {stamp}"
    width = max(charts.W, *(charts.chart_width(r, spec(s), fonts) for s, r in picked),
                math.ceil(70 + charts.text_width(fonts[1], title) + charts.text_width(fonts[2], label)))
    height = charts.HEAD_H + len(picked) * charts.CHART_H
    if not renderbudget.check(KEY, 4 * (charts.SS * charts.SS + 4) * width * height + 131072):
        return view.placeholder(KEY, title, width, now, tz)
    im = Image.new("RGB", (width, height), charts.BG)
    d = ImageDraw.Draw(im)
    lines = Image.new("RGBA", (width * charts.SS, height * charts.SS), (0, 0, 0, 0))
    d.text((22, 14), title, font=fonts[1], fill=charts.DIM)
    d.text((width - 24, 18), label, font=fonts[2], fill=charts.DIM, anchor="ra")
    ld = ImageDraw.Draw(lines)
    for n, (s, r) in enumerate(picked):
        y = charts.HEAD_H + n * charts.CHART_H
        holes = charts.gaps([x[0] for x in r], now - window, now, gap)
        charts._chart(d, ld, fonts, (0, y, width, y + charts.CHART_H), r, now, window, spec(s), holes)
    small = lines.reduce(charts.SS)
    im.paste(small, (0, 0), small)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def section(s: Series, r: list, window: int, now: float) -> Section:
    """One series as a panel Section, so view.rich and view.text draw it as they draw the panel."""
    title = f"{s.name}, last {charts.span(window)}"
    vals = [(x[0], x[s.col]) for x in r]
    if not vals:
        return Section(KEY, title, [], note="No samples in this span.", stamp=True)
    bucket, cols = bucket_layout(window, GRAPH_COLS)
    graph = spark(vals, cols, start=now - window, end=now, bucket=bucket)
    nums = [v for _, v in vals]

    def m(v: float) -> str:
        return charts._measure(v, s.unit)
    stats = [(FINE, ("Now", m(nums[-1]))), (FINE, ("Peak", m(max(nums)))), (FINE, ("Low", m(min(nums)))),
             (FINE, ("Average", m(sum(nums) / len(nums))))]
    return Section(KEY, title, [Part([(FINE, (graph,))], f"Last {charts.span(window)}", cut=None, only="text"),
                                Part([*stats, (FINE, ("Trend", graph))], grid=True, only="embed"),
                                Part(stats, "Values", only="text")], stamp=True)


@renderbudget.serialized
def build(picked: Sequence[tuple[Series, list]], window: int, now: float, mode: str, tz: tzinfo | None,
          gap: float) -> tuple[list[discord.Embed], bytes | None]:
    """Blocking: run it in a worker thread."""
    if mode == "image":
        e = view.embed(Section(KEY, f"Charts, last {charts.span(window)}", []), "image")
        return [e], image(picked, window, now, tz, gap)
    return [view.embed(section(s, r, window, now), "embed" if mode == "rich" else "text") for s, r in picked], None


def payload(embeds: list[discord.Embed], png: bytes | None) -> dict[str, Any]:
    """Fresh each send: a discord.File is spent once uploaded."""
    return {"embeds": embeds, "files": [discord.File(io.BytesIO(png), f"{KEY}.png")] if png else [],
            "allowed_mentions": discord.AllowedMentions.none()}


async def deliver(ctx: discord.ApplicationContext, embeds: list[discord.Embed], png: bytes | None) -> str:
    try:
        await ctx.author.send(**payload(embeds, png))
    except discord.Forbidden:
        await ctx.respond("I cannot DM you (your DMs are closed to this server), so here it is privately.",
                          ephemeral=True, **payload(embeds, png))
        return "ephemeral fallback"
    except (discord.HTTPException, discordauth.Blocked):
        log.exception("chartgen DM failed")
        await ctx.respond("That did not work. The details are in the bot log.", ephemeral=True)
        return "error"
    except TimeoutError:
        await ctx.respond("The Discord rate budget is full right now. Try again in a few minutes.", ephemeral=True)
        return "error"
    await ctx.respond("Sent to your DMs.", ephemeral=True)
    return "sent to DM"


def audit_line(series: Sequence[str], span: str, mode: str, outcome: str) -> str:
    fields = (("series", ",".join(value for value in series if value.strip()), 200),
              ("span", span, 32), ("mode", mode, 16), ("outcome", outcome, 40))
    return "/chartgen " + " ".join(f"{name}={' '.join(value.split())[:limit] or '-'}" for name, value, limit in fields)


def register(bot: Bot) -> None:
    from .bot import denied

    last: dict[int, float] = {}
    busy = asyncio.Lock()  # ponytail: one render at a time bot-wide, a semaphore if one is too few

    async def series_choices(actx: discord.AutocompleteContext) -> list[discord.OptionChoice]:
        return matches(available(bot.history.samples), str(actx.value or ""))

    async def spans(actx: discord.AutocompleteContext) -> list[str]:
        return charts.span_choices(str(actx.value or ""), bot.cfg.history_days * charts.DAY)

    hint = 'Type to filter, or pick "list" to see every option'

    @bot.slash_command(name="chartgen", description="DM yourself up to four history charts")
    @discord.option("series1", str, description=hint, autocomplete=series_choices)
    @discord.option("series2", str, description="Optional second chart", autocomplete=series_choices,
                    required=False, default="")
    @discord.option("series3", str, description="Optional third chart", autocomplete=series_choices,
                    required=False, default="")
    @discord.option("series4", str, description="Optional fourth chart", autocomplete=series_choices,
                    required=False, default="")
    @discord.option("span", str, description="5m to 7d, like 45m or 6h", autocomplete=spans,
                    required=False, default="")
    @discord.option("mode", str, description="How to draw it", choices=list(MODES), required=False, default="image")
    async def chartgen(ctx: discord.ApplicationContext, series1: str, series2: str = "", series3: str = "",
                       series4: str = "", span: str = "", mode: str = "image") -> None:
        if await denied(bot, ctx, "view"):
            return
        outcome = "error"
        audit_span = span if span.strip() else charts.span(bot.window)
        try:
            samples = list(bot.history.samples)
            known = {s.key: s for s in available(samples)}
            wanted = list(dict.fromkeys(k.strip().lower() for k in (series1, series2, series3, series4) if k.strip()))
            window = charts.parse_span(span) if span.strip() else bot.window
            unknown = [k for k in wanted if k not in known]
            if not wanted or LIST in wanted or unknown or window is None or mode not in MODES:
                why = (f"Unknown series: {', '.join(unknown)[:200]}.\n" if unknown and LIST not in wanted else
                       "Use a span from 5m to 7d.\n" if window is None and LIST not in wanted else "")
                await ctx.respond(why + options(list(known.values()), bot.window), ephemeral=True,
                                  allowed_mentions=discord.AllowedMentions.none())
                outcome = "options"
                return
            window = min(window, bot.cfg.history_days * charts.DAY)
            audit_span = charts.span(window)
            now_m = time.monotonic()
            wait = last.get(ctx.author.id, -math.inf) + COOLDOWN - now_m
            if wait > 0:
                await ctx.respond(f"You can request another chart in {math.ceil(wait)} seconds.", ephemeral=True)
                outcome = "cooldown"
                return
            if busy.locked():
                await ctx.respond("Another chart is rendering. Try again in a few seconds.", ephemeral=True)
                outcome = "busy"
                return
            async with busy:
                for user in [u for u, t in last.items() if t + COOLDOWN <= now_m]:
                    del last[user]
                last[ctx.author.id] = now_m
                await ctx.defer(ephemeral=True)
                now = time.time()
                picked = [(known[k], rows(samples, known[k], window, now)) for k in wanted]
                embeds, png = await asyncio.to_thread(build, picked, window, now, mode, bot.tz, 3 * bot.cfg.interval)
                outcome = await deliver(ctx, embeds, png)
        finally:
            await bot.audit(ctx.author, audit_line((series1, series2, series3, series4),
                                                  audit_span, mode, outcome))
