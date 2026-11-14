import asyncio
import json
import multiprocessing
import os
import time
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from shazzzbot.helper import config, readings
from shazzzbot.helper.platforms import tasks, unraid
from tests.test_helper_support import signed, source_paths, tree, writer


def test_tasks_is_an_explicit_enabled_capability_and_host_paths_cover_all_sources(setup):
    _, _, _, path = setup
    path.write_text(json.dumps({"format": 1, "enabled": ["tasks"]}))
    assert config.load_config(path, unraid.Unraid())[0]["enabled"] == ["tasks"]
    assert set(unraid.TASK_PATHS) == set(tasks.SOURCES)
    assert unraid.TASK_PATHS["cron.d"] == Path("/etc/cron.d")
    assert unraid.TASK_PATHS["crontabs"] == Path("/var/spool/cron/crontabs")
    assert unraid.TASK_PATHS["user.scripts.state"] == Path("/tmp/user.scripts")
    assert unraid.TASK_PATHS["parity"] == Path("/boot/config")
    with pytest.raises(ValueError, match="unknown read-only"):
        asyncio.run(unraid.Unraid().read("other", 1, 4096))


def test_authenticated_tasks_reads_all_sources_without_exporting_commands_or_log_contents(helper, monkeypatch,
                                                                                        tmp_path):
    tree(tmp_path, {"isNewer": True, "version": "7.3.4"})
    (tmp_path / "localtime").write_bytes(files("tzdata.zoneinfo").joinpath("UTC").read_bytes())
    (tmp_path / "crontabs").mkdir()
    (tmp_path / "crontabs" / "root").write_text("15 3 * * * /usr/bin/trim --private-secret\n")
    for period in ("hourly", "daily", "weekly", "monthly"):
        folder = tmp_path / ("cron." + period)
        folder.mkdir()
        (folder / "maintenance").touch(mode=0o755)
    with (tmp_path / "cron.d" / "root").open("a") as stream:
        stream.write("0 4 * * * run-parts /etc/cron.daily\n0 5 1 * * run-parts /etc/cron.monthly\n")
    logs = tmp_path / "user.scripts.state" / "tmpScripts" / "old"
    logs.mkdir(parents=True)
    (logs / "log.txt").write_text("Script Starting Nov 09, 2026  12:00.00\nprivate-secret\n"
                                  "Script Finished Nov 09, 2026  12:00.45\n")
    parity = tmp_path / "parity"
    parity.mkdir()
    (parity / "parity-checks.log").write_text("Nov 09 12:00:00|3900|100 MB/s|0|0|check P\n")
    backup = tmp_path / "appdata.backup"
    backup.mkdir()
    (backup / "running").touch()
    monkeypatch.setattr(unraid, "TASK_PATHS", source_paths(tmp_path))
    monkeypatch.setattr(time, "time", lambda: 1234)
    helper.cfg.update(enabled=["tasks"], peer_uids=[10001])
    expected = tasks.Collector(source_paths(tmp_path)).collect()
    output = writer()
    asyncio.run(helper.client(SimpleNamespace(readline=AsyncMock(return_value=signed(helper, action="tasks"))), output))
    messages = [json.loads(call.args[0]) for call in output.write.call_args_list]
    assert messages[0] == {"tasks": expected} and messages[1]["done"] == 0
    assert expected["durations"]["Parity check"]["seconds"] == 3900
    assert expected["running"]["Appdata.Backup"] > 0
    assert len([job for job in expected["jobs"] if job["name"] == "maintenance"]) == 4
    assert "private-secret" not in json.dumps(messages)
    audit = b"".join(helper.audit.lines).decode()
    assert "action tasks target=host" in audit and "done tasks target=host rc=0" in audit
    assert not helper.actions and not helper.busy


@pytest.mark.parametrize("case", ["disabled", "args", "dotted", "peer", "signature", "stale", "replay"])
def test_tasks_never_bypasses_configuration_peer_or_request_authentication(helper, case):
    helper.cfg.update(enabled=[] if case == "disabled" else ["tasks"], peer_uids=[10001])
    helper.platform.read = AsyncMock(return_value={})
    request = signed(helper, action="tasks.run" if case == "dotted" else "tasks",
                     args={"name": "x"} if case == "args" else {},
                     ts=time.time() - 100 if case == "stale" else time.time())
    if case == "signature":
        value = json.loads(request)
        value["sig"] = "0" * 64
        request = json.dumps(value).encode() + b"\n"
    if case == "replay":
        helper.verify(request)
    output = writer(0 if case == "peer" else 10001)
    asyncio.run(helper.client(SimpleNamespace(readline=AsyncMock(return_value=request)), output))
    assert [json.loads(call.args[0]) for call in output.write.call_args_list] == [{"error": "refused"}]
    helper.platform.read.assert_not_awaited()
    assert b"request refused" in b"".join(helper.audit.lines)


