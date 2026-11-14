"""Bound repeated Discord authorization failures and admit one recovery probe."""
import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import aiohttp
import discord

log = logging.getLogger("shazzzbot")
FAILURES = 3
PROBE_EVERY = 900.0
NOTICE_WAIT = 20.0
_CHANNEL = re.compile(r"(?:^|/)channels/(\d+)(?:/|$)")


class Blocked(Exception):
    pass


@dataclass
class _State:
    failures: int = 0
    until: float = 0.0
    probing: bool = False

    def blocked(self, now: float) -> bool:
        return self.failures >= FAILURES and (self.probing or now < self.until)


@dataclass(frozen=True)
class Ticket:
    channel: int | None
    global_state: _State
    local_state: _State
    global_probe: bool
    local_probe: bool


class Guard:
    def __init__(self) -> None:
        self._global = _State()
        self._paths: dict[int, _State] = {}

    def begin(self, path: str) -> Ticket:
        path = urlsplit(path).path
        match = _CHANNEL.search(path)
        channel = int(match[1]) if match else None
        local = self._paths.setdefault(channel, _State()) if channel is not None else _State()
        now = time.monotonic()
        if self._global.blocked(now) or local.blocked(now):
            raise Blocked("Discord authorization path is paused")
        global_probe = self._global.failures >= FAILURES
        local_probe = local.failures >= FAILURES
        for state, probe in ((self._global, global_probe), (local, local_probe)):
            if probe:
                state.probing, state.until = True, now + PROBE_EVERY
        return Ticket(channel, self._global, local, global_probe, local_probe)

    def finish(self, ticket: Ticket, status: int | None) -> bool:
        if ticket.global_state is not self._global:
            return False
        for state, probe in ((ticket.global_state, ticket.global_probe), (ticket.local_state, ticket.local_probe)):
            if probe:
                state.probing = False
        if status is not None and 200 <= status < 300:
            for state in (ticket.global_state, ticket.local_state):
                state.failures, state.until, state.probing = 0, 0.0, False
            return False
        if status not in (401, 403):
            return False
        if status == 403 and ticket.channel is None:
            log.warning("Discord 403 on a route without a channel; authorization circuit unchanged")
            return False
        state = ticket.global_state if status == 401 else ticket.local_state
        opened = state.failures < FAILURES
        state.failures = min(FAILURES, state.failures + 1)
        if state.failures < FAILURES:
            return False
        state.until = max(state.until, time.monotonic() + PROBE_EVERY)
        if opened:
            if status == 401:
                log.error("Discord bot authorization blocked after %s 401 replies; probing every 15 minutes",
                          FAILURES)
            else:
                log.error("Discord channel %s blocked after %s consecutive 403 replies; probing every 15 minutes",
                          ticket.channel, FAILURES)
        return opened

    def blocked(self, channel_id: int | None = None) -> bool:
        now = time.monotonic()
        local = self._paths.get(channel_id) if channel_id is not None else None
        return self._global.blocked(now) or local is not None and local.blocked(now)

    def check(self, ticket: Ticket) -> None:
        local = self._paths.get(ticket.channel) if ticket.channel is not None else None
        for state, owner, probe in ((self._global, ticket.global_state, ticket.global_probe),
                                    (local, ticket.local_state, ticket.local_probe)):
            if state is not None and state.failures >= FAILURES \
                    and not (state is owner and probe and state.probing):
                raise Blocked("Discord authorization path is paused")

    def reset(self) -> None:
        self._global = _State()
        self._paths.clear()


async def notify(bot: Any, channel_id: int | None) -> None:
    audit = bot.audit_channel()
    if channel_id is None or audit is None or audit.id == channel_id or audit.guild.id != bot.cfg.guild_id \
            or bot.rate_budget.auth.blocked(audit.id):
        return
    try:
        await asyncio.wait_for(audit.send(
            f"Output <#{channel_id}> blocked after 3 Discord 403 replies; checking again in 15 minutes.",
            allowed_mentions=discord.AllowedMentions.none()), NOTICE_WAIT)
    except (discord.HTTPException, Blocked, TimeoutError, aiohttp.ClientError):
        log.warning("could not post the Discord authorization notice to the audit channel")
