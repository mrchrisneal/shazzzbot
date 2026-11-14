import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import runpy
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from shazzzbot import discordlog, panel, preview, previewcfg, schemaview
from shazzzbot.config import Preview
from shazzzbot.glyphs import OK

SALT = b"s" * 16


def quick(password):
    """A valid but cheap scrypt hash, so tests do not pay for the real cost."""
    return f"scrypt$2$1$1${SALT.hex()}${hashlib.scrypt(password.encode(), salt=SALT, n=2, r=1, p=1, dklen=32).hex()}"


def basic(user, password, kind="Basic"):
    return kind + " " + base64.b64encode(f"{user}:{password}".encode()).decode()


def test_hash_round_trip_and_rejects_bad_or_costly_hashes():
    stored = preview.make_hash("correct horse")
    assert stored.startswith("scrypt$16384$8$1$")
    assert preview.verify("correct horse", stored) and not preview.verify("wrong", stored)
    assert preview.verify("pw", quick("pw"))
    for bad in ("", "md5$1$1$1$00$00", "scrypt$1048576$8$1$00$00", "scrypt$2$99$1$00$00", "scrypt$2$1$9$00$00",
                "scrypt$x$1$1$00$00", "scrypt$2$1$1$zz$00"):
        assert not preview.verify("pw", bad)


def test_pinned_login_is_used_as_given(tmp_path, caplog):
    cfg = SimpleNamespace(preview=Preview(user="me", password_hash="h"), state_file=str(tmp_path / "state.json"))
    assert preview.login(cfg) == ("me", "h")
    assert not caplog.records and not list(tmp_path.iterdir())


def test_generated_login_keeps_the_password_private_and_logs_only_its_location(tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.INFO, "shazzzbot")
    cfg = SimpleNamespace(preview=Preview(), state_file=str(tmp_path / "state.json"))
    user, stored = preview.login(cfg)
    [record] = caplog.records
    bootstrap = Path(record.args[1])
    password = bootstrap.read_text().rstrip("\n")
    assert record.args[0] == user and user.startswith("preview-") and len(password) >= 20
    assert password not in record.getMessage() and password not in record.args
    assert bootstrap.parent == tmp_path and stat.S_IMODE(bootstrap.stat().st_mode) == 0o600
    assert preview.verify(password, stored)
    path = tmp_path / "preview-login.json"
    text = path.read_text()
    assert password not in text and json.loads(text) == {"format": 1, "user": user, "hash": stored}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and set(tmp_path.iterdir()) == {path, bootstrap}
    caplog.clear()
    assert preview.login(cfg) == (user, stored)  # a restart reads the hash back and logs nothing
    assert not caplog.records and password not in path.read_text()
    path.write_text("not json")
    assert preview.login(cfg)[0] != user and len(caplog.records) == 1

    def fail(*_a):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", fail)
    path.unlink()
    before = set(tmp_path.iterdir())
    with pytest.raises(OSError):
        preview.login(cfg)
    assert set(tmp_path.iterdir()) == before


def test_auth_checks_the_scheme_user_and_password_and_caches_good_logins():
    auth = preview.Auth("me", quick("pw"))

    async def go():
        assert not await auth.ok("")
        assert not await auth.ok("Basic !!!")
        assert not await auth.ok("Basic " + base64.b64encode(b"\xff").decode())
        assert not await auth.ok("Basic " + base64.b64encode(b"no-colon").decode())
        assert not await auth.ok(basic("me", "pw", "Bearer"))
        assert not await auth.ok(basic("you", "pw"))
        assert not await auth.ok(basic("me", "nope"))
        assert auth.good == set()
        assert await auth.ok(basic("me", "pw")) and len(auth.good) == 1
        auth.stored = "changed"
        assert await auth.ok(basic("me", "pw"))  # served from the cache
        auth.stored = quick("pw")
        for kind in ("basic", "BASIC", "bASIC", "BaSIC", "BAsIC", "BASiC", "BASIc", "baSIC"):
            assert await auth.ok(basic("me", "pw", kind))
        assert len(auth.good) == 1
    asyncio.run(go())