@pytest.mark.parametrize("case", ["oversize", "malformed", "timeout"])
def test_failed_collection_never_sends_a_partial_snapshot_and_reaps_the_worker(helper, monkeypatch, tmp_path, case):
    helper.cfg["enabled"] = ["tasks"]
    monkeypatch.setattr(unraid, "TASK_PATHS", source_paths(tmp_path))
    if case == "oversize":
        helper.cfg["output_bytes"] = 10
    elif case == "malformed":
        (tmp_path / "user.scripts").mkdir()
        (tmp_path / "user.scripts" / "schedule.json").write_text("private-secret invalid JSON")
    else:
        helper.cfg["action_timeout"] = 0.03
        monkeypatch.setattr(tasks.Collector, "collect", lambda self: time.sleep(60))
    before = {child.pid for child in multiprocessing.active_children()}
    emit = AsyncMock()
    asyncio.run(helper.dispatch("tasks", {}, emit))
    messages = [call.args[0] for call in emit.await_args_list]
    assert messages[0] == {"error": "tasks collection failed"} and messages[1]["done"] == 1
    assert "tasks" not in messages[0] and "private-secret" not in json.dumps(messages)
    assert {child.pid for child in multiprocessing.active_children()} == before
    assert not helper.actions and not helper.busy


def test_tasks_shares_the_busy_and_shutdown_bookkeeping(helper):
    helper.cfg["enabled"] = ["tasks"]
    async def go():
        started, release = asyncio.Event(), asyncio.Event()
        async def collect(*_):
            started.set()
            await release.wait()
            return {"jobs": []}
        helper.platform.read = collect
        first = asyncio.create_task(helper.dispatch("tasks", {}, AsyncMock()))
        await started.wait()
        assert first in helper.actions and helper.busy == {"tasks:host"}
        emit = AsyncMock()
        await helper.dispatch("tasks", {}, emit)
        emit.assert_awaited_once_with({"error": "busy"})
        release.set()
        await first
        assert not helper.busy and not helper.actions
    asyncio.run(go())


def test_cancelling_collection_stops_the_worker(monkeypatch, tmp_path):
    monkeypatch.setattr(tasks.Collector, "collect", lambda self: time.sleep(60))
    before = {child.pid for child in multiprocessing.active_children()}
    async def go():
        entered = asyncio.Event()
        original = readings._read
        async def wait(reader, cap):
            entered.set()
            return await original(reader, cap)
        monkeypatch.setattr(readings, "_read", wait)
        task = asyncio.create_task(readings.gather(source_paths(tmp_path), 5, 65536))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(go())
    assert {child.pid for child in multiprocessing.active_children()} == before


@pytest.mark.parametrize("raw", [b"x" * 1025, b"", b"not JSON", b"[]", b'{"error":"failed"}', b'{"tasks":[]}'])
def test_invalid_worker_replies_are_never_used(monkeypatch, tmp_path, raw):
    monkeypatch.setattr(readings, "_read", AsyncMock(return_value=raw))
    with pytest.raises(ValueError):
        asyncio.run(readings.gather(source_paths(tmp_path), 1, 1024))


@pytest.mark.parametrize("case", ["valid", "oversize", "invalid", "error"])
def test_worker_serializes_one_bounded_snapshot_or_one_safe_error(monkeypatch, tmp_path, caplog, case):
    def collect(_):
        if case == "error":
            raise ValueError("private-secret")
        return {"generated": float("nan")} if case == "invalid" else {"jobs": []}
    monkeypatch.setattr(tasks.Collector, "collect", collect)
    incoming, outgoing = os.pipe()
    with os.fdopen(incoming, "rb") as stream:
        readings._worker(source_paths(tmp_path), 1 if case == "oversize" else 1024, os.dup(incoming), outgoing)
        result = json.loads(stream.read())
    assert result == ({"tasks": {"jobs": []}} if case == "valid" else {"error": "tasks collection failed"})
    assert "private-secret" not in caplog.text


def test_pipe_reader_stops_at_the_output_limit_instead_of_buffering_the_full_reply():
    async def go():
        reader = asyncio.StreamReader()
        reader.feed_data(b"abcdef")
        assert await readings._read(reader, 2) == b"abc"
    asyncio.run(go())


