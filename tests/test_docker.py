import pytest

from shazzzbot import docker, panel, view
from shazzzbot.docker import Container, Engine, Inventory, parse
from shazzzbot.glyphs import WARN


def ct(name, state="running", status="Up 1 hour", project="", nets=("bridge",)):
    return Container(name, name, state, status, project, nets)


def stack_cases():
    return [ct("healthy", project="healthy"), ct("warning", status="Up (unhealthy)", project="warning"),
            ct("critical-a", status="Up (unhealthy)", project="critical"),
            ct("critical-b", "exited", "Exited (0)", "critical"),
            ct("off-a", "exited", "Exited (1)", "off"), ct("off-b", "paused", "Up (unhealthy)", "off")]


def test_inventory_freshness_tracks_success_and_keeps_monotonic_zero(monkeypatch):
    now = 0.0
    monkeypatch.setattr(docker.time, "monotonic", lambda: now)
    inventory = Inventory()
    assert inventory.note() == ""
    inventory.failed = True
    assert inventory.note() == "Inventory unavailable; no successful read"
    inventory.success()
    assert inventory.updated == 0 and not inventory.failed and inventory.note() == ""
    now = 720
    inventory.failed = True
    assert inventory.note() == "Inventory stale; last updated 12m ago"
    now = -1
    assert inventory.note() == "Inventory stale; last updated 0s ago"
    now = 900
    inventory.success()
    assert inventory.updated == 900 and not inventory.failed and inventory.note() == ""


def test_parse_reads_project_and_networks():
    rows = [{"Id": "a", "Names": ["/web"], "State": "running", "Status": "Up",
             "Labels": {"com.docker.compose.project": "media"},
             "NetworkSettings": {"Networks": {"proxy": {}, "media_default": {}}},
             "Ports": [{"IP": "0.0.0.0", "PrivatePort": 80, "PublicPort": 8080, "Type": "tcp"},
                       {"IP": "::", "PrivatePort": 80, "PublicPort": 8080, "Type": "tcp"},
                       {"IP": "127.0.0.1", "PrivatePort": 9, "PublicPort": 9000, "Type": "tcp"},
                       {"IP": "::ffff:127.0.0.1", "PrivatePort": 9, "PublicPort": 9001, "Type": "tcp"},
                       {"IP": "203.0.113.4", "PrivatePort": 9, "PublicPort": 9002, "Type": "tcp"},
                       {"IP": "0.0.0.0", "PrivatePort": 53, "PublicPort": 53, "Type": "udp"},
                       {"PrivatePort": 443, "Type": "tcp"}, {"PrivatePort": 22, "PublicPort": 2222, "Type": "tcp"}]},
            {"Id": "b", "Names": ["/x"], "State": "exited", "Labels": None, "NetworkSettings": None}]
    web, x = parse(rows)
    assert (web.name, web.project, web.networks, web.ports) == (
        "web", "media", ("media_default", "proxy"), (2222, 8080, 9002))
    assert (x.project, x.networks, x.status, x.ports) == ("", (), "", ())


def test_list_metadata_exposes_scheduler_image_and_command_without_repr_secrets():
    command = '/watchtower --schedule "0 0 3 * * *" --http-api-token private-secret'
    container, = parse([{"Id": "w", "Names": ["/updater"], "State": "running",
                         "Image": "containrrr/watchtower:latest", "Command": command}])
    assert container.image == "containrrr/watchtower:latest" and container.command == command
    assert "private-secret" not in repr(container)


@pytest.mark.parametrize("value", [None, 1, []])
def test_invalid_scheduler_metadata_is_ignored(value):
    container, = parse([{"Id": "w", "Names": ["/updater"], "State": "running", "Image": value, "Command": value}])
    assert container.image == container.command == ""


def test_exit_code():
    assert ct("a", "exited", "Exited (137) 2 hours ago").exit_code == 137
    assert ct("a").exit_code is None


def test_stack_state():
    init = ct("init", "exited", "Exited (0) 1 hour ago")
    assert panel.stack_state([ct("a"), init]) == (panel.CRITICAL, "degraded")
    assert panel.stack_state([ct("a"), ct("b", "exited", "Exited (1) 1 minute ago")]) == (panel.CRITICAL, "degraded")
    assert panel.stack_state([ct("a", status="Up 1 hour (unhealthy)")]) == (panel.WARNING, "unhealthy")
    assert panel.stack_state([ct("a", "exited", "Exited (137) 1 day ago")]) == (panel.FINE, "stopped")
    assert panel.stack_state([ct("a")]) == (panel.FINE, "up")


def test_docker_embed():
    cs = [ct("jf", project="media", nets=("media_default", "proxy")),
          ct("sonarr", "exited", "Exited (1) 5 minutes ago", "media", ("media_default",)),
          ct("db", project="apps", nets=("apps_default",)), ct("solo")]
    e = view.text(panel.docker_section(cs, Engine("27.5.1", "overlay2", 41)))
    d = e.description
    assert e.title == "Docker Containers"
    assert "Engine    27.5.1 overlay2" in d and "Images    41" in d and "Running   3 of 4" in d
    assert "1 of 2 stacks up" in d and "4 networks in use" in d
    lines = d.splitlines()
    media = next(i for i, s in enumerate(lines) if " media " in s + " ")
    assert media < next(i for i, s in enumerate(lines) if " apps " in s + " ")
    assert lines[media].startswith(panel.CRIT + "    media") and lines[media].endswith("1/2") and WARN not in d
    assert "(no stack)" in d and "sonarr exited (1)" in d
    assert "Engine" not in view.text(panel.docker_section([], None)).description


def test_client_list_info_restart():
    import asyncio

    import aiohttp
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from shazzzbot.docker import Docker
    seen = []

    async def containers(req):
        seen.append(("list", req.query.get("all")))
        return web.json_response([{"Id": "a", "Names": ["/web"], "State": "running", "Status": "Up"}])

    async def info(_):
        return web.json_response({"ServerVersion": "29.8.2", "Driver": "overlay2", "Images": 146, "HttpProxy": "x"})

    async def restart(req):
        seen.append(("restart", req.match_info["cid"], req.query.get("t")))
        return web.Response(status=204)

    async def go():
        app = web.Application()
        app.router.add_get("/containers/json", containers)
        app.router.add_get("/info", info)
        app.router.add_post("/containers/{cid}/restart", restart)
        async with TestServer(app) as srv, aiohttp.ClientSession() as http:
            d = Docker(str(srv.make_url("/")), http)
            assert [c.name for c in await d.list()] == ["web"]
            assert await d.info() == Engine("29.8.2", "overlay2", 146)
            await d.restart("a")
            try:
                await Docker(str(srv.make_url("/nope")), http).info()
            except aiohttp.ClientResponseError as e:
                return e.status
    assert asyncio.run(go()) == 404
    assert seen == [("list", "1"), ("restart", "a", "20")]