def test_the_source_manifest_matches_the_package():
    from tools import preview_assets
    assert sorted(preview.sources()) == preview_assets.sources()


@pytest.fixture
def site(tmp_path, monkeypatch):
    """Run `call(http, base, bot, path)` against the preview app with login me:pw."""
    conf = tmp_path / "config.toml"
    conf.write_text('config_format = 1\nguild_id = 1\n\n[system]\ntitle = "Old"\n\n[[outputs]]\nchannel = 2\n')
    bot = SimpleNamespace(cfg=SimpleNamespace(interval=60, outputs=[]), tz=None, rate_budget=discordlog.Budget(),
                          boards=[])
    dumps = []

    async def dump(b):
        dumps.append(b)
        return {"format": 1, "n": len(dumps)}
    monkeypatch.setattr(preview.previewdata, "dump", dump)
    monkeypatch.setenv("DISCORD_TOKEN", "t")

    def run(call):
        async def go():
            application = preview.app(bot, preview.Auth("me", quick("pw")), str(conf))
            async with TestServer(application) as srv, \
                    aiohttp.ClientSession(headers={"Authorization": basic("me", "pw")}) as http:
                return await call(http, str(srv.make_url("")).rstrip("/"))
        try:
            return asyncio.run(go())
        finally:
            panel.use_ok_glyph(OK)
    return SimpleNamespace(run=run, conf=conf, bot=bot, dumps=dumps)


def test_every_route_needs_the_login(site):
    async def call(http, base):
        out = []
        async with aiohttp.ClientSession() as anon:
            for path in ("/", "/VERSION", "/docs/demo/", "/docs/demo/demo.js", "/shazzzbot/bot.py",
                         "/api/config", "/nope"):
                async with anon.get(base + path, allow_redirects=False) as r:
                    out.append((r.status, r.headers.get("WWW-Authenticate", ""), r.headers["Content-Security-Policy"]))
        return out
    for status, challenge, csp in site.run(call):
        assert status == 401 and challenge.startswith("Basic ") and "default-src 'self'" in csp


def test_cache_headers_revalidate_assets_and_never_store_live_data(site):
    async def call(http, base):
        paths = {"/": (302, "no-cache"), "/docs/demo/": (200, "no-cache"),
                 "/docs/demo/editor.js": (200, "no-cache"), "/docs/demo/preview.css": (200, "no-cache"),
                 "/missing": (404, "no-cache"), "/api/config": (200, "no-store"),
                 "/api/missing": (404, "no-store"), "/VERSION": (200, "no-store")}
        for path, expected in paths.items():
            async with http.get(base + path, allow_redirects=False) as response:
                assert (response.status, response.headers.get("Cache-Control")) == expected, path
        async with aiohttp.ClientSession() as anonymous:
            for path, expected in (("/docs/demo/editor.js", "no-cache"), ("/api/config", "no-store"),
                                   ("/VERSION", "no-store")):
                async with anonymous.get(base + path) as response:
                    assert response.status == 401 and response.headers.get("Cache-Control") == expected, path
    site.run(call)


