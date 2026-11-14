"""Relay: probes public targets from outside the server's network, for the bot, over TLS 1.3 with a pinned
certificate and an API key. Run it with `python -m shazzzbot.relay`; it is configured by environment only.

The relay probes only names on RELAY_ALLOW, and only when every address they resolve to is a public internet
address. A leaked key can read probe results; it cannot aim the relay at the network the relay sits on.
"""
import asyncio
import collections
import hashlib
import hmac
import ipaddress
import logging
import os
import re
import secrets
import socket
import ssl

# Only to run openssl with fixed arguments. reviewed 2026-11
import subprocess  # nosec B404
import time
from typing import NamedTuple
from urllib.parse import urlsplit

import aiohttp
from aiohttp import abc, web
from aiohttp.typedefs import Handler

from . import __version__, health, services
from .helper.constants import version_number
from .panel import CRITICAL, FINE, WARNING

log = logging.getLogger("shazzzbot.relay")
PROTOCOL = 1
MAX_TARGETS = 64
TCP_TIMEOUT = 4.0
BEAT_EVERY = 60.0
BAD_KEY_DELAY = 1.0
FAILS, KNOWN_FAILS, FAIL_WINDOW, BAN_FOR = 3, 20, 300.0, 86400.0
SIEGE, LOCKDOWN_FOR = 30, 3600.0
PORT = 8443
CLIENT_TIMEOUT = aiohttp.ClientTimeout(total=30)
PING_TIMEOUT = aiohttp.ClientTimeout(total=10)
Probe = tuple[bool, str, float]
REFUSED = ("bad target", "not allowed", "not public", "DNS")


def wire(value: object) -> str:
    if not isinstance(value, dict):
        raise TypeError("invalid relay message")
    protocol = value.get("protocol")
    if type(protocol) is not int or not 0 <= protocol <= 65535:
        raise ValueError("invalid relay protocol")
    if protocol != PROTOCOL:
        log.warning("relay protocol mismatch: expected=%s received=%s", PROTOCOL, protocol)
        raise ValueError("relay protocol mismatch")
    version = value.get("version")
    if not isinstance(version, str) or version != ".".join(map(str, version_number(version))):
        raise ValueError("invalid relay version")
    return version


class Blocked(OSError):
    """An OSError so aiohttp reports it as a failed lookup if it fires inside a connect."""


class Relay(NamedTuple):
    url: str
    key: str
    fingerprint: str  # SHA-256 of the certificate, hex


class Guard:
    """Bans an address for BAN_FOR after FAILS bad keys within FAIL_WINDOW, or KNOWN_FAILS once it has used the right
    key, so a client with a stale key is not shut out at once. SIEGE refusals within that window, from
    any mix of addresses, start a lockdown: only addresses that have used the right key are served until LOCKDOWN_FOR
    passes without such a flood. Kept in memory, so restarting the relay lifts every ban."""

    def __init__(self) -> None:
        self.fails: dict[str, list[float]] = {}
        self.banned: dict[str, float] = {}
        self.known: set[str] = set()
        self.recent: collections.deque[float] = collections.deque(maxlen=SIEGE)
        self.locked_until = 0.0

    def admits(self, ip: str, now: float) -> bool:
        if self.locked_until and now >= self.locked_until:
            self.locked_until = 0.0
            log.warning("lockdown over")
        if self.banned.get(ip, 0.0) > now:
            return False
        if ip in self.known:
            return True
        if self.locked_until:
            self._refused(now)
            return False
        return True

    def failed(self, ip: str, now: float) -> None:
        limit = KNOWN_FAILS if ip in self.known else FAILS
        hits = [t for t in self.fails.get(ip, ()) if t > now - FAIL_WINDOW] + [now]
        if len(hits) >= limit:
            del self.fails[ip]
            self.banned[ip] = now + BAN_FOR
            log.warning("banned %s for %.0f hours after %d bad keys; restart the relay to lift it",
                        ip, BAN_FOR / 3600, limit)
        else:
            self.fails[ip] = hits
        if len(self.fails) > 1024:
            self.fails = {k: v for k, v in self.fails.items() if v[-1] > now - FAIL_WINDOW}
        if len(self.banned) > 1024:
            self.banned = {k: t for k, t in self.banned.items() if t > now}
        self._refused(now)

    def passed(self, ip: str) -> None:
        if ip not in self.known:
            self.known.add(ip)
            log.info("%s used the right key; a lockdown will not refuse it", ip)

    def _refused(self, now: float) -> None:
        self.recent.append(now)
        if len(self.recent) == SIEGE and self.recent[0] > now - FAIL_WINDOW:
            if not self.locked_until:
                log.warning("lockdown: %d refusals in %.0f minutes; serving only addresses that used the right key",
                            SIEGE, FAIL_WINDOW / 60)
            self.locked_until = now + LOCKDOWN_FOR


