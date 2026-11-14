from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import files

BANNED = frozenset({"reboot", "shutdown", "poweroff", "powerdown", "halt", "init", "telinit", "rc.0", "rc.6",
                    "systemctl", "loginctl", "kill", "killall", "pkill", "nohup", "timeout", "env", "sh", "bash",
                    "dash", "zsh", "ksh", "csh", "tcsh", "perl", "setsid", "xargs", "nice", "ionice", "sudo",
                    "su", "busybox", "node", "nodejs", "ruby", "php", "lua", "awk", "gawk", "nawk", "chroot",
                    "emcmd", "killall5",
                    "find", "flock", "stdbuf", "chrt", "taskset", "unshare", "nsenter", "runuser", "script",
                    "watch", "at", "strace", "expect", "tclsh", "rbash", "ash", "mksh", "fish", "rc.docker"})

SCRIPT_BANNED = frozenset({"docker", "curl", "wget", "nc", "ncat", "netcat", "socat", "ssh", "scp", "rsync",
                           "mount", "umount", "dd", "modprobe", "insmod", "rmmod", "ip", "iptables", "nft"})


def banned(binary: str) -> bool:
    return binary in BANNED or binary.startswith(("python", "perl"))


def checked_argv(argv: Any, allowed: Sequence[str] = ()) -> list[str]:
    if not isinstance(argv, list) or not argv or not all(isinstance(v, str) and v and "\0" not in v for v in argv):
        raise ValueError("invalid script argv")
    binary = os.path.basename(argv[0]).lower()
    resolved = Path(argv[0]).resolve().name.lower()
    remote_update = any(v == "--update" or v.startswith("--update=") for v in argv[1:])
    dangerous = any("sysrq" in v.lower() or "emhttpd.socket" in v.lower() for v in argv)
    if not os.path.isabs(argv[0]) or banned(binary) or banned(resolved) or remote_update or dangerous \
            or {binary, resolved} & SCRIPT_BANNED:
        raise ValueError("forbidden executable")
    forbidden = {"start", "stop", "cmdstart", "cmdstop"}
    if {binary, resolved} & {"mdcmd", "emcmd"} and any(
            v.split("=", 1)[0].strip().lower() in forbidden for v in argv[1:]):
        raise ValueError("array control is forbidden")
    if argv[0] not in allowed:
        raise ValueError("script executable is not approved")
    files.trusted_path(Path(argv[0]))
    return argv


def target_name(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 128 or value.startswith("-"):
        raise ValueError("invalid target")
    if any(ord(c) < 32 or ord(c) == 127 for c in value) or "/" in value or value in {".", ".."}:
        raise ValueError("invalid target")
    return value
