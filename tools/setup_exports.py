from __future__ import annotations

import configparser
import os
import re
import shlex
import subprocess
from pathlib import Path

from shazzzbot.helper.platforms.unraid import flash_writable
from tools.setup_files import opened

TESTPARM = Path("/usr/bin/testparm")
SMB_CONF = Path("/etc/samba/smb.conf")
UNRAID = Path("/etc/unraid-version")
NFS_EXPORTS = Path("/etc/exports")
NFS_FOLDER = Path("/etc/exports.d")
MAX_BYTES = 1024**2


def location(text: str) -> Path:
    path = Path(text)
    if not path.is_absolute() or ".." in path.parts or any(c in text for c in "\n\r\0%*?\\"):
        raise ValueError("export path is unavailable or dynamic")
    return path.resolve()


def overlaps(path: Path, export: Path) -> bool:
    return path == export or path in export.parents or export in path.parents


def smb_exports() -> list[Path]:
    if not any(path.exists() or path.is_symlink() for path in (TESTPARM, SMB_CONF, UNRAID)):
        return []
    try:
        with opened(TESTPARM, uid=0):
            pass
        result = subprocess.run([str(TESTPARM), "-s"], stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        raise ValueError("SMB export configuration is unavailable") from None
    if result.returncode or len(result.stdout) > MAX_BYTES:
        raise ValueError("SMB export configuration is unavailable")
    text = result.stdout.decode()
    if flash_writable(text, Path("/__shazzzbot_private_runtime__")):
        raise ValueError("SMB wide links expose private root storage")
    parser = configparser.ConfigParser(interpolation=None, delimiters=("=",))
    parser.read_string(text)
    defaults = parser[next(name for name in parser.sections() if name.lower() == "global")]
    paths = []
    for name in parser.sections():
        if name.lower() == "global":
            continue
        share = parser[name]
        available = share.get("available", defaults.get("available", "yes")).lower()
        if available not in {"yes", "no"}:
            raise ValueError("SMB share availability is unknown")
        if available == "yes":
            paths.append(location(share.get("path", defaults.get("path", "")).strip().strip('"')))
    return paths


def nfs_paths(text: str) -> list[Path]:
    paths = []
    for line in text.replace("\\\n", " ").splitlines():
        escaped = re.sub(r"\\([0-7]{3})", lambda match: "\\\\" + match[1], line)
        fields = shlex.split(escaped, comments=True)
        if not fields:
            continue
        if len(fields) < 2 or any(re.fullmatch(r"[^()\s]+(?:\([^()]*\))?", client) is None for client in fields[1:]):
            raise ValueError("NFS export clients or options are unavailable")
        path = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[0])
        paths.append(location(path))
    return paths


def nfs_exports() -> list[Path]:
    sources = [NFS_EXPORTS]
    try:
        with opened(NFS_FOLDER, directory=True, uid=0) as fd:
            names = os.listdir(fd)
        if len(names) > 1024:
            raise ValueError("too many NFS export files")
        sources.extend(NFS_FOLDER / name for name in sorted(names) if name.endswith(".exports"))
    except FileNotFoundError:
        pass
    size, paths = 0, []
    for source in sources:
        try:
            with opened(source, uid=0) as fd, os.fdopen(os.dup(fd), "rb") as stream:
                data = stream.read(MAX_BYTES - size + 1)
        except FileNotFoundError:
            continue
        size += len(data)
        if size > MAX_BYTES:
            raise ValueError("NFS export configuration exceeds its size limit")
        paths.extend(nfs_paths(data.decode()))
    return paths


def private_runtime(path: Path) -> None:
    target = location(str(path))
    if any(overlaps(target, export) for export in [*smb_exports(), *nfs_exports()]):
        raise ValueError("runtime storage must be outside every SMB and NFS export")
