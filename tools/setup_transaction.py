from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import time
from contextlib import nullcontext
from pathlib import Path

from shazzzbot.helper import formats
from tools.setup_files import helper_home, opened, read_at, read_safe, trusted, unlink_at, write_at, write_safe

APP_FILES = (".env", "config.toml", "compose.yaml", "compose.relay.yaml", "docker-proxy/haproxy.cfg",
             ".setup-state", ".setup-identity")
HELPER_FILES = ("helper.pyz", "helper.pyz.prev", "config.json", "api.key", "policy.json", ".identity.json",
                "shazzzbot-helper-unraid.py", "helper-unraid.json")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def migrate_config(app: Path, original: bytes, candidate: bytes, version: str, folder: Path) -> bool:
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", version) is None:
        raise ValueError("invalid config backup version")
    with opened(app, directory=True) as parent:
        info = os.stat("config.toml", dir_fd=parent, follow_symlinks=False)
        if info.st_uid not in {0, 10001}:
            raise ValueError("config.toml must be owned by root or uid 10001")
        current, info = read_at(parent, "config.toml", private=True, uid=info.st_uid)
        if current != original:
            raise ValueError("config.toml changed during setup; rerun")
        if candidate == original:
            return False
        value = manifest(folder)
        entries = value.get("files")
        watched = entries.get("app/config.toml") if isinstance(entries, dict) else None
        if value.get("app") != str(app) or not isinstance(watched, dict) \
                or not isinstance(watched.get("before"), dict) \
                or watched["before"].get("sha256") != digest(original) \
                or read_backup(folder, "app/config.toml", None) != original:
            raise ValueError("config migration needs its watched preimage in this installation's transaction")
        name = f"config.toml.bak-{version}"
        try:
            backup = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        except FileExistsError:
            saved, _ = read_at(parent, name, private=True)
            if saved != original:
                raise ValueError("config backup already exists with different contents") from None
        else:
            with os.fdopen(backup, "wb") as stream:
                stream.write(original)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(parent)
        current, _ = read_at(parent, "config.toml", private=True, uid=info.st_uid)
        if current != original:
            raise ValueError("config.toml changed during setup; rerun")
        record_intent(folder, value, "app", "config.toml", candidate)
        current, _ = read_at(parent, "config.toml", private=True, uid=info.st_uid)
        if current != original:
            raise ValueError("config.toml changed during setup; rerun")
        write_at(parent, "config.toml", candidate, uid=info.st_uid)
        return True


def helper_binding():
    if not os.environ.get("SHAZZZBOT_HELPER_HOME"):
        return None
    from tools.setup_helper_transaction import binding

    return binding()


def metadata_root(path: Path, create: bool = False):
    if helper_binding() is not None:
        from tools.setup_helper_transaction import roots

        return roots(path, create=create)
    return opened(path, directory=True, create=create)


def manifest(folder: Path) -> dict:
    scope = helper_binding()
    if scope is not None:
        from tools.setup_helper_transaction import read_manifest

        data = read_manifest(folder)
    else:
        data = read_safe(folder / "manifest.json", private=True)
    value = formats.load(json.loads(data), "backup manifest", 2)
    if scope is not None:
        scope.validate(value)
    return value


def save(folder: Path, value: dict) -> None:
    data = json.dumps(formats.dump(value, 2), sort_keys=True).encode()
    if helper_binding() is not None:
        from tools.setup_helper_transaction import save_manifest

        save_manifest(folder, value, data)
    else:
        write_safe(folder / "manifest.json", data)


