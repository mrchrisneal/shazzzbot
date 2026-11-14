import asyncio
import socket
from dataclasses import fields, replace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from shazzzbot import panel, services, view
from shazzzbot.docker import Container
from shazzzbot.services import Service


def reply(delay: float = 0, **kw):
    async def handler(_):
        await asyncio.sleep(delay)
        return web.Response(**kw)
    return handler


def app() -> web.Application:
    a = web.Application()
    a.router.add_get("/ok", reply(text="Healthy"))
    a.router.add_get("/boom", reply(status=502))
    a.router.add_get("/auth", reply(status=401))
    a.router.add_get("/cf", reply(status=403, headers={"cf-mitigated": "challenge"}))
    a.router.add_get("/slow", reply(1, text="late"))
    return a


def run(svcs, cs=(), remote=None, **patch):
    async def go():
        async with TestServer(app()) as srv, aiohttp.ClientSession() as http:
            base = str(srv.make_url(""))
            def fill(u: str) -> str:
                return u.replace("TLSBASE", base.replace("http://", "https://")).replace("BASE", base)
            fixed = [replace(s, url=fill(s.url), public_url=fill(s.public_url), vpn_url=fill(s.vpn_url))
                     for s in svcs]
            return await services.check_all(fixed, list(cs), http, remote)
    with pytest.MonkeyPatch.context() as mp:
        for k, v in patch.items():
            mp.setattr(services, k, v)
        return asyncio.run(go())


UP = Container("1", "jf", "running", "Up 1 hour (healthy)")


def test_stages_pass_and_report_ms():
    (r,) = run([Service("JF", "jf", "BASE/ok", "Healthy", "BASE/auth")], [UP])
    assert r.level == panel.FINE and r.container == "ok" and r.local.endswith("ms") and r.public.endswith("ms")


def test_first_failing_stage_stops_the_rest():
    gone, exited, sick = run([Service("A", "nope", "BASE/ok", public_url="BASE/ok"),
                              Service("B", "jf", "BASE/ok"), Service("C", "jf", public_url="BASE/ok")],
                             [Container("1", "jf", "exited", "Exited (1) 2 minutes ago")])
    assert gone == (panel.CRITICAL, "missing", "-", "N/A", "-", "container missing")
    assert exited.container == "exited (1)" and exited.local == "-" and exited.public == "N/A"
    assert sick.public == "-" and sick.local == "N/A"


def test_local_failures():
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        boom, body, refused, tls = run([Service("A", url="BASE/boom"), Service("B", url="BASE/ok", expect="Nope"),
                                       Service("C", url=f"http://127.0.0.1:{port}/", public_url="BASE/ok"),
                                       Service("D", url="TLSBASE/ok")])
    assert (boom.level, boom.local, boom.why) == (panel.CRITICAL, "HTTP 502", "local HTTP 502")
    assert body.local == "bad body" and refused.local == "refused" and refused.public == "-"
    assert tls.local == "TLS"


def test_public_failure_is_a_warning_when_local_works():
    (r,) = run([Service("A", url="BASE/ok", public_url="BASE/boom")])
    assert (r.level, r.public, r.why) == (panel.WARNING, "HTTP 502", "public HTTP 502")
    (only,) = run([Service("A", public_url="BASE/boom")])
    assert only.level == panel.CRITICAL
    (vpn,) = run([Service("A", url="BASE/ok", vpn_url="BASE/boom", public_url="BASE/ok")])
    assert (vpn.level, vpn.vpn, vpn.public, vpn.why) == (panel.WARNING, "HTTP 502", "-", "vpn HTTP 502")
    (first,) = run([Service("A", vpn_url="BASE/ok", expect="Nope", public_url="BASE/ok")])
    assert first.vpn == "bad body" and first.level == panel.CRITICAL


def test_cloudflare_challenge_is_not_a_pass_or_a_failure():
    (r,) = run([Service("A", url="BASE/ok", public_url="BASE/cf")])
    assert r.level == panel.FINE and r.public == "CF wall"


def test_timeout_and_slow():
    (t,) = run([Service("A", url="BASE/slow")], TIMEOUT=aiohttp.ClientTimeout(total=0.2))
    assert t.local == "timeout"
    (s,) = run([Service("A", url="BASE/ok")], SLOW_MS=-1)
    async def gone(url, expect):
        return None
    (r,) = run([Service("A", url="BASE/ok", public_url="https://x/")], remote=gone, SLOW_MS=-1)
    assert (r.level, r.why) == (panel.WARNING, "no relay")
    assert (s.level, s.why) == (panel.WARNING, "slow")


