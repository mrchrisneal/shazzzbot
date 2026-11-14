import hashlib
import json
import os
import signal
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import setup_helper as helper


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"format": 1, "socket": str(tmp_path / "helper.sock"), "action_timeout": 7200}))
    path.chmod(0o600)
    return path, tmp_path / "helper.pyz", tmp_path / "helper.pid"


class Child:
    pid = 12345

    def __init__(self, code=None, timeout=False):
        self.code, self.timeout, self.waits = code, timeout, []

    def poll(self):
        return self.code

    def wait(self, timeout):
        self.waits.append(timeout)
        if self.timeout:
            self.timeout = False
            raise subprocess.TimeoutExpired("helper", timeout)
        return 0


def setup_child(monkeypatch, child, ticks):
    calls, kills = [], []
    def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return child
    clock = iter(ticks)
    monkeypatch.setattr(helper.subprocess, "Popen", spawn)
    monkeypatch.setattr(helper.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(helper.os, "killpg", lambda *args: kills.append(args))
    return calls, kills


def test_start_polls_for_its_own_pid_with_clean_environment_and_closed_fds(config, monkeypatch):
    cfg, binary, pidfile = config
    child = Child()
    calls, kills = setup_child(monkeypatch, child, [0, 0, 0.1])
    sleeps, checked = [], []
    def ready(seconds):
        sleeps.append(seconds)
        pidfile.write_text(json.dumps({"pid": child.pid, "path": str(binary)}))
        pidfile.chmod(0o600)
    monkeypatch.setattr(helper.time, "sleep", ready)
    monkeypatch.setattr(helper, "process", lambda *args: checked.append(args))
    helper.start(cfg.parent / "script", cfg, binary)
    assert sleeps == [0.1] and checked == [(cfg, binary, "helper")]
    assert kills == [] and child.waits == []
    kwargs = calls[0][1]
    assert kwargs["close_fds"] is True and kwargs["start_new_session"] is True
    assert kwargs["env"] == {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8",
                              "HOME": "/tmp/shazzzbot-helper", "TMPDIR": "/tmp/shazzzbot-helper",
                              "XDG_CACHE_HOME": "/tmp/shazzzbot-helper/cache",
                              "XDG_CONFIG_HOME": "/tmp/shazzzbot-helper/config",
                              "XDG_DATA_HOME": "/tmp/shazzzbot-helper/data"}
    assert calls[0][0][0] == ["/bin/bash", str(cfg.parent / "script")]


@pytest.mark.parametrize("problem", ["timeout", "exited", "identity", "signal"])
def test_failed_start_cleans_only_its_own_group_after_the_full_drain(config, monkeypatch, problem):
    cfg, binary, pidfile = config
    child = Child(code=1 if problem == "exited" else None, timeout=problem == "timeout")
    ticks = [0, 31] if problem == "timeout" else [0, 0]
    _, kills = setup_child(monkeypatch, child, ticks)
    pidfile.write_text(json.dumps({"pid": child.pid + 1, "path": str(binary)}))
    pidfile.chmod(0o600)
    if problem == "signal":
        def interrupt():
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        monkeypatch.setattr(helper.time, "monotonic", interrupt)
    previous = signal.getsignal(signal.SIGTERM)
    with pytest.raises((ValueError, InterruptedError)):
        helper.start(cfg.parent / "script", cfg, binary)
    assert signal.getsignal(signal.SIGTERM) is previous
    if problem == "exited":
        assert kills == []
    else:
        assert kills[0] == (child.pid, signal.SIGTERM) and child.waits[0] == 7230
        if problem == "timeout":
            assert kills[-1] == (child.pid, signal.SIGKILL) and child.waits == [7230, 5]


def test_spawn_failure_restores_the_signal_handler(config, monkeypatch):
    cfg, binary, _ = config
    def failed(*args, **kwargs):
        raise OSError("spawn failed")
    monkeypatch.setattr(helper.subprocess, "Popen", failed)
    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(OSError, match="spawn failed"):
        helper.start(cfg, cfg, binary)
    assert signal.getsignal(signal.SIGTERM) is before


def test_invalid_drain_is_rejected_before_spawning(config, monkeypatch):
    cfg, binary, _ = config
    cfg.write_text(json.dumps({"format": 1, "action_timeout": 0}))
    monkeypatch.setattr(helper.subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn"))
    with pytest.raises(ValueError, match="drain"):
        helper.start(cfg, cfg, binary)


@pytest.mark.parametrize("value", [[], {"format": 1}, {"format": 1, "socket": 2},
                                  {"format": 1, "socket": "relative"}])
def test_bad_helper_socket_identity_is_rejected(config, value):
    cfg, binary, _ = config
    cfg.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="object with a format" if value == [] else "socket identity"):
        helper.process(cfg, binary, "helper")


@pytest.mark.parametrize("item", [[], {"pid": 1, "action_timeout": 1}, {"pid": 2**80, "action_timeout": 1},
                                   {"pid": 50, "action_timeout": 0}, {"pid": 50, "action_timeout": "300"}])
def test_bad_pid_records_never_signal_any_process(config, monkeypatch, item):
    cfg, binary, pidfile = config
    pidfile.write_text(json.dumps(item))
    pidfile.chmod(0o600)
    monkeypatch.setattr(helper.os, "pidfd_open", lambda *a: pytest.fail("must not open a pid"))
    with pytest.raises((TypeError, ValueError), match="pidfile"):
        helper.process(cfg, binary, "signal")


def test_a_running_helper_without_a_pidfile_is_not_reported_stopped(config, monkeypatch, capsys):
    cfg, binary, _ = config
    real_bytes = Path.read_bytes
    monkeypatch.setattr(Path, "iterdir", lambda path: [Path("/proc/123"), Path("/proc/not-a-pid")])
    monkeypatch.setattr(Path, "read_bytes", lambda path: os.fsencode(binary) + b"\0"
                        if str(path) == "/proc/123/cmdline" else real_bytes(path))
    real_stat = Path.stat
    monkeypatch.setattr(Path, "stat", lambda path, **kwargs: SimpleNamespace(st_uid=0)
                        if str(path) == "/proc/123" else real_stat(path, **kwargs))
    helper.process(cfg, binary, "helper")
    assert capsys.readouterr().out == "1\n"
    with pytest.raises(ValueError, match="without its trusted pidfile"):
        helper.process(cfg, binary, "signal")


def test_inspector_and_its_timeout_wrapper_are_not_running_helpers(config, monkeypatch, capsys):
    cfg, binary, _ = config
    monkeypatch.setattr(helper.os, "getpid", lambda: 111)
    monkeypatch.setattr(helper.os, "getppid", lambda: 222)
    monkeypatch.setattr(Path, "iterdir", lambda path: [Path("/proc/111"), Path("/proc/222")])
    commands = {"111": [b"python3", b"setup_manage.py", b"signal", os.fsencode(cfg), os.fsencode(binary)],
                "222": [b"/usr/bin/timeout", b"--foreground", b"300", b"python3", b"setup_manage.py",
                        b"signal", os.fsencode(cfg), os.fsencode(binary)]}
    real_bytes, real_stat = Path.read_bytes, Path.stat
    monkeypatch.setattr(Path, "read_bytes", lambda path: b"\0".join(commands[path.parent.name])
                        if path.name == "cmdline" else real_bytes(path))
    monkeypatch.setattr(Path, "stat", lambda path, **kwargs: SimpleNamespace(st_uid=0)
                        if path.parent == Path("/proc") else real_stat(path, **kwargs))
    helper.process(cfg, binary, "signal")
    assert capsys.readouterr().out == ""
    commands["222"] = [b"python3", os.fsencode(binary), b"--config", os.fsencode(cfg)]
    with pytest.raises(ValueError, match="without its trusted pidfile"):
        helper.process(cfg, binary, "signal")


def test_a_missing_process_does_not_signal_a_reused_pid(config, monkeypatch):
    cfg, binary, pidfile = config
    pidfile.write_text(json.dumps({"pid": 123, "action_timeout": 7200}))
    pidfile.chmod(0o600)
    def vanished(pid):
        raise ProcessLookupError
    monkeypatch.setattr(helper.os, "pidfd_open", vanished)
    assert helper.process(cfg, binary, "signal") is None


def test_missing_pidfile_scans_skip_non_processes_gone_and_unrelated_processes(config, monkeypatch):
    cfg, binary, _ = config
    monkeypatch.setattr(Path, "iterdir", lambda path: [Path("/proc/not-a-pid"), Path("/proc/10"), Path("/proc/20")])
    real_bytes, real_stat = Path.read_bytes, Path.stat
    def read(path):
        if str(path) == "/proc/10/cmdline":
            raise ProcessLookupError
        return b"unrelated\0" if str(path) == "/proc/20/cmdline" else real_bytes(path)
    monkeypatch.setattr(Path, "read_bytes", read)
    monkeypatch.setattr(Path, "stat", lambda path, **kwargs: SimpleNamespace(st_uid=10001)
                        if str(path) == "/proc/20" else real_stat(path, **kwargs))
    assert helper.process(cfg, binary, "helper") is None


@pytest.mark.parametrize("prefix,suffix,accepted", [([], [], False), ([b"backup"], [], False),
                                                   ([b"python3"], [], True), ([b"python3", b"-I", b"-B"], [], True),
                                                   ([b"python3"], [b"--check"], False)])
def test_pid_identity_requires_the_helper_as_the_python_program(config, prefix, suffix, accepted):
    cfg, binary, _ = config
    argv = [*prefix, os.fsencode(binary), *suffix] if prefix else []
    assert helper.command_matches(argv, binary, cfg) is accepted
    assert not helper.command_matches([b"python3", b"unrelated.py", os.fsencode(binary)], binary, cfg)


def test_pid_identity_accepts_only_the_exact_update_guard(config):
    from shazzzbot.helper.updates import REEXEC
    cfg, binary, _ = config
    assert not helper.command_matches([b"python3"], binary, cfg)
    assert helper.command_matches([b"python3", os.fsencode(binary), b"--config", os.fsencode(cfg)], binary, cfg)
    command = [b"python3", b"-I", b"-B", b"-c", REEXEC.encode(), os.fsencode(binary), os.fsencode(cfg),
               b"a" * 64, os.fsencode(cfg.parent / "update.result")]
    assert helper.command_matches(command, binary, cfg)
    for index, value in ((4, b"different code"), (5, b"different helper"), (6, b"different config"),
                         (7, b"bad digest"), (8, b"different result")):
        bad = list(command)
        bad[index] = value
        assert not helper.command_matches(bad, binary, cfg)


@pytest.mark.parametrize("problem", ["none", "mode", "owner", "directory", "oversize", "digest"])
def test_fd_program_identity_uses_a_private_archive_and_root_policy(config, problem):
    cfg, binary, _ = config
    binary.write_bytes(b"verified")
    binary.chmod(0o600)
    (binary.parent / "policy.json").write_text(json.dumps({
        "format": 1, "sha256": hashlib.sha256(b"verified").hexdigest()}))
    (binary.parent / "policy.json").chmod(0o600)
    if problem == "mode":
        binary.chmod(0o644)
    elif problem == "owner":
        os.chown(binary, 1000, -1)
    elif problem == "directory":
        binary.unlink()
        binary.mkdir(mode=0o700)
    elif problem == "oversize":
        binary.write_bytes(b"x" * (1024**2 + 1))
    elif problem == "digest":
        binary.write_bytes(b"different")
    fd = os.open(binary, os.O_RDONLY)
    try:
        argument = os.fsencode(f"/proc/self/fd/{fd}")
        assert helper.loaded_matches(os.getpid(), argument, binary, cfg) is (problem == "none")
        if problem == "none":
            args = [b"python3", b"-I", b"-B", argument, b"--config", os.fsencode(cfg)]
            assert helper.command_matches(args, binary, cfg, os.getpid())
            assert not helper.command_matches(args, binary, cfg)
    finally:
        os.close(fd)
    assert not helper.loaded_matches(os.getpid(), b"/unrelated", binary, cfg)


def test_fd_program_identity_rejects_a_malformed_policy(config):
    cfg, binary, _ = config
    path = binary.parent / "policy.json"
    path.write_text("[]")
    path.chmod(0o600)
    with pytest.raises(ValueError, match="helper policy must be an object with a format"):
        helper.loaded_matches(os.getpid(), b"/proc/self/fd/99", binary, cfg)


@pytest.mark.parametrize("pin", ["match", "upper", "wrong", "short", "not-string"])
def test_loaded_update_uses_only_a_valid_pin_from_the_private_canonical_config(config, pin):
    cfg, binary, _ = config
    loaded = binary.with_name("loaded.pyz")
    loaded.write_bytes(b"new archive after acknowledgement")
    loaded.chmod(0o700)
    digest = hashlib.sha256(loaded.read_bytes()).hexdigest()
    values = {"match": digest, "upper": digest.upper(), "wrong": "f" * 64, "short": "bad", "not-string": 1}
    cfg.write_text(json.dumps({"format": 1, "update_sha256": values[pin]}))
    policy = binary.parent / "policy.json"
    policy.write_text(json.dumps({"format": 1, "sha256": "a" * 64}))
    policy.chmod(0o600)
    fd = os.open(loaded, os.O_RDONLY)
    try:
        argument = os.fsencode(f"/proc/self/fd/{fd}")
        assert helper.loaded_matches(os.getpid(), argument, binary, cfg) is (pin in {"match", "upper"})
        assert not helper.loaded_matches(os.getpid(), argument, binary, cfg.with_name("other.json"))
        args = [b"python3", b"-I", b"-B", argument, b"--config", os.fsencode(cfg)]
        assert helper.command_matches(args, binary, cfg, os.getpid()) is (pin in {"match", "upper"})
    finally:
        os.close(fd)


@pytest.mark.parametrize("bad", ["symlink", "owner", "mode", "shape"])
def test_loaded_update_refuses_untrusted_config_pins(config, bad):
    cfg, binary, _ = config
    policy = binary.parent / "policy.json"
    policy.write_text(json.dumps({"format": 1, "sha256": "a" * 64}))
    policy.chmod(0o600)
    if bad == "symlink":
        target = cfg.with_name("original.json")
        cfg.rename(target)
        cfg.symlink_to(target)
    elif bad == "owner":
        os.chown(cfg, 10001, 10001)
    elif bad == "mode":
        cfg.chmod(0o644)
    else:
        cfg.write_text("[]")
    with pytest.raises((OSError, ValueError, TypeError)):
        helper.loaded_matches(os.getpid(), b"/proc/self/fd/99", binary, cfg)
