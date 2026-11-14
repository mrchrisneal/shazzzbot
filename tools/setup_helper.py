from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import time
from pathlib import Path

from shazzzbot.helper import formats
from tools.setup_files import read_safe


def action_limit() -> int:
    from shazzzbot.helper.constants import MAX_ACTION_TIMEOUT

    return MAX_ACTION_TIMEOUT


def loaded_matches(pid: int, argument: bytes, binary: Path, config: Path) -> bool:
    match = re.fullmatch(rb"/proc/self/fd/([0-9]{1,7})", argument)
    if match is None:
        return False
    if config != binary.parent / "config.json":
        return False
    policy = formats.load(json.loads(read_safe(binary.parent / "policy.json", private=True)), "helper policy", 1)
    pins = [policy.get("sha256"), policy.get("previous_sha256")]
    cfg = formats.load(json.loads(read_safe(config, private=True)), "helper config", 1)
    update_pin = cfg.get("update_sha256")
    if isinstance(update_pin, str) and re.fullmatch(r"[0-9a-fA-F]{64}", update_pin):
        pins.append(update_pin.lower())
    fd = os.open(f"/proc/{pid}/fd/{int(match[1])}", os.O_RDONLY | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if info.st_uid != 0 or info.st_mode & 0o077 or not stat.S_ISREG(info.st_mode):
            return False
        with os.fdopen(os.dup(fd), "rb") as stream:
            data = stream.read(1024**2 + 1)
        digest = hashlib.sha256(data).hexdigest()
        return len(data) <= 1024**2 and digest in pins
    finally:
        os.close(fd)


def command_matches(argv: list[bytes], binary: Path, config: Path, pid: int | None = None) -> bool:
    if not argv or re.fullmatch(rb"python[0-9.]*", os.path.basename(argv[0])) is None:
        return False
    index = 1
    while index < len(argv) and argv[index] in {b"-I", b"-B", b"-u"}:
        index += 1
    args = [part for part in argv[index:] if part]
    if args and args[0] == os.fsencode(binary):
        return args[1:] in ([], [b"--config", os.fsencode(config)])
    if args and pid is not None and args[1:] == [b"--config", os.fsencode(config)]:
        return loaded_matches(pid, args[0], binary, config)
    if len(args) == 6 and args[0] == b"-c":
        from shazzzbot.helper.updates import REEXEC

        cfg = formats.load(json.loads(read_safe(config)), "helper config", 1)
        return (args[1] == REEXEC.encode() and args[2] == os.fsencode(binary) and args[3] == os.fsencode(config)
                and re.fullmatch(rb"[0-9a-f]{64}", args[4]) is not None
                and args[5] == os.fsencode(Path(cfg["socket"]).parent / "update.result"))
    return False


def check_unregistered(binary: Path, config: Path, operation: str) -> None:
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or entry.name == str(os.getpid()):
            continue
        try:
            cmd = (entry / "cmdline").read_bytes().split(b"\0")
            root = entry.stat().st_uid == 0
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        if entry.name == str(os.getppid()) and os.path.basename(cmd[0]) == b"timeout":
            continue
        if root and (os.fsencode(binary) in cmd or command_matches(cmd, binary, config, int(entry.name))):
            if operation == "signal":
                raise ValueError("helper is running without its trusted pidfile; refusing to signal it")
            print(1)
            return


def process(config: Path, binary: Path, operation: str) -> None:
    cfg = formats.load(json.loads(read_safe(config, private=True)), "helper config", 1)
    if not isinstance(cfg.get("socket"), str) or not Path(cfg["socket"]).is_absolute():
        raise ValueError("invalid helper socket identity")
    runtime = Path(cfg["socket"]).parent
    try:
        item = json.loads(read_safe(runtime / "helper.pid", private=True))
    except FileNotFoundError:
        check_unregistered(binary, config, operation)
        return
    if not isinstance(item, dict):
        raise TypeError("invalid helper pidfile")
    pid = item.get("pid")
    timeout = item.get("action_timeout")
    if type(pid) is not int or not 1 < pid < 2**31 \
            or type(timeout) is not int or not 1 <= timeout <= action_limit():
        raise ValueError("invalid active helper pidfile or drain timeout")
    try:
        handle = os.pidfd_open(pid)
    except ProcessLookupError:
        check_unregistered(binary, config, operation)
        return
    try:
        info = Path(f"/proc/{pid}").stat()
        start = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[19]
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        if info.st_uid != 0 or start != item.get("start") or item.get("path") != str(binary) \
                or not command_matches(cmd, binary, config, pid):
            raise ValueError("helper process identity mismatch")
        if operation == "signal":
            signal.pidfd_send_signal(handle, signal.SIGTERM)
        print(timeout + 30)
    finally:
        os.close(handle)


def start(hook: Path, config: Path, binary: Path, timeout: float = 30) -> None:
    cfg = formats.load(json.loads(read_safe(config, private=True)), "helper config", 1)
    drain = cfg.get("action_timeout", 300)
    if type(drain) is not int or not 1 <= drain <= action_limit():
        raise ValueError("invalid helper drain timeout")
    def interrupted(signum: int, frame: object) -> None:
        raise InterruptedError("helper startup interrupted")

    previous = signal.signal(signal.SIGTERM, interrupted)
    child = None
    ready = False
    try:
        child = subprocess.Popen(["/bin/bash", str(hook)],
                                 env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8",
                                      "HOME": "/tmp/shazzzbot-helper", "TMPDIR": "/tmp/shazzzbot-helper",
                                      "XDG_CACHE_HOME": "/tmp/shazzzbot-helper/cache",
                                      "XDG_CONFIG_HOME": "/tmp/shazzzbot-helper/config",
                                      "XDG_DATA_HOME": "/tmp/shazzzbot-helper/data"},
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True, close_fds=True)
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if child.poll() is not None:
                raise ValueError("helper exited before readiness")
            try:
                item = json.loads(read_safe(Path(cfg["socket"]).parent / "helper.pid", private=True))
            except FileNotFoundError:
                time.sleep(0.1)
                continue
            if item.get("pid") != child.pid or item.get("path") != str(binary):
                raise ValueError("unexpected helper process during startup")
            process(config, binary, "helper")
            ready = True
            return
        raise ValueError("helper startup timed out")
    finally:
        signal.signal(signal.SIGTERM, previous)
        if not ready and child is not None and child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=drain + 30)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)