@pytest.mark.parametrize("state", ["start-failure", "finished", "stuck"])
def test_worker_lifecycle_failures_leave_cleanup_bounded_and_visible(monkeypatch, tmp_path, caplog, state):
    child = Mock(pid=None if state == "start-failure" else 42)
    child.is_alive.side_effect = [state == "stuck", state == "stuck"]
    if state == "start-failure":
        child.start.side_effect = OSError("cannot fork")
    monkeypatch.setattr(readings.multiprocessing, "get_context", lambda _: SimpleNamespace(Process=lambda **_: child))
    monkeypatch.setattr(readings, "_read", AsyncMock(return_value=b'{"tasks":{}}'))
    if state == "start-failure":
        with pytest.raises(OSError, match="cannot fork"):
            asyncio.run(readings.gather(source_paths(tmp_path), 1, 1024))
        child.join.assert_not_called()
    else:
        assert asyncio.run(readings.gather(source_paths(tmp_path), 1, 1024)) == {}
        child.join.assert_called_once_with(timeout=1)
        assert child.kill.call_count == (1 if state == "stuck" else 0)
        assert child.close.call_count == (0 if state == "stuck" else 1)
        assert ("cleanup budget" in caplog.text) is (state == "stuck")


@pytest.mark.parametrize("data", [{"value": "x" * 4096}, None])
def test_server_enforces_the_output_contract_even_if_a_platform_misbehaves(helper, data):
    helper.cfg.update(enabled=["tasks"], output_bytes=1024)
    helper.platform.read = AsyncMock(return_value=data)
    emit = AsyncMock()
    asyncio.run(helper.dispatch("tasks", {}, emit))
    assert emit.await_args_list[0].args[0] == {"error": "tasks collection failed"}
    assert emit.await_args_list[1].args[0]["done"] == 1


@pytest.mark.parametrize("strict", [False, True])
def test_source_io_failures_and_nonregular_inputs_are_bounded(tmp_path, strict):
    paths = source_paths(tmp_path)
    paths["cron.d"].touch()
    collector = tasks.Collector(paths, strict=strict)
    assert collector._names("cron.d") == []
    for kind in ("directory", "fifo", "symlink"):
        path = tmp_path / kind
        if kind == "directory":
            path.mkdir()
        elif kind == "fifo":
            os.mkfifo(path)
        else:
            path.symlink_to(paths["cron.d"])
        collector.paths = {"test": path}
        collector.failed.clear()
        assert collector._read("test") == ""


def test_input_size_file_count_and_timezone_errors_refuse_the_whole_collection(tmp_path, monkeypatch):
    paths = source_paths(tmp_path)
    paths["cron.d"].mkdir()
    for i in range(3):
        (paths["cron.d"] / str(i)).write_text("content")
    collector = tasks.Collector(paths, strict=True)
    monkeypatch.setattr(tasks, "FILE_LIMIT", 2)
    with pytest.raises(ValueError, match="too many"):
        collector.collect()
    monkeypatch.setattr(tasks, "FILE_LIMIT", 1024)
    monkeypatch.setattr(tasks, "LOG_LIMIT", 4)
    assert collector._read("cron.d", "0") == ""
    monkeypatch.setattr(tasks, "LOG_LIMIT", 1024)
    monkeypatch.setattr(tasks, "TOTAL_LIMIT", 4)
    with pytest.raises(ValueError, match="source size"):
        collector._read("cron.d", "0")
    monkeypatch.setattr(tasks, "TOTAL_LIMIT", 65536)
    paths["localtime"].write_bytes(b"invalid zone")
    with pytest.raises(ValueError):
        tasks.Collector(paths, strict=True).completed()


@pytest.mark.parametrize("error", [FileNotFoundError(), PermissionError()])
@pytest.mark.parametrize("strict", [False, True])
def test_state_files_disappearing_or_becoming_unreadable_are_handled_explicitly(tmp_path, monkeypatch, error, strict):
    folder = tmp_path / "state"
    folder.mkdir()
    (folder / "job").touch()
    from contextlib import contextmanager

    original = tasks.Collector._open
    @contextmanager
    def fail(self, parts, **kwargs):
        if parts == ("state", "job"):
            raise error
        with original(self, parts, **kwargs) as fd:
            yield fd
    monkeypatch.setattr(tasks.Collector, "_open", fail)
    collector = tasks.Collector({"state": folder}, strict=strict)
    assert collector._mtimes("state") == {}


