import asyncio
import json
import logging
import runpy
import socket
import ssl
import sys

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from shazzzbot import health, panel, relay

DNS = {"svc.example.com": ["93.184.216.34"], "two.example.com": ["93.184.216.34", "2606:2800:220:1::1"],
       "lan.example.com": ["93.184.216.34", "198.18.0.5"], "loop.example.com": ["127.0.0.1"],
       "mapped.example.com": ["::ffff:100.64.0.1"]}
WIRE = {"protocol": relay.PROTOCOL, "version": relay.__version__}
PING = {"protocol": str(relay.PROTOCOL), "version": relay.__version__}


@pytest.fixture
def dns(monkeypatch):
    async def getaddrinfo(self, host, port, **_):
        if host not in DNS:
            raise socket.gaierror("no such name")
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))
                for ip in DNS[host]]
    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", getaddrinfo)


def test_allowlist_matches_names_and_subdomains():
    allow = relay.allowlist("Example.com, 203.0.113.7  other.org.")
    assert allow == ["example.com", "203.0.113.7", "other.org"]
    assert relay.allowed("a.b.EXAMPLE.com.", allow) and relay.allowed("203.0.113.7", allow)
    assert not relay.allowed("badexample.com", allow) and not relay.allowed("example.com.evil.net", allow)


def test_public_ips_refuses_anything_not_public(dns):
    async def go():
        out = [await relay.public_ips("two.example.com")]
        for host in ("lan.example.com", "loop.example.com", "mapped.example.com", "nope.example.com"):
            try:
                await relay.public_ips(host)
            except relay.Blocked as e:
                out.append(str(e))
        hosts = await relay.GuardResolver().resolve("two.example.com", 443)
        await relay.GuardResolver().close()
        return out, [(h["host"], h["family"], h["port"]) for h in hosts]
    out, hosts = asyncio.run(go())
    assert out == [["2606:2800:220:1::1", "93.184.216.34"], "not public", "not public", "not public", "DNS"]
    assert hosts == [("2606:2800:220:1::1", socket.AF_INET6, 443), ("93.184.216.34", socket.AF_INET, 443)]


def test_tcp_open_closed_and_timeout(monkeypatch):
    async def go():
        srv = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            free = s.getsockname()[1]
            async with srv:
                opened = await relay.tcp("127.0.0.1", port)
            return opened, await relay.tcp("127.0.0.1", free)
    opened, closed = asyncio.run(go())
    assert opened[0] and opened[1].endswith("ms") and closed == (False, "closed", 0.0)

    async def hang(*_):
        await asyncio.sleep(10)
    monkeypatch.setattr(relay, "TCP_TIMEOUT", 0.01)
    monkeypatch.setattr(asyncio, "open_connection", hang)
    assert asyncio.run(relay.tcp("127.0.0.1", 1)) == (False, "timeout", 0.0)


def test_run_validates_before_probing(dns, monkeypatch):
    seen = []

    async def probe(http, url, expect):
        seen.append((url, expect))
        return True, "5ms", 5.0

    async def tcp(ip, port):
        seen.append((ip, port))
        return True, "1ms", 1.0
    monkeypatch.setattr(relay.services, "probe", probe)
    monkeypatch.setattr(relay, "tcp", tcp)
    allow = ["example.com"]
    targets = [{"tcp": "svc.example.com:443"}, {"tcp": "[svc.example.com]:22"},
               {"url": "https://svc.example.com/x", "expect": "ok"},
               {"tcp": "svc.example.com:0"}, {"tcp": "svc.example.com:http"}, {"url": "ftp://svc.example.com/"},
               {"url": "https://svc.example.com:99999/"}, {"url": "https:///nohost"}, {"tcp": ":443"},
               {"url": "https://evil.net/"}, {"tcp": "lan.example.com:80"}, {"url": "https://gone.example.com/"}]
    async def go():
        return await asyncio.gather(*(relay.run(t, allow, None) for t in targets))
    out = asyncio.run(go())
    assert [r[1] for r in out] == ["1ms", "1ms", "5ms"] + ["bad target"] * 6 + ["not allowed", "not public", "DNS"]
    assert seen == [("93.184.216.34", 443), ("93.184.216.34", 22), ("https://svc.example.com/x", "ok")]