def allowlist(raw: str) -> list[str]:
    return [a.lower().rstrip(".") for a in raw.replace(",", " ").split()]


def allowed(host: str, allow: list[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == a or host.endswith("." + a) for a in allow)


async def public_ips(host: str) -> list[str]:
    """Every address the name resolves to; Blocked if any is private, loopback, link-local, CGNAT or the like."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise Blocked("DNS") from None
    ips = sorted({str(i[4][0]) for i in infos})
    if not ips or not all(_global(ip) for ip in ips):
        raise Blocked("not public")
    return ips


def _global(ip: str) -> bool:
    """is_global, judging an IPv4-mapped IPv6 address by its IPv4 part, which is_global alone does not."""
    a = ipaddress.ip_address(ip)
    return (a.ipv4_mapped or a).is_global if isinstance(a, ipaddress.IPv6Address) else a.is_global


class GuardResolver(abc.AbstractResolver):
    """Connects only to vetted addresses, so a name cannot be re-pointed at a private one after the check."""

    async def resolve(self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET) -> list:
        return [{"hostname": host, "host": ip, "port": port, "proto": 0, "flags": socket.AI_NUMERICHOST,
                 "family": socket.AF_INET6 if ":" in ip else socket.AF_INET} for ip in await public_ips(host)]

    async def close(self) -> None:
        pass


async def tcp(ip: str, port: int) -> Probe:
    t0 = time.perf_counter()
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), TCP_TIMEOUT)
    except TimeoutError:
        return False, "timeout", 0.0
    except OSError:
        return False, "closed", 0.0
    w.close()
    ms = (time.perf_counter() - t0) * 1000
    return True, f"{ms:.0f}ms", ms


async def run(t: dict, allow: list[str], http: aiohttp.ClientSession) -> Probe:
    """One target: {"url": "https://host/path", "expect": "text"} or {"tcp": "host:port"}."""
    try:
        if "tcp" in t:
            host, _, port_s = str(t["tcp"]).rpartition(":")
            host, port = host.strip("[]"), int(port_s)
            if not 0 < port < 65536:
                raise ValueError
        else:
            u = urlsplit(str(t.get("url", "")))
            host = u.hostname or ""
            if u.scheme not in ("http", "https") or u.port == 0:  # .port raises ValueError when out of range
                raise ValueError
        if not host:
            raise ValueError
    except ValueError:
        return False, "bad target", 0.0
    if not allowed(host, allow):
        return False, "not allowed", 0.0
    try:
        ips = await public_ips(host)
    except Blocked as e:
        return False, str(e), 0.0
    if "tcp" in t:
        return await tcp(ips[0], port)
    return await services.probe(http, str(t["url"]), str(t.get("expect", "")))


def accepted_keys(key: str) -> list[str]:
    keys = [key, *os.environ.get("RELAY_KEYS", "").split()]
    for i, token in enumerate(keys):
        if re.fullmatch(r"[A-Za-z0-9_-]{32,}", token) is None:
            entry = "api.key entry 0" if i == 0 else f"RELAY_KEYS entry {i}"
            raise SystemExit(f"{entry} is invalid: use at least 32 letters, digits, underscores or hyphens")
    return list(dict.fromkeys(keys))


def app(key: str, allow: list[str], http: aiohttp.ClientSession) -> web.Application:
    gate, guard = asyncio.Semaphore(16), Guard()
    keys = [f"Bearer {token}".encode() for token in accepted_keys(key)]

    @web.middleware
    async def auth(request: web.Request, handler: Handler) -> web.StreamResponse:
        ip, now = request.remote or "", time.monotonic()
        if not guard.admits(ip, now):
            raise web.HTTPForbidden()
        supplied = request.headers.get("Authorization", "").encode()
        accepted = False
        for expected in keys:
            accepted |= hmac.compare_digest(supplied, expected)
        if not accepted:
            guard.failed(ip, now)
            await asyncio.sleep(BAD_KEY_DELAY)
            raise web.HTTPUnauthorized()
        guard.passed(ip)
        return await handler(request)

    async def ping(req: web.Request) -> web.Response:
        protocol = req.query.get("protocol", "")
        try:
            if re.fullmatch(r"0|[1-9][0-9]{0,4}", protocol) is None:
                raise ValueError("invalid relay protocol")
            wire({"protocol": int(protocol), "version": req.query.get("version")})
        except ValueError:
            raise web.HTTPBadRequest() from None
        return web.json_response({"ok": True, "protocol": PROTOCOL, "version": __version__})

    async def probe(req: web.Request) -> web.Response:
        try:
            body = await req.json()
            wire(body)
            targets = body["targets"]
        except (ValueError, KeyError, TypeError):
            raise web.HTTPBadRequest() from None
        if not (isinstance(targets, list) and len(targets) <= MAX_TARGETS
                and all(isinstance(t, dict) for t in targets)):
            raise web.HTTPBadRequest()

        async def one(t: dict) -> Probe:
            async with gate:
                return await run(t, allow, http)
        return web.json_response({"protocol": PROTOCOL, "version": __version__,
                                  "results": await asyncio.gather(*(one(t) for t in targets))})

    a = web.Application(middlewares=[auth], client_max_size=64 * 1024)
    a.router.add_get("/v1/ping", ping)
    a.router.add_post("/v1/probe", probe)
    return a


def credentials(d: str) -> tuple[str, str, str, bool]:
    """(API key, cert path, private key path, newly made). Made on first start and kept in d."""
    keyfile, cert, pkey = (os.path.join(d, n) for n in ("api.key", "cert.pem", "key.pem"))
    made = not os.path.exists(keyfile)
    if made:
        # Fixed arguments; the only variables are paths under RELAY_DIR. reviewed 2026-11
        subprocess.run(["/usr/bin/openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:secp384r1",  # nosec B603
                        "-noenc", "-days", "3650", "-subj", "/CN=shazzzbot-relay", "-keyout", pkey, "-out", cert],
                       check=True, capture_output=True)
        os.chmod(pkey, 0o600)
        fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(16))
    with open(keyfile) as f:
        return f.read().strip(), cert, pkey, made


def fingerprint(cert: str) -> str:
    with open(cert) as f:
        return hashlib.sha256(ssl.PEM_cert_to_DER_cert(f.read())).hexdigest()


def server_tls(cert: str, pkey: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(cert, pkey)
    return ctx


async def serve(key: str, allow: list[str], ctx: ssl.SSLContext, port: int) -> None:
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=GuardResolver())) as http:
        runner = web.AppRunner(app(key, allow, http))
        await runner.setup()
        # Every interface inside the container; compose decides which host address publishes it. reviewed 2026-11
        await web.TCPSite(runner, "0.0.0.0", port, ssl_context=ctx).start()  # nosec B104
        log.info("relay listening on %d for %s", port, " ".join(allow))
        try:
            while True:
                health.beat(BEAT_EVERY)
                await asyncio.sleep(BEAT_EVERY)
        finally:
            await runner.cleanup()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    allow = allowlist(os.environ.get("RELAY_ALLOW", ""))
    if not allow:
        raise SystemExit("RELAY_ALLOW is not set: list the hostnames or IPs this relay may probe, e.g. example.com")
    d = os.environ.get("RELAY_DIR", "/data")
    key, cert, pkey, made = credentials(d)
    accepted_keys(key)
    fp = fingerprint(cert)
    port = PORT
    if made:
        log.info("first start: add these lines to the bot's .env\n\nRELAY_URL=https://<this machine's address>:%d\n"
                 "RELAY_KEY=%s\nRELAY_FINGERPRINT=%s\n", port, key, fp)
    else:
        log.info("RELAY_FINGERPRINT=%s (the key is in %s)", fp, os.path.join(d, "api.key"))
    asyncio.run(serve(key, allow, server_tls(cert, pkey), port))


async def ask(http: aiohttp.ClientSession, r: Relay, targets: list[dict]) -> list[Probe] | None:
    """The relay's verdict on each target, or None when the relay cannot be reached."""
    try:
        async with http.post(r.url.rstrip("/") + "/v1/probe",
                             json={"protocol": PROTOCOL, "version": __version__, "targets": targets},
                             headers={"Authorization": f"Bearer {r.key}"}, timeout=CLIENT_TIMEOUT,
                             ssl=aiohttp.Fingerprint(bytes.fromhex(r.fingerprint))) as resp:
            resp.raise_for_status()
            body = await resp.json()
            wire(body)
            got = [(bool(ok), str(text), float(ms)) for ok, text, ms in body["results"]]
    except aiohttp.ServerFingerprintMismatch:
        log.error("relay certificate does not match RELAY_FINGERPRINT; refusing to use it")
        return None
    except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError):
        return None
    return got if len(got) == len(targets) else None