@pytest.mark.parametrize("where", ["source", "middle", "file"])
@pytest.mark.parametrize("problem", ["symlink", "owner", "group-write", "other-write"])
def test_strict_source_walk_skips_untrusted_components_without_leaking_fds(tmp_path, where, problem, caplog):
    root = tmp_path / "source"
    middle = root / "middle"
    middle.mkdir(parents=True, mode=0o750)
    path = middle / "state"
    path.write_text("trusted data")
    target = {"source": root, "middle": middle, "file": path}[where]
    if problem == "symlink":
        actual = tmp_path / "outside"
        target.rename(actual)
        target.symlink_to(actual, target_is_directory=where != "file")
    elif problem == "owner":
        os.chown(target, 10001, -1)
    else:
        target.chmod(target.stat().st_mode | (0o020 if problem == "group-write" else 0o002))
    collector = tasks.Collector({"state": root}, strict=True)
    before = len(os.listdir("/proc/self/fd"))
    assert collector._read("state", "middle", "state") == ""
    assert len(os.listdir("/proc/self/fd")) == before
    assert collector._mtimes("state", "middle") == {}
    assert "skipped" in caplog.text


def test_strict_source_reads_names_markers_and_cron_files_through_verified_descriptors(tmp_path):
    root = tmp_path / "source"
    folder = root / "middle"
    folder.mkdir(parents=True, mode=0o750)
    path = folder / "state"
    path.write_text("trusted data")
    collector = tasks.Collector({"state": root, "cron.hourly": folder, "localtime": path,
                                 "missing": tmp_path / "missing"}, strict=True)
    assert collector._read("state", "middle", "state") == "trusted data"
    assert collector._names("state", "middle") == ["state"]
    assert collector._mtimes("state", "middle") == {"state": path.stat().st_mtime}
    assert collector._data("localtime", follow=True)[0] == b"trusted data"
    assert collector.scheduled_files({"hourly": "0 * * * *"}) == []
    path.chmod(0o755)
    assert collector.scheduled_files({"hourly": "0 * * * *"}) == [{"name": "state", "cron": "0 * * * *"}]
    stub = folder / "user.script.start.test"
    stub.symlink_to(tmp_path / "missing")
    assert collector.scheduled_files({"hourly": "0 * * * *"}) == [{"name": "state", "cron": "0 * * * *"}]
    path.unlink()
    assert collector._data("state", "middle", "state") == (b"", 0.0)
    assert collector._names("missing") == []
    path.symlink_to(tmp_path / "outside")
    (tmp_path / "outside").write_text("untrusted")
    assert collector._data("localtime", follow=True)[0] == b"untrusted"


@pytest.mark.parametrize("parts", [("..", "state"), ("",), (".",), ("middle/state",)])
def test_source_walk_rejects_traversal_before_opening_any_child(tmp_path, parts):
    from shazzzbot.helper import files

    with pytest.raises(ValueError, match="untrusted root path"), files.trusted_open(tmp_path, parts):
        pytest.fail("untrusted path yielded a descriptor")


@pytest.mark.parametrize("relative", [True, False])
def test_source_walk_requires_an_absolute_nontraversing_root(tmp_path, relative):
    from shazzzbot.helper import files

    root = Path("relative") if relative else tmp_path / ".." / "outside"
    with pytest.raises(ValueError, match="untrusted root path"), files.trusted_open(root):
        pytest.fail("untrusted root yielded a descriptor")


def test_source_root_rename_cannot_redirect_an_already_open_walk(tmp_path, monkeypatch):
    root, moved, outside = (tmp_path / name for name in ("source", "moved", "outside"))
    root.mkdir()
    outside.mkdir()
    (root / "state").write_text("trusted")
    (outside / "state").write_text("outside data")
    original = os.open
    def replace_root(path, flags, *args, **kwargs):
        fd = original(path, flags, *args, **kwargs)
        if path == root.name:
            root.rename(moved)
            root.symlink_to(outside, target_is_directory=True)
        return fd
    monkeypatch.setattr(os, "open", replace_root)
    collector = tasks.Collector({"state": root}, strict=True)
    assert collector._read("state", "state") == "trusted"
    assert (root / "state").read_text() == "outside data"


def test_disappearing_cron_entries_remain_empty_instead_of_breaking_the_gather(tmp_path, monkeypatch):
    from contextlib import contextmanager

    root = tmp_path / "cron"
    root.mkdir()
    (root / "gone").touch(mode=0o755)
    original = tasks.Collector._open
    @contextmanager
    def vanish(self, parts, **kwargs):
        if parts == ("cron.hourly", "gone"):
            raise FileNotFoundError
        with original(self, parts, **kwargs) as fd:
            yield fd
    monkeypatch.setattr(tasks.Collector, "_open", vanish)
    collector = tasks.Collector({"cron.hourly": root}, strict=True)
    assert collector.scheduled_files({"hourly": "0 * * * *"}) == []
