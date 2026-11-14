import io
import os
import runpy
import signal
import subprocess
import sys
import tarfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import setup_ship as ship

REVISION = "a" * 40
ROOT = Path(__file__).resolve().parent.parent


def archive(entries=None):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as result:
        for name, body in entries or [("source/setup.sh", b"true\n"), (".ship/git.pack", b"pack"),
                                      ("source/tools/setup_ship.py", b"drainer")]:
            entry = tarfile.TarInfo(name)
            if body in {tarfile.DIRTYPE, tarfile.SYMTYPE, tarfile.LNKTYPE}:
                entry.type = body
                entry.linkname = "/etc/passwd"
                result.addfile(entry)
            else:
                entry.size = len(body)
                result.addfile(entry, io.BytesIO(body))
    return output.getvalue()


@pytest.mark.parametrize("args", [[], ["/", REVISION, "bot"], ["relative", REVISION, "bot"],
                                      ["/a/../b", REVISION, "bot"], ["/a\nb", REVISION, "bot"],
                                      ["/a", "bad", "bot"], ["/a", REVISION, "bad"]])
def test_arguments_fail_closed(args):
    with pytest.raises(ValueError):
        ship.arguments(args)


def test_arguments_preserve_spaces_without_shell_interpretation():
    assert ship.arguments(["/a path/$x", REVISION, "relay"]) == (Path("/a path/$x"), REVISION, "relay")


def pipe(data):
    reader, writer = os.pipe()
    os.write(writer, data)
    os.close(writer)
    return os.fdopen(reader, "rb")


def test_receive_reads_to_eof_and_caps_size(monkeypatch):
    output = io.BytesIO()
    with pipe(b"data") as source:
        ship.receive(source, output)
    assert output.getvalue() == b"data"
    monkeypatch.setattr(ship, "MAX_BYTES", 3)
    with pipe(b"four") as source, pytest.raises(ValueError, match="size limit"):
        ship.receive(source, io.BytesIO())


