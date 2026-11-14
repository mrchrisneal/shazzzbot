import asyncio
import hashlib
import io
import runpy
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from shazzzbot.helper import config, constants, footprint, security, updates
from shazzzbot.helper import files as storage
from shazzzbot.helper.platforms import unraid
from tests.test_helper_support import update_fixture

RELEASE = updates.release


def test_http_total_deadline_interrupts_even_a_slow_drip():
    with pytest.raises(TimeoutError, match="total deadline"), updates.deadline(0.02):
        while True:
            time.sleep(0.002)


def test_installer_and_helper_use_the_same_drain_timeout_ceiling(setup):
    from tools import setup_helper

    path = setup[3]
    storage.write_private(path,
                          ('{"format":1,"action_timeout":' + str(constants.MAX_ACTION_TIMEOUT + 1) + '}').encode())
    assert setup_helper.action_limit() == constants.MAX_ACTION_TIMEOUT
    with pytest.raises(ValueError, match="action_timeout must be at most"):
        config.load_config(path, unraid.Unraid())


@pytest.mark.parametrize("kind", ["ok", "failed", "large", "timeout", "cancel", "error"])
def test_async_release_worker_is_bounded_and_cancelled_on_shutdown(helper, monkeypatch, kind):
    helper.cfg["release_url"] = "https://example.invalid/releases"
    child = SimpleNamespace(returncode=None, wait=AsyncMock())

    async def communicate(data):
        assert data == b"https://example.invalid/releases"
        if kind in ("timeout", "cancel"):
            await asyncio.Future()
        if kind == "error":
            raise OSError("unavailable")
        child.returncode = 1 if kind == "failed" else 0
        return (b"x" * 129 if kind == "large" else b"0.2.0\n"), b""

    def kill():
        child.returncode = -signal.SIGKILL

    child.communicate, child.kill = communicate, Mock(side_effect=kill)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=child))
    wait_for = asyncio.wait_for
    monkeypatch.setattr(asyncio, "wait_for", lambda value, timeout: wait_for(value, min(timeout, 0.02)))

    async def run():
        request = asyncio.create_task(RELEASE(helper.cfg))
        if kind == "cancel":
            await asyncio.sleep(0)
            request.cancel()
        if kind == "ok":
            assert await request == "0.2.0"
        else:
            with pytest.raises((ValueError, TimeoutError, asyncio.CancelledError, OSError)):
                await request
    asyncio.run(run())
    assert child.kill.call_count == (1 if kind in ("timeout", "cancel", "error") else 0)
    args = asyncio.create_subprocess_exec.call_args
    assert args.args[1:3] == ("-I", "-B") and args.kwargs["close_fds"]


@pytest.mark.parametrize("loaded_previous", [False, True])
def test_release_worker_executes_only_a_pinned_archive_descriptor(helper, monkeypatch, loaded_previous):
    import json
    import os

    original = helper.path.read_bytes()
    previous = Path(str(helper.path) + ".prev")
    storage.write_private(previous, original, 0o700)
    policy_path = Path(helper.cfg["installation_policy"])
    policy = json.loads(policy_path.read_bytes())
    fd = os.open(previous, os.O_RDONLY)
    storage.write_private(helper.path, b"unverified canonical artifact", 0o700)
    if loaded_previous:
        policy["previous_sha256"] = policy["sha256"]
        policy["sha256"] = "d" * 64
        storage.write_private(policy_path, json.dumps(policy).encode())
        helper.cfg["_loaded_archive_fd"] = fd
    child = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b"0.2.0\n", b"")))
    async def spawn(*_args, **kwargs):
        assert os.pread(kwargs["pass_fds"][0], len(original), 0) == original
        assert kwargs["env"]["HOME"] == str(footprint.ensure_workdir())
        assert len(kwargs["pass_fds"]) == 1
        assert not os.readlink("/proc/self/fd/" + str(kwargs["pass_fds"][0])).startswith(str(helper.path.parent))
        return child
    execute = AsyncMock(side_effect=spawn)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", execute)
    try:
        if loaded_previous:
            assert asyncio.run(RELEASE(helper.cfg)) == "0.2.0"
            execute.assert_awaited_once()
        else:
            with pytest.raises(ValueError, match="pinned hash"):
                asyncio.run(RELEASE(helper.cfg))
            execute.assert_not_awaited()
    finally:
        os.close(fd)


