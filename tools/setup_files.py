from __future__ import annotations

import json
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path

from shazzzbot.helper import formats

MAX_FILE = 16 * 1024 * 1024


def trusted(info: os.stat_result, *, directory: bool, ancestor: bool = False, uid: int | None = None) -> None:
    owners = {0} if uid is None else {uid}
    sticky = ancestor and directory and info.st_uid == 0 and info.st_mode & stat.S_ISVTX
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if info.st_uid not in owners or info.st_mode & 0o022 and not sticky or not kind(info.st_mode):
        raise ValueError(f"untrusted path: owner must be uid {sorted(owners)}, with no group/other write bits")


@contextmanager
def opened(path: Path, *, directory: bool = False, create: bool = False, uid: int | None = None):
    if not path.is_absolute() or ".." in path.parts or any(c in str(path) for c in "\n\r\x00"):
        raise ValueError("path must be absolute without parent traversal or control characters")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        trusted(os.fstat(fd), directory=True, ancestor=True, uid=0)
        for i, part in enumerate(path.parts[1:]):
            is_dir = i < len(path.parts) - 2 or directory
            location = Path(*path.parts[:i + 2])
            pool_parent = False
            if location.parts[:1] == ("/",) and len(location.parts) in {3, 4} and location.parts[1] == "mnt":
                from shazzzbot.helper.footprint import pool_ancestor

                pool_parent = pool_ancestor(location)
            try:
                child = os.open(part, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW |
                                (os.O_DIRECTORY if is_dir else 0), dir_fd=fd)
            except FileNotFoundError:
                if not create or not is_dir:
                    raise
                os.mkdir(part, 0o700, dir_fd=fd)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                info = os.fstat(child)
                if pool_parent:
                    if not is_dir or not stat.S_ISDIR(info.st_mode):
                        raise ValueError("pool ancestors must be directories")
                else:
                    trusted(info, directory=is_dir, ancestor=i < len(path.parts) - 2,
                            uid=uid if i == len(path.parts) - 2 else None)
                    if len(location.parts) == 5 and location.parts[1] == "mnt" \
                            and location.parts[3] == "appdata" and info.st_mode & 0o077:
                        raise ValueError("appdata home must be root-owned with mode 0700")
            except BaseException:
                os.close(child)
                raise
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


@contextmanager
def helper_home(path: Path, expected: list[int] | None = None):
    from shazzzbot.helper import footprint

    with footprint.open_home(path) as fd:
        info = os.fstat(fd)
        if expected is not None and (not isinstance(expected, list) or len(expected) != 2
                or any(type(value) is not int or value < 0 for value in expected)
                or expected != [info.st_dev, info.st_ino]):
            raise ValueError("helper home identity changed")
        yield fd


@contextmanager
def _relative_parent(rootfd: int, relative: str, *, create: bool = False):
    if not isinstance(relative, str) or any(c in relative for c in "\n\r\x00") \
            or any(part in {"", ".", ".."} for part in relative.split("/")):
        raise ValueError("path must be relative without parent traversal or control characters")
    parts = relative.split("/")
    fd = os.dup(rootfd)
    try:
        trusted(os.fstat(fd), directory=True)
        for part in parts[:-1]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o700, dir_fd=fd)
                os.fsync(fd)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                trusted(os.fstat(child), directory=True)
            except BaseException:
                os.close(child)
                raise
            os.close(fd)
            fd = child
        yield fd, parts[-1]
    finally:
        os.close(fd)


def _read_fd(fd: int, *, private: bool = False, uid: int | None = None) -> tuple[bytes, os.stat_result]:
    info = os.fstat(fd)
    trusted(info, directory=False, uid=uid)
    if private and info.st_mode & 0o077:
        raise ValueError("secret file must be root-owned (config.toml: uid 10001), mode 0600 (0700 on vfat)")
    if info.st_size > MAX_FILE:
        raise ValueError("file exceeds the size limit")
    with os.fdopen(os.dup(fd), "rb") as stream:
        data = stream.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise ValueError("file exceeds the size limit")
    return data, info


def read_at(rootfd: int, relative: str, private: bool = False,
            uid: int | None = None) -> tuple[bytes, os.stat_result]:
    with _relative_parent(rootfd, relative) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
        try:
            return _read_fd(fd, private=private, uid=uid)
        finally:
            os.close(fd)


def read_safe(path: Path, *, uid: int | None = None, private: bool = False) -> bytes:
    if uid is None and path.name == "config.toml":
        uid = path.lstat().st_uid
        if uid not in {0, 10001}:
            raise ValueError("config.toml must be owned by root or uid 10001")
    with opened(path, uid=uid) as fd:
        return _read_fd(fd, private=private, uid=uid)[0]


def write_at(rootfd: int, relative: str, data: bytes, uid: int | None = None,
             gid: int | None = None, mode: int = 0o600) -> None:
    if any(value is not None and (type(value) is not int or not 0 <= value < 2**32 - 1) for value in (uid, gid)) \
            or type(mode) is not int or mode & ~0o777 or mode & 0o022:
        raise ValueError("invalid file ownership or mode")
    with _relative_parent(rootfd, relative, create=True) as (parent, name):
        tmp = ".setup-" + os.urandom(12).hex()
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                if uid is not None or gid is not None:
                    os.fchown(stream.fileno(), -1 if uid is None else uid, -1 if gid is None else gid)
                os.fchmod(stream.fileno(), mode)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(tmp, dir_fd=parent)
            except FileNotFoundError:
                pass