@pytest.mark.parametrize("deadline", [False, True])
def test_receive_has_total_deadline(monkeypatch, deadline):
    times = iter([0, ship.READ_SECONDS + 1 if deadline else 0])
    monkeypatch.setattr(ship.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(ship.select, "select", lambda *_: ([], [], []))
    with pipe(b"data") as source, pytest.raises(TimeoutError):
        ship.receive(source, io.BytesIO())


def test_extract_uses_private_fixed_prefixes(tmp_path):
    path = tmp_path / "upload.tar"
    path.write_bytes(archive([("source", tarfile.DIRTYPE), ("source/setup.sh", b"true\n"),
                             (".ship/git.pack", b"pack"), (".ship/.env", b"TOKEN=secret\n"),
                             ("source/tools/setup_ship.py", b"drainer")]))
    root = tmp_path / "root"
    root.mkdir()
    ship.extract(path, root)
    assert (root / "source/setup.sh").read_bytes() == b"true\n"
    assert (root / ".ship/.env").stat().st_mode & 0o777 == 0o600
    assert (root / "source").stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("name,kind", [("/etc/passwd", b""), ("source/../../escape", b""),
                                              ("other/file", b""), ("source/a\nb", b""),
                                              ("source/link", tarfile.SYMTYPE),
                                              ("source/hard", tarfile.LNKTYPE)])
def test_extract_rejects_unsafe_entries(tmp_path, name, kind):
    path = tmp_path / "upload.tar"
    path.write_bytes(archive([(name, kind)]))
    with pytest.raises(ValueError, match="unsafe"):
        ship.extract(path, tmp_path)


def test_extract_rejects_duplicate_and_incomplete_archives(tmp_path):
    path = tmp_path / "upload.tar"
    path.write_bytes(archive([("source/setup.sh", b"a"), ("source/setup.sh", b"b")]))
    with pytest.raises(FileExistsError):
        ship.extract(path, tmp_path)
    path.write_bytes(archive([("source/other", b"a")]))
    with pytest.raises(ValueError, match="incomplete"):
        ship.extract(path, tmp_path)


@pytest.mark.parametrize("limit", ["MAX_BYTES", "MAX_FILES"])
def test_extract_enforces_both_limits(tmp_path, monkeypatch, limit):
    path = tmp_path / "upload.tar"
    path.write_bytes(archive())
    monkeypatch.setattr(ship, limit, 0)
    with pytest.raises(ValueError):
        ship.extract(path, tmp_path)


def test_extract_refuses_missing_file_body(tmp_path, monkeypatch):
    path = tmp_path / "upload.tar"
    path.write_bytes(archive())
    monkeypatch.setattr(tarfile.TarFile, "extractfile", lambda *_: None)
    with pytest.raises(ValueError, match="no body"):
        ship.extract(path, tmp_path)


def test_deploy_passes_only_fixed_flags(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ship, "execute", lambda command, root: calls.append((command, root)) or 7)
    assert ship.deploy(Path("/logical path"), REVISION, "relay", tmp_path) == 7
    command, root = calls[0]
    assert root == tmp_path
    assert command[:2] == ["bash", str(tmp_path / "source/setup.sh")]
    assert command[command.index("--dir") + 1] == "/logical path"
    assert command[command.index("--ship-revision") + 1] == REVISION
    assert "--upgrade" not in command


def test_environment_excludes_ambient_remote_context(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote.invalid:2375")
    monkeypatch.setenv("DOCKER_CONTEXT", "remote")
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    value = ship.environment()
    assert set(value) == {"PATH", "HOME", "LANG", "PYTHONDONTWRITEBYTECODE", "DOCKER_HOST"}
    assert value["DOCKER_HOST"] == "unix:///var/run/docker.sock"


def test_deploy_flags_are_accepted_by_actual_installer_without_mutation(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "setup.sh").write_bytes((ROOT / "setup.sh").read_bytes())

    def parse(command, root):
        assert root == tmp_path
        result = subprocess.run([*command, "--help"], env=ship.environment(), capture_output=True,
                                text=True, timeout=10, check=False)
        assert result.returncode == 0, result.stderr
        assert "--method" in result.stdout
        return result.returncode

    monkeypatch.setattr(ship, "execute", parse)
    assert ship.deploy(Path("/logical"), REVISION, "relay", tmp_path) == 0
    assert list(tmp_path.iterdir()) == [source]


def test_run_stages_only_in_ram_and_cleans_it(tmp_path, monkeypatch):
    monkeypatch.setattr(ship, "RAM", tmp_path)
    monkeypatch.setattr(ship.os, "geteuid", lambda: 0)
    monkeypatch.setattr(ship, "receive", lambda _, output: output.write(archive()))
    roots = []

    def deploy(logical, revision, role, root):
        roots.append(root)
        assert root.stat().st_mode & 0o777 == 0o700
        assert (root / "upload.tar").stat().st_mode & 0o777 == 0o600
        assert (logical, revision, role) == (Path("/logical"), REVISION, "bot")
        return 3

    monkeypatch.setattr(ship, "deploy", deploy)
    assert ship.run(["/logical", REVISION, "bot"], io.BytesIO()) == 3
    assert len(roots) == 1 and not roots[0].exists()


def test_run_requires_root_and_private_ram(tmp_path, monkeypatch):
    monkeypatch.setattr(ship.os, "geteuid", lambda: 1000)
    with pytest.raises(ValueError, match="requires root"):
        ship.run(["/logical", REVISION, "bot"], io.BytesIO())
    monkeypatch.setattr(ship.os, "geteuid", lambda: 0)
    monkeypatch.setattr(ship, "RAM", tmp_path)
    tmp_path.chmod(0o777)
    with pytest.raises(ValueError, match="trusted RAM"):
        ship.run(["/logical", REVISION, "bot"], io.BytesIO())


def test_work_staging_refuses_an_untrusted_ancestor(tmp_path, monkeypatch):
    parent = tmp_path / "writable"
    parent.mkdir(mode=0o777)
    parent.chmod(0o777)
    monkeypatch.setattr(ship, "RAM", parent / "work")
    with pytest.raises(ValueError, match="ancestors must be trusted"):
        ship.prepare_work()
    assert not (parent / "work").exists()


@pytest.mark.parametrize("error", [None, ValueError("secret"), OSError("secret"), tarfile.ReadError("secret"),
                                       subprocess.TimeoutExpired("secret", 1)])
def test_main_returns_clean_status(monkeypatch, capsys, error):
    def invoke(*_):
        if error is not None:
            raise error
        return 4

    monkeypatch.setattr(ship, "run", invoke)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO()))
    with pytest.raises(SystemExit) as exit_info:
        ship.main()
    assert exit_info.value.code == (1 if error else 4)
    assert "secret" not in capsys.readouterr().err