def snapshot(app: Path | None, helper: Path, backups: Path, keep: int, bot: str = "", proxy: str = "") -> Path:
    if not 1 <= keep <= 100:
        raise ValueError("backup retention must be 1 to 100")
    folder = backups / (time.strftime("%Y%m%d-%H%M%S") + "." + os.urandom(6).hex())
    scope = helper_binding()
    with metadata_root(backups, create=True) as parent:
        if scope is not None:
            os.mkdir(folder.name, 0o700, dir_fd=parent)
            os.fsync(parent)
        else:
            with opened(folder, directory=True, create=True):
                pass
    helper_inode = None
    if os.environ.get("SHAZZZBOT_HELPER_INSTALLING") == "1":
        from shazzzbot.helper.footprint import identity as helper_identity

        with scope.opened() if scope is not None else helper_home(helper) as fd:
            if helper_identity(fd)["install_id"] != os.environ.get("SHAZZZBOT_INSTALL_ID"):
                raise ValueError("helper directory belongs to another installation")
            info = os.fstat(fd)
            helper_inode = [info.st_dev, info.st_ino]
    save(folder, {"format": 2, "created_ns": time.time_ns(), "app": str(app) if app is not None else None,
                  "helper": str(helper),
                  "helper_inode": helper_inode,
                  "hook": os.environ.get("SHAZZZBOT_HELPER_HOOK_DIR",
                                         "/boot/config/plugins/user.scripts/scripts/shazzzbot-helper"),
                  "files": {},
                  "images": {"bot": bot, "proxy": proxy}})
    candidates = []
    with metadata_root(backups) as parent:
        for name in os.listdir(parent):
            if re.fullmatch(r"[0-9]{8}-[0-9]{6}\.[0-9a-f]{12}", name):
                candidate = backups / name
                try:
                    value = verify_snapshot(candidate, app, helper)
                except formats.FormatError:
                    raise
                except (OSError, ValueError):
                    continue
                if candidate != folder:
                    candidates.append((value["created_ns"], candidate, value))
        for _, old, value in sorted(candidates, reverse=True)[keep - 1:]:
            prune_payload(old, value)
            shutil.rmtree(old.name, dir_fd=parent)
    return folder