def test_static_files_are_served_only_from_the_page_and_the_manifest(site, tmp_path, monkeypatch):
    page = tmp_path / "page"
    page.mkdir()
    (page / "index.html").write_text("<p>hi</p>")
    (page / "a.js").write_text("x")
    (page / "leak").symlink_to(site.conf)
    monkeypatch.setattr(preview, "PAGE", page)

    async def call(http, base):
        out = {}
        for path in ("/", "/docs/demo/", "/docs/demo/a.js", "/docs/demo/leak", "/docs/demo/missing.js",
                     "/docs/demo/.hidden", "/docs/demo/..%2Fconfig.toml", "/shazzzbot/bot.py",
                     "/shazzzbot/../tools/demo_build.py", "/shazzzbot/nothere.py"):
            async with http.get(base + path, allow_redirects=False) as r:
                out[path] = (r.status, r.headers.get("Content-Type", ""), r.headers.get("Location"),
                             r.headers.get("X-Frame-Options"), await r.read())
        return out
    got = site.run(call)
    assert got["/"][0] == 302 and got["/"][2] == "/docs/demo/"
    assert got["/docs/demo/"][:2] == (200, "text/html") and got["/docs/demo/"][4] == b"<p>hi</p>"
    assert got["/docs/demo/a.js"][:2] == (200, "text/javascript") and got["/docs/demo/a.js"][3] == "DENY"
    assert got["/shazzzbot/bot.py"][0] == 200 and b"class Bot" in got["/shazzzbot/bot.py"][4]
    for path in ("/docs/demo/leak", "/docs/demo/missing.js", "/docs/demo/.hidden", "/docs/demo/..%2Fconfig.toml",
                 "/shazzzbot/../tools/demo_build.py", "/shazzzbot/nothere.py"):
        assert got[path][0] == 404, path


def test_version_is_authenticated_fixed_root_resource_without_a_package_fallback(site, tmp_path, monkeypatch):
    canonical = tmp_path / "VERSION"
    canonical.write_bytes(b"9.8.7\n")
    package = tmp_path / "shazzzbot"
    package.mkdir()
    (package / "VERSION").write_bytes(b"0.0.0\n")
    monkeypatch.setattr(preview, "ROOT", tmp_path)

    async def call(http, base):
        async with http.get(base + "/VERSION") as response:
            assert response.status == 200 and await response.read() == b"9.8.7\n"
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["Cross-Origin-Resource-Policy"] == "same-origin"
        async with http.get(base + "/shazzzbot/VERSION") as response:
            assert response.status == 404
        canonical.unlink()
        async with http.get(base + "/VERSION") as response:
            assert response.status == 404
    site.run(call)


def test_unknown_file_types_are_served_as_bytes(tmp_path):
    (tmp_path / "blob.unknownext").write_bytes(b"x")
    assert preview._file(tmp_path, "blob.unknownext").headers["Content-Type"] == "application/octet-stream"


def test_snapshot_is_cached_for_one_interval_and_never_stored(site):
    async def call(http, base):
        out = []
        for _ in range(2):
            async with http.get(base + "/api/snapshot") as r:
                out.append((await r.json(), r.headers["Cache-Control"]))
        site.bot.cfg.interval = 0
        async with http.get(base + "/api/snapshot") as r:
            out.append((await r.json(), r.headers["Cache-Control"]))
        return out
    assert site.run(call) == [({"format": 1, "n": 1}, "no-store")] * 2 + [({"format": 1, "n": 2}, "no-store")]


def put(http, base, body, **headers):
    h = {"Content-Type": "application/json", preview.HEADER: "1", **headers}
    data = body if isinstance(body, (str, bytes)) else json.dumps(body)
    return http.put(base + "/api/config", data=data, headers=h)


def test_get_config_shows_only_the_display_settings(site):
    async def call(http, base):
        async with http.get(base + "/api/config") as r:
            return await r.json()
    assert site.run(call) == {"toml": '[system]\ntitle = "Old"\n', "live": True, "schema": schemaview.document()}


def test_config_schema_is_generated_once_and_survives_snapshot_cache_updates(site, monkeypatch):
    document = schemaview.document
    expected = document()
    calls = []

    def generate():
        calls.append(True)
        return document()

    monkeypatch.setattr(schemaview, "document", generate)

    async def call(http, base):
        async with aiohttp.ClientSession() as anon, anon.get(base + "/api/config") as response:
            assert response.status == 401
        async with http.get(base + "/api/config") as response:
            first = await response.json()
            assert response.headers["Cache-Control"] == "no-store"
        first["schema"]["client_mutation"] = True
        for _ in range(2):
            async with http.get(base + "/api/snapshot") as response:
                assert (await response.json())["n"] == 1
        site.bot.cfg.interval = 0
        async with http.get(base + "/api/snapshot") as response:
            assert (await response.json())["n"] == 2
        async with http.get(base + "/api/config") as response:
            return await response.json()

    assert site.run(call) == {"toml": '[system]\ntitle = "Old"\n', "live": True, "schema": expected}
    assert calls == [True]


