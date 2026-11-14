from __future__ import annotations

import asyncio
import builtins
import configparser
import contextlib
import hashlib
import os
import re
import signal
import socket
import struct
import time
from asyncio.exceptions import TimeoutError as AsyncTimeoutError
from pathlib import Path
from typing import Any, cast

from . import audit, files, footprint, installation, process, security, updates
from .auth import ProtocolMismatch, Verifier, canonical
from .constants import MAX_REQUEST, MAX_UPDATE, PROTOCOL, VERSION, Send
from .platforms.base import Platform


class Helper(Verifier):
    def __init__(self, cfg: dict[str, Any], key: bytes, path: Path, platform: Platform):
        self.cfg, self.key, self.path = cfg, key, footprint.home(cfg) / "helper.pyz"
        self.sha256 = cfg.get("_loaded_sha256") or hashlib.sha256(files.private_read(path, MAX_UPDATE)).hexdigest()
        installation.guard(cfg)
        self.env = updates.environment(cfg)
        self.audit = audit.Audit(cfg, key)
        super().__init__(key)
        self.platform = platform
        self.busy: set[str] = set()
        self.actions: set[asyncio.Task] = set()
        self.stopping = False
        self.clients: set[asyncio.Task] = set()
        self.protocol_mismatch: tuple[int, int] | None = None
        self.audit.add(f"startup version={VERSION} sha256={self.sha256}")

    async def check_release(self) -> None:
        try:
            installation.guard(self.cfg)
            version = await updates.release(self.cfg)
            self.audit.notice(version, self.cfg["update_sha256"], self.sha256, self.path)
        except (OSError, ValueError, AsyncTimeoutError):
            self.audit.add("startup update check unavailable")

    async def run(self, argv: list[str], emit: Send, userscript: bool = False) -> int:
        installation.guard(self.cfg)
        permitted_shell = userscript and len(argv) == 2 and argv[0] == "/bin/bash"
        if not os.path.isabs(argv[0]) or (security.banned(os.path.basename(argv[0]).lower()) and not permitted_shell):
            raise ValueError("forbidden executable")
        rc, lines, truncated = await process.execute(argv, self.cfg["action_timeout"], self.cfg["output_bytes"],
                                                    env=self.env)
        try:
            for line in lines:
                await emit({"out": line})
            if truncated:
                await emit({"truncated": True})
        except (OSError, AsyncTimeoutError):
            self.audit.add("output delivery failed")
        return rc

    async def query(self, argv: list[str]) -> list[str]:
        output: list[str] = []

        async def collect(obj: dict[str, Any]) -> None:
            if "out" not in obj:
                raise ValueError("live inventory truncated")
            output.append(obj["out"] + "\n")
        if await self.run(argv, collect) != 0:
            raise ValueError("live inventory unavailable")
        return "".join(output).splitlines()

    async def resolve(self, family: str, op: str, name: str) -> list[str]:
        return await self.platform.resolve(family, op, name, self.cfg, self.query)

    async def read(self, action: str, emit: Send) -> int:
        installation.guard(self.cfg)
        try:
            timeout = min(self.cfg["action_timeout"], 5)
            data = await asyncio.wait_for(self.platform.read(action, timeout, self.cfg["output_bytes"]), timeout)
            payload = {action: data}
            if not isinstance(data, dict) or len(canonical(payload)) > self.cfg["output_bytes"]:
                raise ValueError("tasks output limit exceeded")
        except (OSError, ValueError, TypeError, AsyncTimeoutError, configparser.Error, RecursionError) as error:
            self.audit.add("tasks collection failed (" + type(error).__name__ + ")")
            await emit({"error": "tasks collection failed"})
            return 1
        await emit(payload)
        return 0

    async def dispatch(self, action: str, args: dict[str, Any], emit: Send) -> None:
        started = time.monotonic()
        if self.stopping:
            raise ValueError("helper stopping")
        installation.guard(self.cfg)
        if action == "hello":
            if set(args) not in (set(), {"latest_version", "latest_sha256"}):
                raise ValueError("invalid hello arguments")
            if args:
                v, digest = args["latest_version"], args["latest_sha256"]
                if not isinstance(v, str) or re.fullmatch(r"[A-Za-z0-9_.+-]{1,64}", v) is None\
                        or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                    raise ValueError("invalid update metadata")
                updates.version_number(v)
                self.audit.notice(v, digest, self.sha256, self.path)
            with footprint.open_home(self.cfg) as home:
                bundle = footprint.bundle(home)
            await emit({"version": VERSION, "sha256": self.sha256, "protocol": PROTOCOL,
                        "bundle_version": bundle["version"].removeprefix("v"),
                        "entry_sha256": bundle["files"][footprint.BUNDLE_FILES[0]]["sha256"]})
            await emit({"done": 0, "secs": 0})
            return
        readonly = action in self.platform.readonly
        if readonly:
            if action not in self.cfg["enabled"] or args:
                raise ValueError("action not enabled or takes no arguments")
            family, op, name = action, "", "host"
        else:
            pieces = action.split(".")
            if len(pieces) != 2 or pieces[0] not in self.cfg["enabled"] or pieces[0] not in self.platform.operations \
                    or pieces[1] not in self.platform.operations[pieces[0]]:
                raise ValueError("action not enabled")
            family, op = pieces
            name = self.platform.target(family, args)
        target = family + ":" + name
        if target in self.busy:
            await emit({"error": "busy"})
            return
        self.busy.add(target)
        task = cast(asyncio.Task, asyncio.current_task())
        self.actions.add(task)
        try:
            await self.platform.ensure_safe(self.cfg["accept_flash_share"])
            self.audit.add("action " + action + " target=" + name)
            if readonly:
                rc = await self.read(action, emit)
            else:
                argv = await self.resolve(family, op, name)
                try:
                    rc = await self.run(argv, emit, userscript=True) if family == "userscript"\
                        else await self.run(argv, emit)
                except OSError:
                    rc = 127
            self.audit.add(f"done {action} target={name} rc={rc}")
            await emit({"done": rc, "secs": round(time.monotonic() - started, 3)})
        finally:
            self.busy.remove(target)
            self.actions.remove(task)

    async def client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = cast(asyncio.Task, asyncio.current_task())
        if len(self.clients) >= 64:
            writer.close()
            await writer.wait_closed()
            return
        self.clients.add(task)

        async def emit(obj: dict[str, Any]) -> None:
            writer.write(canonical(obj) + b"\n")
            await asyncio.wait_for(writer.drain(), self.cfg["read_timeout"])
        try:
            peer = writer.get_extra_info("socket").getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            _, uid, _ = struct.unpack("3i", peer)
            if uid not in self.cfg["peer_uids"]:
                raise ValueError("denied")
            raw = await asyncio.wait_for(reader.readline(), self.cfg["read_timeout"])
            action, args = self.verify(raw)
            self.protocol_mismatch = None
            await self.dispatch(action, args, emit)
        except ProtocolMismatch as error:
            mismatch = (error.expected, error.received)
            if mismatch != self.protocol_mismatch:
                self.protocol_mismatch = mismatch
                self.audit.add(f"protocol mismatch expected={error.expected} received={error.received}")
            with contextlib.suppress(OSError, AsyncTimeoutError):
                await emit({"error": "protocol mismatch", "expected": error.expected, "received": error.received})
        except (TypeError, ValueError, OSError, AsyncTimeoutError, configparser.Error, RecursionError):
            self.audit.add("request refused")
            with contextlib.suppress(OSError, AsyncTimeoutError):
                await emit({"error": "refused"})
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            self.clients.remove(task)


