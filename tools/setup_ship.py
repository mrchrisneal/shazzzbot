from __future__ import annotations

import fcntl
import os
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

MAX_BYTES = 64 * 1024**2
MAX_FILES = 10000
READ_SECONDS = 120
INSTALL_SECONDS = 3600
RECOVERY_SECONDS = 330
KILL_SECONDS = 10
LOG_SECONDS = INSTALL_SECONDS + RECOVERY_SECONDS + KILL_SECONDS + 60
MAX_LOG = 1024**2
PROC = Path("/proc")
RAM = Path("/tmp/shazzzbot-helper")


class Retained(RuntimeError):
    pass


@dataclass
class Cancellation:
    count: int = 0
    first: int = 0

    def receive(self, signum, _frame) -> None:
        self.count += 1
        if not self.first:
            self.first = signum


@contextmanager
def signals(cancel: Cancellation):
    previous = {number: signal.signal(number, cancel.receive) for number in
                (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def emit(data: bytes, fd: int = 1) -> None:
    try:
        original = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, original | os.O_NONBLOCK)
    except OSError:
        return
    try:
        while data:
            sent = os.write(fd, data)
            if sent == 0:
                break
            data = data[sent:]
    except OSError:
        pass
    finally:
        try:
            fcntl.fcntl(fd, fcntl.F_SETFL, original)
        except OSError:
            pass


def drain(stream: BinaryIO, output: BinaryIO) -> None:
    deadline, size = time.monotonic() + LOG_SECONDS, 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([stream], [], [], remaining)[0]:
            return
        data = os.read(stream.fileno(), 65536)
        if not data:
            return
        if size < MAX_LOG:
            block = data[:MAX_LOG - size]
            size += len(block)
            try:
                output.write(block)
                output.flush()
            except OSError:
                size = MAX_LOG


def progress(stream: BinaryIO) -> None:
    try:
        while block := stream.read(65536):
            emit(block)
    except OSError:
        pass


def group_alive(group: int) -> bool:
    deadline = time.monotonic() + 1
    for index, path in enumerate(PROC.iterdir()):
        if index >= 65536 or time.monotonic() >= deadline:
            return True
        if not path.name.isdecimal():
            continue
        try:
            fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
        except FileNotFoundError:
            continue
        except (OSError, IndexError):
            return True
        if len(fields) < 4:
            return True
        if fields[2:4] == [str(group), str(group)] and fields[0] not in {"Z", "X"}:
            return True
    return False


def send(group: int, number: int) -> None:
    try:
        os.killpg(group, number)
    except ProcessLookupError:
        pass


def monitor(child: subprocess.Popen, cancel: Cancellation, log: BinaryIO) -> int:
    deadline, stop, kill = time.monotonic() + INSTALL_SECONDS, None, None
    while True:
        progress(log)
        exited = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if exited is not None and not group_alive(child.pid):
            status = child.wait(timeout=5)
            return 128 + cancel.first if cancel.first else 124 if stop is not None else status
        now = time.monotonic()
        if stop is None and (cancel.count or now >= deadline):
            send(child.pid, signal.SIGTERM)
            stop = now + RECOVERY_SECONDS
        if stop is not None and kill is None and (cancel.count > 1 or now >= stop):
            send(child.pid, signal.SIGKILL)
            kill = now + KILL_SECONDS
        if kill is not None and now >= kill:
            raise Retained("installer group did not exit after termination")
        time.sleep(0.1)


def finish_logger(logger: subprocess.Popen) -> None:
    try:
        logger.wait(timeout=5)
    except subprocess.TimeoutExpired:
        logger.terminate()
        try:
            logger.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.kill()
            try:
                logger.wait(timeout=5)
            except subprocess.TimeoutExpired:
                raise Retained("installer log drainer did not exit") from None


def execute(command: list[str], root: Path) -> int:
    cancel, retained = Cancellation(), False
    fd = os.open(root / "installer.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as output, (root / "installer.log").open("rb") as log, signals(cancel):
        logger = subprocess.Popen([sys.executable, "-I", "-B", str(root / "source/tools/setup_ship.py"), "--drain"],
                                  stdin=subprocess.PIPE, stdout=output, stderr=subprocess.DEVNULL,
                                  env=environment(), start_new_session=True, close_fds=True)
        try:
            if cancel.count:
                return 128 + cancel.first
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=logger.stdin, stderr=subprocess.STDOUT,
                                     env=environment(), start_new_session=True, close_fds=True)
            logger.stdin.close()
            try:
                status = monitor(child, cancel, log)
            except (Retained, ChildProcessError):
                retained = True
                raise
            except BaseException:
                cancel.receive(signal.SIGTERM, None)
                try:
                    monitor(child, cancel, log)
                except BaseException:
                    retained = True
                    raise
                raise
            finish_logger(logger)
            progress(log)
            return status
        finally:
            logger.stdin.close()
            if not retained:
                finish_logger(logger)
            if retained:
                raise Retained("installer state is uncertain; keep staging at " + str(root)) from None


def environment() -> dict[str, str]:
    return {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root",
            "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "DOCKER_HOST": "unix:///var/run/docker.sock"}


def arguments(args: list[str]) -> tuple[Path, str, str]:
    if len(args) != 3:
        raise ValueError("expected install directory, full revision, and role")
    logical, revision, role = Path(args[0]), args[1], args[2]
    if not logical.is_absolute() or logical == Path("/") or ".." in logical.parts \
            or any(c in str(logical) for c in "\n\r\0:,\\"):
        raise ValueError("invalid logical install directory")
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None or role not in {"bot", "relay"}:
        raise ValueError("invalid ship revision or role")
    return logical, revision, role


def receive(stream: BinaryIO, output: BinaryIO) -> None:
    deadline, size = time.monotonic() + READ_SECONDS, 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([stream], [], [], remaining)[0]:
            raise TimeoutError("ship upload timed out")
        block = os.read(stream.fileno(), 65536)
        if not block:
            return
        size += len(block)
        if size > MAX_BYTES:
            raise ValueError("ship upload exceeds its size limit")
        output.write(block)


def extract(archive: Path, root: Path) -> None:
    count, size = 0, 0
    with tarfile.open(archive, "r|") as source:
        for item in source:
            count += 1
            parts = Path(item.name).parts
            if count > MAX_FILES or not parts or parts[0] not in {"source", ".ship"} \
                    or ".." in parts or any(c in item.name for c in "\n\r\0\\") \
                    or not (item.isdir() or item.isfile()):
                raise ValueError("unsafe ship archive entry")
            size += item.size
            if item.size < 0 or size > MAX_BYTES:
                raise ValueError("ship archive exceeds its size limit")
            target = root.joinpath(*parts)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if item.isdir():
                target.mkdir(mode=0o700, exist_ok=True)
            else:
                body = source.extractfile(item)
                if body is None:
                    raise ValueError("ship archive entry has no body")
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as output, body:
                    while block := body.read(65536):
                        output.write(block)
    for name in ("source/setup.sh", "source/tools/setup_ship.py", ".ship/git.pack"):
        if not (root / name).is_file():
            raise ValueError("ship archive is incomplete")


def deploy(logical: Path, revision: str, role: str, root: Path) -> int:
    command = ["bash", str(root / "source/setup.sh"), "--role", role,
               "--method", "compose-build", "--dir", str(logical), "--ship-pack", str(root / ".ship/git.pack"),
               "--ship-revision", revision, "--config-source", str(root / ".ship"), "--no-helper", "-y"]
    return execute(command, root)


def prepare_work() -> None:
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in RAM.parent.parts[1:]:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            item = os.fstat(fd)
            if item.st_uid != 0 or item.st_mode & 0o022 and not item.st_mode & stat.S_ISVTX:
                raise ValueError("ship staging ancestors must be trusted")
        try:
            os.mkdir(RAM.name, 0o700, dir_fd=fd)
        except FileExistsError:
            pass
        child = os.open(RAM.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            item = os.fstat(child)
            if item.st_uid != 0 or stat.S_IMODE(item.st_mode) != 0o700:
                raise ValueError("ship staging requires a trusted RAM directory")
        finally:
            os.close(child)
    finally:
        os.close(fd)


def run(args: list[str], stream: BinaryIO) -> int:
    logical, revision, role = arguments(args)
    if os.geteuid() != 0:
        raise ValueError("shipping requires root on the target")
    prepare_work()
    root, retained = Path(tempfile.mkdtemp(prefix="shazzzbot-ship.", dir=RAM)), False
    try:
        root.chmod(0o700)
        archive = root / "upload.tar"
        fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as output:
            receive(stream, output)
        extract(archive, root)
        return deploy(logical, revision, role, root)
    except Retained:
        retained = True
        emit(("ship failed; staging retained at " + str(root) + "\n").encode(), 2)
        raise
    finally:
        if not retained:
            shutil.rmtree(root)


def main() -> None:
    if sys.argv[1:] == ["--drain"]:
        drain(sys.stdin.buffer, sys.stdout.buffer)
        return
    try:
        status = run(sys.argv[1:], sys.stdin.buffer)
    except (OSError, ValueError, Retained, tarfile.TarError, subprocess.SubprocessError):
        emit(b"ship failed; inspect the installer result; no public source was executed\n", 2)
        status = 1
    raise SystemExit(status)


if __name__ == "__main__":
    main()