def test_entrypoint_rejects_bad_arguments(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["setup_ship.py"])
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO()))
    with pytest.raises(SystemExit, match="1"):
        runpy.run_path(str(ROOT / "tools/setup_ship.py"), run_name="__main__")


def test_signals_count_and_restore_handlers(monkeypatch):
    calls = []
    monkeypatch.setattr(ship.signal, "signal", lambda number, handler: calls.append((number, handler)) or "previous")
    cancel = ship.Cancellation()
    with ship.signals(cancel):
        cancel.receive(signal.SIGHUP, None)
        cancel.receive(signal.SIGINT, None)
    assert (cancel.count, cancel.first) == (2, signal.SIGHUP)
    assert len(calls) == 6
    assert all(handler == "previous" for _, handler in calls[3:])


@pytest.mark.parametrize("failure", ["get", "set", "write", "zero", "restore", "none"])
def test_emit_never_blocks_or_fails_closed_ssh(monkeypatch, failure):
    flags, writes = [], []

    def fcntl(fd, operation, value=None):
        flags.append((fd, operation, value))
        if (failure == "get" and len(flags) == 1) or (failure == "set" and len(flags) == 2) \
                or (failure == "restore" and len(flags) == 3):
            raise OSError("closed")
        return 0

    def write(fd, data):
        writes.append((fd, data))
        if failure == "write":
            raise BrokenPipeError("closed ssh")
        return 0 if failure == "zero" else min(2, len(data))

    monkeypatch.setattr(ship.fcntl, "fcntl", fcntl)
    monkeypatch.setattr(ship.os, "write", write)
    ship.emit(b"progress", 99)
    assert flags[0][0] == 99
    if failure not in {"get", "set"}:
        assert writes[0] == (99, b"progress")
        assert flags[-1][2] == 0


def test_drain_caps_log_but_continues_consuming(monkeypatch):
    blocks = iter([b"abcd", b"efgh", b""])
    monkeypatch.setattr(ship.select, "select", lambda *_: ([1], [], []))
    monkeypatch.setattr(ship.os, "read", lambda *_: next(blocks))
    monkeypatch.setattr(ship, "MAX_LOG", 2)
    output = io.BytesIO()
    ship.drain(SimpleNamespace(fileno=lambda: 99), output)
    assert output.getvalue() == b"ab"
    with pytest.raises(StopIteration):
        next(blocks)


