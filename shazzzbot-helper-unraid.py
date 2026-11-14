#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import sys
import time
from pathlib import Path

HOME = None
HOOK = None
MOUNTINFO = Path("/proc/self/mountinfo")
MAX_BYTES = 1024**2
WORK = Path("/tmp/shazzzbot-helper")
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
ENTRY = "shazzzbot-helper-unraid.py"


def mounted(home: Path) -> bool:
    if not home.is_absolute() or len(home.parts) != 5 or home.parts[1] != "mnt" \
            or home.parts[2] in {"user", "user0", "disks", "remotes"} or home.parts[3] != "appdata" \
            or re.fullmatch(r"disk[0-9]+", home.parts[2]) is not None or ".." in home.parts:
        raise ValueError("helper home must be on a real pool under appdata")
    with MOUNTINFO.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("mount information exceeds its size limit")
    pool = str(Path(*home.parts[:3]))
    for row in data.decode().splitlines():
        fields = row.split()
        if len(fields) < 10 or fields[4] != pool or "-" not in fields:
            continue
        index = fields.index("-")
        if len(fields) > index + 3 and fields[index + 1] in {"xfs", "btrfs", "zfs", "ext4"} \
                and "rw" in fields[5].split(",") and "rw" in fields[index + 3].split(","):
            return True
    return False


def directory(path: Path, *, home: bool = False, allow_sticky_leaf: bool = False) -> int:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("invalid helper path")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for index, name in enumerate(path.parts[1:], 1):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                item = os.fstat(child)
                public = home and index in {2, 3}
                leaf = home and index == len(path.parts) - 1
                sticky = (index < len(path.parts) - 1 or allow_sticky_leaf) \
                    and item.st_uid == 0 and item.st_mode & stat.S_ISVTX
                if not public and (item.st_uid != 0 or leaf and stat.S_IMODE(item.st_mode) != 0o700
                                   or not leaf and item.st_mode & 0o022 and not sticky):
                    raise ValueError("helper directory ownership or permissions are invalid")
            except BaseException:
                os.close(child)
                raise
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def read(root: int, name: str) -> tuple[int, bytes]:
    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=root)
    try:
        item = os.fstat(fd)
        if not stat.S_ISREG(item.st_mode) or item.st_uid != 0 or item.st_mode & 0o077:
            raise ValueError("helper file ownership or permissions are invalid")
        with os.fdopen(os.dup(fd), "rb") as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("helper file exceeds its size limit")
        return fd, data
    except BaseException:
        os.close(fd)
        raise


def document(root: int, name: str) -> dict:
    fd, data = read(root, name)
    os.close(fd)
    result = json.loads(data)
    if not isinstance(result, dict):
        raise TypeError("helper document must be an object")
    return result


def header(value: dict, kind: str) -> dict:
    number = value.get("format")
    if type(number) is not int or number < 1:
        raise ValueError(kind + ": invalid format")
    if number != 1:
        raise ValueError(kind + ": written by a newer Shazzzbot")
    return value


