"""Container checks followed by Local, VPN, and Public HTTP or TCP probes."""
import asyncio
import contextlib
import ipaddress
import logging
import re
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import NamedTuple

import aiohttp

from . import schema
from .docker import Container
from .panel import CRITICAL, FINE, ICON_TEXT_WIDTH, WARNING, WIDTH, Part, Section

log = logging.getLogger("shazzzbot")

SLOW_MS = 1500
TIMEOUT = aiohttp.ClientTimeout(total=8, connect=4)
AUTH_WALL = frozenset({401, 403})
BODY_LIMIT = 65536
NA, SKIP = "N/A", "-"  # not configured; not reached because an earlier stage failed
STAGES = ("local", "vpn", "public")
COLUMNS = tuple(schema.default("services", "columns"))


@dataclass(frozen=True)
class Service:
    name: str
    container: str = schema.default("service_check", "container")
    url: str = schema.default("service_check", "url")
    expect: str = schema.default("service_check", "expect")
    public_url: str = schema.default("service_check", "public_url")
    vpn_url: str = schema.default("service_check", "vpn_url")
    tcp: str = schema.default("service_check", "tcp")
    vpn_tcp: str = schema.default("service_check", "vpn_tcp")
    public_tcp: str = schema.default("service_check", "public_tcp")
    every: int | None = schema.default("service_check", "every")


class Result(NamedTuple):
    level: int
    container: str  # "" when not checked, else "ok" or the failure
    local: str  # NA, SKIP, elapsed "12ms", or the failure
    vpn: str
    public: str
    why: str = ""  # the failing stage and reason, for alerts


def validate(raw: list[dict]) -> list[Service]:
    schema.validate_field("services", "checks", raw, "services.checks", items=False)
    out = []
    for i, entry in enumerate(raw):
        schema.validate("service_check", entry, f"services.checks entry {i + 1}")
        s = Service(**entry)
        if not (s.container or s.url or s.vpn_url or s.public_url or s.tcp or s.vpn_tcp or s.public_tcp):
            raise SystemExit(f"[[services.checks]] entry {i + 1}: needs a name and a container, URL, or TCP target")
        minimum = 1 if s.container else 60
        if s.every is not None and s.every < minimum:
            raise SystemExit(f"[[services.checks]] {s.name}: every must be whole seconds, {minimum} or more")
        for u, tcp in ((s.url, s.tcp), (s.vpn_url, s.vpn_tcp), (s.public_url, s.public_tcp)):
            if u and tcp:
                raise SystemExit(f"[[services.checks]] {s.name}: choose either URL or TCP for each stage")
            if u and not u.startswith(("http://", "https://")):
                raise SystemExit(f"[[services.checks]] {s.name}: {u!r} is not an http(s) URL")
            if tcp:
                try:
                    tcp_target(tcp)
                except ValueError:
                    msg = f"[[services.checks]] {s.name}: TCP target must be host:port or [IPv6]:port"
                    raise SystemExit(msg) from None
        out.append(s)
    return out


def tcp_target(target: str) -> tuple[str, int]:
    host, sep, raw_port = target.rpartition(":")
    bracketed = host.startswith("[") and host.endswith("]")
    if not sep or not raw_port.isascii() or not raw_port.isdecimal() or not 1 <= int(raw_port) <= 65535:
        raise ValueError("invalid TCP port")
    if bracketed:
        host = str(ipaddress.IPv6Address(host[1:-1]))
    elif re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", host) is None:
        raise ValueError("invalid TCP host")
    return host, int(raw_port)


async def probe_tcp(target: str) -> tuple[bool, str, float]:
    host, port = tcp_target(target)
    started = time.perf_counter()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), TIMEOUT.connect)
    except TimeoutError:
        return False, "timeout", 0.0
    except socket.gaierror:
        return False, "DNS", 0.0
    except OSError as e:
        return False, "refused" if isinstance(e, ConnectionRefusedError) else "no route", 0.0
    ms = (time.perf_counter() - started) * 1000
    writer.close()
    with contextlib.suppress(OSError, TimeoutError):
        await asyncio.wait_for(writer.wait_closed(), TIMEOUT.connect)
    return True, f"{ms:.0f}ms", ms


