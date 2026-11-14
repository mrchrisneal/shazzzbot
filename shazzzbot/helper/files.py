from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import re
import stat
import tempfile
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def private_read(path: Path, limit: int = 65536) -> bytes:
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if info.st_uid != 0 or info.st_mode & 0o077 or not stat.S_ISREG(info.st_mode):
            raise ValueError("private file must be root-owned with no group/other permissions")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("private file too large")
        return data


def trusted_file(path: Path, optional: bool = False, directory: bool = False) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if optional:
            return False
        raise
    _trusted(info, directory)
    return True


def _trusted(info: os.stat_result, directory: bool, *, ancestor: bool = False) -> None:
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    sticky = directory and ancestor and info.st_mode & stat.S_ISVTX
    if info.st_uid != 0 or info.st_mode & 0o022 and not sticky or not kind(info.st_mode):
        raise ValueError("untrusted root file")


class SourceError(ValueError):
    pass


def _walk(fd: int, parts: tuple[str, ...], directory: bool, follow: bool, *, ancestors: bool = False) -> int:
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
    pending, links = deque(parts), 0
    try:
        _trusted(os.fstat(fd), True, ancestor=bool(parts) and ancestors)
        while pending:
            part = pending.popleft()
            is_dir = bool(pending) or directory
            try:
                child = os.open(part, flags | (os.O_DIRECTORY if is_dir else 0), dir_fd=fd)
            except OSError as error:
                if not follow or error.errno not in (errno.ELOOP, errno.ENOTDIR) or links == 40:
                    raise
                info = os.stat(part, dir_fd=fd, follow_symlinks=False)
                if info.st_uid != 0 or not stat.S_ISLNK(info.st_mode):
                    raise ValueError("untrusted symlink") from None
                target = Path(os.readlink(part, dir_fd=fd))
                if target.is_absolute():
                    child = os.open("/", flags | os.O_DIRECTORY)
                    os.close(fd)
                    fd = child
                    _trusted(os.fstat(fd), True, ancestor=True)
                pending.extendleft(reversed(target.parts[1:] if target.is_absolute() else target.parts))
                ancestors, links = True, links + 1
                continue
            try:
                _trusted(os.fstat(child), is_dir, ancestor=ancestors and bool(pending))
            except BaseException:
                os.close(child)
                raise
            os.close(fd)
            fd = child
        _trusted(os.fstat(fd), directory)
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def trusted_open(root: Path, parts: tuple[str, ...] = (), *, directory: bool = False,
                 follow: bool = False) -> Iterator[int]:
    """Verify source ancestors and open descendants, optionally resolving trusted root-managed links."""
    if not root.is_absolute() or ".." in root.parts \
            or any(not part or part in (".", "..") or "/" in part for part in parts):
        raise ValueError("untrusted root path")
    try:
        fd = _walk(os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW), root.parts[1:],
                   bool(parts) or directory, follow and not parts, ancestors=True)
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as error:
        raise SourceError("untrusted or unreadable source root") from error
    try:
        if parts:
            child = _walk(os.dup(fd), parts, directory, follow)
            try:
                yield child
            finally:
                os.close(child)
        else:
            yield fd
    finally:
        os.close(fd)


def trusted_path(path: Path) -> None:
    with trusted_open(path):
        pass


def runtime(cfg: dict[str, Any]) -> tuple[Path, int]:
    from . import footprint

    path = Path(cfg["socket"])
    if path != footprint.WORK / "socket/helper.sock":
        raise ValueError("unsafe runtime path")
    with trusted_open(footprint.WORK, directory=True) as work_fd:
        directory = os.open("socket", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=work_fd)
    try:
        info = os.fstat(directory)
        if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o750:
            raise ValueError("unsafe runtime directory")
        os.fchown(directory, 0, cfg["socket_gid"])
        fd = os.open("helper.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            info = os.fstat(fd)
            if info.st_uid != 0 or info.st_mode & 0o077 or not stat.S_ISREG(info.st_mode):
                raise ValueError("unsafe lock file")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                info = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISSOCK(info.st_mode):
                    raise ValueError("unsafe socket path")
                os.unlink(path.name, dir_fd=directory)
        except BaseException:
            os.close(fd)
            raise
    finally:
        os.close(directory)
    return path, fd


def process_start(pid: int) -> str:
    if type(pid) is not int or pid <= 1:
        raise ValueError("invalid process id")
    root = os.open("/proc", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        process = os.open(str(pid), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        try:
            fd = os.open("stat", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=process)
            with os.fdopen(fd, "r") as stream:
                return stream.read(4096).rpartition(")")[2].split()[19]
        finally:
            os.close(process)
    finally:
        os.close(root)


def clear_update(parent: Path, result: bool = False, root: Path | None = None) -> None:
    from . import footprint

    root = parent.parent if root is None else root
    if parent != root / "socket":
        raise ValueError("unsafe update directory")
    match = re.fullmatch(r"/proc/self/fd/([0-9]+)", str(root))
    with contextlib.ExitStack() as stack:
        if match:
            directory = os.dup(int(match[1]))
            stack.callback(os.close, directory)
            _trusted(os.fstat(directory), True)
        else:
            if root != footprint.WORK:
                raise ValueError("unsafe update directory")
            directory = stack.enter_context(trusted_open(root, directory=True))
        child = os.open("socket", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        try:
            _trusted(os.fstat(child), True)
            os.unlink("update.result" if result else "update.json", dir_fd=child)
        finally:
            os.close(child)


def fsync_parent(path: Path) -> None:
    match = re.fullmatch(r"/proc/self/fd/([0-9]+)", str(path.parent))
    fd = os.dup(int(match[1])) if match else os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _trusted(os.fstat(fd), True)
        os.fsync(fd)
    finally:
        os.close(fd)


def write_private(path: Path, data: bytes, mode: int = 0o600) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".helper-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, str(path))
        fsync_parent(path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