def active(root: int, home: Path, hook: Path) -> dict:
    manifest = header(document(root, "helper-unraid.json"), "helper platform manifest")
    entries = manifest.get("files")
    record = entries.get(ENTRY) if isinstance(entries, dict) else None
    if manifest.get("platform") != "unraid" \
            or not isinstance(record, dict) or set(record) != {"sha256", "size"} \
            or not isinstance(record["sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) is None \
            or type(record["size"]) is not int or not 0 < record["size"] <= MAX_BYTES:
        raise ValueError("invalid helper platform manifest")
    entry_fd, source = read(root, ENTRY)
    os.close(entry_fd)
    if len(source) != record["size"] or not hmac.compare_digest(hashlib.sha256(source).hexdigest(), record["sha256"]):
        raise ValueError("helper platform entry does not match its manifest")
    identity = header(document(root, ".identity.json"), "helper identity")
    if set(identity) != {"format", "install_id", "hook", "hook_sha256", "active"} \
            or identity.get("active") is not True or identity.get("hook") != str(hook) \
            or not isinstance(identity.get("install_id"), str) \
            or re.fullmatch(r"[0-9a-f]{32}", identity["install_id"]) is None:
        raise ValueError("helper installation is not active")
    folder = directory(hook.parent)
    try:
        fd, body = read(folder, hook.name)
        os.close(fd)
        digest = identity.get("hook_sha256")
        if not isinstance(digest, str) or not hmac.compare_digest(hashlib.sha256(body).hexdigest(), digest):
            raise ValueError("helper hook no longer matches its identity")
    finally:
        os.close(folder)
    folder = directory(hook.parent.parent.parent)
    try:
        schedule = document(folder, "schedule.json")
    finally:
        os.close(folder)
    if schedule.get("shazzzbot-helper") != {"frequency": "boot", "script": str(hook)}:
        raise ValueError("helper startup hook is not enabled")
    cfg = header(document(root, "config.json"), "helper config")
    if cfg.get("key_file") != str(home / "api.key") or cfg.get("socket") != str(WORK / "socket/helper.sock") \
            or cfg.get("installation_policy") != str(home / "policy.json"):
        raise ValueError("helper configuration leaves its private home")
    fd, _key = read(root, "api.key")
    os.close(fd)
    return cfg


def archive(root: int, home: Path) -> int:
    policy = header(document(root, "policy.json"), "helper policy")
    if set(policy) not in ({"format", "artifact", "sha256", "config"},
                          {"format", "artifact", "sha256", "config", "previous_sha256"}) \
            or policy.get("artifact") != str(home / "helper.pyz") or policy.get("config") != str(home / "config.json"):
        raise ValueError("invalid helper policy")
    pins = [policy[key] for key in ("sha256", "previous_sha256") if key in policy]
    if not all(isinstance(pin, str) and re.fullmatch(r"[0-9a-f]{64}", pin) for pin in pins):
        raise ValueError("invalid helper pin")
    for name in ("helper.pyz", "helper.pyz.prev"):
        try:
            fd, data = read(root, name)
        except (OSError, ValueError):
            continue
        if any(hmac.compare_digest(hashlib.sha256(data).hexdigest(), pin) for pin in pins):
            os.lseek(fd, 0, os.SEEK_SET)
            return fd
        os.close(fd)
    raise ValueError("no verified helper archive is available")


def workdir(gid: int = 10001) -> None:
    if type(gid) is not int or not 0 <= gid <= 4294967294:
        raise ValueError("invalid helper socket group")
    parent = directory(WORK.parent, allow_sticky_leaf=True)
    try:
        try:
            os.mkdir(WORK.name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
        fd = os.open(WORK.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            item = os.fstat(fd)
            if item.st_uid != 0 or item.st_mode & 0o022:
                raise ValueError("helper working directory must be root-owned without group/other writes")
            # Root-owned private work directories require owner traversal. reviewed 2026-11
            # bearer:disable python_lang_file_permissions
            # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
            os.fchmod(fd, 0o700)
            for name, mode in (("runtime", 0o700), ("socket", 0o750)):
                try:
                    os.mkdir(name, mode, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    info = os.fstat(child)
                    if info.st_uid != 0 or info.st_mode & 0o022:
                        raise ValueError("helper work subdirectory is not trusted")
                    os.fchmod(child, mode)
                    if name == "socket":
                        os.fchown(child, 0, gid)
                finally:
                    os.close(child)
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def publish(data: bytes) -> int:
    root = directory(WORK / "runtime")
    name = ".helper." + os.urandom(8).hex()
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700, dir_fd=root)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, "helper.pyz", src_dir_fd=root, dst_dir_fd=root)
        os.fsync(root)
        return os.open("helper.pyz", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root)
    finally:
        try:
            os.unlink(name, dir_fd=root)
        except FileNotFoundError:
            pass
        os.close(root)


def launch(home: Path, hook: Path, timeout: float = 30, options: list[str] | None = None) -> bool:
    if os.geteuid() != 0:
        raise ValueError("helper hook requires root")
    options = options or []
    if options not in ([], ["--check"]) and not (len(options) == 2 and options[0] == "--update"
                                                and re.fullmatch(r"[0-9a-f]{64}", options[1])):
        raise ValueError("invalid helper hook option")
    deadline = time.monotonic() + timeout
    while not mounted(home):
        if time.monotonic() >= deadline:
            return False
        time.sleep(1)
    root = directory(home, home=True)
    fd = None
    try:
        cfg = active(root, home, hook)
        fd = archive(root, home)
        with os.fdopen(os.dup(fd), "rb") as stream:
            data = stream.read(MAX_BYTES + 1)
        os.close(fd)
        fd = None
        workdir(cfg.get("socket_gid", 10001))
        fd = publish(data)
        os.close(root)
        root = None
        os.chdir(WORK)
        os.set_inheritable(fd, True)
        env = {**ENV, "HOME": str(WORK), "TMPDIR": str(WORK), "XDG_CACHE_HOME": str(WORK / "cache"),
               "XDG_CONFIG_HOME": str(WORK / "config"), "XDG_DATA_HOME": str(WORK / "data"),
               "SHAZZZBOT_HELPER_ARCHIVE_FD": str(fd)}
        # Executes the SHA-256-verified archive from a private RAM fd. reviewed 2026-11
        # bearer:disable python_lang_code_injection
        os.execve("/usr/bin/python3", ["/usr/bin/python3", "-I", "-B", f"/proc/self/fd/{fd}",
                  "--config", str(home / "config.json"), *options], env)
        return True
    finally:
        if fd is not None:
            os.close(fd)
        if root is not None:
            os.close(root)


def main() -> None:
    try:
        home = Path(os.path.abspath(__file__)).parent if HOME is None else HOME
        hook = Path("/boot/config/plugins/user.scripts/scripts/shazzzbot-helper/script") if HOOK is None else HOOK
        if not isinstance(home, Path) or not isinstance(hook, Path):
            raise TypeError("helper hook has no installation")
        if not launch(home, hook, options=sys.argv[1:]):
            print("helper storage is not mounted; nothing started", file=sys.stderr)
    except (OSError, ValueError, TypeError, KeyError):
        raise SystemExit("helper hook refused") from None


if __name__ == "__main__":
    main()