def serve_app(key="k" * 43, allow=("example.com",)):
    async def go(calls):
        async with aiohttp.ClientSession() as http, TestServer(relay.app(key, list(allow), http)) as srv, \
                aiohttp.ClientSession() as c:
            return [await calls(c, str(srv.make_url("")).rstrip("/"))]
    return go


def test_app_requires_the_key_and_valid_bodies(monkeypatch):
    monkeypatch.setattr(relay, "BAD_KEY_DELAY", 0)
    good = {"Authorization": "Bearer " + "k" * 43}

    async def calls(c, base):
        out = []
        for headers in (good, {}, {"Authorization": "Bearer wrong"}, {"Authorization": "k" * 43}, good):
            async with c.get(base + "/v1/ping", params=PING, headers=headers) as r:
                out.append(r.status)
        for body in (b"not json", json.dumps(WIRE).encode(),
                     *(json.dumps({**WIRE, "targets": targets}).encode() for targets in
                       ("x", [1], [{"tcp": "a:1"}] * 65))):
            async with c.post(base + "/v1/probe", data=body, headers=good) as r:
                out.append(r.status)
        async with c.post(base + "/v1/probe", json={**WIRE, "targets": [{"url": "https://evil.net/"}]},
                          headers=good) as r:
            out.append(await r.json())
        return out
    (out,) = asyncio.run(serve_app()(calls))
    assert out == [200, 401, 401, 401, 200, 400, 400, 400, 400, 400,
                   {**WIRE, "results": [[False, "not allowed", 0.0]]}]


def test_three_bad_keys_ban_the_address(monkeypatch):
    monkeypatch.setattr(relay, "BAD_KEY_DELAY", 0)

    async def calls(c, base):
        out = []
        for k in ("a", "b", "c", "k" * 43):
            async with c.get(base + "/v1/ping", params=PING, headers={"Authorization": f"Bearer {k}"}) as r:
                out.append(r.status)
        return out
    assert asyncio.run(serve_app()(calls)) == [[401, 401, 401, 403]]


def test_guard_bans_locks_down_and_recovers(caplog):
    g = relay.Guard()
    g.passed("203.0.113.7")
    g.passed("203.0.113.7")
    for t in range(relay.KNOWN_FAILS - 1):
        g.failed("203.0.113.7", t)
    assert g.admits("203.0.113.7", 30)
    g.failed("203.0.113.7", 31)
    assert not g.admits("203.0.113.7", 32)
    g.banned.clear()
    for t in (0, 100, 400):
        g.failed("1.1.1.1", t)
    assert g.admits("1.1.1.1", 401)
    g.failed("1.1.1.1", 402)
    assert g.admits("1.1.1.1", 402)
    g.failed("1.1.1.1", 403)
    assert not g.admits("1.1.1.1", 404) and g.admits("1.1.1.1", 403 + relay.BAN_FOR)
    with caplog.at_level(logging.WARNING, "shazzzbot.relay"):
        for n in range(relay.SIEGE):
            g.failed(f"2.0.0.{n}", 1000 + n)
        assert g.locked_until and not g.admits("3.3.3.3", 1100) and g.admits("203.0.113.7", 1100)
        assert not g.admits("2.0.0.1", 1100 + relay.FAIL_WINDOW)
        assert g.admits("3.3.3.3", g.locked_until) and not g.locked_until
    assert "lockdown: 30 refusals" in caplog.text and "lockdown over" in caplog.text


def test_guard_forgets_old_entries():
    g = relay.Guard()
    for n in range(1025):
        g.failed(f"2.0.{n // 256}.{n % 256}", n * relay.FAIL_WINDOW)
    assert len(g.fails) == 1
    for n in range(1025 * 3):
        g.failed(f"5.0.{n // 768}.{n // 3 % 256}", 10.0**9 + n // 3 * relay.BAN_FOR)
    assert len(g.banned) == 1


def test_credentials_are_made_once_and_tls_is_1_3(tmp_path):
    key, cert, pkey, made = relay.credentials(str(tmp_path))
    assert made and len(key) == 32 and all(c in "0123456789abcdef" for c in key)
    assert (tmp_path / "key.pem").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "api.key").stat().st_mode & 0o777 == 0o600
    assert relay.credentials(str(tmp_path)) == (key, cert, pkey, False)
    fp = relay.fingerprint(cert)
    assert len(fp) == 64 and int(fp, 16)
    assert relay.server_tls(cert, pkey).minimum_version == ssl.TLSVersion.TLSv1_3


