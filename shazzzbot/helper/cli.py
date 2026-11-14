from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import os
import re
import stat
import sys
from pathlib import Path

from . import config, files, footprint, formats, installation, platforms, server, updates
from .constants import MAX_UPDATE


def artifact_path() -> Path:
    return Path(os.path.abspath(str(getattr(globals().get("__loader__"), "archive", __file__))))


def bind_artifact(cfg: dict, loaded: Path, *, checking: bool = False) -> Path:
    canonical = footprint.home(cfg) / "helper.pyz"
    archive_fd = os.environ.pop("SHAZZZBOT_HELPER_ARCHIVE_FD", "")
    match = re.fullmatch(r"/proc/self/fd/([0-9]+)", str(loaded))
    if archive_fd and (not archive_fd.isdecimal() or not match or int(archive_fd) != int(match[1])):
        raise ValueError("loaded helper archive descriptor does not match")
    executable = footprint.ensure_workdir() / "runtime/helper.pyz"
    if match:
        fd = int(match[1])
        if fd < 3:
            raise ValueError("invalid helper archive descriptor")
        os.set_inheritable(fd, False)
        info = os.fstat(fd)
        with files.trusted_open(executable) as current:
            expected = os.fstat(current)
            if (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino):
                raise ValueError("loaded helper archive is outside its RAM runtime")
    elif checking and loaded.is_relative_to(footprint.WORK):
        with files.trusted_open(loaded) as source:
            fd = os.dup(source)
        info = os.fstat(fd)
    else:
        raise ValueError("start the helper through its startup hook")
    if info.st_uid != 0 or info.st_mode & 0o077 or not stat.S_ISREG(info.st_mode):
        raise ValueError("loaded helper archive must be private and root-owned")
    data = os.pread(fd, MAX_UPDATE + 1, 0)
    if len(data) > MAX_UPDATE:
        raise ValueError("loaded helper archive is too large")
    cfg["_loaded_archive_fd"] = fd
    cfg["_loaded_file"] = str(executable)
    cfg["_loaded_sha256"] = hashlib.sha256(data).hexdigest()
    value = installation.policy(cfg, canonical, active=not checking)
    allowed = (value["sha256"], value.get("previous_sha256", ""), cfg["update_sha256"])
    if not any(hmac.compare_digest(cfg["_loaded_sha256"], digest) for digest in allowed):
        raise ValueError("loaded helper archive does not match a trusted pin")
    return canonical


def main() -> None:
    parser = argparse.ArgumentParser(description="Local signed host actions over a Unix socket", allow_abbrev=False)
    parser.add_argument("--config", type=Path, default=None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--update", metavar="SHA256")
    args = parser.parse_args()
    try:
        platform = platforms.detect()
        config_path = args.config or footprint.resolve_home(footprint.DEFAULT_HOME) / "config.json"
        cfg, key = config.load_config(config_path, platform)
        path = bind_artifact(cfg, artifact_path(), checking=args.check)
        os.chdir(footprint.ensure_workdir())
        if args.check:
            print("enabled: hello " + " ".join(cfg["enabled"]), flush=True)
        elif args.update:
            updates.update(cfg, path, args.update)
        else:
            installation.policy(cfg, path)
            helper = server.Helper(cfg, key, path, platform)
            if asyncio.run(server.serve(helper)):
                cfg, _ = config.load_config(config_path, platform)
                # Fixed Python source; config values are separate argv entries. reviewed 2026-11
                # bearer:disable python_lang_code_injection
                os.execve(  # nosec B606 -- fixed isolated re-exec; reviewed 2026-11
                    sys.executable, [sys.executable, "-I", "-B", "-c", updates.REEXEC,
                                     str(path), cfg["_config"], cfg["update_sha256"],
                                     str(Path(cfg["socket"]).parent / "update.result")], updates.environment(cfg))
    except formats.FormatError as exc:
        print("helper failed: " + str(exc), file=sys.stderr, flush=True)
        raise SystemExit(1) from None
    except (ValueError, OSError, KeyError, TypeError, SyntaxError, AttributeError) as exc:
        print("helper failed: " + type(exc).__name__, file=sys.stderr, flush=True)
        raise SystemExit(1) from None
