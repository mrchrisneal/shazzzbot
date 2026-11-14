"""Observe and budget Discord requests without changing SDK retries."""
from __future__ import annotations

import asyncio
import contextvars
import copy
import logging
import math
import re
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

import aiohttp
import discord

from .config import IMAGE_FLOOR, MAX_IMAGE_SPACING
from .discordauth import Guard
from .discordauth import Ticket as AuthTicket
from .helper import formats

log = logging.getLogger("shazzzbot")
GLOBAL_LIMIT = 50
GLOBAL_USABLE = 45
MAX_RECORDS = 256
MAX_EVENTS = 256
DAY = 86400.0
PROBE_EVERY = 3600.0
VERSION = 1
PERSIST_EVERY = 60.0
AUTH: contextvars.ContextVar[AuthTicket | None] = contextvars.ContextVar("discord_auth", default=None)


def clean_path(path: str) -> str:
    return re.sub(r"(/(?:webhooks|interactions)/[^/]+/)[^/]+", r"\1[redacted]", urlsplit(path).path)


def route_key(method: str, path: str) -> str:
    parts = clean_path(path).split("/")
    major = ""
    for i, part in enumerate(parts):
        if part.isdigit():
            if i and parts[i - 1] in {"channels", "guilds", "webhooks"}:
                major += "/" + parts[i - 1] + "/" + part
            parts[i] = "{id}"
    return method + " " + "/".join(parts).removeprefix("/api/v10") + major


def number(value: Any, maximum: float = 10**12) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise TypeError("invalid number")
    parsed = float(value)
    if not math.isfinite(parsed) or not 0 <= parsed <= maximum:
        raise ValueError("invalid number")
    return parsed


@dataclass
class Bucket:
    limit: int
    remaining: int
    reset: float
    window: float

    @property
    def buffer(self) -> int:
        return math.ceil(self.limit / 10) if self.limit >= 2 else 0

    def at(self, at: float) -> float:
        return self.reset if at < self.reset and self.remaining <= self.buffer else at

    def take(self, at: float) -> None:
        if at >= self.reset:
            self.remaining = max(1, self.limit)
            self.reset += (math.floor((at - self.reset) / self.window) + 1) * self.window
        self.remaining -= 1


@dataclass
class Evidence:
    kind: str
    sends: deque[float] = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))
    failures: deque[tuple[float, int, float, float, float]] = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))
    target: float | None = None
    count: int = 0
    weight: float = 0.0
    last_429: float = 0.0
    retry: float = 0.0
    header_retry: float = 0.0
    body_retry: float = 0.0
    prior_target: float | None = None
    scope: str = "unknown"
    before: int = 0
    window: float = 0.0
    sample_window: float = 0.0
    limit: int = 0
    probe_at: float = 0.0
    until: float = 0.0
    divergent: bool = False

    def ceiling(self) -> float:
        buffer = math.ceil(self.limit / 10) if self.limit >= 2 else 0
        rate = ((max(1, self.limit) - buffer) / self.window
                if self.window and self.scope != "shared" else GLOBAL_USABLE)
        return min(rate, 1 / IMAGE_FLOOR if self.kind == "media" else GLOBAL_USABLE)

    def evolve(self, now: float) -> None:
        if self.target is not None and (not math.isfinite(self.target) or self.target <= 0):
            self.target = self.ceiling()
        if now >= max(self.last_429, self.probe_at) + PROBE_EVERY:
            steps = min(24, math.floor((now - max(self.last_429, self.probe_at)) / PROBE_EVERY))
            if self.target is not None:
                self.target = min(self.ceiling(), self.target * 1.05**steps)
            self.weight *= 0.5 ** (steps * PROBE_EVERY / DAY)
            self.probe_at = now


@dataclass
class Ticket:
    budget: Budget
    route: str
    media: bool
    at: float
    bucket: str
    reset: float
    used: bool = False
    released: bool = False


PERMIT: contextvars.ContextVar[Ticket | None] = contextvars.ContextVar("discord_budget_permit", default=None)
MEDIA: contextvars.ContextVar[bool] = contextvars.ContextVar("discord_media_request", default=False)