def test_probe_via_pins_the_certificate(tmp_path, caplog):
    key, cert, pkey, _ = relay.credentials(str(tmp_path))
    fp = relay.fingerprint(cert)

    async def go():
        async with aiohttp.ClientSession() as http:
            runner = web.AppRunner(relay.app(key, ["example.com"], http))
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=relay.server_tls(cert, pkey))
            await site.start()
            url = f"https://127.0.0.1:{runner.addresses[0][1]}"
            rs = (relay.Relay(url, key, fp), relay.Relay(url, key, "00" * 32), relay.Relay(url, "wrong", fp),
                  relay.Relay("https://127.0.0.1:1", key, fp))
            try:
                return ([await relay.probe_via(http, r, "https://evil.net/", "") for r in rs],
                        [await relay.ping(http, r) for r in rs])
            finally:
                await runner.cleanup()
    with caplog.at_level(logging.ERROR, "shazzzbot.relay"):
        probes, pings = asyncio.run(go())
    assert probes == [(False, "not allowed", 0.0), None, None, None]
    assert "does not match RELAY_FINGERPRINT" in caplog.text
    assert [lv for lv, _, _, _ in pings] == [panel.FINE, panel.CRITICAL, panel.WARNING, panel.WARNING]
    assert pings[0][1] > 0 and pings[0][2] == "" and "RELAY_FINGERPRINT" in pings[1][2]
    assert pings[0][3] == relay.__version__ and all(p[3] is None for p in pings[1:])
    assert pings[2][2].startswith("unreachable (ClientResponseError")


def test_any_relay_answers(monkeypatch):
    a, b, c = (relay.Relay(u, "k", "0" * 64) for u in ("https://a", "https://b", "https://c"))
    probes = {a: None, b: (False, "closed", 0.0), c: (True, "5ms", 5.0)}
    ports = {a: None, b: [22], c: [22, 443]}

    async def probe_via(http, r, url, expect):
        return probes[r]

    async def open_ports(http, r, host, p):
        return ports[r]
    monkeypatch.setattr(relay, "probe_via", probe_via)
    monkeypatch.setattr(relay, "open_ports", open_ports)

    async def go():
        return [await relay.probe_any(None, [a, b, c], "u", ""), await relay.probe_any(None, [a, b], "u", ""),
                await relay.probe_any(None, [a], "u", ""), await relay.probe_any(None, [], "u", ""),
                await relay.open_ports_any(None, [a, b, c], "h", [22, 443]),
                await relay.open_ports_any(None, [a], "h", [22])]
    assert asyncio.run(go()) == [(True, "5ms", 5.0), (False, "closed", 0.0), None, None, [22, 443], None]


def test_serve_beats_and_cleans_up(tmp_path):
    _, cert, pkey, _ = relay.credentials(str(tmp_path))

    class Stop(Exception):
        pass

    beats = []

    def beat(_):
        beats.append(1)
        if len(beats) == 2:
            raise Stop
    orig, health.beat = health.beat, beat
    relay.BEAT_EVERY, every = 0.01, relay.BEAT_EVERY
    try:
        with pytest.raises(Stop):
            asyncio.run(relay.serve("k" * 32, ["example.com"], relay.server_tls(cert, pkey), 0))
    finally:
        health.beat, relay.BEAT_EVERY = orig, every


def test_main_prints_the_env_block_once(tmp_path, monkeypatch, caplog):
    started = []

    async def serve(key, allow, ctx, port):
        started.append((key, allow, port))
    monkeypatch.setattr(relay, "serve", serve)
    monkeypatch.setenv("RELAY_DIR", str(tmp_path))
    monkeypatch.setenv("RELAY_ALLOW", "example.com")
    with caplog.at_level(logging.INFO, "shazzzbot.relay"):
        relay.main()
        first = caplog.text
        caplog.clear()
        relay.main()
    key = (tmp_path / "api.key").read_text()
    assert f"RELAY_KEY={key}" in first and "RELAY_FINGERPRINT=" in first and key not in caplog.text
    assert started == [(key, ["example.com"], 8443)] * 2