def test_config_schema_never_contains_environment_or_runtime_credentials(site, monkeypatch):
    token = "fixture-" + "discord-credential"
    jellyfin = "fixture-" + "jellyfin-credential"
    password_hash = quick("not-the-preview-login")
    monkeypatch.setenv("DISCORD_TOKEN", token)
    monkeypatch.setenv("JELLYFIN_KEY", jellyfin)
    site.bot.cfg.token = token
    site.bot.cfg.jellyfin_key = jellyfin
    site.bot.cfg.preview = Preview(user="operator", password_hash=password_hash)
    site.conf.write_text(site.conf.read_text() + '\n[preview]\nuser = "operator"\n'
                         + f'password_hash = "{password_hash}"\n')

    async def call(http, base):
        async with http.get(base + "/api/config") as response:
            assert response.status == 200
            body = await response.json()
            assert body["schema"] == schemaview.document()
            return json.dumps(body)

    body = site.run(call)
    for credential in (token, jellyfin, password_hash):
        assert credential not in body


def test_writes_must_be_same_origin_marked_json_and_small(site):
    async def call(http, base):
        out = []
        for body, headers in ((({"toml": ""}), {preview.HEADER: "0"}),
                              ({"toml": ""}, {"Content-Type": "text/plain"}),
                              ({"toml": ""}, {"Origin": "https://evil.example"}),
                              ({"toml": ""}, {"Sec-Fetch-Site": "cross-site"}),
                              ("{not json", {}), ([1], {}), ({"toml": 5}, {}),
                              ({"toml": "x" * (preview.MAX_BODY + 1)}, {})):
            async with put(http, base, body, **headers) as r:
                out.append(r.status)
        return out
    assert site.run(call) == [403, 403, 403, 403, 400, 400, 400, 413]
    assert "Old" in site.conf.read_text()


def test_save_validates_writes_and_applies(site, monkeypatch, caplog):
    for _ in range(3):
        site.bot.rate_budget.auth.finish(site.bot.rate_budget.auth.begin("/users/@me"), 401)
    message = SimpleNamespace(image_wanted=False, edit_slot=7)
    site.bot.boards = [SimpleNamespace(messages=[message])]
    async def call(http, base):
        out = []
        for body in ({"toml": 'timezone = "UTC"\nguild_id = 9\n'},
                     {"toml": 'timezone = "UTC"\n[system]\ntitle = "New"\n'}):
            async with put(http, base, body, Origin=base, **{"Sec-Fetch-Site": "same-origin"}) as r:
                out.append((r.status, await r.json()))
        return out
    bad, good = site.run(call)
    assert bad[0] == 422 and "guild_id cannot be edited" in bad[1]["diagnostics"][0]["message"]
    assert good == (200, {"diagnostics": []})
    assert 'title = "New"' in site.conf.read_text() and "guild_id = 1" in site.conf.read_text()
    assert site.bot.cfg.title == "New" and str(site.bot.tz) == "UTC"
    assert message.image_wanted and message.edit_slot is None
    assert not site.bot.rate_budget.auth.blocked()

    def readonly(*_a):
        raise OSError(30, "Read-only file system")
    monkeypatch.setattr(previewcfg, "write", readonly)

    async def again(http, base):
        async with put(http, base, {"toml": '[system]\ntitle = "Other"\n'}) as r:
            return r.status, await r.json()
    assert site.run(again) == (409, {"diagnostics": [], "error": "config.toml is read-only to the bot"})
    assert "Read-only file system" in caplog.text and "Other" not in site.conf.read_text()