def test_container_states():
    cs = [Container("1", "a", "running", "Up (unhealthy)"), Container("2", "b", "running", "Up (health: starting)"),
          Container("3", "c", "restarting", "Restarting (1) 3 seconds ago")]
    assert services.container_state("a", cs) == (panel.CRITICAL, "unhealthy")
    assert services.container_state("b", cs) == (panel.WARNING, "starting")
    assert services.container_state("c", cs) == (panel.CRITICAL, "restarting")
    (r,) = run([Service("B", "b")], cs)
    assert (r.level, r.why) == (panel.WARNING, "container starting")


def test_validate():
    assert services.validate([{"name": "A", "url": "http://x"}]) == [Service("A", url="http://x")]
    for bad in ({"name": "A"}, {"name": "", "url": "http://x"}, {"name": "A", "url": "ftp://x"},
                {"name": "A", "bogus": 1}):
        with pytest.raises(SystemExit):
            services.validate([bad])


def test_service_fields_and_defaults_follow_the_schema_and_typos_name_the_setting():
    assert {field.name for field in fields(Service)} == set(services.schema.names("service_check"))
    default = Service("Example")
    for name in services.schema.names("service_check"):
        if name != "name":
            assert getattr(default, name) == services.schema.default("service_check", name)
    with pytest.raises(SystemExit) as error:
        services.validate([{"name": "Example", "publc_url": "https://example.com"}])
    assert "publc_url" in str(error.value) and "public_url" in str(error.value)


@pytest.mark.parametrize("raw", [None, {}, "services", 1])
def test_service_collection_rejects_non_lists_through_schema(raw):
    with pytest.raises(SystemExit, match="services"):
        services.validate(raw)


def test_section_and_alerts():
    def text(*a):
        return view.text(services.section(*a)).description
    svcs = [Service("Jellyfin", "jf", "u"), Service("Auth", public_url="p")]
    res = [services.Result(panel.FINE, "ok", "12ms", "N/A", "N/A"),
           services.Result(panel.WARNING, "", "N/A", "N/A", "HTTP 502", "public HTTP 502")]
    d = text(svcs, res)
    assert "Jellyfin      ok   12ms       N/A" in d and "VPN" not in d and "N/A  HTTP 502" in d
    assert "Checked in order" not in d
    sec = services.section(svcs, res, None, ("LAN", "TS", "WAN"))
    assert sec.parts[0].rows[0][1] == ("Service", "Status", "LAN", "TS", "WAN")
    assert sec.parts[0].marks[2] == (0, 0, 0, 0, panel.CRITICAL) and sec.parts[0].icons == ["", "run", "warn"]
    down = text([Service("A very long service name that cannot fit in the table", "c")],
                [services.Result(panel.CRITICAL, "missing", "-", "N/A", "N/A", "container missing")])
    assert "A very long service name" in down and chr(0x2026) in down
    assert services.alerts(svcs, res) == [(panel.WARNING, "Auth public HTTP 502")]
    exposed = text(svcs, res, [("web", 443), ("ssh", 22)])
    assert "Open to the internet: web 443, ssh 22\n```\n**Issues & Alerts**" in exposed
    assert exposed.endswith(panel.WARN + " Auth public HTTP 502\n```")
    assert text([], [], []) == "```\nOpen to the internet: none\n```"
    assert len(text([], [], [("web", p) for p in range(10000, 10400)])) <= 4096


class NoDNS(aiohttp.abc.AbstractResolver):
    """Fails every lookup at once, so the test never waits on the real network."""
    async def resolve(self, host, port=0, family=0):
        raise OSError(f"{host}: no such host")

    async def close(self):
        pass


def test_probe_names_dns_and_garbage_replies():
    async def garbage(_, w):
        w.write(b"not http at all\r\n\r\n")
        await w.drain()
        w.close()

    async def go():
        srv = await asyncio.start_server(garbage, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        async with srv, aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=NoDNS())) as http:
            return [(await services.probe(http, u))[:2] for u in ("http://x.invalid/", f"http://127.0.0.1:{port}/")]
    assert asyncio.run(go()) == [(False, "DNS"), (False, "bad reply")]