def test_release_worker_entry_point_uses_only_bounded_stdin(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("https://example.invalid/releases"))
    monkeypatch.setattr(updates, "latest_release", Mock(return_value="0.2.0"))
    updates.main()
    assert capsys.readouterr().out == "0.2.0\n"
    updates.latest_release.assert_called_once_with("https://example.invalid/releases")
    reply = SimpleNamespace(geturl=lambda: "https://example.invalid/releases",
                            read=lambda n: b'{"tag_name":"v0.2.0"}')
    context = Mock(__enter__=Mock(return_value=reply), __exit__=Mock(return_value=False))
    opener = SimpleNamespace(open=lambda *a, **k: context)
    monkeypatch.setattr(updates.urllib.request, "build_opener", Mock(return_value=opener))
    monkeypatch.setattr(sys, "stdin", io.StringIO("https://example.invalid/releases"))
    runpy.run_module("shazzzbot.helper.updates", run_name="__main__")
    assert capsys.readouterr().out == "0.2.0\n"


@pytest.mark.parametrize("text,writable", [
    ("", False), ('# exports\n/boot *(ro,sync)', False), ('/boot *(rw,sync)', True),
    ('/ *(rw,sync)', True), ('/boot/config host(rw)', True), ('/mnt/user *(rw)', False),
    ('/boot *(ro) other(rw)', True),
])
def test_flash_nfs_exports_are_checked(text, writable):
    assert unraid.nfs_writable(text) is writable


@pytest.mark.parametrize("text", ["relative *(rw)", "/boot *", "/boot *(rw"])
def test_flash_nfs_unknown_options_fail_closed(text):
    with pytest.raises(ValueError, match="NFS"):
        unraid.nfs_writable(text)


@pytest.mark.parametrize("case", ["missing", "ro", "rw", "large", "untrusted"])
def test_flash_nfs_reads_are_bounded_root_trusted(tmp_path, monkeypatch, case):
    exports = tmp_path / "exports"
    if case != "missing":
        exports.write_bytes(b"x" * 1048577 if case == "large" else
                            b"/boot *(rw)" if case == "rw" else b"/boot *(ro)")
    if case == "untrusted":
        exports.chmod(0o666)
    monkeypatch.setattr(unraid, "NFS_EXPORTS", exports)
    monkeypatch.setattr(unraid, "NFS_FOLDER", tmp_path / "exports.d")
    if case in ("large", "untrusted"):
        with pytest.raises(ValueError):
            unraid.nfs_flash_writable()
    else:
        assert unraid.nfs_flash_writable() is (case == "rw")


@pytest.mark.parametrize("case", ["readonly", "writable", "untrusted", "many", "size"])
def test_flash_guard_checks_all_nfs_export_fragments(tmp_path, monkeypatch, case):
    root = tmp_path / "exports.d"
    root.mkdir()
    (root / "README").write_text("not an export")
    first = root / "a.exports"
    first.write_text("/boot *(rw)" if case != "readonly" else "/boot *(ro)")
    second = root / "b.exports"
    second.write_text("/mnt/user *(ro)")
    monkeypatch.setattr(unraid, "NFS_FOLDER", root)
    monkeypatch.setattr(unraid, "NFS_EXPORTS", tmp_path / "exports")
    if case == "untrusted":
        second.chmod(0o666)
    elif case == "many":
        monkeypatch.setattr(unraid.os, "listdir", lambda fd: ["x"] * 1025)
    elif case == "size":
        first.write_bytes(b"#" + b"x" * 600000)
        second.write_bytes(b"#" + b"x" * 600000)
    if case in ("untrusted", "many", "size"):
        with pytest.raises(ValueError):
            unraid.nfs_flash_writable()
    else:
        assert unraid.nfs_flash_writable() is (case == "writable")


