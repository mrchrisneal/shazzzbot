import asyncio
import builtins
import hashlib
import json
import multiprocessing
import os
import shutil
import signal
import socket
import stat
from asyncio.exceptions import TimeoutError as AsyncTimeoutError
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from shazzzbot.helper import constants, footprint, installation
from shazzzbot.helper import files as storage
from shazzzbot.helper import server as service
from tests.test_helper_support import signal_callbacks, signed


def test_audit_ring_flush_rotation_and_no_per_request_flash(helper, tmp_path, capsys):
    audit = helper.audit
    audit.cfg["log_bytes"] = 4096
    for _ in range(20):
        audit.add("x" * 1024)
    assert (audit.size <= 4096 and not Path(audit.cfg["log_file"]).exists()) and ("x" * 100 in capsys.readouterr().out)
    audit.flush()
    log = Path(audit.cfg["log_file"])
    assert log.stat().st_size <= 4096 and not audit.lines
    audit.flush()
    audit.add("b" * 1024)
    audit.add("c" * 1024)
    audit.flush()
    assert Path(str(log) + ".1").is_file() and log.stat().st_size <= 4096
    log.chmod(0o644)
    audit.add("refused")
    with pytest.raises(ValueError, match="unsafe log file"):
        audit.flush()
    log.chmod(0o600)
    footprint.MNT.parent.chmod(0o777)
    with pytest.raises(ValueError, match="untrusted|unsafe"):
        audit.flush()


def test_unix_server_real_signed_hello_and_sigterm_cleanup(helper, monkeypatch, tmp_path):
    helper.cfg["peer_uids"] = [os.geteuid()]
    async def go():
        loop = asyncio.get_running_loop()
        callbacks, ready = signal_callbacks(loop, monkeypatch), asyncio.Event()
        real_write = storage.write_private
        def write(path, data, mode=0o600):
            real_write(path, data, mode)
            if path.name == "helper.pid":
                ready.set()
        monkeypatch.setattr(storage, "write_private", write)
        acknowledge = Mock()
        monkeypatch.setattr(builtins, "_shazzzbot_ready", acknowledge, raising=False)
        task = asyncio.create_task(service.serve(helper))
        await ready.wait()
        acknowledge.assert_called_once_with(helper.sha256)
        assert not Path.cwd().is_relative_to(helper.path.parent)
        for descriptor in Path("/proc/self/fd").iterdir():
            try:
                target = os.readlink(descriptor)
            except FileNotFoundError:
                continue
            assert not target.startswith(str(helper.path.parent) + "/"), target
        path = Path(helper.cfg["socket"])
        assert (stat.S_IMODE(path.stat().st_mode) == 0o660) and (path.stat().st_gid == helper.cfg["socket_gid"])
        identity = json.loads((path.parent / "helper.pid").read_bytes())
        assert identity["pid"] == os.getpid() and identity["path"] == str(helper.path)
        reader, client = await asyncio.open_unix_connection(str(path))
        client.write(signed(helper))
        await client.drain()
        replies = [json.loads(line) for line in (await reader.read()).splitlines()]
        assert replies == [{"version": constants.VERSION, "sha256": helper.sha256,
            "protocol": constants.PROTOCOL, "bundle_version": constants.VERSION,
            "entry_sha256": hashlib.sha256((helper.path.parent / footprint.BUNDLE_FILES[0]).read_bytes()).hexdigest()},
            {"done": 0, "secs": 0}]
        client.close()
        await client.wait_closed()
        held = asyncio.create_task(asyncio.sleep(1000))
        helper.clients.add(held)
        callback, args = callbacks[signal.SIGTERM]
        callback(*args)
        assert (not await task) and (held.cancelled())
        assert not path.exists() and not (path.parent / "helper.pid").exists()
        assert "startup version=" in Path(helper.cfg["log_file"]).read_text()
    asyncio.run(go())


