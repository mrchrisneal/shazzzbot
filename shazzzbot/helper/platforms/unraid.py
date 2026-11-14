from __future__ import annotations

import asyncio
import configparser
import fnmatch
import os
import re
import shlex
from pathlib import Path
from typing import Any

from .. import files, process, security
from ..constants import MAX_UPDATE
from . import ups_shutdown
from .base import Query

BIN = {"vm": "/usr/bin/virsh", "container": "/usr/bin/docker", "stack": "/usr/bin/docker",
       "parity": "/usr/local/sbin/mdcmd", "smart": "/usr/sbin/smartctl"}


OPS: dict[str, dict[str, Any]] = {
    "vm": {"start": "start", "shutdown": "shutdown", "restart": "reboot", "pause": "suspend",
              "resume": "resume", "force-stop": "destroy"},
       "stack": {v: v for v in ("up", "down", "pull", "restart")},
       "container": {v: v for v in ("start", "stop", "restart")},
       "parity": {"check": ("check", "CORRECT"), "nocorrect": ("check", "NOCORRECT"),
                  "pause": ("nocheck", "PAUSE"), "resume": ("check", "RESUME"), "cancel": ("nocheck",)},
       "smart": {"short": "short", "long": "long"}, "script": {"run": "run"}, "userscript": {"run": "run"}}


PROJECTS = Path("/boot/config/plugins/compose.manager/projects")


USER_SCRIPTS = Path("/boot/config/plugins/user.scripts/scripts")


DISKS = Path("/var/local/emhttp/disks.ini")
NFS_EXPORTS = Path("/etc/exports")
NFS_FOLDER = Path("/etc/exports.d")

TASK_SCRIPTS = Path(
  "/tmp/user.scripts"  # nosec B108 -- fixed Unraid state path, opened via root-owned no-follow walk; reviewed 2026-11
)
TASK_CHECK = Path(
  "/tmp/unraidcheck"  # nosec B108 -- fixed Unraid state path, opened via root-owned no-follow walk; reviewed 2026-11
)
TASK_BACKUP = Path(
  "/tmp/appdata.backup"  # nosec B108 -- fixed Unraid state path, opened via root-owned no-follow walk; reviewed 2026-11
)

TASK_PATHS = {"cron.d": Path("/etc/cron.d"), "crontabs": Path("/var/spool/cron/crontabs"),
              "user.scripts": Path("/boot/config/plugins/user.scripts"),
              "user.scripts.state": TASK_SCRIPTS, "unraidcheck": TASK_CHECK,
              "appdata.backup": TASK_BACKUP, "parity": Path("/boot/config"),
              "localtime": Path("/etc/localtime"), "emhttp": Path("/var/local/emhttp"),
              **{"cron." + period: Path("/etc/cron." + period) for period in ("hourly", "daily", "weekly", "monthly")}}






def flash_writable(text: str, target: Path = Path("/boot")) -> bool:
    ini = configparser.ConfigParser(interpolation=None, delimiters=("=",))  # smb.conf keys may contain ":"
    try:
        ini.read_string(text)
    except configparser.Error:
        raise ValueError("SMB configuration unavailable") from None
    sections = {name.lower(): ini[name] for name in ini.sections()}
    if "global" not in sections:
        raise ValueError("SMB configuration unavailable")
    defaults = sections["global"]
    wide_links = any(share.get("wide links", "no").lower() == "yes" for share in sections.values())
    boot = target
    for name, share in sections.items():
        if name == "global":
            continue
        if share.get("available", defaults.get("available", "yes")).lower() == "no":
            continue
        readonly = share.get("read only", defaults.get("read only", "yes")).lower()
        writable = (readonly != "yes" or bool(share.get("write list", defaults.get("write list", "")).strip())
                    or bool(share.get("admin users", defaults.get("admin users", "")).strip()))
        if wide_links and writable and share.get("force user", defaults.get("force user", "")).strip('"') == "root":
            return True
        location = share.get("path", defaults.get("path", "")).strip().strip('"')
        if location and (not Path(location).is_absolute() or "%" in location):
            raise ValueError("SMB share path unavailable")
        path = Path(location).resolve() if location else None
        if not (name == "flash" and boot == Path("/boot")) and not (path is not None and
                (path == boot or boot in path.parents or path in boot.parents)):
            continue
        if writable:
            return True
    return False


def nfs_writable(text: str) -> bool:
    for line in text.splitlines():
        fields = shlex.split(line, comments=True)
        if not fields:
            continue
        path = Path(fields[0])
        if not path.is_absolute():
            raise ValueError("NFS export path unavailable")
        if path == Path("/boot") or Path("/boot") in path.parents or path in Path("/boot").parents:
            for client in fields[1:]:
                if "(" not in client or not client.endswith(")"):
                    raise ValueError("NFS export options unavailable")
                if "rw" in client.split("(", 1)[1][:-1].split(","):
                    return True
    return False