@pytest.mark.parametrize("elapsed", [False, True])
def test_drain_total_deadline_is_bounded(monkeypatch, elapsed):
    times = iter([0, ship.LOG_SECONDS + 1 if elapsed else 0])
    monkeypatch.setattr(ship.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(ship.select, "select", lambda *_: ([], [], []))
    assert ship.drain(io.BytesIO(), io.BytesIO()) is None


def test_drain_keeps_reading_when_log_write_fails(monkeypatch):
    blocks = iter([b"data", b"more", b""])
    monkeypatch.setattr(ship.select, "select", lambda *_: ([1], [], []))
    monkeypatch.setattr(ship.os, "read", lambda *_: next(blocks))
    output = SimpleNamespace(write=lambda _: (_ for _ in ()).throw(OSError("full RAM")))
    ship.drain(SimpleNamespace(fileno=lambda: 99), output)
    with pytest.raises(StopIteration):
        next(blocks)


def test_progress_forwards_fixed_chunks_and_ignores_read_errors(monkeypatch):
    calls = []
    monkeypatch.setattr(ship, "emit", calls.append)
    ship.progress(io.BytesIO(b"data"))
    assert calls == [b"data"]
    ship.progress(SimpleNamespace(read=lambda _: (_ for _ in ()).throw(OSError("read failed"))))
    assert calls == [b"data"]


def process_stat(root, name, state="S", group=321, session=321, *, text=None):
    path = root / name
    path.mkdir()
    (path / "stat").write_text(text if text is not None else f"123 (name with ) chars) {state} 1 {group} {session}")
    return path


def test_group_scan_excludes_zombies_dead_and_other_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(ship, "PROC", tmp_path)
    (tmp_path / "not-a-pid").mkdir()
    (tmp_path / "999").mkdir()
    process_stat(tmp_path, "100", "Z")
    process_stat(tmp_path, "101", "X")
    process_stat(tmp_path, "102", group=322)
    process_stat(tmp_path, "103", session=322)
    assert not ship.group_alive(321)
    process_stat(tmp_path, "104")
    assert ship.group_alive(321)


@pytest.mark.parametrize("text", ["invalid", "123 (name) S 1"])
def test_group_scan_fails_closed_on_bad_kernel_data(tmp_path, monkeypatch, text):
    monkeypatch.setattr(ship, "PROC", tmp_path)
    process_stat(tmp_path, "100", text=text)
    assert ship.group_alive(321)


def test_group_scan_fails_closed_on_permission_error(tmp_path, monkeypatch):
    monkeypatch.setattr(ship, "PROC", tmp_path)
    process_stat(tmp_path, "100")
    monkeypatch.setattr(Path, "read_text", lambda _: (_ for _ in ()).throw(PermissionError("stat")))
    assert ship.group_alive(321)


@pytest.mark.parametrize("limit", ["count", "time"])
def test_group_scan_has_size_and_time_limits(monkeypatch, limit):
    monkeypatch.setattr(ship, "PROC", SimpleNamespace(iterdir=lambda: iter([Path("not-pid")] * 65537)))
    times = iter([0, 2])
    monkeypatch.setattr(ship.time, "monotonic", (lambda: 0) if limit == "count" else lambda: next(times))
    assert ship.group_alive(321)


def test_send_only_targets_selected_group_and_handles_disappearance(monkeypatch):
    calls = []

    def kill(group, number):
        calls.append((group, number))
        raise ProcessLookupError("gone")

    monkeypatch.setattr(ship.os, "killpg", kill)
    ship.send(321, signal.SIGTERM)
    assert calls == [(321, signal.SIGTERM)]


class Clock:
    value = 0

    def monotonic(self):
        return self.value

    def sleep(self, _seconds):
        self.value += 1


@pytest.mark.parametrize("kind,done,result,expected", [
    ("normal", 1, 7, []), ("timeout", 4, 124, [signal.SIGTERM]),
    ("hup", 2, 129, [signal.SIGTERM]), ("second", 1, 130, [signal.SIGTERM, signal.SIGKILL]),
    ("stuck", 100, None, [signal.SIGTERM, signal.SIGKILL]),
])
def test_monitor_bounded_recovery_keeps_leader_until_group_exits(monkeypatch, kind, done, result, expected):
    clock, calls, waited = Clock(), [], []
    monkeypatch.setattr(ship.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(ship.time, "sleep", clock.sleep)
    monkeypatch.setattr(ship, "INSTALL_SECONDS", 2)
    monkeypatch.setattr(ship, "RECOVERY_SECONDS", 3)
    monkeypatch.setattr(ship, "KILL_SECONDS", 2)
    monkeypatch.setattr(ship.os, "waitid", lambda *_: object() if clock.value >= done else None)
    monkeypatch.setattr(ship, "group_alive", lambda _: False)
    monkeypatch.setattr(ship, "send", lambda group, number: calls.append((group, number)))
    child = SimpleNamespace(pid=321, wait=lambda **_: waited.append(clock.value) or 7)
    cancel = ship.Cancellation()
    if kind in {"hup", "second"}:
        cancel.receive(signal.SIGHUP if kind == "hup" else signal.SIGINT, None)
    if kind == "second":
        cancel.receive(signal.SIGINT, None)
    if kind == "stuck":
        with pytest.raises(ship.Retained):
            ship.monitor(child, cancel, io.BytesIO())
        assert not waited
    else:
        assert ship.monitor(child, cancel, io.BytesIO()) == result
        assert waited == [done]
    assert calls == [(321, number) for number in expected]


def test_monitor_waits_for_live_descendants_after_leader_exit(monkeypatch):
    clock, waited = Clock(), []
    monkeypatch.setattr(ship.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(ship.time, "sleep", clock.sleep)
    monkeypatch.setattr(ship.os, "waitid", lambda *_: object())
    monkeypatch.setattr(ship, "group_alive", lambda _: clock.value < 2)
    child = SimpleNamespace(pid=321, wait=lambda **_: waited.append(clock.value) or 0)
    assert ship.monitor(child, ship.Cancellation(), io.BytesIO()) == 0
    assert waited == [2]


@pytest.mark.parametrize("timeouts", [0, 1, 2, 3])
def test_finish_logger_has_bounded_escalation(timeouts):
    calls, count = [], 0

    def wait(**options):
        nonlocal count
        count += 1
        calls.append(options["timeout"])
        if count <= timeouts:
            raise subprocess.TimeoutExpired("logger", 5)

    logger = SimpleNamespace(wait=wait, terminate=lambda: calls.append("term"), kill=lambda: calls.append("kill"))
    if timeouts == 3:
        with pytest.raises(ship.Retained, match="log drainer"):
            ship.finish_logger(logger)
    else:
        ship.finish_logger(logger)
    assert calls == [[5], [5, "term", 5], [5, "term", 5, "kill", 5], [5, "term", 5, "kill", 5]][timeouts]


@pytest.mark.parametrize("mode", ["normal", "cancel", "spawn-fail", "uncertain", "recover-error", "recovery-fails",
                                  "reaped"])
def test_execute_owns_sessions_stdin_and_retains_uncertain_state(tmp_path, monkeypatch, mode):
    calls, waits, checks = [], [], []
    logger = SimpleNamespace(stdin=io.BytesIO(), wait=lambda **_: waits.append("logger") or 0)
    child = SimpleNamespace(pid=321)

    def spawn(command, **options):
        calls.append((command, options))
        if len(calls) == 1:
            return logger
        if mode == "spawn-fail":
            raise OSError("spawn")
        return child

    @contextmanager
    def signals(cancel):
        if mode == "cancel":
            cancel.receive(signal.SIGTERM, None)
        yield

    def monitor(*_):
        checks.append("monitor")
        if mode == "uncertain":
            raise ship.Retained("group lives")
        if mode == "reaped":
            raise ChildProcessError("identity unavailable")
        if mode == "recovery-fails" or mode == "recover-error" and len(checks) == 1:
            raise OSError("monitor failed")
        return 7

    monkeypatch.setattr(ship.subprocess, "Popen", spawn)
    monkeypatch.setattr(ship, "signals", signals)
    monkeypatch.setattr(ship, "monitor", monitor)
    if mode == "spawn-fail":
        with pytest.raises(OSError, match="spawn"):
            ship.execute(["bash", "installer"], tmp_path)
    elif mode in {"uncertain", "recovery-fails", "reaped"}:
        with pytest.raises(ship.Retained, match=str(tmp_path)):
            ship.execute(["bash", "installer"], tmp_path)
        assert not waits
    elif mode == "recover-error":
        with pytest.raises(OSError, match="monitor failed"):
            ship.execute(["bash", "installer"], tmp_path)
        assert checks == ["monitor", "monitor"]
        assert waits == ["logger"]
    else:
        assert ship.execute(["bash", "installer"], tmp_path) == (143 if mode == "cancel" else 7)
    assert (tmp_path / "installer.log").stat().st_mode & 0o777 == 0o600
    assert all(options["start_new_session"] and options["close_fds"] for _, options in calls)
    assert all(options["env"] == ship.environment() for _, options in calls)
    assert logger.stdin.closed


def test_run_retains_staging_only_when_process_exit_is_uncertain(tmp_path, monkeypatch):
    monkeypatch.setattr(ship, "RAM", tmp_path)
    monkeypatch.setattr(ship.os, "geteuid", lambda: 0)
    monkeypatch.setattr(ship, "receive", lambda _, output: output.write(archive()))
    monkeypatch.setattr(ship, "deploy", lambda *_: (_ for _ in ()).throw(ship.Retained("lives")))
    messages = []
    monkeypatch.setattr(ship, "emit", lambda data, fd: messages.append((data, fd)))
    with pytest.raises(ship.Retained):
        ship.run(["/logical", REVISION, "bot"], io.BytesIO())
    roots = list(tmp_path.iterdir())
    assert len(roots) == 1 and (roots[0] / "source/setup.sh").is_file()
    assert messages == [(("ship failed; staging retained at " + str(roots[0]) + "\n").encode(), 2)]


def test_main_drainer_mode_has_no_installer_access(monkeypatch):
    calls = []
    with monkeypatch.context() as patch:
        patch.setattr(sys, "argv", ["setup_ship.py", "--drain"])
        patch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"input")))
        patch.setattr(sys, "stdout", SimpleNamespace(buffer=io.BytesIO()))
        patch.setattr(ship, "drain", lambda source, output: calls.append((source.read(), output)))
        assert ship.main() is None
    assert calls[0][0] == b"input"


