from __future__ import annotations

import asyncio
import fnmatch
import io
import logging
from dataclasses import replace
from typing import Any, Literal, cast

import aiohttp
import discord

from . import bot as B
from .docker import Container, Docker

log = logging.getLogger("shazzzbot")
Kind = Literal["stack", "group"]
BATCH_TIMEOUT = 12 * 60


async def reply(ctx: discord.ApplicationContext, text: str, *, edit: bool = False,
                view: discord.ui.View | None = None) -> None:
    options: dict[str, Any] = {"allowed_mentions": discord.AllowedMentions.none(), "view": view}
    if len(text) > 1900:
        if edit:
            await ctx.edit(content="Restart results are attached below.", **options)
            edit = False
        options["file"] = discord.File(io.BytesIO(text.encode()), filename="restart-details.txt")
        text = "Restart details are in the attached file."
    if edit:
        await ctx.edit(content=text, **options)
    else:
        await ctx.respond(text, ephemeral=True, **options)


def blocked(bot: B.Bot, names: list[str]) -> list[str]:
    return [name for name in names if not any(fnmatch.fnmatchcase(name, glob) for glob in bot.cfg.restart_allow)]


def labels(names: list[str]) -> str:
    return ", ".join("`" + name.replace("`", "'") + "`" for name in names)


async def ready(bot: B.Bot, ctx: discord.ApplicationContext, names: list[str], *, single: bool = False) -> bool:
    if await B.denied(bot, ctx, "operate"):
        return False
    message = None
    if not bot.docker:
        message = "Docker is not configured."
    elif not names:
        message = "No containers match."
    elif disallowed := blocked(bot, names):
        message = (f"`{names[0]}` is not in restarts.allow." if single else
                   f"Not in restarts.allow: {labels(disallowed)}. Nothing changed.")
    elif bot.audit_channel() is None:
        message = B.NO_AUDIT
    elif single and bot.containers and not any(c.name == names[0] for c in bot.containers):
        message = f"No container named `{names[0]}`."
    if message is None:
        return True
    if single:
        await ctx.respond(message, ephemeral=True)
    else:
        await reply(ctx, message)
    return False


async def restart_container(bot: B.Bot, ctx: discord.ApplicationContext, container: str) -> None:
    if not await ready(bot, ctx, [container], single=True):
        return
    docker = cast(Docker, bot.docker)
    if not await B.confirm(ctx, f"Restart `{container}`?"):
        return
    try:  # a fresh list, so a container renamed since the last poll is never restarted under its old name
        target = next((c for c in await docker.list() if c.name == container), None)
    except (TimeoutError, aiohttp.ClientError) as e:
        log.warning("docker list failed: %s", e)
        target = None
    if not target:
        await ctx.edit(content=f"`{container}` could not be confirmed. Nothing changed.")
        return
    if not await bot.audit(ctx.author, f"is restarting `{target.name}`"):
        await ctx.edit(content=B.NO_AUDIT)
        return
    try:
        await docker.restart(target.id)
    except (TimeoutError, aiohttp.ClientError) as e:
        log.warning("restart of %s failed: %s", target.name, e)
        await ctx.edit(content=B.FAILED)
        await bot.audit(ctx.author, f"restart of `{target.name}` FAILED: {e}")
        return
    await ctx.edit(content=f"Restarted `{target.name}`.")
    await bot.audit(ctx.author, f"restarted `{target.name}`")


def matches(bot: B.Bot, container: Container, kind: Kind, name: str) -> bool:
    if kind == "stack":
        return bool(name) and container.project == name
    return any(fnmatch.fnmatchcase(container.name, glob) for glob in bot.cfg.restart_groups.get(name, []))


async def confirm(ctx: discord.ApplicationContext, names: list[str]) -> bool:
    view = B.Confirm(ctx.author.id)
    await reply(ctx, "Restart these containers?\n" + labels(names), view=view)
    if await view.wait():
        await reply(ctx, "Timed out. Nothing changed.", edit=True)
    return bool(view.ok)