def nfs_flash_writable() -> bool:
    locations = [NFS_EXPORTS]
    try:
        with files.trusted_open(NFS_FOLDER, directory=True) as fd:
            names = os.listdir(fd)
            if len(names) > 1024:
                raise ValueError("too many NFS export files")
            locations.extend(NFS_FOLDER / name for name in sorted(names) if name.endswith(".exports"))
    except FileNotFoundError:
        pass
    size, writable = 0, False
    for path in locations:
        try:
            with files.trusted_open(path) as fd, os.fdopen(os.dup(fd), "rb") as stream:
                data = stream.read(MAX_UPDATE + 1)
        except FileNotFoundError:
            continue
        size += len(data)
        if size > MAX_UPDATE:
            raise ValueError("NFS exports exceed the size limit")
        writable = nfs_writable(data.decode()) or writable
    return writable


async def flash_is_writable() -> bool:
    rc, lines, truncated = await process.execute(["/usr/bin/testparm", "-s"], 5, MAX_UPDATE, errors=False)
    if rc or truncated:
        raise ValueError("flash SMB configuration unavailable")
    smb, nfs = flash_writable("\n".join(lines)), nfs_flash_writable()
    return smb or nfs


async def ensure_flash_safe(accept_flash_share: bool = False) -> None:
    if await flash_is_writable() and not accept_flash_share:
        raise ValueError("flash SMB/NFS export must be unavailable or read-only without write overrides")


def checked_compose(path: Path) -> None:
    files.trusted_path(path)
    with path.open("rb") as stream:
        data = stream.read(MAX_UPDATE + 1)
    text = data.decode("utf-8-sig")
    if len(data) > MAX_UPDATE or "\\" in text or "\0" in text or "!" in text\
            or re.search(r"include|extends|env_file|build|dockerfile|configs|secrets|label_file", text):
        raise ValueError("compose external files or encoded YAML are forbidden")



class Unraid:
    operations = OPS
    readonly = frozenset({"tasks", "ups_shutdown"})


    async def ensure_safe(self, accept_flash_share: bool = False) -> None:
        await ensure_flash_safe(accept_flash_share)

    async def read(self, action: str, timeout: float, cap: int) -> dict[str, Any]:
        from .. import readings

        if action == "ups_shutdown":
            return await asyncio.to_thread(ups_shutdown.read, ups_shutdown.CONFIG, cap)
        if action != "tasks":
            raise ValueError("unknown read-only capability")
        return await readings.gather(TASK_PATHS, timeout, cap)

    def target(self, family: str, args: dict[str, Any]) -> str:
        if family == "parity":
            if args:
                raise ValueError("parity takes no arguments")
            return "array"
        if set(args) != {"name"}:
            raise ValueError("action requires name only")
        return security.target_name(args["name"])

    async def resolve(self, family: str, op: str, name: str, cfg: dict[str, Any], query: Query) -> list[str]:
        binary = BIN.get(family, "")
        if family == "vm":
            if name not in await query([binary, "list", "--all", "--name"]):
                raise ValueError("unknown VM")
            matches = []
            for uuid in await query([binary, "list", "--all", "--uuid"]):
                if not uuid.strip():
                    continue
                if re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", uuid) is None:
                    raise ValueError("invalid VM UUID")
                if await query([binary, "domname", uuid]) == [name]:
                    matches.append(uuid)
            if len(matches) != 1:
                raise ValueError("VM changed during lookup")
            name = matches[0]
            return [binary, OPS[family][op], name]
        if family == "container":
            if name not in await query([binary, "ps", "-a", "--format", "{{.Names}}"]):
                raise ValueError("unknown container")
            return [binary, op, name]
        if family == "stack":
            found = [p for p in PROJECTS.iterdir() if p.is_dir() and not p.is_symlink()
                     and re.sub(r"[. -]", "_", p.name).lower() == name]
            if len(found) != 1:
                raise ValueError("unknown or ambiguous stack")
            project = found[0]
            files.trusted_file(project, directory=True)
            files.trusted_file(project / ".env", optional=True)
            compose_files = [p for p in (project / "compose.yaml", project / "docker-compose.yml",
                                 project / "docker-compose.yaml", project / "compose.yml")
                     if files.trusted_file(p, optional=True)]
            if not compose_files:
                raise ValueError("stack has no compose file")
            checked_compose(compose_files[0])
            return ([binary, "compose", "--project-directory", str(project), "-p", name,
                     "-f", str(compose_files[0]), op] + (["-d"] if op == "up" else []))
        if family == "parity":
            return [binary, *OPS[family][op]]
        if family == "smart":
            ini = configparser.ConfigParser(interpolation=None)
            ini.read_string(DISKS.read_text())
            devices = {v.get("device", "").strip('"') for v in ini.values()}
            device = name.removeprefix("/dev/")
            if device not in devices or re.fullmatch(r"(?:sd[a-z]+|nvme\d+n\d+)", device) is None:
                raise ValueError("unknown disk")
            return [binary, "-t", op, "/dev/" + device]
        if family == "userscript":
            path = USER_SCRIPTS / name / "script"
            if not any(fnmatch.fnmatchcase(name, g) for g in cfg["userscript_allow"]):
                raise ValueError("User Script not allowed")
            files.trusted_path(path)
            return ["/bin/bash", str(path)]
        if name not in cfg["scripts"]:
            raise ValueError("unknown config script")
        return security.checked_argv(cfg["scripts"][name], cfg["script_binaries"])