def test_main_needs_an_allowlist(monkeypatch):
    monkeypatch.delenv("RELAY_ALLOW", raising=False)
    monkeypatch.delitem(sys.modules, "shazzzbot.relay")
    with pytest.raises(SystemExit, match="RELAY_ALLOW"):
        runpy.run_module("shazzzbot.relay", run_name="__main__")


def test_ask_and_open_ports(monkeypatch, caplog):
    replies = []

    class Resp:
        def __init__(self, body):
            self.body = body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        def raise_for_status(self):
            pass

        async def json(self):
            return self.body

    class Http:
        def post(self, url, json, **_):
            assert json["protocol"] == relay.PROTOCOL and json["version"] == relay.__version__
            replies.append(json["targets"])
            return Resp(body.pop(0))
    r = relay.Relay("https://r", "k", "0" * 64)
    closed = [False, "closed", 0.0]
    body = [{**WIRE, "results": values} for values in
            ([[True, "1ms", 1.0], closed], [[True, "1ms", 1.0]],
             [closed, [False, "not public", 0.0]], [], [closed] * 64, [[True, "1ms", 1.0]])]

    async def go():
        return [await relay.open_ports(Http(), r, "x.example.com", [443, 22, 443]),
                await relay.open_ports(Http(), r, "x.example.com", [1, 2]),
                await relay.open_ports(Http(), r, "x.example.com", [1, 2]),
                await relay.probe_via(Http(), r, "https://x/", ""),
                await relay.open_ports(Http(), r, "x.example.com", list(range(1, 66)))]
    with caplog.at_level(logging.WARNING, "shazzzbot.relay"):
        assert asyncio.run(go()) == [[22], None, None, None, [65]]
    assert [len(t) for t in replies[-2:]] == [64, 1]
    assert replies[0] == [{"tcp": "[x.example.com]:22"}, {"tcp": "[x.example.com]:443"}]
    assert "add it to the relay's RELAY_ALLOW" in caplog.text