def test_start_serves_only_when_enabled_and_never_stops_the_bot(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, "shazzzbot")
    monkeypatch.setenv("SHAZZZBOT_CONFIG", str(tmp_path / "config.toml"))

    def bot(settings):
        return SimpleNamespace(cfg=SimpleNamespace(preview=settings, state_file=str(tmp_path / "state.json"),
                                                   interval=60), image_files=AsyncMock(), _send=AsyncMock())

    async def go():
        await preview.start(bot(None))
        assert preview._runners == []
        await preview.start(bot(Preview(bind="127.0.0.1", port=0, user="me", password_hash=quick("pw"))))
        assert len(preview._runners) == 1
        port = preview._runners[0].addresses[0][1]
        async with aiohttp.ClientSession(headers={"Authorization": basic("me", "pw")}) as http, \
                http.get(f"http://127.0.0.1:{port}/docs/demo/index.html") as r:
            assert r.status == 200
        await preview.start(bot(Preview(bind="127.0.0.1", port=port, user="me", password_hash=quick("pw"))))
        assert len(preview._runners) == 1
        await preview._runners.pop().cleanup()
        return port
    try:
        port = asyncio.run(go())
    finally:
        for h in [h for h in logging.getLogger().handlers if type(h).__name__ == "Buffer"]:
            logging.getLogger().removeHandler(h)
    assert "preview listening on 127.0.0.1:0" in caplog.text
    assert f"preview could not start on 127.0.0.1:{port}" in caplog.text


def test_hash_cli(monkeypatch, capsys):
    with pytest.raises(SystemExit, match="usage"):
        preview.main([])
    monkeypatch.setattr(sys, "stdin", io.StringIO("short\n"))
    with pytest.raises(SystemExit, match="12 or more"):
        preview.main(["hash"])
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(preview.getpass, "getpass", lambda prompt: "a typed password")
    preview.main(["hash"])
    assert preview.verify("a typed password", capsys.readouterr().out.strip())
    monkeypatch.setattr(sys, "stdin", io.StringIO("a long enough password\n"))
    monkeypatch.setattr(sys, "argv", ["preview", "hash"])
    monkeypatch.delitem(sys.modules, "shazzzbot.preview")
    runpy.run_module("shazzzbot.preview", run_name="__main__")
    assert preview.verify("a long enough password", capsys.readouterr().out.strip())



def test_log_buffer_scrubs_every_secret_class_and_skips_the_login_line(monkeypatch):
    from shazzzbot import previewlog
    monkeypatch.setenv("FORGE_TOKEN_1", "forgetokenvalue")
    cfg = SimpleNamespace(token="bottokenvalue", jellyfin_key="jellykeyvalue",
                          relays=[SimpleNamespace(key="relaykeyvalue")],
                          preview=Preview(user="me", password_hash="scrypt$hashvalue"))
    buf = previewlog.Buffer(previewlog.secrets_of(cfg), size=3)
    lg = logging.getLogger("shazzzbot.testlog")
    lg.addHandler(buf)
    try:
        lg.warning("preview login, shown once: user u password p")
        lg.error("bottokenvalue jellykeyvalue relaykeyvalue scrypt$hashvalue forgetokenvalue "
                 "Authorization: Bearer abc.def https://username:password@host/x?api_key=qqqval&n=1")
        try:
            raise ValueError("boom bottokenvalue")
        except ValueError:
            lg.exception("failed")
    finally:
        lg.removeHandler(buf)
    got = buf.after(0)
    assert [r["seq"] for r in got] == [1, 2] and buf.after(1) == got[1:]
    text = json.dumps(got)
    for secret in ("bottokenvalue", "jellykeyvalue", "relaykeyvalue", "hashvalue", "forgetokenvalue", "abc.def",
                   "username:password", "qqqval", "shown once"):
        assert secret not in text
    assert "https://[redacted]@host/x?api_key=[redacted]&n=1" in got[0]["message"]
    assert got[0]["level"] == "ERROR" and "ValueError" in got[1]["message"]