def test_public_stage_goes_through_the_relay_when_set():
    asked = []

    async def remote(url, expect):
        asked.append((url, expect))
        return {"https://up/": (True, "40ms", 40.0), "https://down/": (False, "timeout", 0.0)}.get(url)
    up, down, gone = run([Service("A", url="BASE/ok", expect="Healthy", public_url="https://up/"),
                          Service("B", public_url="https://down/", expect="x"),
                          Service("C", url="BASE/ok", public_url="https://gone/")], remote=remote)
    assert sorted(asked) == [("https://down/", "x"), ("https://gone/", ""), ("https://up/", "")]
    assert (up.level, up.public) == (panel.FINE, "40ms") and (down.level, down.public) == (panel.CRITICAL, "timeout")
    assert (gone.level, gone.public, gone.why) == (panel.WARNING, "no relay", "no relay")


def test_icons():
    def r(level, box, why=""):
        return services.Result(level, box, "-", "-", "-", why)
    assert services.icon(r(panel.FINE, "ok")) == "run"
    assert services.icon(r(panel.CRITICAL, "paused", "container paused")) == "pause"
    assert services.icon(r(panel.CRITICAL, "exited (1)", "container exited (1)")) == "stop"
    assert services.icon(r(panel.CRITICAL, "missing", "container missing")) == "stop"
    assert services.icon(r(panel.CRITICAL, "unhealthy", "container unhealthy")) == "warn"
    assert services.icon(r(panel.CRITICAL, "", "local refused")) == "warn"


@pytest.mark.parametrize("target", ["", "host", "host:0", "host:65536", "host:port", ":80", "host: 80",
                                   "https://host:80", "user@host:80", "::1:80", "[host]:80", "host:\u0661"])
def test_tcp_targets_reject_bad_hosts_and_ports(target):
    with pytest.raises(ValueError):
        services.tcp_target(target)


def test_tcp_config_and_cadence_validation():
    assert services.tcp_target("[2001:db8::1]:443") == ("2001:db8::1", 443)
    assert services.tcp_target("host.example:22") == ("host.example", 22)
    assert services.validate([{"name": "SSH", "tcp": "host:22", "every": 120}]) == [
        Service("SSH", tcp="host:22", every=120)]
    assert services.validate([{"name": "Box", "container": "box", "every": 1}])[0].every == 1
    for bad in ({"tcp": "host:0"}, {"tcp": 80}, {"url": "http://x", "tcp": "host:80"},
                {"every": 59}, {"every": True}, {"every": 60.0}, {"container": "box", "every": 0}):
        with pytest.raises(SystemExit):
            services.validate([{"name": "X", "tcp": "host:80", **bad}])


def test_tcp_probe_connects_closes_and_reports_failures(monkeypatch):
    writer = Mock(close=Mock(), wait_closed=AsyncMock())
    connect = AsyncMock(return_value=(Mock(), writer))
    monkeypatch.setattr(services.asyncio, "open_connection", connect)
    monkeypatch.setattr(services.time, "perf_counter", Mock(side_effect=[0.0, 0.012]))
    ok, text, ms = asyncio.run(services.probe_tcp("[::1]:22"))
    assert (ok, text) == (True, "12ms") and ms == pytest.approx(12.0)
    connect.assert_awaited_once_with("::1", 22)
    writer.close.assert_called_once_with()
    writer.wait_closed.assert_awaited_once_with()
    monkeypatch.setattr(services.time, "perf_counter", lambda: 0.0)
    for error, reason in ((TimeoutError(), "timeout"), (socket.gaierror(), "DNS"),
                          (ConnectionRefusedError(), "refused"), (OSError(), "no route")):
        connect.side_effect = error
        assert asyncio.run(services.probe_tcp("host:22")) == (False, reason, 0.0)
    connect.side_effect = None
    for error in (OSError(), TimeoutError()):
        writer.wait_closed.side_effect = error
        assert asyncio.run(services.probe_tcp("host:22")) == (True, "0ms", 0.0)


def test_tcp_stages_stop_at_failure_and_preserve_first_http_body_check(monkeypatch):
    tcp = AsyncMock(side_effect=[(True, "2ms", 2.0), (False, "refused", 0.0)])
    monkeypatch.setattr(services, "probe_tcp", tcp)
    http = AsyncMock(return_value=(True, "3ms", 3.0))
    monkeypatch.setattr(services, "probe", http)
    s = Service("Mixed", tcp="lan:22", vpn_url="https://vpn", public_tcp="wan:22", expect="healthy")
    r = asyncio.run(services.check(s, [], Mock()))
    assert r == services.Result(panel.WARNING, "", "2ms", "3ms", "refused", "public refused")
    assert http.await_args.args[1:] == ("https://vpn", "healthy")
    tcp.side_effect = [(False, "refused", 0.0)]
    http.reset_mock()
    r = asyncio.run(services.check(s, [], Mock()))
    assert r == services.Result(panel.CRITICAL, "", "refused", "-", "-", "local refused")
    http.assert_not_awaited()
    tcp.side_effect = None
    tcp.return_value = (True, "2ms", 2.0)
    r = asyncio.run(services.check(Service("VPN", vpn_tcp="vpn:22"), [], Mock()))
    assert r.vpn == "2ms" and r.local == r.public == services.NA


