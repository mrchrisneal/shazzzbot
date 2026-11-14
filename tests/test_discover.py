import asyncio
import base64
import json
import runpy
import sys

import aiohttp
import pytest

from shazzzbot import discover

TOKEN = "MTAxMjM0NTY3ODkwMTIzNDU2Nzg5.GaBcDe.abcdefghijklmnopqrstuvwxyz0123456789"
GUILD = "123456789012345678"


class Resp:
    def __init__(self, status, data):
        self.status, self.data = status, data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self.data


class Http:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def request(self, method, url, json=None):
        self.calls.append((method, url, json))
        answer = self.answers[(method, url.removeprefix(discover.API))]
        if isinstance(answer, Exception):
            raise answer
        return Resp(*answer)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


CHANNELS = [
    {"id": 3, "name": "general", "type": 0, "position": 0},
    {"id": 10, "name": "Ops", "type": 4, "position": 1},
    {"id": 11, "name": "Info", "type": 4, "position": 0},
    {"id": 4, "name": "status", "type": 0, "position": 2, "parent_id": 10},
    {"id": 5, "name": "news", "type": 5, "position": 1, "parent_id": 11},
    {"id": 6, "name": "voice", "type": 2, "position": 0},
    {"id": 7, "name": "audit", "type": 0, "parent_id": 10},
    {"id": 8, "name": "orphan", "type": 0, "position": 3, "parent_id": 99},
]

ROLES = [
    {"id": GUILD, "name": "@everyone", "position": 0},
    {"id": "21", "name": "Mods", "position": 2},
    {"id": "22", "name": "Shazzzbot", "position": 3, "managed": True},
    {"id": "23", "name": "Admins", "position": 5},
    {"id": "24", "name": "Members", "position": 1},
]


def http_for(extra=None):
    answers = {("GET", "/users/@me/guilds"): (200, [{"id": 1, "name": "Home"}]),
               ("GET", f"/guilds/{GUILD}/channels"): (200, CHANNELS),
               ("GET", f"/guilds/{GUILD}/roles"): (200, ROLES),
               ("GET", f"/guilds/{GUILD}"): (200, {"id": GUILD, "owner_id": "223456789012345678"})}
    answers.update(extra or {})
    return Http(answers)


def test_snowflake_and_headers():
    assert discover.snowflake(GUILD) == GUILD
    with pytest.raises(discover.DiscoverError):
        discover.snowflake("12")
    assert discover.headers("t")["Authorization"] == "Bot t"


def test_guilds_and_channels_sort_by_category():
    http = http_for()
    assert asyncio.run(discover.guilds(http)) == [{"id": "1", "name": "Home"}]
    rows = asyncio.run(discover.channels(http, GUILD))
    assert [(r["name"], r["category"]) for r in rows] == [
        ("general", ""), ("orphan", ""), ("news", "Info"), ("audit", "Ops"), ("status", "Ops")]
    with pytest.raises(discover.DiscoverError):
        asyncio.run(discover.channels(http, "../x"))


def test_roles_skip_everyone_and_managed_and_owner_is_separate():
    http = http_for()
    assert [r["name"] for r in asyncio.run(discover.roles(http, GUILD))] == ["Admins", "Mods", "Members"]
    assert asyncio.run(discover.owner(http, GUILD)) == [{"id": "223456789012345678"}]
    with pytest.raises(discover.DiscoverError):
        asyncio.run(discover.roles(http, "../x"))


def test_call_raises_on_http_error():
    http = http_for({("GET", "/users/@me/guilds"): (401, {})})
    with pytest.raises(discover.DiscoverError, match="HTTP 401"):
        asyncio.run(discover.guilds(http))


def test_set_icon_reports_each_target_without_failing():
    http = Http({("PATCH", "/applications/@me"): (200, {}),
                 ("PATCH", "/users/@me"): (429, {})})
    rows = asyncio.run(discover.set_icon(http, b"png"))
    assert rows == [{"target": "application", "result": "ok"},
                    {"target": "avatar", "result": "HTTP 429 for PATCH /users/@me"}]
    image = "data:image/png;base64," + base64.b64encode(b"png").decode()
    assert http.calls[0][2] == {"icon": image} and http.calls[1][2] == {"avatar": image}
    http = Http({("PATCH", "/applications/@me"): TimeoutError(),
                 ("PATCH", "/users/@me"): aiohttp.ClientError("down")})
    assert [r["result"] for r in asyncio.run(discover.set_icon(http, b"png"))] == ["TimeoutError", "down"]


@pytest.fixture
def session(monkeypatch):
    http = http_for({("PATCH", "/applications/@me"): (200, {}), ("PATCH", "/users/@me"): (200, {})})
    seen = {}

    def factory(headers, timeout):
        seen.update(headers)
        return http
    monkeypatch.setattr(discover.aiohttp, "ClientSession", factory)
    monkeypatch.setenv("DISCORD_TOKEN", TOKEN)
    return seen