class Budget:
    def __init__(self) -> None:
        self.auth = Guard()
        self.routes: dict[str, str] = {}
        self.buckets: dict[str, Bucket] = {}
        self.evidence: dict[str, Evidence] = {}
        self.global_sends: deque[float] = deque(maxlen=GLOBAL_LIMIT)
        self.global_until = 0.0
        self.last_media = -math.inf
        self.summary_at = -math.inf

    def clone(self) -> Budget:
        return copy.deepcopy(self)

    def media_spacing(self, cap: float) -> float:
        spacing = float(IMAGE_FLOOR)
        for state in self.evidence.values():
            state.evolve(time.time())
            if state.kind == "media" and state.target is not None:
                spacing = max(spacing, min(cap, 1 / state.target if state.target else cap))
        return spacing

    def _state(self, route: str, media: bool, scope: str = "") -> Evidence:
        key = route + ("|media" if media else "|text") + ("|" + scope if scope else "")
        if key not in self.evidence:
            if len(self.evidence) >= MAX_RECORDS:
                del self.evidence[next(iter(self.evidence))]
            self.evidence[key] = Evidence("media" if media else "text")
            if scope:
                self.evidence[key].scope = scope
        return self.evidence[key]

    def _states(self, route: str, media: bool) -> list[Evidence]:
        prefix = route + ("|media|" if media else "|text|")
        return [self._state(route, media), *(self.evidence[key] for scope in ("user", "shared", "global")
                                            if (key := prefix + scope) in self.evidence)]

    def next(self, route: str, at: float, media: bool = False) -> float:
        if not math.isfinite(at):
            return math.inf
        for record in self.evidence.values():
            record.evolve(time.time())
        at = max(at, self.global_until)
        if media and math.isfinite(self.last_media):
            shared = [1 / state.target for state in self.evidence.values()
                      if state.kind == "media" and state.scope == "shared" and state.target is not None]
            at = max(at, self.last_media + max(shared, default=0.0))
        for state in self._states(route, media):
            at = max(at, state.until)
            if state.target is not None and state.sends:
                at = max(at, state.sends[-1] + 1 / state.target)
        bucket = self.buckets.get(self.routes.get(route, ""))
        if bucket:
            at = bucket.at(at)
        recent = [sent for sent in self.global_sends if at - 1 < sent <= at]
        if len(recent) >= GLOBAL_USABLE:
            at = max(at, recent[-GLOBAL_USABLE] + 1)
        return at

    def take(self, route: str, at: float, media: bool = False) -> None:
        self.global_sends.append(at)
        if media:
            self.last_media = at
        for state in self._states(route, media):
            state.sends.append(at)
        bucket = self.buckets.get(self.routes.get(route, ""))
        if bucket:
            bucket.take(at)

    async def acquire(self, route: str, media: bool = False) -> float:
        remaining = MAX_IMAGE_SPACING
        while (delay := self.next(route, time.time(), media) - time.time()) > 0:
            if not math.isfinite(delay) or delay > remaining:
                log.warning("Discord rate budget wait exceeds %ss; deferring %s", MAX_IMAGE_SPACING, route)
                raise TimeoutError("Discord rate budget wait exceeded")
            await asyncio.sleep(delay)
            remaining -= delay
        at = time.time()
        self.take(route, at, media)
        return at

    @asynccontextmanager
    async def permit(self, route: str, media: bool = False) -> AsyncIterator[None]:
        at = await self.acquire(route, media)
        key = self.routes.get(route, "")
        bucket = self.buckets.get(key)
        token = PERMIT.set(Ticket(self, route, media, at, key, bucket.reset if bucket else 0.0))
        try:
            yield
        finally:
            PERMIT.reset(token)

    def release(self, ticket: Ticket) -> None:
        if ticket.released:
            return
        ticket.released = True
        for events in (self.global_sends, *(state.sends for state in self._states(ticket.route, ticket.media))):
            try:
                events.remove(ticket.at)
            except ValueError:
                pass
        bucket = self.buckets.get(ticket.bucket)
        if bucket and bucket.reset == ticket.reset:
            bucket.remaining = min(max(1, bucket.limit), bucket.remaining + 1)
        if ticket.media:
            self.last_media = max((state.sends[-1] for state in self.evidence.values()
                                   if state.kind == "media" and state.sends), default=-math.inf)

    def _backoff(self, state: Evidence, now: float, delay: float) -> tuple[float, int, float, float, float]:
        recent = [sent for sent in state.sends if sent >= now - DAY]
        state.before = len(recent)
        if delay and recent:
            state.sample_window = now - recent[0] + delay
            observed = max(1, len(recent) - 1) / state.sample_window
            state.target = max(1 / DAY, 0.9 * min(state.prior_target or state.ceiling(), observed))
        return now, state.before, state.header_retry, state.body_retry, state.target or 0.0

    def defer(self, route: str, delay: float, media: bool, *, body: bool = True) -> None:
        delay = min(DAY, max(0.0, delay))
        state = self._state(route, media)
        now, missing = time.time(), state.body_retry == 0
        measured = self.evidence.get(route + ("|media|" if media else "|text|") + state.scope, state)
        for record in (state, measured):
            record.until = max(record.until, now + delay)
            if body:
                record.body_retry = delay
                record.retry = max(record.header_retry, delay)
        if body and state.scope == "global":
            self.global_until = max(self.global_until, state.until)
        if body and missing and state.failures and now - state.last_429 <= 1:
            event = self._backoff(measured, now, delay)
            measured.failures[-1] = state.failures[-1] = event
            state.before, state.sample_window = measured.before, measured.sample_window

    def observe(self, response: Any, method: str, path: str, media: bool = False) -> bool:
        if getattr(response, "_shazzzbot_budget_seen", None) is self:
            return False
        response._shazzzbot_budget_seen = self
        now, headers = time.time(), response.headers
        route = route_key(method, path)
        state = self._state(route, media)
        scope = headers.get("X-RateLimit-Scope", "unknown")
        if scope in {"user", "shared", "global"}:
            state.scope = scope
        try:
            limit = number(headers.get("X-RateLimit-Limit"), 10**6)
            remaining = number(headers.get("X-RateLimit-Remaining"), limit)
            window = number(headers.get("X-RateLimit-Reset-After"), DAY)
            name = headers.get("X-RateLimit-Bucket")
            if not limit.is_integer() or not remaining.is_integer() or not window \
                    or not isinstance(name, str) or not name:
                raise ValueError("invalid bucket")
            key = name[:128] + " " + "".join(re.findall(r"/(?:channels|guilds|webhooks)/[0-9]+", route))
            previous = self.buckets.get(key)
            if previous and now < previous.reset:
                remaining = min(remaining, previous.remaining)
            if key not in self.buckets and len(self.buckets) >= MAX_RECORDS:
                del self.buckets[next(iter(self.buckets))]
            self.buckets[key] = Bucket(int(limit), int(remaining), now + window,
                                       max(window, previous.window if previous else 0))
            if route not in self.routes and len(self.routes) >= MAX_RECORDS:
                del self.routes[next(iter(self.routes))]
            self.routes[route] = key
            state.limit, state.window = int(limit), self.buckets[key].window
            if state.target is not None:
                state.target = min(state.target, state.ceiling())
        except (ValueError, TypeError):
            pass
        if response.status == 429:
            report(response, method, path)
            try:
                header_delay = number(headers.get("Retry-After"), DAY)
            except (ValueError, TypeError):
                header_delay = 0.0
            try:
                body_delay = number(getattr(response, "_shazzzbot_body_retry", None), DAY)
            except (ValueError, TypeError):
                body_delay = 0.0
            delay = max(header_delay, body_delay)
            measured = self._state(route, media, state.scope) if state.scope != "unknown" else state
            measured.prior_target = measured.target
            if measured is not state:
                measured.sends = deque(state.sends, maxlen=MAX_EVENTS)
                measured.limit, measured.window = state.limit, state.window
            records = [state] if measured is state else [state, measured]
            for record in records:
                record.header_retry, record.body_retry = header_delay, body_delay
                record.count = min(10**6, record.count + 1)
                record.weight = min(10**6, record.weight + 1)
                record.last_429 = record.probe_at = now
                record.retry, record.until = delay, max(record.until, now + delay)
            if state.scope == "global":
                self.global_until = max(self.global_until, state.until)
            event = self._backoff(measured, now, delay)
            for record in records:
                record.failures.append(event)
            state.before, state.sample_window = measured.before, measured.sample_window
            other = self.evidence.get(route + ("|text" if media else "|media"))
            bucket = self.buckets.get(self.routes.get(route, ""))
            if media and other and bucket and bucket.remaining > bucket.buffer:
                state.divergent = other.divergent = True
        self.log_summary(now)
        return True

    def summary(self) -> dict[str, Any]:
        now = time.time()
        out = {}
        for state in self.evidence.values():
            state.evolve(now)
        for key, state in list(self.evidence.items()):
            scoped = key.rsplit("|", 1)[-1] in {"user", "shared", "global"}
            route = key.rsplit("|", 2 if scoped else 1)[0]
            bucket = self.buckets.get(self.routes.get(route, ""))
            targets = [state.target if state.target is not None else state.ceiling()]
            if not scoped:
                targets += [record.target for record in self._states(route, state.kind == "media")
                            if record.target is not None]
            out[key] = {"class": state.kind, "bucket": self.routes.get(route),
                        "target_rate": min(targets),
                        "header_limit": state.limit,
                        "window": state.window, "buffer": bucket.buffer if bucket else None,
                        "remaining": bucket.remaining if bucket else None, "429_count": state.count,
                        "evidence": state.weight, "last_429": state.last_429, "retry_after": state.retry,
                        "header_retry_after": state.header_retry, "body_retry_after": state.body_retry,
                        "scope": state.scope, "sends_before_429": state.before, "divergent": state.divergent,
                        "sample_window": state.sample_window, "failures": list(state.failures)}
        return out

    def log_summary(self, now: float) -> None:
        near = [key for key, bucket in self.buckets.items()
                if now < bucket.reset and bucket.remaining <= bucket.buffer + 1]
        if near and now - self.summary_at >= 60:
            log.info("Discord rate budget near buffer: %s", ", ".join(near))
            self.summary_at = now

    def dump(self) -> dict[str, Any] | None:
        if not self.evidence:
            return None
        for state in self.evidence.values():
            state.evolve(time.time())
        return formats.dump({"routes": self.routes, "buckets": {key: vars(b) for key, b in self.buckets.items()},
                "saved_at": time.time(),
                "evidence": {key: {**vars(state), "sends": list(state.sends), "failures": list(state.failures)}
                             for key, state in self.evidence.items()},
                "global_sends": [at for at in self.global_sends if time.time() - 1 < at <= time.time()],
                "global_until": self.global_until,
                "last_media": self.last_media if math.isfinite(self.last_media) else None}, VERSION)

    def restore(self, value: Any) -> None:
        if value is None:
            return
        try:
            value = formats.load(value, "Discord rate budget", VERSION)
            restored = Budget()
            uncertain = time.time() > number(value.get("saved_at", 0))
            for name in ("routes", "buckets", "evidence"):
                if not isinstance(value.get(name), dict) or len(value[name]) > MAX_RECORDS:
                    raise ValueError("invalid records")
            for key, raw in value["buckets"].items():
                parsed_limit = number(raw["limit"], 10**6)
                if not parsed_limit.is_integer():
                    raise ValueError("invalid limit")
                limit = int(parsed_limit)
                remaining = number(raw["remaining"], limit)
                if not remaining.is_integer():
                    raise ValueError("invalid remaining")
                if not isinstance(key, str) or len(key) > 1024:
                    raise ValueError("invalid bucket key")
                restored.buckets[key] = Bucket(limit, int(remaining),
                                               min(time.time() + DAY, number(raw["reset"])),
                                               max(0.001, number(raw["window"], DAY)))
                if uncertain:
                    bucket = restored.buckets[key]
                    # Requests after the debounced snapshot may have spent its remaining allowance.
                    bucket.remaining = min(bucket.remaining, bucket.buffer)
                    if bucket.reset <= time.time():
                        bucket.reset = time.time() + bucket.window
            for key, target in value["routes"].items():
                if not isinstance(key, str) or len(key) > 1024 or target not in restored.buckets:
                    raise ValueError("invalid route")
                restored.routes[key] = target
            for key, raw in value["evidence"].items():
                if not isinstance(key, str) or len(key) > 1024 or raw["kind"] not in {"media", "text"}:
                    raise ValueError("invalid class")
                state = Evidence(raw["kind"])
                for name in ("count", "weight", "last_429", "retry", "header_retry", "body_retry", "before", "window",
                             "sample_window", "limit",
                             "probe_at", "until"):
                    setattr(state, name, number(raw[name]))
                if any(not getattr(state, name).is_integer() for name in ("count", "before", "limit")):
                    raise ValueError("invalid counter")
                state.count, state.before, state.limit = int(state.count), int(state.before), int(state.limit)
                state.before, state.limit = min(MAX_EVENTS, state.before), min(10**6, state.limit)
                state.retry, state.window = min(DAY, state.retry), min(DAY, state.window)
                state.header_retry, state.body_retry = min(DAY, state.header_retry), min(DAY, state.body_retry)
                state.sample_window = min(2 * DAY, state.sample_window)
                state.count, state.weight = min(10**6, state.count), min(10**6, state.weight)
                state.last_429, state.probe_at = min(time.time(), state.last_429), min(time.time(), state.probe_at)
                state.until = min(time.time() + DAY, state.until)
                state.scope = raw["scope"] if raw["scope"] in {"user", "shared", "global"} else "unknown"
                state.divergent = raw["divergent"] is True
                if raw["target"] is not None:
                    target = number(raw["target"])
                    state.target = min(state.ceiling(), max(1 / DAY, target)) if target else state.ceiling()
                if raw["prior_target"] is not None:
                    state.prior_target = min(state.ceiling(), max(1 / DAY, number(raw["prior_target"])))
                if not isinstance(raw["sends"], list) or len(raw["sends"]) > MAX_EVENTS:
                    raise ValueError("invalid sends")
                state.sends.extend(sorted(number(at) for at in raw["sends"]
                                          if time.time() - DAY <= number(at) <= time.time()))
                if not isinstance(raw["failures"], list) or len(raw["failures"]) > MAX_EVENTS:
                    raise ValueError("invalid failures")
                for event in raw["failures"]:
                    at, count, header, body, rate = (number(item) for item in event)
                    state.failures.append((at, min(MAX_EVENTS, int(count)), min(DAY, header), min(DAY, body),
                                           min(GLOBAL_USABLE, rate)))
                restored.evidence[key] = state
            if not isinstance(value["global_sends"], list) or len(value["global_sends"]) > GLOBAL_LIMIT:
                raise ValueError("invalid global sends")
            restored.global_sends.extend(sorted(number(at) for at in value["global_sends"]
                                                 if time.time() - 1 < number(at) <= time.time()))
            restored.global_until = min(time.time() + DAY, number(value["global_until"]))
            if uncertain:
                restored.global_until = max(restored.global_until, time.time() + 1)
            restored.last_media = (min(time.time(), number(value["last_media"]))
                                   if value["last_media"] is not None else -math.inf)
            self.routes, self.buckets, self.evidence = restored.routes, restored.buckets, restored.evidence
            self.global_sends, self.global_until = restored.global_sends, restored.global_until
            self.last_media = restored.last_media
        except formats.FormatError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError):
            log.warning("Discord rate budget state is invalid; starting fresh")
            Budget.__init__(self)