@pytest.mark.parametrize("number", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_real_child_recovers_after_interrupt_with_closed_ssh_stdout(tmp_path, monkeypatch, number):
    tools = tmp_path / "source/tools"
    tools.mkdir(parents=True)
    (tools / "setup_ship.py").write_bytes((ROOT / "tools/setup_ship.py").read_bytes())
    script, marker = tmp_path / "child.py", tmp_path / "recovered"
    script.write_text("import os, signal, sys, time\nfrom pathlib import Path\n"
                      "def recover(_number, _frame):\n"
                      "    print('recovering', flush=True)\n"
                      "    time.sleep(0.1)\n"
                      "    Path(sys.argv[1]).write_text('recovered')\n"
                      "    raise SystemExit(0)\n"
                      "signal.signal(signal.SIGTERM, recover)\n"
                      "print('started', flush=True)\n"
                      "os.kill(os.getppid(), int(sys.argv[2]))\n"
                      "while True: time.sleep(1)\n")
    real_write = os.write

    def write(fd, data):
        if fd == 1:
            raise BrokenPipeError("ssh closed")
        return real_write(fd, data)

    monkeypatch.setattr(ship.os, "write", write)
    monkeypatch.setattr(ship, "INSTALL_SECONDS", 5)
    monkeypatch.setattr(ship, "RECOVERY_SECONDS", 2)
    assert ship.execute([sys.executable, str(script), str(marker), str(number)], tmp_path) == 128 + number
    assert marker.read_text() == "recovered"
    assert b"recovering" in (tmp_path / "installer.log").read_bytes()
    assert script.is_file()


@pytest.fixture
def client(tmp_path):
    binary = tmp_path / "bin"
    binary.mkdir()
    scripts = {
        "git": """#!/bin/bash
case "$1" in
 status) printf '%s' "${SHIP_DIRTY:-}" ;;
 fetch) [ "${SHIP_FETCH_FAIL:-0}" = 0 ] ;;
 rev-parse) if [ "$2" = forge/main ] && [ "${SHIP_UNPUSHED:-0}" = 1 ]; then
   echo bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
   else echo aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa; fi ;;
 show) printf '%s\\n' 'raise SystemExit(0)' ;;
 archive) cat "$SHIP_BASE_TAR" ;;
 ls-tree) printf '%s\\n' '100644 blob bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb file' ;;
 pack-objects) cat > "$SHIP_ROOT/objects"; printf PACK ;;
 check-ignore) [ "${SHIP_NOT_IGNORED:-0}" = 0 ] ;;
 *) exit 3 ;;
esac
""",
        "ssh": """#!/bin/bash
printf '%s\\n' "$*" >> "$SHIP_ROOT/ssh.calls"
cat > "$SHIP_ROOT/bot.tar"
exit "${SHIP_SEND_FAIL:-0}"
""",
        "python3": """#!/bin/bash
printf '%s\\n' "$*" >> "$SHIP_ROOT/relay.calls"
cat > "$SHIP_ROOT/relay.tar"
""",
        "sudo": """#!/bin/bash
[ "$1" != -n ] || shift
exec "$@"
""",
    }
    for name, body in scripts.items():
        path = binary / name
        path.write_text(body)
        path.chmod(0o700)
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    (home / ".config/shazzzbot-ship-cn.env").write_text("HOST=server\nDIR=/logical path/it's-safe\n")
    (tmp_path / "base.tar").write_bytes(archive([("source/setup.sh", b"true\n")]))
    script = tmp_path / "ship.sh"
    script.write_bytes((ROOT / "ship.sh").read_bytes())
    return {**os.environ, "HOME": str(home), "PATH": str(binary) + ":" + os.environ["PATH"],
            "SHIP_ROOT": str(tmp_path), "SHIP_BASE_TAR": str(tmp_path / "base.tar"), "TMPDIR": str(tmp_path)}


def invoke_client(client, *args):
    return subprocess.run(["bash", str(Path(client["SHIP_ROOT"]) / "ship.sh"), *args], env=client,
                          capture_output=True, text=True, timeout=10, check=False)


def test_client_sends_committed_source_pack_and_private_config(client):
    root = Path(client["SHIP_ROOT"])
    (root / "config.server.toml").write_text("token=secret-config\n")
    (root / ".env.server").write_text("TOKEN=secret-environment\n")
    result = invoke_client(client, "cn")
    assert result.returncode == 0, result.stderr
    assert "secret" not in result.stdout + result.stderr + (root / "ssh.calls").read_text()
    with tarfile.open(root / "bot.tar") as payload:
        assert payload.extractfile(".ship/git.pack").read() == b"PACK"
        assert payload.extractfile(".ship/.env").read() == b"TOKEN=secret-environment\n"
        assert payload.getmember(".ship/config.toml").mode == 0o600
    assert not list(root.glob("shazzzbot-ship.*"))
    assert (root / "objects").read_text().splitlines() == [REVISION, REVISION, "b" * 40]
    assert "python3 -I -B -c" in (root / "ssh.calls").read_text()


def test_client_updates_local_relay_with_separate_payload(client):
    root = Path(client["SHIP_ROOT"])
    config = Path(client["HOME"]) / ".config/shazzzbot-ship-cn.env"
    config.write_text("HOST=server\nDIR=/logical\nNODES='local'\n")
    (root / ".env").write_text("RELAY_ALLOW=service.test\n")
    (root / "config.server.toml").write_text("bot-only\n")
    result = invoke_client(client, "cn")
    assert result.returncode == 0, result.stderr
    with tarfile.open(root / "relay.tar") as payload:
        assert ".ship/config.toml" not in payload.getnames()
        assert payload.extractfile(".ship/.env").read() == b"RELAY_ALLOW=service.test\n"
    assert "relay" in (root / "relay.calls").read_text()


@pytest.mark.parametrize("key,value,message", [("SHIP_DIRTY", " M file", "not committed"),
                                                     ("SHIP_FETCH_FAIL", "1", "fetch failed"),
                                                     ("SHIP_UNPUSHED", "1", "not pushed"),
                                                     ("SHIP_SEND_FAIL", "1", "deployment failed"),
                                                     ("SHIP_NOT_IGNORED", "1", "must be gitignored")])
def test_client_fails_closed_and_cleans_payload(client, key, value, message):
    root = Path(client["SHIP_ROOT"])
    (root / ".env.server").write_text("TOKEN=secret\n")
    result = invoke_client({**client, key: value}, "cn")
    assert result.returncode != 0 and message in result.stderr
    assert "secret" not in result.stdout + result.stderr
    assert not list(root.glob("shazzzbot-ship.*"))


def test_client_rejects_config_symlink_and_unknown_node(client):
    root = Path(client["SHIP_ROOT"])
    (root / ".env.server").symlink_to(root / "base.tar")
    result = invoke_client(client, "cn")
    assert result.returncode != 0 and "not a symlink" in result.stderr
    config = Path(client["HOME"]) / ".config/shazzzbot-ship-cn.env"
    config.write_text("HOST=server\nDIR=/logical\nNODES=other\n")
    result = invoke_client(client, "cn")
    assert result.returncode != 0 and "unsupported relay node" in result.stderr