async def ping(http: aiohttp.ClientSession, r: Relay) -> tuple[int, float, str, str | None]:
    """(level, round trip ms, why, version)."""
    t0 = time.perf_counter()
    try:
        async with http.get(r.url.rstrip("/") + "/v1/ping", headers={"Authorization": f"Bearer {r.key}"},
                            params={"protocol": str(PROTOCOL), "version": __version__},
                            timeout=PING_TIMEOUT, ssl=aiohttp.Fingerprint(bytes.fromhex(r.fingerprint))) as resp:
            resp.raise_for_status()
            body = await resp.json()
            if not isinstance(body, dict) or body.get("ok") is not True:
                raise ValueError("invalid ping reply")
            version = wire(body)
    except aiohttp.ServerFingerprintMismatch:
        return CRITICAL, 0.0, "certificate does not match its RELAY_FINGERPRINT", None
    except (aiohttp.ClientError, TimeoutError, ValueError) as e:
        return WARNING, 0.0, f"unreachable ({type(e).__name__}: {e})", None
    return FINE, (time.perf_counter() - t0) * 1000, "", version


async def probe_any(http: aiohttp.ClientSession, rs: list[Relay], url: str, expect: str) -> Probe | None:
    """Up if any relay reaches url; a failure stands only when no relay did. Relays are asked at once."""
    got = [g for g in await asyncio.gather(*(probe_via(http, r, url, expect) for r in rs)) if g]
    return next((g for g in got if g[0]), got[0] if got else None)