@pytest.mark.parametrize("outdated", [False, True])
def test_periodic_log_flush_update_notice_and_manual_reexec_signal(helper, monkeypatch, tmp_path, outdated):
    if outdated:
        helper.audit.latest = ("99.0.0", "b" * 64)
    original_flush = helper.audit.flush
    def fail_once():
        if flush.call_count == 1:
            raise ValueError("log permissions changed")
        original_flush()
    flush = Mock(side_effect=fail_once)
    helper.audit.flush = flush
    async def go():
        callbacks, real_wait = signal_callbacks(asyncio.get_running_loop(), monkeypatch), asyncio.wait_for
        calls = 0
        async def wait(awaitable, timeout):
            nonlocal calls
            calls += 1
            if calls == 1:
                awaitable.close()
                raise AsyncTimeoutError
            if outdated:
                storage.write_private(Path(helper.cfg["socket"]).parent / "update.json", b"manual update old -> new")
            callback, args = callbacks[signal.SIGUSR1 if outdated else signal.SIGTERM]
            callback(*args)
            return await real_wait(awaitable, timeout)
        monkeypatch.setattr(asyncio, "wait_for", wait)
        assert (await service.serve(helper) is outdated) and (calls == 2)
    asyncio.run(go())
    assert flush.call_count == 2
    log = Path(helper.cfg["log_file"]).read_text()
    assert ("log flush failed; retaining RAM log" in log) and (("update available" in log) is outdated)
    assert ("manual update old -> new" in log) is outdated


def test_server_start_failure_releases_runtime_lock(helper, monkeypatch, tmp_path):
    monkeypatch.setattr(asyncio, "start_unix_server", AsyncMock(side_effect=OSError("bind failed")))
    async def go():
        signal_callbacks(asyncio.get_running_loop(), monkeypatch)
        with pytest.raises(OSError, match="bind failed"):
            await service.serve(helper)
    asyncio.run(go())
    _, fd = storage.runtime(helper.cfg)
    os.close(fd)


def test_audit_redacts_key_from_stdout_and_stderr(helper, capsys):
    helper.audit.add("sensitive " + helper.key.decode())
    helper.audit.notice(helper.key.decode(), "b" * 64, helper.sha256, helper.path)
    output = capsys.readouterr()
    assert helper.key.decode() not in output.out + output.err
    assert "[redacted]" in output.out and "[redacted]" in output.err
    assert "invalid update version [redacted]" in output.err
    assert helper.audit.latest is None and "--update" not in output.err
    assert helper.key not in b"".join(helper.audit.lines)


@pytest.mark.parametrize("restart,expired", [(False, False), (True, False), (True, True)])
def test_shutdown_waits_for_busy_actions_and_survives_log_permission_drift(helper, monkeypatch, tmp_path,
                                                                          restart, expired):
    helper.cfg["enabled"] = ["parity"]
    helper.resolve = AsyncMock(return_value=["/usr/bin/true"])
    helper.audit.flush = Mock(side_effect=ValueError("log permissions changed"))
    async def go():
        callbacks = signal_callbacks(asyncio.get_running_loop(), monkeypatch)
        begun, release, listening = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def run(argv, emit):
            begun.set()
            await release.wait()
            return 0
        helper.run = run
        monkeypatch.setattr(builtins, "_shazzzbot_ready", lambda digest: listening.set(), raising=False)
        action = asyncio.create_task(helper.dispatch("parity.check", {}, AsyncMock()))
        await begun.wait()
        server = asyncio.create_task(service.serve(helper))
        await listening.wait()
        real_wait = asyncio.wait
        async def drain(tasks, timeout):
            assert tasks == {action} and timeout == helper.cfg["action_timeout"] and not action.cancelled()
            if expired:
                return set(), tasks
            release.set()
            return await real_wait(tasks, timeout=timeout)
        monkeypatch.setattr(asyncio, "wait", drain)
        if restart:
            storage.write_private(Path(helper.cfg["socket"]).parent / "update.json", b"manual update 1 -> 2")
        callback, args = callbacks[signal.SIGUSR1 if restart else signal.SIGTERM]
        callback(*args)
        with pytest.raises(ValueError, match="stopping"):
            await helper.dispatch("parity.check", {}, AsyncMock())
        assert (await server is restart) and (action.cancelled() is expired)
        assert (not helper.busy and not helper.actions) and (b"log flush failed" in b"".join(helper.audit.lines))
    asyncio.run(go())
    path = Path(helper.cfg["socket"])
    assert not path.exists() and not (path.parent / "helper.pid").exists()
    _, fd = storage.runtime(helper.cfg)
    os.close(fd)