def write_safe(path: Path, data: bytes, *, uid: int | None = None) -> None:
    with opened(path.parent, directory=True) as parent:
        write_at(parent, path.name, data, uid=uid)


def unlink_at(rootfd: int, relative: str) -> None:
    with _relative_parent(rootfd, relative) as (parent, name):
        os.unlink(name, dir_fd=parent)
        os.fsync(parent)


def trusted_code(root: Path) -> None:
    def scan(fd: int) -> None:
        for name in os.listdir(fd):
            child = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=fd)
            try:
                info = os.fstat(child)
                directory = stat.S_ISDIR(info.st_mode)
                trusted(info, directory=directory)
                if directory:
                    scan(child)
            finally:
                os.close(child)
    for name in (".git", "Dockerfile", ".dockerignore", "shazzzbot", "tools", "docker-proxy", "assets",
                 "compose.yaml", "compose.relay.yaml", "requirements.txt", "helper.example.json"):
        path = root / name
        try:
            directory = path.is_dir()
            with opened(path, directory=directory) as fd:
                if directory:
                    scan(fd)
        except FileNotFoundError:
            continue



def inspect_file(path: Path, *, private: bool = False, uid: int | None = None) -> None:
    with opened(path, uid=uid) as fd:
        if private and os.fstat(fd).st_mode & 0o077:
            owner = uid if uid is not None else 0
            raise ValueError(f"{path.name} must be owned by uid {owner} with mode 0600 (0700 on vfat)")


def host_identity(path: Path | None = None) -> str:
    path = path or Path(os.environ.get("SHAZZZBOT_MACHINE_ID", "/etc/machine-id"))
    value = read_safe(path).decode().strip()
    if not re.fullmatch(r"[0-9a-f]{32}", value) or value == "0" * 32:
        raise ValueError("a valid /etc/machine-id is required")
    return value


def install_identity(machine: str, app: Path, backups: Path | None = None) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", machine):
        raise ValueError("invalid machine id")
    try:
        value = formats.load(json.loads(read_safe(app / ".setup-identity", private=True)), "install identity", 2)
    except FileNotFoundError:
        pass
    except OSError as error:
        raise ValueError("untrusted install identity path") from error
    else:
        if value.get("host") != machine or value.get("app") != str(app):
            raise ValueError("host or installation identity mismatch")
        identifier = value.get("install_id")
        if not isinstance(identifier, str) or re.fullmatch(r"[0-9a-f]{32}", identifier) is None:
            raise ValueError("invalid install id")
        return identifier
    if backups is not None:
        try:
            saved = formats.load(json.loads(read_safe(backups / "install-identity.json", private=True)),
                                 "saved install identity", 1)
        except FileNotFoundError:
            pass
        else:
            if saved.get("host") != machine or not isinstance(saved.get("app"), str) \
                    or not isinstance(saved.get("install_id"), str) \
                    or re.fullmatch(r"[0-9a-f]{32}", saved["install_id"]) is None:
                raise ValueError("saved install identity mismatch")
            if saved["app"] == str(app):
                return saved["install_id"]
    return os.urandom(16).hex()


def verify_pin(value: dict, digest: str) -> None:
    import hmac

    pins = [value.get("sha256"), value.get("previous_sha256")]
    if not any(isinstance(pin, str) and re.fullmatch(r"[0-9a-f]{64}", pin) and hmac.compare_digest(pin, digest)
               for pin in pins):
        raise ValueError("helper SHA-256 mismatch; refusing to execute")


def select_pinned_helper(home: Path, destination: Path, expected: list[int]) -> str:
    import hashlib

    from shazzzbot.helper import footprint
    from shazzzbot.helper.constants import MAX_UPDATE

    if destination.name != "rollback.pyz" or not destination.is_relative_to(footprint.WORK):
        raise ValueError("helper rollback staging must stay in its private work directory")
    with opened(destination.parent, directory=True) as stage:
        if os.fstat(stage).st_mode & 0o077:
            raise ValueError("helper rollback staging directory must be private")
        with helper_home(home, expected) as source:
            policy = formats.load(json.loads(read_at(source, "policy.json", private=True)[0]), "helper policy", 1)
            required = {"format", "artifact", "config", "sha256"}
            if set(policy) not in (required, required | {"previous_sha256"}) \
                    or policy["artifact"] != str(home / "helper.pyz") \
                    or policy["config"] != str(home / "config.json") \
                    or any(not isinstance(policy[key], str) or re.fullmatch(r"[0-9a-f]{64}", policy[key]) is None
                           for key in ("sha256", "previous_sha256") if key in policy):
                raise ValueError("invalid helper rollback policy")
            for name in ("helper.pyz", "helper.pyz.prev"):
                try:
                    data = read_at(source, name, private=True)[0]
                    if not 1 <= len(data) <= MAX_UPDATE:
                        raise ValueError("helper archive exceeds its size limit")
                    digest = hashlib.sha256(data).hexdigest()
                    verify_pin(policy, digest)
                except (OSError, ValueError):
                    continue
                write_at(stage, destination.name, data, mode=0o700)
                return digest
    raise ValueError("no pinned helper archive is available for rollback")