async def open_ports_any(http: aiohttp.ClientSession, rs: list[Relay], host: str, ports: list[int]) -> list[int] | None:
    """Ports any relay could open; None when no relay answered. Relays are asked at once."""
    seen = [g for g in await asyncio.gather(*(open_ports(http, r, host, ports) for r in rs)) if g is not None]
    return sorted(set().union(*seen)) if seen else None


async def probe_via(http: aiohttp.ClientSession, r: Relay, url: str, expect: str) -> Probe | None:
    got = await ask(http, r, [{"url": url, "expect": expect}])
    return got[0] if got else None


async def open_ports(http: aiohttp.ClientSession, r: Relay, host: str, ports: list[int]) -> list[int] | None:
    """Which of these ports on host the relay can open from outside; None when the relay cannot be reached."""
    ports = sorted(set(ports))
    batches = [ports[i:i + MAX_TARGETS] for i in range(0, len(ports), MAX_TARGETS)]
    got: list[Probe] = []
    for b in batches:
        part = await ask(http, r, [{"tcp": f"[{host}]:{p}"} for p in b])
        if part is None:
            return None
        got += part
    if refused := next((text for _, text, _ in got if text in REFUSED), None):
        log.warning("relay will not probe %s: %s; add it to the relay's RELAY_ALLOW", host, refused)
        return None
    return [p for p, (ok, _, _) in zip(ports, got) if ok]


if __name__ == "__main__":
    main()