def test_flash_opt_in_does_not_skip_nfs_validation_when_smb_is_writable(monkeypatch):
    monkeypatch.setattr(unraid.process, "execute", AsyncMock(return_value=(0, ["[global]", "[flash]",
                                                                              "read only = no"], False)))
    monkeypatch.setattr(unraid, "nfs_flash_writable", Mock(side_effect=ValueError("untrusted NFS exports")))
    with pytest.raises(ValueError, match="NFS"):
        asyncio.run(unraid.ensure_flash_safe(True))


@pytest.mark.parametrize("binary", sorted(security.SCRIPT_BANNED))
def test_direct_network_and_privileged_binaries_cannot_be_config_scripts(binary):
    path = "/usr/bin/" + binary
    with pytest.raises(ValueError, match="forbidden executable"):
        security.checked_argv([path], [path])
    assert not security.banned("docker")


def test_update_persistence_failure_stops_new_daemon_before_restoring_old(helper, monkeypatch, capsys):
    _data, digest, _ = update_fixture(helper, monkeypatch)
    original = helper.path.read_bytes()
    calls = []
    monkeypatch.setattr(updates.installation, "commit", Mock(side_effect=OSError("flash full")))

    def restore(cfg, path, pinned, data, pidfd, result, timeout):
        assert data == original and path.read_bytes() != original
        calls.append((pidfd, result, timeout))
        storage.write_private(pinned, data, 0o700)
    monkeypatch.setattr(updates, "restore_running", restore)
    with pytest.raises(OSError, match="flash full"):
        updates.update(helper.cfg, helper.path, digest)
    assert helper.path.read_bytes() == original and len(calls) == 1
    assert "update persistence failed; previous helper restarted" in capsys.readouterr().err


@pytest.mark.parametrize("case", ["ok", "drain", "start"])
def test_old_daemon_recovery_uses_verified_clean_exec_and_bounded_drain(helper, monkeypatch, case):
    original = helper.path.read_bytes()
    result = Path(helper.cfg["socket"]).parent / "update.result"
    storage.write_private(result, b"stale")
    events = []
    monkeypatch.setattr(signal, "pidfd_send_signal", lambda fd, sig: events.append((fd, sig)))
    poll = Mock(return_value=[] if case == "drain" else [(9, 1)])
    monkeypatch.setattr(updates.select, "poll", lambda: SimpleNamespace(register=Mock(), poll=poll))
    child = SimpleNamespace(terminate=Mock())
    monkeypatch.setattr(updates.subprocess, "Popen", Mock(return_value=child))
    failure = ValueError("start failed") if case == "start" else None
    monkeypatch.setattr(updates, "wait_update", Mock(side_effect=failure))
    if case == "ok":
        updates.restore_running(helper.cfg, helper.path, helper.path, original, 9, result, 300)
    else:
        with pytest.raises(ValueError, match="manual recovery" if case == "drain" else "start failed"):
            updates.restore_running(helper.cfg, helper.path, helper.path, original, 9, result, 300)
    assert events == [(9, signal.SIGTERM)] and poll.call_args.args == (330000,)
    if case != "drain":
        args = updates.subprocess.Popen.call_args
        assert args.args[0][1:3] == ["-I", "-B"]
        assert args.kwargs["env"]["HOME"] == str(footprint.ensure_workdir())
        assert args.kwargs["env"]["TMPDIR"].endswith("shazzzbot-helper")
        assert args.kwargs["close_fds"] and args.kwargs["start_new_session"]
        updates.wait_update.assert_called_once_with(helper.cfg, result, hashlib.sha256(original).hexdigest())
        assert child.terminate.call_count == (case == "start")