def test_main_actions(session, capsys, tmp_path):
    assert discover.main(["guilds"]) == 0
    assert json.loads(capsys.readouterr().out) == {"id": "1", "name": "Home"}
    assert session["Authorization"] == f"Bot {TOKEN}"
    assert discover.main(["channels", GUILD]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 5
    assert discover.main(["roles", GUILD]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 3
    assert discover.main(["owner", GUILD]) == 0
    assert json.loads(capsys.readouterr().out) == {"id": "223456789012345678"}
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"png")
    assert discover.main(["icon", str(icon)]) == 0
    assert capsys.readouterr().out.count('"ok"') == 2


@pytest.mark.parametrize("argv", [[], ["guilds", "x"], ["channels"], ["roles"], ["owner"], ["icon"],
                                  ["other", "x"]])
def test_main_usage_errors(session, capsys, argv):
    assert discover.main(argv) == 1
    err = capsys.readouterr().err
    assert "usage" in err and TOKEN not in err


def test_main_reports_failures_without_the_token(session, capsys):
    assert discover.main(["channels", "1"]) == 1
    assert discover.main(["icon", "/no/such/file"]) == 1
    err = capsys.readouterr().err
    assert err.count("discover failed") == 2 and TOKEN not in err


def test_main_needs_a_token(monkeypatch, capsys):
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    assert discover.main(["guilds"]) == 2
    monkeypatch.setattr(sys, "argv", ["discover"])
    monkeypatch.delitem(sys.modules, "shazzzbot.discover", raising=False)
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("shazzzbot.discover", run_name="__main__")
    assert exit_info.value.code == 2


def test_main_reads_sys_argv(session, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["discover", "guilds"])
    assert discover.main() == 0


def readiness_http(monkeypatch, *, permissions=117760, output=True):
    from types import SimpleNamespace

    from shazzzbot import config, health
    user = "523456789012345678"
    channel = "223456789012345678"
    audit = "323456789012345678"
    cfg = SimpleNamespace(guild_id=int(GUILD), outputs=[SimpleNamespace(channel=int(channel))] if output else [],
                          audit_channel_id=int(audit), commands_channel_id=int(channel))
    monkeypatch.setattr(config, "load", lambda path: cfg)
    monkeypatch.setattr(health, "ok", lambda: True)
    http = Http({("GET", f"/guilds/{GUILD}"): (200, {"id": GUILD, "owner_id": "other"}),
                 ("GET", "/users/@me"): (200, {"id": user}),
                 ("GET", f"/guilds/{GUILD}/members/{user}"): (200, {"roles": []}),
                 ("GET", f"/guilds/{GUILD}/roles"): (200, [{"id": GUILD, "permissions": str(permissions)}]),
                 ("GET", f"/channels/{channel}"): (200, {"id": channel, "guild_id": GUILD, "type": 0}),
                 ("GET", f"/channels/{audit}"): (200, {"id": audit, "guild_id": GUILD, "type": 0})})
    return cfg, http


def test_readiness_proves_main_loop_guild_and_channel_permissions(monkeypatch):
    cfg, http = readiness_http(monkeypatch)
    assert asyncio.run(discover.ready(http, "cfg")) == [{"result": "ready"}]
    assert len(http.calls) == 6
    cfg.audit_channel_id = cfg.commands_channel_id = 0
    assert asyncio.run(discover.ready(http, "cfg")) == [{"result": "ready"}]


@pytest.mark.parametrize("problem", ["guild", "channel", "type", "permissions", "schema", "empty", "heartbeat"])
def test_readiness_fails_closed(monkeypatch, problem):
    from shazzzbot import health
    cfg, http = readiness_http(monkeypatch)
    key = ("GET", "/channels/223456789012345678")
    if problem == "guild":
        http.answers[("GET", f"/guilds/{GUILD}")] = (200, {"id": "another"})
    elif problem in {"channel", "type", "schema"}:
        data = http.answers[key][1]
        if problem == "channel":
            data["guild_id"] = "another"
        elif problem == "type":
            data["type"] = 2
        else:
            data.pop("guild_id")
    elif problem == "permissions":
        http.answers[("GET", f"/guilds/{GUILD}/roles")] = (200, [{"id": GUILD, "permissions": "1024"}])
    elif problem == "empty":
        cfg.outputs = []
        cfg.audit_channel_id = cfg.commands_channel_id = 0
    else:
        monkeypatch.setattr(health, "ok", lambda: False)
    with pytest.raises(discover.DiscoverError):
        asyncio.run(discover.ready(http, "cfg"))


def test_readiness_applies_role_and_member_overwrites_in_discord_order():
    guild = {"id": "guild", "owner_id": "owner"}
    member = {"roles": ["role"]}
    roles = [{"id": "guild", "permissions": "1024"}, {"id": "role", "permissions": "2048"},
             {"id": "ignored", "permissions": "8"}]
    channel = {"permission_overwrites": [{"id": "guild", "type": 0, "deny": "1024", "allow": "0"},
                                         {"id": "role", "type": 0, "deny": "2048", "allow": "1024"},
                                         {"id": "user", "type": 1, "deny": "0", "allow": "2048"}]}
    assert discover.permissions(guild, member, roles, channel, "user") == 3072
    assert discover.permissions(guild, member, roles, channel, "owner") == (1 << 53) - 1
    assert discover.permissions(guild, member, [{"id": "guild", "permissions": "8"}], {}, "user") == (1 << 53) - 1


def test_ready_routes_and_relay_health(session, monkeypatch, tmp_path):
    from unittest.mock import AsyncMock

    from shazzzbot import relay
    ask = AsyncMock(return_value=[{"result": "ready"}])
    monkeypatch.setattr(discover, "ready", ask)
    assert discover.main(["ready", "cfg"]) == 0
    ask.assert_awaited_once()
    monkeypatch.setenv("RELAY_DIR", str(tmp_path))
    (tmp_path / "api.key").write_text("k" * 32)
    monkeypatch.setattr(relay, "fingerprint", lambda path: "a" * 64)
    ping = AsyncMock(return_value=(relay.FINE, 1, "", "0.1.0"))
    monkeypatch.setattr(relay, "ping", ping)
    monkeypatch.delenv("DISCORD_TOKEN")
    assert discover.main(["ready-relay"]) == 0
    assert ping.call_args.args[1].url == "https://127.0.0.1:8443"
    ping.return_value = (relay.WARNING, 0, "down", None)
    assert discover.main(["ready-relay"]) == 1