def test_log_secrets_are_collected_when_preview_is_disabled(monkeypatch):
    from shazzzbot import previewlog

    monkeypatch.setenv("FORGE_TOKEN_1", "forgetokenvalue")
    cfg = SimpleNamespace(token="bottokenvalue", jellyfin_key="tiny",
                          relays=[SimpleNamespace(key="relaykeyvalue")], preview=None)
    found = previewlog.secrets_of(cfg)
    assert {"bottokenvalue", "relaykeyvalue", "forgetokenvalue"} <= found
    assert "tiny" not in found


def test_log_format_errors_use_the_real_handler_and_do_not_break_later_records(monkeypatch, capsys):
    from shazzzbot import previewlog

    monkeypatch.setattr(logging, "raiseExceptions", True)
    buf = previewlog.Buffer(set())
    bad = logging.LogRecord("preview-test", logging.ERROR, __file__, 0, "counter %d", ("invalid",), None)
    buf.handle(bad)
    diagnostic = capsys.readouterr()
    assert not diagnostic.out and "Logging error" in diagnostic.err and "TypeError" in diagnostic.err
    assert buf.seq == 0 and buf.after(0) == []
    good = logging.LogRecord("preview-test", logging.INFO, __file__, 0, "still available", (), None)
    buf.handle(good)
    assert [(r["seq"], r["level"], r["message"]) for r in buf.after(0)] == [(1, "INFO", "still available")]


def test_log_route_needs_a_buffer_and_a_number(tmp_path, monkeypatch):
    from shazzzbot import previewlog
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    conf = tmp_path / "config.toml"
    conf.write_text("guild_id = 1\n")
    bot = SimpleNamespace(cfg=SimpleNamespace(interval=60, timezone="UTC"), tz=None)
    buf = previewlog.Buffer(set())
    buf.handle(logging.makeLogRecord({"msg": "hello", "levelname": "INFO", "name": "x"}))

    async def go(logs):
        application = preview.app(bot, preview.Auth("me", quick("pw")), str(conf), logs)
        async with TestServer(application) as srv, \
                aiohttp.ClientSession(headers={"Authorization": basic("me", "pw")}) as http:
            base = str(srv.make_url("")).rstrip("/")
            out = []
            for q in ("?after=0", "?after=1", "?after=x"):
                async with http.get(base + "/api/log" + q) as r:
                    out.append((r.status, await r.json() if r.status == 200 else None))
            return out
    ok, empty, bad = asyncio.run(go(buf))
    assert ok == (200, {"records": buf.after(0), "seq": 1, "tz": "UTC"}) and ok[1]["records"][0]["message"] == "hello"
    assert empty[1]["records"] == [] and bad[0] == 400
    assert asyncio.run(go(None))[0][0] == 404


def test_the_snapshot_route_answers_activity_alone_behind_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    conf = tmp_path / "config.toml"
    conf.write_text("guild_id = 1\n")
    bot = SimpleNamespace(cfg=SimpleNamespace(interval=60, timezone="UTC"), tz=None)
    stage = {"stage": "render", "targets": ["Disks"], "n": 1, "of": 1}

    async def go():
        application = preview.app(bot, preview.Auth("me", quick("pw")), str(conf), None, lambda: stage)
        async with TestServer(application) as srv, aiohttp.ClientSession() as http:
            base = str(srv.make_url("")).rstrip("/")
            async with http.get(base + "/api/snapshot?only=activity") as r:
                anon = r.status
            async with http.get(base + "/api/snapshot?only=activity",
                                headers={"Authorization": basic("me", "pw")}) as r:
                return anon, await r.json()
    anon, body = asyncio.run(go())
    assert anon == 401 and body == {"activity": stage}