def prune_payload(folder: Path, value: dict) -> None:
    if not any(private_area(key.split("/", 1)[0]) for key in value["files"]):
        return
    with lease(value, True) as home:
        home = require_home(home)
        try:
            parent = os.open("backups", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=home)
        except FileNotFoundError:
            return
        try:
            trusted(os.fstat(parent), directory=True)
            try:
                entry = os.open(folder.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            except FileNotFoundError:
                return
            try:
                trusted(os.fstat(entry), directory=True)
            finally:
                os.close(entry)
            shutil.rmtree(folder.name, dir_fd=parent)
        finally:
            os.close(parent)


def target(value: dict, area: str, name: str) -> Path:
    scope = helper_binding()
    if scope is not None:
        scope.validate(value)
        if area == "app" and (scope.app is None or name != ".env"):
            raise ValueError("file is outside the helper-only transaction write set")
    allowed = {"app": APP_FILES, "helper": HELPER_FILES,
               "schedule": ("schedule.json",), "hook": ("script", "name", "description")}.get(area, ())
    if name not in allowed:
        raise ValueError("path is not in the installer write set")
    return Path(value[area]) / name


def lease(value: dict, needed: bool):
    scope = helper_binding()
    if scope is not None:
        scope.validate(value)
        return scope.opened()
    if not needed:
        return nullcontext(None)
    expected = value.get("helper_inode")
    if expected is None:
        raise ValueError("helper operation requires its recorded directory identity")
    return helper_home(Path(value["helper"]), expected)


def private_area(area: str) -> bool:
    return helper_binding() is not None or area in {"helper", "hook", "schedule"}


def require_home(home: int | None) -> int:
    if home is None:
        raise ValueError("helper operation requires a validated directory lease")
    return home


def read_target(home: int | None, path: Path, area: str, name: str) -> tuple[bytes, os.stat_result]:
    private = name in {".env", "config.toml", "config.json", "api.key"}
    if area == "helper":
        return read_at(require_home(home), name, private=private)
    data = read_safe(path, private=private)
    with opened(path, uid=path.lstat().st_uid if name == "config.toml" else None) as fd:
        return data, os.fstat(fd)


def read_backup(folder: Path, key: str, home: int | None) -> bytes:
    if private_area(key.split("/", 1)[0]):
        return read_at(require_home(home), "backups/" + folder.name + "/" + key, private=True)[0]
    return read_safe(folder / key, private=True)


def watch(folder: Path, area: str, name: str) -> None:
    value = manifest(folder)
    path = target(value, area, name)
    key = area + "/" + name
    if key in value["files"]:
        return
    with lease(value, private_area(area)) as home:
        try:
            data, info = read_target(home, path, area, name)
            if private_area(area):
                write_at(require_home(home), "backups/" + folder.name + "/" + key, data)
            else:
                copy = folder / key
                with opened(copy.parent, directory=True, create=True):
                    pass
                write_safe(copy, data)
            entry = {"sha256": digest(data), "uid": info.st_uid, "gid": info.st_gid,
                     "mode": stat.S_IMODE(info.st_mode)}
        except FileNotFoundError:
            entry = None
    value["files"][key] = {"before": entry, "after": entry["sha256"] if entry else None}
    save(folder, value)


def seal(folder: Path, area: str, name: str) -> None:
    value = manifest(folder)
    path = target(value, area, name)
    with lease(value, private_area(area)) as home:
        data, _ = read_target(home, path, area, name)
    value["files"][area + "/" + name]["after"] = digest(data)
    save(folder, value)


def replace(folder: Path, area: str, name: str, data: bytes, uid: int | None = None) -> None:
    watch(folder, area, name)
    value = manifest(folder)
    path = record_intent(folder, value, area, name, data)
    with lease(value, private_area(area)) as home:
        if area == "helper":
            write_at(require_home(home), name, data, uid=uid,
                     mode=0o700 if name in {"helper.pyz", "helper.pyz.prev", "shazzzbot-helper-unraid.py"} else 0o600)
        else:
            write_safe(path, data, uid=uid)


def record_intent(folder: Path, value: dict, area: str, name: str, data: bytes) -> Path:
    path = target(value, area, name)
    value["files"][area + "/" + name]["after"] = digest(data)
    save(folder, value)
    return path


def verify_entries(folder: Path, value: dict, app: Path | None, helper: Path, home: int | None) -> None:
    if value["app"] != (str(app) if app is not None else None) or value["helper"] != str(helper):
        raise ValueError("backup does not match this installation")
    for key, entry in value["files"].items():
        area, name = key.split("/", 1)
        target(value, area, name)
        if entry["before"] and digest(read_backup(folder, key, home)) != entry["before"]["sha256"]:
            raise ValueError("backup verification failed")


def verify_snapshot(folder: Path, app: Path | None, helper: Path) -> dict:
    value = manifest(folder)
    with lease(value, any(private_area(key.split("/", 1)[0]) for key in value["files"])) as home:
        verify_entries(folder, value, app, helper, home)
    return value


def restore_snapshot(folder: Path, app: Path | None, helper: Path, *, helper_changed: bool = False) -> None:
    value = manifest(folder)
    with lease(value, any(private_area(key.split("/", 1)[0]) for key in value["files"])) as home:
        verify_entries(folder, value, app, helper, home)
        for key, entry in value["files"].items():
            area, name = key.split("/", 1)
            if area in {"helper", "hook", "schedule"} and not helper_changed:
                continue
            path = target(value, area, name)
            try:
                current = digest(read_target(home, path, area, name)[0])
            except FileNotFoundError:
                current = None
            before = entry["before"]
            if current not in {entry["after"], before["sha256"] if before else None}:
                raise ValueError(f"{name} changed outside this transaction; refusing to overwrite it")
            if before:
                data = read_backup(folder, key, home)
                if area == "helper":
                    write_at(require_home(home), name, data, uid=before["uid"], gid=before["gid"], mode=before["mode"])
                else:
                    write_safe(path, data, uid=before["uid"])
                    with opened(path, uid=before["uid"]) as fd:
                        os.fchown(fd, before["uid"], before["gid"])
                        os.fchmod(fd, before["mode"])
            elif current is not None:
                if area == "helper":
                    unlink_at(require_home(home), name)
                else:
                    with opened(path.parent, directory=True) as parent:
                        os.unlink(path.name, dir_fd=parent)
                        os.fsync(parent)