class _Requests(Protocol):
    request: Callable[..., Awaitable[Any]]


def report(response: Any, method: str, path: str) -> None:
    if getattr(response, "_shazzzbot_limit_logged", False) is True:
        return
    response._shazzzbot_limit_logged = True
    log.warning("Discord 429 scope=%r bucket=%r retry_after_header=%r retry_after_body=%r route=%s %r",
                response.headers.get("X-RateLimit-Scope", "unknown"),
                response.headers.get("X-RateLimit-Bucket", "unknown"),
                response.headers.get("Retry-After", "unknown"),
                getattr(response, "_shazzzbot_body_retry", "unknown"), method, clean_path(path))


async def _response(_session: aiohttp.ClientSession, _context: Any, params: aiohttp.TraceRequestEndParams,
                    *, budget: Budget) -> bool:
    if params.response.status == 429 and callable(parse := getattr(params.response, "json", None)):
        try:
            body = await asyncio.wait_for(parse(), MAX_IMAGE_SPACING)
            if isinstance(body, dict) and "retry_after" in body:
                cast(Any, params.response)._shazzzbot_body_retry = number(body["retry_after"], DAY)
        except (ValueError, TypeError, TimeoutError, aiohttp.ClientError):
            pass
    return budget.observe(params.response, params.method, params.url.path, MEDIA.get())