async def run(bot: B.Bot, ctx: discord.ApplicationContext, targets: list[Container], current: dict[str, Container],
              kind: Kind, name: str, restarted: list[str]) -> bool:
    docker = cast(Docker, bot.docker)
    for target in targets:
        fresh = current.get(target.id)
        result = f"restart of {labels([target.name])} FAILED: no longer present in the confirmed selection"
        if fresh and fresh.name == target.name and matches(bot, fresh, kind, name):
            try:
                await docker.restart(target.id)
            except (TimeoutError, aiohttp.ClientError) as error:
                log.warning("restart of %s failed: %s", target.name, error)
                result = f"restart of {labels([target.name])} FAILED"
            else:
                restarted.append(target.name)
                result = f"restarted {labels([target.name])}"
        if not await bot.audit(ctx.author, result):
            return False
    return True


async def restart_many(bot: B.Bot, ctx: discord.ApplicationContext, kind: Kind, name: str) -> None:
    targets = list({c.id: replace(c) for c in bot.containers if matches(bot, c, kind, name)}.values())
    names = [c.name for c in targets]
    if not await ready(bot, ctx, names) or not await confirm(ctx, names):
        return
    selection = name[:200].replace("`", "'")
    description = f"restart-{kind} {selection} ({len(names)} containers)"
    if not await bot.audit(ctx.author, "starting " + description):
        await reply(ctx, B.NO_AUDIT, edit=True)
        return
    restarted: list[str] = []
    audit_ok = True
    note = ""
    try:
        docker = cast(Docker, bot.docker)
        current = {c.id: c for c in await docker.list()}
    except (TimeoutError, aiohttp.ClientError) as error:
        log.warning("docker list failed: %s", error)
        current = {}
    if disallowed := blocked(bot, names):
        audit_ok = await bot.audit(ctx.author, "refused after confirmation: " + labels(disallowed))
        note = f"\nNot in restarts.allow: {labels(disallowed)}. Nothing changed."
    else:
        try:
            audit_ok = await asyncio.wait_for(run(bot, ctx, targets, current, kind, name, restarted), BATCH_TIMEOUT)
        except TimeoutError:
            log.warning("%s timed out", description)
            note = "\nRestart sequence timed out; remaining restarts were not confirmed."
    done = set(restarted)
    failed = [target for target in names if target not in done]
    audit_ok = await bot.audit(ctx.author, f"ended {description}: {len(restarted)} restarted, {len(failed)} failed") \
        and audit_ok
    text = f"Restarted: {labels(restarted) or 'none'}\nFailed: {labels(failed) or 'none'}" + note
    if not audit_ok:
        text += "\nAudit logging failed; no further restarts were attempted."
    await reply(ctx, text, edit=True)


def choices(bot: B.Bot, kind: Kind, value: Any) -> list[str]:
    names = {c.project for c in bot.containers} if kind == "stack" else set(bot.cfg.restart_groups)
    query = str(value or "").lower()
    return [name for name in sorted(names) if name and len(name) <= 100 and query in name.lower()][:25]


def register(bot: B.Bot, group: discord.SlashCommandGroup) -> None:
    async def stacks(ctx: discord.AutocompleteContext) -> list[str]:
        return choices(bot, "stack", ctx.value)

    async def groups(ctx: discord.AutocompleteContext) -> list[str]:
        return choices(bot, "group", ctx.value)

    @group.command(name="restart-stack", description="Restart all containers in a compose stack")
    @discord.option("stack", str, description="Compose project name", autocomplete=stacks)
    async def restart_stack(ctx: discord.ApplicationContext, stack: str) -> None:
        await restart_many(bot, ctx, "stack", stack)

    @group.command(name="restart-group", description="Restart a configured group of containers")
    @discord.option("group", str, description="Configured restart group", autocomplete=groups)
    async def restart_group(ctx: discord.ApplicationContext, group: str) -> None:
        await restart_many(bot, ctx, "group", group)
