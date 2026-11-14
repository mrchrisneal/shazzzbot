from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import constants, files, formats

MNT = Path("/mnt")
MOUNTINFO = Path("/proc/self/mountinfo")
WORK = Path("/tmp/shazzzbot-helper")  # nosec B108 -- fixed root-only no-follow workdir; reviewed 2026-11
DEFAULT_HOME = Path("/mnt/user/appdata/shazzzbot-helper")
HOOK = Path("/boot/config/plugins/user.scripts/scripts/shazzzbot-helper/script")
SHARE_CONFIG = Path("/boot/config/shares/appdata.cfg")
POOL_TYPES = {"xfs", "btrfs", "zfs", "ext4"}
BUNDLE_FILES = ("shazzzbot-helper-unraid.py", "helper-unraid.json")
CORE_FILES = ("config.json", "api.key", "policy.json", "helper.pyz", *BUNDLE_FILES)
MAX_BYTES = 1024 * 1024


def _read(path: Path, limit: int = MAX_BYTES) -> bytes:
    actual = Path(f"/proc/{os.getpid()}/mountinfo") if path == Path("/proc/self/mountinfo") else path
    with files.trusted_open(actual) as fd, os.fdopen(os.dup(fd), "rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("helper footprint input exceeds its size limit")
    return data


def _unescape(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def _pool_name(name: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name) is not None \
        and name not in {"user", "user0", "disks", "remotes", "addons"} \
        and re.fullmatch(r"disk[0-9]+", name) is None


def mounted_pools() -> dict[str, int]:
    pools = {}
    for line in _read(MOUNTINFO).decode().splitlines():
        before, separator, after = line.partition(" - ")
        fields, filesystem = before.split(), after.split()
        if not separator or len(fields) < 6 or len(filesystem) < 3:
            raise ValueError("invalid mount information")
        path = Path(_unescape(fields[4]))
        if path.parent != MNT or not _pool_name(path.name) or filesystem[0] not in POOL_TYPES \
                or "rw" not in fields[5].split(","):
            continue
        device = fields[2].split(":")
        if len(device) != 2 or not all(re.fullmatch(r"[0-9]{1,9}", part) for part in device):
            raise ValueError("invalid pool device")
        if not re.fullmatch(r"[1-9][0-9]{0,19}", fields[0]):
            raise ValueError("invalid pool mount id")
        if path.name in pools:
            raise ValueError("ambiguous pool mount")
        pools[path.name] = int(fields[0])
    return pools


def _mount_id(fd: int) -> int:
    data = _read(Path(f"/proc/{os.getpid()}/fdinfo/{fd}"), 4096).decode()
    lines = [line for line in data.splitlines(keepends=True) if line.startswith("mnt_id:")]
    if len(lines) != 1:
        raise ValueError("invalid pool descriptor mount id")
    match = re.fullmatch(r"mnt_id:[ \t]*([1-9][0-9]{0,19})\n", lines[0])
    if match is None:
        raise ValueError("invalid pool descriptor mount id")
    return int(match[1])


def _shape(path: Path, *, fuse: bool = False) -> tuple[str, str]:
    if not path.is_absolute() or ".." in path.parts or any(c in str(path) for c in "\n\r\0:,\\"):
        raise ValueError("helper home must be a real pool appdata directory")
    try:
        parts = path.relative_to(MNT).parts
    except ValueError:
        raise ValueError("helper home must be under /mnt/<pool>/appdata") from None
    if len(parts) != 3 or parts[1] != "appdata" or not (_pool_name(parts[0]) or fuse and parts[0] == "user"):
        raise ValueError("helper home must be /mnt/<pool>/appdata/<name>")
    return parts[0], parts[2]


@contextmanager
def pool_open(name: str) -> Iterator[int]:
    mount_id = mounted_pools().get(name)
    if mount_id is None:
        raise ValueError("helper pool is not mounted; no directory was created")
    with files.trusted_open(MNT, directory=True) as parent:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        if _mount_id(fd) != mount_id:
            raise ValueError("helper pool mount changed")
        yield fd
    finally:
        os.close(fd)


def pool_ancestor(path: Path) -> bool:
    try:
        parts = path.relative_to(MNT).parts
    except ValueError:
        return False
    if len(parts) not in {1, 2} or len(parts) == 2 and parts[1] != "appdata" or not _pool_name(parts[0]):
        return False
    with pool_open(parts[0]):
        return True


def resolve_home(requested: Path) -> Path:
    name, leaf = _shape(requested, fuse=True)
    pools = mounted_pools()
    if name != "user":
        with pool_open(name):
            pass
        return requested
    location = ""
    for path in (requested, requested.parent):
        try:
            location = os.getxattr(path, "system.LOCATION").decode().strip()
            break
        except OSError as error:
            if error.errno not in {errno.ENOENT, errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP}:
                raise
    selected = {part for part in re.split(r"[,\s]+", location) if part}
    if location:
        selected = {Path(part).relative_to(MNT).parts[0] if part.startswith(str(MNT) + "/")
                    else part.split("/", 1)[0] for part in selected}
        if not selected <= pools.keys():
            raise ValueError("appdata location does not identify a mounted real pool")
    else:
        selected = set()
        for pool in pools:
            candidate = MNT / pool / "appdata" / leaf
            try:
                with open_home(candidate):
                    selected.add(pool)
            except FileNotFoundError:
                continue
        if not selected:
            try:
                text = _read(SHARE_CONFIG, 65536).decode()
            except FileNotFoundError:
                text = ""
            matches = re.findall(r'^shareCachePool="([A-Za-z0-9_.-]+)"\s*$', text, re.MULTILINE)
            if len(matches) == 1 and matches[0] in pools:
                selected.add(matches[0])
    if len(selected) != 1:
        raise ValueError("appdata pool is ambiguous; set SHAZZZBOT_HELPER_DIR=/mnt/<pool>/appdata/shazzzbot-helper")
    return MNT / selected.pop() / "appdata" / leaf


def home(cfg: dict[str, Any]) -> Path:
    path = Path(cfg["_config"])
    if path.name != "config.json":
        raise ValueError("helper config must be H/config.json")
    _shape(path.parent)
    return path.parent


def fd_path(fd: int) -> Path:
    return Path(f"/proc/self/fd/{fd}")


def validate_paths(cfg: dict[str, Any]) -> None:
    root = home(cfg)
    for key, name in (("key_file", "api.key"), ("log_file", "helper.log"), ("installation_policy", "policy.json")):
        if cfg.get(key) != str(root / name):
            raise ValueError(key + " must stay in the helper home")
    if cfg.get("socket") != str(WORK / "socket/helper.sock"):
        raise ValueError("socket must stay in the fixed helper work directory")


@contextmanager
def open_home(value: Path | dict[str, Any]) -> Iterator[int]:
    root = home(value) if isinstance(value, dict) else value
    pool, leaf = _shape(root)
    with pool_open(pool) as parent:
        appdata = os.open("appdata", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        fd = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=appdata)
    finally:
        os.close(appdata)
    try:
        info = os.fstat(fd)
        if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("helper home must be root-owned mode 0700")
        if isinstance(value, dict) and "_home_inode" in value and value["_home_inode"] != [info.st_dev, info.st_ino]:
            raise ValueError("helper home identity changed")
        yield fd
    finally:
        os.close(fd)


def read_at(fd: int, name: str, limit: int = MAX_BYTES) -> bytes:
    if not name or "/" in name or name in {".", ".."}:
        raise ValueError("invalid helper file name")
    child = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=fd)
    with os.fdopen(child, "rb") as stream:
        info = os.fstat(stream.fileno())
        if info.st_uid != 0 or info.st_mode & 0o077 or not stat.S_ISREG(info.st_mode):
            raise ValueError("helper files must be root-owned and private")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("helper file exceeds its size limit")
    return data


def identity(fd: int) -> dict[str, Any]:
    value = formats.load(json.loads(read_at(fd, ".identity.json", 65536)), "helper identity", 1)
    if set(value) != {"format", "install_id", "hook", "hook_sha256", "active"} \
            or type(value["active"]) is not bool \
            or not isinstance(value["install_id"], str) or re.fullmatch(r"[0-9a-f]{32}", value["install_id"]) is None \
            or value["hook"] != str(HOOK) or not isinstance(value["hook_sha256"], str) \
            or value["hook_sha256"] and re.fullmatch(r"[0-9a-f]{64}", value["hook_sha256"]) is None \
            or value["active"] and not value["hook_sha256"]:
        raise ValueError("invalid helper installation identity")
    return value


def bundle(fd: int) -> dict[str, Any]:
    entry, manifest = BUNDLE_FILES
    value = formats.load(json.loads(read_at(fd, manifest, 65536)), "helper platform manifest", 1)
    if set(value) != {"format", "platform", "version", "revision", "files"} or value["platform"] != "unraid" \
            or not isinstance(value["revision"], str) or re.fullmatch(r"[0-9a-f]{40}", value["revision"]) is None:
        raise ValueError("invalid helper bundle manifest")
    version = value["version"]
    if not isinstance(version, str) or version != "v" + ".".join(map(str, constants.version_number(version))):
        raise ValueError("invalid helper bundle version")
    records = value["files"]
    if not isinstance(records, dict) or set(records) != {"helper.pyz", entry}:
        raise ValueError("invalid helper bundle files")
    for name, record in records.items():
        limit = 65536 if name == entry else MAX_BYTES
        if not isinstance(record, dict) or set(record) != {"sha256", "size"} \
                or not isinstance(record["sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) is None \
                or type(record["size"]) is not int or not 0 < record["size"] <= limit:
            raise ValueError("invalid helper bundle file metadata")
    source = read_at(fd, entry, 65536)
    if len(source) != records[entry]["size"] \
            or not hmac.compare_digest(hashlib.sha256(source).hexdigest(), records[entry]["sha256"]):
        raise ValueError("helper bundle entry changed")
    return value


def initialize(root: Path, install_id: str) -> None:
    if re.fullmatch(r"[0-9a-f]{32}", install_id) is None:
        raise ValueError("invalid helper install id")
    pool, leaf = _shape(root)
    with pool_open(pool) as parent:
        appdata = os.open("appdata", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        try:
            os.mkdir(leaf, 0o700, dir_fd=appdata)
            os.fsync(appdata)
        except FileExistsError:
            pass
    finally:
        os.close(appdata)
    with open_home(root) as fd:
        try:
            existing = identity(fd)
        except FileNotFoundError:
            # Integer FD pins the root-owned no-follow directory. reviewed 2026-11
            # bearer:disable python_lang_path_traversal
            if os.listdir(fd):
                raise ValueError("helper directory contains unidentified files")
            existing = formats.dump({"install_id": install_id, "hook": str(HOOK),
                                     "hook_sha256": "", "active": False}, 1)
            files.write_private(fd_path(fd) / ".identity.json", json.dumps(existing).encode())
        if existing["install_id"] != install_id:
            raise ValueError("helper directory belongs to another installation")


def _hook(value: dict[str, Any]) -> None:
    data = _read(HOOK, 65536)
    if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), value["hook_sha256"]):
        raise ValueError("helper startup hook changed")
    schedule = json.loads(_read(HOOK.parent.parent.parent / "schedule.json", 65536))
    row = schedule.get("shazzzbot-helper") if isinstance(schedule, dict) else None
    if not isinstance(row, dict) or row.get("frequency") != "boot" or row.get("script") != str(HOOK):
        raise ValueError("helper startup hook is not enabled")


def identity_payload(root: Path, install_id: str, hook: Path | None = None, *, active: bool = True) -> bytes:
    if hook is not None and hook != HOOK or type(active) is not bool:
        raise ValueError("invalid helper startup hook")
    with open_home(root) as fd:
        value = identity(fd)
        if value["install_id"] != install_id:
            raise ValueError("helper install identity mismatch")
        value["active"] = active
        value["hook_sha256"] = hashlib.sha256(_read(HOOK, 65536)).hexdigest()
        if active:
            _hook(value)
        return json.dumps(formats.dump(value, 1)).encode()


def record_identity(root: Path, install_id: str, hook: Path | None = None, *, active: bool = True) -> None:
    data = identity_payload(root, install_id, hook, active=active)
    with open_home(root) as fd:
        files.write_private(fd_path(fd) / ".identity.json", data)


def guard(cfg: dict[str, Any], *, active: bool = True) -> None:
    validate_paths(cfg)
    with open_home(cfg) as fd:
        value = identity(fd)
        if "_install_id" in cfg and cfg["_install_id"] != value["install_id"]:
            raise ValueError("helper installation identity changed")
        for name in CORE_FILES:
            if name not in BUNDLE_FILES:
                read_at(fd, name)
        bundle(fd)
        if active:
            if not value["active"]:
                raise ValueError("helper startup hook is not active")
            _hook(value)
    with files.trusted_open(WORK, directory=True) as fd:
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
            raise ValueError("unsafe helper work directory")
    with files.trusted_open(WORK / "socket", directory=True) as fd:
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o750:
            raise ValueError("unsafe helper socket directory")


def _work_child(parent: int, name: str, mode: int, gid: int | None = None) -> None:
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
        os.fsync(parent)
    except FileExistsError:
        pass
    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        files._trusted(os.fstat(child), True)
        # Root-owned socket directories require group traversal, never write access. reviewed 2026-11
        # bearer:disable python_lang_file_permissions
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.fchmod(child, mode)  # nosec B103 -- fixed private work/socket modes; reviewed 2026-11
        if gid is not None:
            os.fchown(child, 0, gid)
        os.fsync(child)
    finally:
        os.close(child)


def ensure_workdir() -> Path:
    with files.trusted_open(WORK.parent.parent, directory=True) as ancestor:
        parent = os.open(WORK.parent.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=ancestor)
    try:
        files._trusted(os.fstat(parent), True, ancestor=True)
        _work_child(parent, WORK.name, 0o700)
    finally:
        os.close(parent)
    with files.trusted_open(WORK, directory=True) as fd:
        _work_child(fd, "runtime", 0o700)
    return WORK


def ensure_socket(gid: int = 10001) -> Path:
    if type(gid) is not int or not 0 <= gid < 2**32 - 1:
        raise ValueError("invalid helper socket group")
    root = ensure_workdir()
    with files.trusted_open(root, directory=True) as fd:
        _work_child(fd, "socket", 0o750, gid)
    return root / "socket"