async def serve(helper: Helper) -> bool:
    installation.guard(helper.cfg)
    _, lock = files.runtime(helper.cfg)
    with files.trusted_open(Path(helper.cfg["socket"]).parent, directory=True) as directory:
        return await _serve(helper, footprint.fd_path(directory) / "helper.sock", lock)


async def _serve(helper: Helper, path: Path, lock: int) -> bool:
    pidfile = path.parent / "helper.pid"
    stop = asyncio.Event()
    restart = False
    unavailable = False

    def halt(again: bool = False) -> None:
        nonlocal restart
        restart = again
        helper.stopping = True
        stop.set()

    async def supervise() -> None:
        nonlocal unavailable
        while True:
            await asyncio.sleep(1)
            try:
                installation.guard(helper.cfg)
            except (OSError, ValueError):
                unavailable = True
                helper.audit.add("helper disabled; installation or startup hook is unavailable")
                halt()
                for action in helper.actions.copy():
                    action.cancel()
                return
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, halt)
    loop.add_signal_handler(signal.SIGUSR1, halt, True)
    server = None
    release = None
    watchdog = None
    try:
        mask = os.umask(0o117)
        try:
            server = await asyncio.start_unix_server(helper.client, path=str(path), limit=MAX_REQUEST + 1)
        finally:
            os.umask(mask)
        os.chown(str(path), helper.cfg["socket_uid"], helper.cfg["socket_gid"])
        identity = {"pid": os.getpid(), "start": files.process_start(os.getpid()), "path": str(helper.path),
                    "action_timeout": helper.cfg["action_timeout"]}
        files.write_private(pidfile, canonical(identity))
        ready = getattr(builtins, "_shazzzbot_ready", None)
        if ready is not None:
            ready(helper.sha256)
        release = asyncio.create_task(helper.check_release())
        watchdog = asyncio.create_task(supervise())
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), helper.cfg["flush_minutes"] * 60)
            except AsyncTimeoutError:
                if helper.audit.latest:
                    helper.audit.notice(*helper.audit.latest, helper.sha256, helper.path)
                helper.audit.safe_flush()
    finally:
        helper.stopping = True
        if release is not None:
            release.cancel()
            await asyncio.gather(release, return_exceptions=True)
        if server:
            server.close()
        if helper.actions and not unavailable:
            _, pending = await asyncio.wait(helper.actions.copy(), timeout=helper.cfg["action_timeout"])
            if pending:
                helper.audit.add("action drain timed out")
        clients = helper.clients | helper.actions
        for task in clients:
            task.cancel()
        await asyncio.gather(*clients, return_exceptions=True)
        if watchdog is not None:
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
        if server:
            await server.wait_closed()
        try:
            try:
                installation.guard(helper.cfg)
            except (OSError, ValueError):
                unavailable = True
            event = path.parent / "update.json"
            restart = restart and not unavailable and event.exists()
            if restart:
                helper.audit.add(files.private_read(event).decode())
                event.unlink()
            if not unavailable:
                helper.audit.safe_flush()
        finally:
            if not unavailable:
                for entry in (pidfile, path):
                    with contextlib.suppress(FileNotFoundError):
                        entry.unlink()
            os.close(lock)
            loop.remove_signal_handler(signal.SIGTERM)
            loop.remove_signal_handler(signal.SIGUSR1)
    return restart
