"""The config preview web page, served from the running bot behind HTTP Basic auth.

It serves docs/demo and the package sources it lists, the bot's current inputs, and the display settings. Saving
re-runs the config loader on the merged file before anything is written.
"""
import asyncio
import base64
import binascii
import getpass
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import secrets
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aiohttp import web

from . import config, previewactivity, previewcfg, previewdata, previewlog, schemaview
from .helper import formats

log = logging.getLogger("shazzzbot")
ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "docs" / "demo"
SOURCES = PAGE / "sources.json"
N, R, P = 2**14, 8, 1
MAX_BODY = 65536
HEADER = "X-Shazzzbot-Preview"
REALM = 'Basic realm="shazzzbot preview", charset="UTF-8"'
CSP = ("default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self' blob:; img-src 'self' data: blob:; "
       "connect-src 'self'; style-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
       "form-action 'none'")
SAFE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*(/[A-Za-z0-9_][A-Za-z0-9_.-]*)*")
for ext, kind in ((".mjs", "text/javascript"), (".js", "text/javascript"), (".wasm", "application/wasm"),
                  (".whl", "application/zip"), (".json", "application/json")):
    mimetypes.add_type(kind, ext)
_runners: list[web.AppRunner] = []


def make_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P, dklen=32)
    return f"scrypt${N}${R}${P}${salt.hex()}${key.hex()}"


def verify(password: str, stored: str) -> bool:
    try:
        name, n, r, p, salt, key = stored.split("$")
        if name != "scrypt" or not (2 <= int(n) <= 2**17 and 1 <= int(r) <= 16 and 1 <= int(p) <= 4):
            return False
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(bytes.fromhex(key)), maxmem=2**28)
    except ValueError:
        return False
    return hmac.compare_digest(got.hex(), key)



def login(cfg: config.Config) -> tuple[str, str]:
    """Use pinned credentials, else keep the generated password in a private bootstrap file."""
    settings = cfg.preview
    if settings is not None and settings.user:
        return settings.user, settings.password_hash
    # The state directory comes only from operator configuration, never a request. reviewed 2026-11
    # bearer:disable python_lang_path_traversal
    path = os.path.join(os.path.dirname(cfg.state_file) or ".", "preview-login.json")
    try:
        # This fixed filename is under the operator's state directory. reviewed 2026-11
        # bearer:disable python_lang_path_traversal
        with open(path) as f:
            saved = json.load(f)
        saved = formats.load(saved, "preview login", 1)
        return str(saved["user"]), str(saved["hash"])
    except formats.FormatError:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        pass
    user, password = "preview-" + secrets.token_hex(3), secrets.token_urlsafe(18)
    stored = make_hash(password)
    # mkstemp creates a private file in the operator's state directory. reviewed 2026-11
    # bearer:disable python_lang_path_traversal
    fd, bootstrap = tempfile.mkstemp(prefix="preview-password-", suffix=".txt", dir=os.path.dirname(path))
    published = False
    try:
        with os.fdopen(fd, "w") as f:
            f.write(password + "\n")
            f.flush()
            os.fsync(f.fileno())
        # Both paths stay inside the operator's state directory. reviewed 2026-11
        # bearer:disable python_lang_path_traversal
        fd, tmp = tempfile.mkstemp(prefix=".preview-login-", dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(formats.dump({"user": user, "hash": stored}, 1), f)
                f.flush()
                os.fsync(f.fileno())
            # The destination is fixed and the source was created with mkstemp. reviewed 2026-11
            # bearer:disable python_lang_path_traversal
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        published = True
    finally:
        if not published:
            os.unlink(bootstrap)
    # Only the username and local file paths are logged, never the password. reviewed 2026-11
    # bearer:disable python_lang_path_traversal
    log.warning("preview login for %s: read %s (delete %s to make a new one)", user, bootstrap, path)
    return user, stored


class Auth:
    def __init__(self, user: str, stored: str):
        self.user, self.stored = user, stored
        self.good: set[str] = set()
        self.lock = asyncio.Lock()

    async def ok(self, header: str) -> bool:
        digest = hashlib.sha256(header.encode()).hexdigest()
        if any(hmac.compare_digest(digest, g) for g in self.good):
            return True
        kind, _, blob = header.partition(" ")
        try:
            user, sep, password = base64.b64decode(blob, validate=True).decode().partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return False
        if kind.lower() != "basic" or not sep:
            return False
        async with self.lock:  # one scrypt at a time bounds what guessing can cost the host
            good = await asyncio.to_thread(verify, password, self.stored)
            good = hmac.compare_digest(user.encode(), self.user.encode()) and good
            if not good:
                await asyncio.sleep(0.5)
        if good:
            self.good = {digest} if len(self.good) >= 8 else self.good | {digest}
        return good


def sources() -> set[str]:
    with open(SOURCES) as f:
        return set(json.load(f)["files"])


def _file(root: Path, name: str) -> web.FileResponse:
    if not SAFE.fullmatch(name):
        raise web.HTTPNotFound()
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Content-Type": mimetypes.guess_type(path.name)[0]
                                           or "application/octet-stream"})


