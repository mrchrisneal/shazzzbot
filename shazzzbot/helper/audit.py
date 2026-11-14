from __future__ import annotations

import collections
import os
import shlex
import stat
import sys
import time
from pathlib import Path
from typing import Any

from . import footprint, installation
from .constants import VERSION, version_number


class Audit:
    def __init__(self, cfg: dict[str, Any], key: bytes):
        self.cfg = cfg
        self.secret = key.decode("ascii")
        self.lines: collections.deque[bytes] = collections.deque()
        self.size = 0
        self.latest: tuple[str, str] | None = None
        self.notice_window = -1

    def add(self, text: str) -> None:
        text = text.replace(self.secret, "[redacted]")
        line = (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + " " + text.replace("\n", " ")[:1024])
        print(line, flush=True)
        data = (line + "\n").encode()
        self.lines.append(data)
        self.size += len(data)
        while self.size > self.cfg["log_bytes"]:
            self.size -= len(self.lines.popleft())

    def notice(self, version: str, digest: str, own: str, path: Path) -> None:
        try:
            target = version_number(version)
        except ValueError:
            line = ("invalid update version " + version).replace(self.secret, "[redacted]")
            print(line, file=sys.stderr, flush=True)
            self.add(line)
            self.latest = None
            return
        with footprint.open_home(self.cfg) as home:
            bundle = footprint.bundle(home)
            runtime_old = target > version_number(VERSION)
            bundle_old = target > version_number(bundle["version"])
            hook = footprint.identity(home)["hook"]
        self.latest = (version, digest) if runtime_old or bundle_old else None
        window = int(time.monotonic() // (60 * self.cfg["flush_minutes"]))
        if self.latest and window != self.notice_window:
            self.notice_window = window
            config_path = shlex.quote(self.cfg["_config"])
            instructions = []
            if bundle_old:
                instructions.append("full helper bundle update required")
            instructions.append("run the installer on this host with --upgrade --helper")
            if runtime_old and digest and self.cfg["update_version"] \
                    and version.removeprefix("v") == self.cfg["update_version"] \
                    and digest == self.cfg["update_sha256"] \
                    and digest != own:
                command = f"bash {shlex.quote(hook)} --update {digest}"
                instructions.append(f"runtime only; run: {command}")
            elif runtime_old:
                instructions.append("runtime only; pin update_version and update_sha256 in "
                                    f"{config_path} before updating")
            instruction = "; ".join(instructions)
            line = (f"update available runtime {VERSION}, bundle {bundle['version']} -> {version}; "
                    f"{instruction}").replace(self.secret, "[redacted]")
            print(line, file=sys.stderr, flush=True)
            self.add(line)

    def flush(self) -> None:
        if not self.lines:
            return
        installation.guard(self.cfg)
        with footprint.open_home(self.cfg) as home:
            fd = os.open("helper.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600, dir_fd=home)
            with os.fdopen(fd, "ab") as stream:
                info = os.fstat(stream.fileno())
                if info.st_uid != 0 or info.st_mode & 0o077 or not stat.S_ISREG(info.st_mode):
                    raise ValueError("unsafe log file")
                data = b"".join(self.lines)
                if info.st_size + len(data) > self.cfg["log_bytes"]:
                    stream.close()
                    os.replace("helper.log", "helper.log.1", src_dir_fd=home, dst_dir_fd=home)
                    os.fsync(home)
                    fd = os.open("helper.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=home)
                    with os.fdopen(fd, "wb") as fresh:
                        fresh.write(data)
                        fresh.flush()
                        os.fsync(fresh.fileno())
                else:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
        self.lines.clear()
        self.size = 0

    def safe_flush(self) -> None:
        try:
            self.flush()
        except (OSError, ValueError):
            self.add("log flush failed; retaining RAM log")