def install(http: discord.http.HTTPClient, on_change: Callable[[], None] | None = None,
            on_auth: Callable[[int | None], Awaitable[None]] | None = None) -> Budget:
    budget = Budget()
    trace = aiohttp.TraceConfig()
    saved_at = -math.inf

    def persist(force: bool = False) -> None:
        nonlocal saved_at
        now = time.monotonic()
        if on_change and (force or now - saved_at >= PERSIST_EVERY):
            saved_at = now
            try:
                on_change()
            except OSError:
                log.warning("could not persist the Discord rate budget")

    async def start(_session: aiohttp.ClientSession, _context: Any, params: aiohttp.TraceRequestStartParams) -> None:
        route = route_key(params.method, params.url.path)
        ticket = PERMIT.get()
        if ticket and ticket.budget is budget and ticket.route == route and not ticket.used:
            ticket.used = True
            budget.release(ticket)
        await budget.acquire(route, MEDIA.get())
        if auth := AUTH.get():
            budget.auth.check(auth)

    async def end(session: aiohttp.ClientSession, context: Any, params: aiohttp.TraceRequestEndParams) -> None:
        fresh = await _response(session, context, params, budget=budget)
        persist(force=fresh and params.response.status == 429)

    trace.on_request_start.append(start)
    trace.on_request_end.append(end)
    trace.freeze()
    original = http.request
    tracing_unavailable = False

    def attach_trace(*, completed: bool = False) -> None:
        nonlocal tracing_unavailable
        supported = False
        try:
            session = getattr(http, "_HTTPClient__session", None)
            configs = session.trace_configs if isinstance(session, aiohttp.ClientSession) else None
            if isinstance(configs, list):
                if trace not in configs:
                    configs.append(trace)
                supported = True
        except (AttributeError, RuntimeError, TypeError):
            pass
        if supported:
            if tracing_unavailable:
                log.info("Discord response tracing recovered")
                tracing_unavailable = False
        elif completed and not tracing_unavailable:
            log.warning("Discord response tracing unavailable; verify the pinned py-cord/aiohttp versions and restart. "
                        "Rate budgets remain conservative.")
            tracing_unavailable = True

    async def request(route: discord.http.Route, *args: Any, **kwargs: Any) -> Any:
        path = clean_path(route.url)
        token_route = "/interactions/" in path or re.search(r"/webhooks/\d+/[^/]+", path) is not None
        ticket = None if token_route else budget.auth.begin(path)
        auth_token = AUTH.set(ticket)
        attach_trace()
        token = MEDIA.set(bool(kwargs.get("files")))
        status = None
        try:
            result = await original(route, *args, **kwargs)
            status = 200
            return result
        except discord.HTTPException as error:
            status = error.status
            if error.status == 429:
                fresh = budget.observe(error.response, route.method, route.url, MEDIA.get())
                if fresh:
                    persist(force=True)
            raise
        finally:
            attach_trace(completed=status is not None)
            MEDIA.reset(token)
            AUTH.reset(auth_token)
            if ticket is not None and budget.auth.finish(ticket, status) and on_auth:
                await on_auth(None if status == 401 else ticket.channel)

    cast(_Requests, http).request = request
    return budget