def app(bot: Any, auth: Auth, config_path: str, logs: previewlog.Buffer | None = None,
        activity: Callable[[], dict | None] = lambda: None) -> web.Application:
    snap: dict[str, Any] = {"at": -1e9, "data": None}
    snap_lock, save_lock = asyncio.Lock(), asyncio.Lock()
    allowed = sources()
    metadata = schemaview.document()

    @web.middleware
    async def guard(request: web.Request, handler: Any) -> web.StreamResponse:
        if not await auth.ok(request.headers.get("Authorization", "")):
            raise web.HTTPUnauthorized(text="login required", headers={"WWW-Authenticate": REALM})
        return await handler(request)

    async def secure(request: web.Request, response: web.StreamResponse) -> None:
        response.headers.update({"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                                 "Cross-Origin-Opener-Policy": "same-origin",
                                 "Cross-Origin-Resource-Policy": "same-origin"})
        if request.path.startswith("/api/") or request.path == "/VERSION":
            response.headers["Cache-Control"] = "no-store"
        else:
            response.headers["Cache-Control"] = "no-cache"

    async def home(_request: web.Request) -> web.StreamResponse:
        raise web.HTTPFound("/docs/demo/")

    async def page(request: web.Request) -> web.StreamResponse:
        return _file(PAGE, request.match_info.get("name") or "index.html")

    async def source(request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        if name == "VERSION" or name not in allowed:
            raise web.HTTPNotFound()
        return _file(ROOT / "shazzzbot", name)

    async def version(_request: web.Request) -> web.StreamResponse:
        return _file(ROOT, "VERSION")

    async def snapshot(request: web.Request) -> web.StreamResponse:
        if request.query.get("only") == "activity":
            return web.json_response({"activity": activity()})
        async with snap_lock:
            if time.monotonic() - snap["at"] >= bot.cfg.interval:
                snap["data"], snap["at"] = await previewdata.dump(bot), time.monotonic()
        return web.json_response(snap["data"])

    def current() -> str:
        # The path comes from the operator's environment/default, never HTTP input. reviewed 2026-11
        # bearer:disable python_lang_path_traversal
        with open(config_path, encoding="utf-8") as f:
            return f.read()

    async def get_config(_request: web.Request) -> web.StreamResponse:
        return web.json_response({"toml": previewcfg.subset(current()), "live": True, "schema": metadata})

    async def put_config(request: web.Request) -> web.StreamResponse:
        origin = request.headers.get("Origin")
        if request.headers.get(HEADER) != "1" or request.content_type != "application/json" \
                or origin is not None and origin != f"{request.scheme}://{request.host}" \
                or request.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin":
            raise web.HTTPForbidden(text="cross-origin or unmarked write")
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="body must be JSON") from None
        text = body.get("toml") if isinstance(body, dict) else None
        if not isinstance(text, str):
            raise web.HTTPBadRequest(text="body must be {\"toml\": text}")
        async with save_lock:
            base = current()
            diagnostics, cfg = previewcfg.check(text, base)
            if cfg is None:
                return web.json_response({"diagnostics": diagnostics}, status=422)
            try:
                values = previewcfg.unmask(tomllib.loads(text), tomllib.loads(base))
                await asyncio.to_thread(previewcfg.write, config_path, previewcfg.splice(base, values))
            except OSError as e:
                log.warning("preview could not save %s: %s", config_path, e.strerror)
                return web.json_response({"diagnostics": [], "error": "config.toml is read-only to the bot"},
                                         status=409)
            previewcfg.apply(bot, cfg)
            for board in bot.boards:
                for message in board.messages:
                    message.image_wanted, message.edit_slot = True, None
        log.info("preview saved the display settings to %s", config_path)
        return web.json_response({"diagnostics": diagnostics})

    async def get_log(request: web.Request) -> web.StreamResponse:
        if logs is None:
            raise web.HTTPNotFound()
        try:
            after = int(request.query.get("after", "0"))
        except ValueError:
            raise web.HTTPBadRequest(text="after must be a number") from None
        return web.json_response({"records": logs.after(after), "seq": logs.seq, "tz": bot.cfg.timezone})

    application = web.Application(middlewares=[guard], client_max_size=MAX_BODY)
    application.on_response_prepare.append(secure)
    application.router.add_get("/", home)
    application.router.add_get("/VERSION", version)
    application.router.add_get("/docs/demo/", page)
    application.router.add_get("/docs/demo/{name:.+}", page)
    application.router.add_get("/shazzzbot/{name:.+}", source)
    application.router.add_get("/api/snapshot", snapshot)
    application.router.add_get("/api/config", get_config)
    application.router.add_get("/api/log", get_log)
    application.router.add_put("/api/config", put_config)
    return application


async def start(bot: Any) -> None:
    """Serve the preview when [preview] is enabled; a failure is logged and never stops the bot."""
    settings = bot.cfg.preview
    if settings is None:
        return
    try:
        user, stored = login(bot.cfg)
        logs = previewlog.Buffer(previewlog.secrets_of(bot.cfg))
        runner = web.AppRunner(app(bot, Auth(user, stored), os.environ.get("SHAZZZBOT_CONFIG", "/config/config.toml"),
                                   logs, previewactivity.watch(bot)))
        await runner.setup()
        await web.TCPSite(runner, settings.bind, settings.port).start()
    except OSError as e:
        log.error("preview could not start on %s:%d: %s", settings.bind, settings.port, e)
        return
    _runners.append(runner)
    logging.getLogger().addHandler(logs)
    log.info("preview listening on %s:%d", settings.bind, settings.port)


def main(argv: list[str]) -> None:
    if argv != ["hash"]:
        raise SystemExit("usage: python -m shazzzbot.preview hash  (reads the password from stdin)")
    password = getpass.getpass("Password: ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\n")
    if len(password) < 12:
        raise SystemExit("use a password of 12 or more characters")
    print(make_hash(password))


if __name__ == "__main__":
    main(sys.argv[1:])
