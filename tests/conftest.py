import asyncio
import hashlib
import json
import os
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from shazzzbot.helper import config as configuration
from shazzzbot.helper import constants, footprint, platforms, updates
from shazzzbot.helper import files as storage
from shazzzbot.helper import server as service
from shazzzbot.helper.platforms import unraid
from tests.test_helper_support import archive_bytes, directory_mount_id


@pytest.fixture
def serve():
    """Run `call(http, base_url)` against a local app whose handlers return {"json": ...} or {"status": n}."""
    def run(routes, call):
        def wrap(h):
            async def handler(req):
                r = await h(req)
                return web.json_response(r["json"]) if "json" in r else web.Response(status=r["status"])
            return handler

        async def go():
            app = web.Application()
            for path, h in routes.items():
                app.router.add_get(path, wrap(h))
            async with TestServer(app) as srv, aiohttp.ClientSession() as http:
                return await call(http, str(srv.make_url("")).rstrip("/"))
        return asyncio.run(go())
    return run


@pytest.fixture(autouse=True)
def heartbeat(tmp_path, monkeypatch):
    monkeypatch.setenv("HEARTBEAT", str(tmp_path / "heartbeat"))


@pytest.fixture
def setup(tmp_path, tmp_path_factory, monkeypatch):
    monkeypatch.setattr(unraid, "ensure_flash_safe", AsyncMock())
    monkeypatch.setattr(updates, "latest_release", lambda _: constants.VERSION)
    monkeypatch.setattr(updates, "release", AsyncMock(return_value=constants.VERSION))
    base = tmp_path_factory.mktemp("h")
    base.chmod(0o700)
    mnt = base / "mnt"
    pool = mnt / "cache"
    (pool / "appdata").mkdir(parents=True)
    mountinfo = base / "mountinfo"
    device = pool.stat().st_dev
    mount_id = directory_mount_id(pool)
    mountinfo.write_text(f"{mount_id} 0 {os.major(device)}:{os.minor(device)} / {pool} rw - xfs /dev/test rw\n")
    hook = base / "boot/user.scripts/scripts/shazzzbot-helper/script"
    hook.parent.mkdir(parents=True)
    hook.write_text("#!/bin/sh\nexit 0\n")
    schedule = hook.parent.parent.parent / "schedule.json"
    schedule.write_text(json.dumps({"shazzzbot-helper": {"frequency": "boot", "script": str(hook)}}))
    monkeypatch.setattr(footprint, "MNT", mnt)
    monkeypatch.setattr(footprint, "MOUNTINFO", mountinfo)
    monkeypatch.setattr(footprint, "HOOK", hook)
    monkeypatch.setattr(footprint, "WORK", base / "tmp/shazzzbot-helper")
    (base / "tmp").mkdir()
    root = pool / "appdata/shazzzbot-helper"
    footprint.initialize(root, "b" * 32)
    marker = tmp_path / "unraid-version"
    marker.touch()
    monkeypatch.setattr(platforms, "UNRAID_VERSION", marker)
    key, config, program = (root / name for name in ("api.key", "config.json", "helper.pyz"))
    storage.write_private(key, b"a" * 32)
    storage.write_private(config, b'{"format":1}')
    storage.write_private(program, archive_bytes(version="0.1.0"), 0o700)
    entry = root / "shazzzbot-helper-unraid.py"
    source = b'#!/usr/bin/env python3\nraise SystemExit("fixture entry is not executed")\n'
    storage.write_private(entry, source, 0o700)
    manifest = {"format": 1, "platform": "unraid", "version": "v" + constants.VERSION, "revision": "a" * 40,
                "files": {entry.name: {"sha256": hashlib.sha256(source).hexdigest(), "size": len(source)},
                          program.name: {"sha256": hashlib.sha256(program.read_bytes()).hexdigest(),
                                         "size": program.stat().st_size}}}
    storage.write_private(root / "helper-unraid.json", json.dumps(manifest).encode())
    policy = {"format": 1, "artifact": str(program), "config": str(config),
              "sha256": hashlib.sha256(program.read_bytes()).hexdigest()}
    storage.write_private(root / "policy.json", json.dumps(policy).encode())
    footprint.record_identity(root, "b" * 32)
    cfg, secret = configuration.load_config(config, unraid.Unraid())
    unraid.ensure_flash_safe.assert_awaited_once()
    return cfg, secret, program, config


@pytest.fixture
def helper(setup, monkeypatch):
    cfg, secret, program, _ = setup
    return service.Helper(cfg, secret, program, unraid.Unraid())