def test_public_tcp_uses_remote_without_local_fallback(monkeypatch):
    direct = AsyncMock()
    remote = AsyncMock(return_value=(True, "5ms", 5.0))
    monkeypatch.setattr(services, "probe_tcp", direct)
    r = asyncio.run(services.check(Service("SSH", public_tcp="host:22"), [], Mock(), remote))
    assert r.level == panel.FINE and r.public == "5ms"
    remote.assert_awaited_once_with("tcp://host:22", "")
    direct.assert_not_awaited()


def test_check_all_reuses_each_result_until_its_own_check_deadline(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(services.time, "monotonic", lambda: now[0])
    fine = services.Result(panel.FINE, "", "5ms", "N/A", "N/A")
    failed = services.Result(panel.CRITICAL, "", "refused", "N/A", "N/A", "local refused")
    probe = AsyncMock(return_value=fine)
    monkeypatch.setattr(services, "check", probe)
    svcs = [Service("Box", "box"), Service("Slow", tcp="host:22", every=120)]
    cache = {}
    async def go():
        first = await services.check_all(svcs, [], Mock(), cache=cache, every=60)
        assert probe.await_count == 2
        probe.return_value = failed
        now[0] = 59
        again = await services.check_all(svcs, [], Mock(), cache=cache)
        assert all(a is b for a, b in zip(first, again)) and probe.await_count == 2
        now[0] = 60
        assert await services.check_all(svcs, [], Mock(), cache=cache) == [failed, fine]
        assert probe.await_count == 3
        now[0] = 120
        assert await services.check_all(svcs, [], Mock(), cache=cache) == [failed, failed]
        assert probe.await_count == 5
    asyncio.run(go())


def test_probe_duration_does_not_delay_the_next_default_cycle(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(services.time, "monotonic", lambda: now[0])
    result = services.Result(panel.FINE, "", "5ms", "N/A", "N/A")
    async def slow(*args):
        now[0] += 5
        return result
    probe = AsyncMock(side_effect=slow)
    monkeypatch.setattr(services, "check", probe)
    async def go():
        cache = {}
        svcs = [Service("SSH", tcp="host:22")]
        await services.check_all(svcs, [], Mock(), cache=cache)
        now[0] = 60
        await services.check_all(svcs, [], Mock(), cache=cache)
        assert probe.await_count == 2
    asyncio.run(go())


@pytest.mark.parametrize("containers", [False, True])
def test_external_checks_merge_under_services_with_headings_and_level_dots(containers):
    svcs = [Service("Good", tcp="h:1"), Service("Slow", tcp="h:2"), Service("Down", tcp="h:3")]
    results = [services.Result(lv, "", local, "N/A", "N/A", why) for lv, local, why in
               ((panel.FINE, "5ms", ""), (panel.WARNING, "2000ms", "slow"),
                (panel.CRITICAL, "refused", "local refused"))]
    if containers:
        svcs.insert(0, Service("Container", "box"))
        results.insert(0, services.Result(panel.CRITICAL, "missing", "-", "N/A", "N/A", "container missing"))
    combined, = services.sections(svcs, results, [("web", 443)] if containers else None)
    assert (combined.key, combined.title) == ("services", "Services")
    if containers:
        assert [cells[0] for _, cells in combined.parts[0].rows[1:]] == ["Container"]
    external = next(p for p in combined.parts if p.only == "embed" and p.heads)
    assert [cells[0] for _, cells in external.rows[1:]] == ["Good", "Slow", "Down"]
    assert not external.icons
    assert all(not p.icons for p in combined.parts if p.heading == "External Checks" and p.only == "embed")
    assert combined.issues == services.alerts(svcs, results)
    dots = next(p for p in combined.parts if p.only == "image")
    assert dots.heading == "External Checks" and dots.icons == ["", "dot0", "dot1", "dot2"]
    assert [lv for lv, _ in dots.rows[1:]] == [panel.FINE, panel.WARNING, panel.CRITICAL]
    text, rich = view.text(combined).description, str(view.rich(combined).to_dict())
    assert text.count("External Checks") == rich.count("External Checks") == 1
    assert "Slow" in text and "Down" in rich
    assert ("Open to the internet" in text) is containers
    assert ("Container" in text) is containers
    with patch.object(view, "_icon", wraps=view._icon) as icon, \
            patch.object(view, "draw_box", wraps=view.draw_box) as box:
        assert view.image(combined).startswith(b"\x89PNG")
    assert [c.args[1] for c in icon.call_args_list] == (["stop"] if containers else []) + ["dot0", "dot1", "dot2"]
    assert sum(c.args[1].heading == "External Checks" for c in box.call_args_list) == 1
    assert box.call_args_list[-1].args[1].heading == view.ISSUES


def test_empty_or_container_only_services_keep_the_services_key():
    assert services.sections([], []) == []
    exposed, = services.sections([], [], [])
    assert exposed.key == "services" and "Open to the internet: none" in view.text(exposed).description
    container, = services.sections([Service("Box", "box")],
                                   [services.Result(panel.FINE, "ok", "N/A", "N/A", "N/A")])
    assert container.key == "services" and "External Checks" not in view.text(container).description


def test_many_external_checks_keep_the_heading_when_rich_tables_fall_back():
    svcs = [Service(f"Check{i}", tcp="h:1") for i in range(30)]
    results = [services.Result(panel.FINE, "", "1ms", "N/A", "N/A")] * len(svcs)
    combined, = services.sections(svcs, results)
    rich = view.rich(combined)
    assert rich.title == "Services" and len(rich.fields) <= 25 and len(rich) <= 6000
    assert [field.name for field in rich.fields] == ["External Checks"]
    assert "Check0" in rich.fields[0].value


def test_text_services_keep_state_icons_and_one_leading_level_dot():
    import re

    from shazzzbot.glyphs import PAUSE, PLAY, STOP

    results = [services.Result(panel.FINE, "running", "N/A", "N/A", "N/A"),
               services.Result(panel.FINE, "paused", "N/A", "N/A", "N/A"),
               services.Result(panel.CRITICAL, "exited", "N/A", "N/A", "N/A", why="container exited"),
               services.Result(panel.WARNING, "unhealthy", "N/A", "N/A", "N/A", why="container unhealthy"),
               services.Result(panel.CRITICAL, "running", "N/A", "N/A", "N/A", why="local HTTP 500")]
    names = ["Running", "Paused", "Stopped", "Warning", "Critical"]
    sec = services.section([Service(name, name) for name in names], results)
    part = next(p for p in sec.parts if p.only == "text")
    assert part.icons == ["", "run", "pause", "stop", "warn", "warn"]
    lines = view._text_rows(part, view._lines(part, True), True)
    assert [level for level, _ in lines] == [panel.FINE, panel.FINE, panel.FINE, panel.CRITICAL,
                                            panel.WARNING, panel.CRITICAL]
    visible = [re.sub(r"\x1b\[[0-9;]*m", "", text) for text in panel.table(lines).splitlines()[1:-1]]
    for line, icon, name in zip(visible[1:], (PLAY + " ", PAUSE + " ", STOP + " ", "  ", "  "), names):
        assert line.split(" ", 1)[1].startswith(icon + " " + name)
    assert visible[3].startswith(panel.CRIT) and visible[4].startswith(panel.WARN) and visible[5].startswith(panel.CRIT)
    assert all(sum(line.count(dot) for dot in (panel.WARN, panel.CRIT)) <= 1 for line in visible)
    assert "\x1b[30m" not in "\n".join(line for _, line in lines)
    assert f"\x1b[32m{PLAY}\x1b[0m" in view.text(sec).description


def test_text_service_icons_keep_the_phone_width_by_dropping_optional_probes():
    import re

    result = services.Result(panel.CRITICAL, "unhealthy", "HTTP 500" * 6, "HTTP 502" * 6, "HTTP 504" * 6,
                             "local HTTP 500")
    results = [result]
    sec = services.section([Service("A service name much longer than a phone can hold", "box")], results)
    part = next(p for p in sec.parts if p.only == "text")
    assert part.icons == ["", "warn"]
    assert len(part.rows[0][1]) == 2
    assert part.rows[0][1] == ("Service", "Status")
    rendered = panel.table(view._text_rows(part, view._lines(part, True), True))
    lines = [re.sub(r"\x1b\[[0-9;]*m", "", text) for text in rendered.splitlines()[1:-1]]
    assert lines[1].startswith(panel.CRIT + " ") and lines[1].count(panel.CRIT) == 1
    assert all(len(line) <= panel.WIDTH + 3 for line in lines)
    assert lines[1].endswith("unhealthy")