def test_app_accepts_all_keys_and_compares_every_key(monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setenv("RELAY_KEYS", " " + "b" * 32 + "   " + "c" * 32 + " ")
    monkeypatch.setattr(relay, "BAD_KEY_DELAY", 0)
    compare = Mock(wraps=relay.hmac.compare_digest)
    monkeypatch.setattr(relay.hmac, "compare_digest", compare)

    async def calls(c, base):
        out = []
        for key in ("a" * 32, "b" * 32, "c" * 32, "wrong", ""):
            compare.reset_mock()
            async with c.get(base + "/v1/ping", params=PING, headers={"Authorization": f"Bearer {key}"}) as r:
                out.append(r.status)
            assert compare.call_count == 3
        return out
    assert asyncio.run(serve_app(key="a" * 32)(calls)) == [[200, 200, 200, 401, 401]]


def test_app_without_keys_fails_closed(monkeypatch):
    monkeypatch.setenv("RELAY_KEYS", "   ")
    with pytest.raises(SystemExit, match="api.key entry 0"):
        relay.app(" ", [], None)


@pytest.mark.parametrize("key", ["short", "!" * 32, "a" * 31, "a" * 32 + "\n"])
def test_invalid_generated_or_file_key_is_rejected(monkeypatch, key):
    monkeypatch.delenv("RELAY_KEYS", raising=False)
    with pytest.raises(SystemExit, match="api.key entry 0") as exc:
        relay.accepted_keys(key)
    assert key not in str(exc.value)


def test_extra_key_validation_names_the_index_without_the_key(monkeypatch):
    monkeypatch.setenv("RELAY_KEYS", "a" * 32 + " bad!key")
    with pytest.raises(SystemExit, match="RELAY_KEYS entry 2") as exc:
        relay.accepted_keys("b" * 32)
    assert "bad!key" not in str(exc.value)


def test_valid_hex_and_legacy_urlsafe_keys_are_accepted_and_deduplicated(monkeypatch):
    old = "Ab_9-" * 8 + "xyz"
    hex_key = bytes(range(16)).hex()
    monkeypatch.setenv("RELAY_KEYS", f"{hex_key} {old} {hex_key}")
    assert relay.accepted_keys(old) == [old, hex_key]


def test_existing_legacy_api_key_is_preserved(tmp_path):
    old = "Ab_9-" * 8 + "xyz"
    (tmp_path / "api.key").write_text(old)
    key, _, _, made = relay.credentials(str(tmp_path))
    assert not made and key == old and (tmp_path / "api.key").read_text() == old


def test_main_rejects_invalid_keys_before_logging_or_serving(tmp_path, monkeypatch, caplog):
    from unittest.mock import Mock

    monkeypatch.setenv("RELAY_DIR", str(tmp_path))
    monkeypatch.setenv("RELAY_ALLOW", "example.com")
    monkeypatch.setenv("RELAY_KEYS", "bad!key")
    (tmp_path / "api.key").write_text("a" * 32)
    serve = Mock()
    monkeypatch.setattr(relay, "serve", serve)
    with pytest.raises(SystemExit, match="RELAY_KEYS entry 1"):
        relay.main()
    serve.assert_not_called()
    assert "bad!key" not in caplog.text


@pytest.mark.parametrize("protocol", [None, True, "1", -1, 65536, 2])
def test_probe_protocol_is_required_before_any_target_runs(protocol, monkeypatch, caplog):
    from unittest.mock import AsyncMock

    run = AsyncMock()
    monkeypatch.setattr(relay, "run", run)
    body = {"version": relay.__version__, "targets": [{"url": "https://example.com"}]}
    if protocol is not None:
        body["protocol"] = protocol

    async def calls(c, base):
        async with c.post(base + "/v1/probe", json=body,
                          headers={"Authorization": "Bearer " + "k" * 43}) as response:
            return response.status

    assert asyncio.run(serve_app()(calls)) == [400]
    run.assert_not_called()
    assert ("expected=1 received=2" in caplog.text) == (protocol == 2)


@pytest.mark.parametrize("protocol", [None, "true", "01", "65536", "2"])
def test_ping_protocol_is_required_and_canonical(protocol, caplog):
    params = {"version": relay.__version__}
    if protocol is not None:
        params["protocol"] = protocol

    async def calls(c, base):
        async with c.get(base + "/v1/ping", params=params,
                         headers={"Authorization": "Bearer " + "k" * 43}) as response:
            return response.status

    assert asyncio.run(serve_app()(calls)) == [400]
    assert ("expected=1 received=2" in caplog.text) == (protocol == "2")


@pytest.mark.parametrize("version", [None, True, "01.2.3", "v1.2.3", "1.2", "not-a-version"])
def test_server_requires_a_release_version_on_ping_and_probe(version):
    body = {"protocol": relay.PROTOCOL, "targets": []}
    params = {"protocol": str(relay.PROTOCOL)}
    if version is not None:
        body["version"] = version
        params["version"] = str(version)

    async def calls(c, base):
        statuses = []
        headers = {"Authorization": "Bearer " + "k" * 43}
        async with c.get(base + "/v1/ping", params=params, headers=headers) as response:
            statuses.append(response.status)
        async with c.post(base + "/v1/probe", json=body, headers=headers) as response:
            statuses.append(response.status)
        return statuses

    assert asyncio.run(serve_app()(calls)) == [[400, 400]]


@pytest.mark.parametrize("body", [[], {"ok": False, **WIRE}, {"ok": True},
                                {"ok": True, **WIRE, "protocol": True},
                                {"ok": True, **WIRE, "protocol": 2},
                                {"ok": True, "protocol": relay.PROTOCOL},
                                {"ok": True, **WIRE, "version": "invalid"}])
def test_clients_refuse_unversioned_or_incompatible_replies(body, caplog):
    async def reply(request):
        if request.method == "GET":
            assert dict(request.query) == PING
        else:
            assert await request.json() == {**WIRE, "targets": []}
        return web.json_response(body)

    async def go():
        app = web.Application()
        app.router.add_get("/v1/ping", reply)
        app.router.add_post("/v1/probe", reply)
        async with TestServer(app) as server, aiohttp.ClientSession() as http:
            target = relay.Relay(str(server.make_url("")), "key", "00" * 32)
            return await relay.ping(http, target), await relay.ask(http, target, [])

    ping, probe = asyncio.run(go())
    assert ping[0] == panel.WARNING and ping[3] is None and probe is None
    assert ("expected=1 received=2" in caplog.text) == (isinstance(body, dict) and body.get("protocol") == 2)


def test_wire_requires_an_object_and_accepts_different_release_versions():
    with pytest.raises(TypeError, match="invalid relay message"):
        relay.wire([])
    assert relay.wire({"protocol": relay.PROTOCOL, "version": "1.2.3"}) == "1.2.3"