@pytest.mark.parametrize("loss", ["hook", "core", "home", "entry", "manifest"])
def test_watchdog_stops_and_cancels_owned_actions_without_recreating_the_home(helper, monkeypatch, loss):
    helper.cfg["enabled"] = ["parity"]
    helper.resolve = AsyncMock(return_value=["/usr/bin/true"])
    home = helper.path.parent
    async def go():
        begun, listening = asyncio.Event(), asyncio.Event()
        ticks = asyncio.Queue()
        original_sleep = asyncio.sleep
        async def tick(_):
            await ticks.get()
        async def run(argv, emit):
            begun.set()
            await asyncio.Event().wait()
            return 0
        helper.run = run
        monkeypatch.setattr(asyncio, "sleep", tick)
        monkeypatch.setattr(builtins, "_shazzzbot_ready", lambda digest: listening.set(), raising=False)
        signal_callbacks(asyncio.get_running_loop(), monkeypatch)
        action = asyncio.create_task(helper.dispatch("parity.check", {}, AsyncMock()))
        await begun.wait()
        server = asyncio.create_task(service.serve(helper))
        await listening.wait()
        ticks.put_nowait(None)
        await original_sleep(0)
        assert not server.done() and not action.done()
        if loss == "hook":
            schedule = footprint.HOOK.parent.parent.parent / "schedule.json"
            schedule.write_text("{}")
        elif loss == "core":
            (home / "policy.json").unlink()
        elif loss in {"entry", "manifest"}:
            name = "helper-unraid.json" if loss == "manifest" else "shazzzbot-helper-unraid.py"
            (home / name).unlink()
        else:
            shutil.rmtree(home)
        ticks.put_nowait(None)
        assert not await asyncio.wait_for(server, 1)
        assert action.cancelled() and helper.stopping and not helper.actions and not helper.busy
    asyncio.run(go())
    assert not (home / "helper.log").exists()
    if loss == "home":
        assert not home.exists()
    assert b"helper disabled" in b"".join(helper.audit.lines)


@pytest.mark.parametrize("name", footprint.CORE_FILES)
def test_missing_core_refuses_requests_and_process_launch_before_any_action(helper, monkeypatch, name):
    helper.cfg["enabled"] = ["tasks", "parity"]
    (helper.path.parent / name).unlink()
    execute = AsyncMock()
    monkeypatch.setattr(service.process, "execute", execute)
    async def go():
        for action, args in (("hello", {}), ("tasks", {}), ("parity.check", {})):
            with pytest.raises(OSError):
                await helper.dispatch(action, args, AsyncMock())
        with pytest.raises(OSError):
            await helper.run(["/usr/bin/true"], AsyncMock())
        with pytest.raises(OSError):
            await helper.read("tasks", AsyncMock())
    asyncio.run(go())
    execute.assert_not_awaited()
    assert not helper.busy and not helper.actions


def test_audit_does_not_recreate_a_removed_helper_home(helper):
    home = helper.path.parent
    shutil.rmtree(home)
    helper.audit.safe_flush()
    assert not home.exists() and b"log flush failed" in b"".join(helper.audit.lines)


def test_loaded_archive_hash_is_reported_even_when_the_canonical_path_changes(helper):
    helper.cfg["_loaded_sha256"] = "c" * 64
    new = service.Helper(helper.cfg, helper.key, helper.path, helper.platform)
    assert new.sha256 == "c" * 64 and new.path == helper.path
    installation.guard(new.cfg)


def test_idle_helper_process_holds_no_pool_descriptors_or_working_directory(helper):
    helper.cfg["peer_uids"] = [os.geteuid()]
    context = multiprocessing.get_context("fork")
    reader, writer = context.Pipe(duplex=False)
    def daemon():
        os.chdir(footprint.ensure_workdir())
        builtins._shazzzbot_ready = lambda digest: writer.send(os.getpid())
        asyncio.run(service.serve(helper))
    child = context.Process(target=daemon)
    child.start()
    try:
        assert reader.poll(10)
        pid = reader.recv()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(helper.cfg["socket"])
            connection.sendall(signed(helper))
            output = bytearray()
            while data := connection.recv(4096):
                output.extend(data)
        assert json.loads(output.splitlines()[0])["version"] == constants.VERSION
        pool = str(footprint.MNT) + "/"
        assert not os.readlink(f"/proc/{pid}/cwd").startswith(pool)
        for descriptor in Path(f"/proc/{pid}/fd").iterdir():
            try:
                target = os.readlink(descriptor)
            except FileNotFoundError:
                continue
            assert not target.startswith(pool), target
    finally:
        child.terminate()
        child.join(10)
        if child.is_alive():
            child.kill()
            child.join(5)
    assert child.exitcode == 0