def container_state(name: str, cs: list[Container]) -> tuple[int, str]:
    c = next((c for c in cs if c.name == name), None)
    if c is None:
        return CRITICAL, "missing"
    if c.state != "running":
        return CRITICAL, c.state if c.exit_code is None else f"{c.state} ({c.exit_code})"
    if c.unhealthy:
        return CRITICAL, "unhealthy"
    if "(health: starting)" in c.status:
        return WARNING, "starting"
    return FINE, "ok"


async def probe(http: aiohttp.ClientSession, url: str, expect: str = "") -> tuple[bool, str, float]:
    """(passed, what to show, elapsed ms). Redirects are not followed: a 3xx means the server answered."""
    t0 = time.perf_counter()
    try:
        async with http.get(url, allow_redirects=False, timeout=TIMEOUT) as r:
            body = await r.content.read(BODY_LIMIT) if expect else b""
            status, challenged = r.status, r.headers.get("cf-mitigated") == "challenge"
    except TimeoutError:
        return False, "timeout", 0.0
    except aiohttp.ClientConnectorDNSError:
        return False, "DNS", 0.0
    except aiohttp.ClientSSLError:
        return False, "TLS", 0.0
    except aiohttp.ClientConnectorError as e:
        return False, "refused" if isinstance(e.os_error, ConnectionRefusedError) else "no route", 0.0
    except aiohttp.ClientError:
        return False, "bad reply", 0.0
    ms = (time.perf_counter() - t0) * 1000
    if challenged:
        return True, "CF wall", ms  # Cloudflare answered with a bot challenge; the origin is not verified
    if status >= 400 and status not in AUTH_WALL:
        return False, f"HTTP {status}", ms
    if expect and expect.encode() not in body:
        return False, "bad body", ms
    return True, f"{ms:.0f}ms", ms


Remote = Callable[[str, str], Awaitable[tuple[bool, str, float] | None]]


async def check(s: Service, cs: list[Container], http: aiohttp.ClientSession, remote: Remote | None = None) -> Result:
    """Public probes can use a relay; it receives an HTTP URL or tcp://host:port."""
    urls = {"local": s.url, "vpn": s.vpn_url, "public": s.public_url}
    tcps = {"local": s.tcp, "vpn": s.vpn_tcp, "public": s.public_tcp}
    out = {k: SKIP if urls[k] or tcps[k] else NA for k in STAGES}
    box, level, passed, slow = "", FINE, False, False
    body_checked = False

    def done(lv: int, why: str) -> Result:
        return Result(lv, box, out["local"], out["vpn"], out["public"], why)
    if s.container:
        level, box = container_state(s.container, cs)
        if level == CRITICAL:
            return done(level, f"container {box}")
    for k in STAGES:
        if not (urls[k] or tcps[k]):
            continue
        expect = "" if body_checked or not urls[k] else s.expect
        body_checked = body_checked or bool(urls[k])
        if remote and k == "public":
            got = await remote(urls[k] or "tcp://" + tcps[k], expect)
        else:
            got = await probe(http, urls[k], expect) if urls[k] else await probe_tcp(tcps[k])
        if got is None:
            out[k] = "no relay"
            return done(max(level, WARNING), "no relay")
        ok, out[k], ms = got
        if not ok:
            return done(max(level, WARNING) if passed else CRITICAL, f"{k} {out[k]}")
        passed, slow = True, slow or ms > SLOW_MS
    if slow:
        return done(max(level, WARNING), "slow")
    return done(level, f"container {box}" if level else "")


def status(r: Result) -> str:
    """The container state when one was checked, else ok or error from the probes."""
    return r.container or ("error" if r.level == CRITICAL else "ok")


def icon(r: Result) -> str:
    """The image's state icon: run, pause, stop, or warn for a running service with an issue."""
    if r.container == "paused":
        return "pause"
    if r.why.startswith("container") and r.level == CRITICAL and r.container != "unhealthy":
        return "stop"
    return "warn" if r.level else "run"


Cache = dict[Service, tuple[float, Result]]


async def check_all(svcs: list[Service], cs: list[Container], http: aiohttp.ClientSession,
                    remote: Remote | None = None, *, cache: Cache | None = None, every: float = 60.0,
                    now: float | None = None) -> list[Result]:
    started = time.monotonic() if now is None else now
    async def one(s: Service) -> Result:
        cadence = max(1 if s.container else 60, s.every if s.every is not None else every)
        if cache is not None and s in cache and started - cache[s][0] < cadence:
            return cache[s][1]
        result = await check(s, cs, http, remote)
        log.debug("service %s: level %d, %s", s.name, result.level, result.why or "ok")
        if cache is not None:
            cache[s] = (started, result)
        return result
    return list(await asyncio.gather(*(one(s) for s in svcs)))


def alerts(svcs: list[Service], results: list[Result]) -> list[tuple[int, str]]:
    return [(r.level, f"{s.name} {r.why}") for s, r in zip(svcs, results) if r.level]


def section(svcs: list[Service], results: list[Result], exposed: list[tuple[str, int]] | None = None,
            columns: tuple[str, ...] = COLUMNS, *, external: bool = False) -> Section:
    """`exposed` is (container, port) for each published port the relay could open from the internet."""
    cells = [(FINE, ("Service", "Status", *columns))]
    cells += [(r.level, (s.name, status(r), r.local, r.vpn, r.public)) for s, r in zip(svcs, results)]
    failed = [tuple(CRITICAL if r.why.startswith(f"{k} ") else FINE for k in ("", "container", *STAGES))
              for r in results]
    full = Part(cells if results else [], align="lrrrr", heads=True, on_value=True,
                marks=[(), *failed], icons=["", *map(icon, results)], only="embed image")
    keep = [0, 1, *(c for c in (2, 3, 4) if any(row[c] != NA for _, row in cells[1:]))]  # phone width
    while len(keep) > 2 and 6 + sum(max(len(row[c]) for _, row in cells) for c in keep[1:]) \
            + 2 * (len(keep) - 1) > WIDTH - ICON_TEXT_WIDTH:
        keep.pop()
    narrow = Part([(lv, tuple(row[c] for c in keep)) for lv, row in full.rows], align="l" + "r" * (len(keep) - 1),
                  heads=True, icons=["", *(f"dot{r.level}" if external else icon(r) for r in results)], only="text")
    parts = [full, narrow]
    if external:
        dots = full._replace(only="image", icons=["", *(f"dot{r.level}" for r in results)])
        full = full._replace(only="embed", icons=(),
                             marks=[(), *((FINE, r.level, FINE, FINE, FINE) for r in results)])
        parts = [full, narrow, dots]
    if exposed is not None:
        parts.append(Part([(FINE, ("Open to the internet: " + (", ".join(f"{n} {p}" for n, p in exposed) or "none"),))],
                          cut=None))
    return Section("external" if external else "services", "External Checks" if external else "Services", parts,
                   issues=alerts(svcs, results))


def sections(svcs: list[Service], results: list[Result], exposed: list[tuple[str, int]] | None = None,
             columns: tuple[str, ...] = COLUMNS) -> list[Section]:
    parts: list[Part] = []
    issues: list[tuple[int, str]] = []
    for external in (False, True):
        pairs = [(s, r) for s, r in zip(svcs, results) if bool(s.container) != external]
        if pairs or (not external and exposed is not None):
            built = section([s for s, _ in pairs], [r for _, r in pairs], None if external else exposed,
                            columns, external=external)
            if external:
                if not parts:
                    # A preceding part keeps the first grid table's heading visible in rich().
                    parts.append(Part([(FINE, ("\u200b",))], only="embed"))
                parts.extend(p._replace(heading="External Checks") for p in built.parts)
            else:
                parts.extend(built.parts)
            issues.extend(built.issues)
    return [Section("services", "Services", parts, issues=issues)] if parts else []
