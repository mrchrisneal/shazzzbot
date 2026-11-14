from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from tools.setup_files import opened, read_safe, trusted_code, write_safe


def environment() -> dict[str, str]:
    values = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    values.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_CONFIG_GLOBAL="/dev/null",
                  GIT_CONFIG_SYSTEM="/dev/null", GIT_ATTR_NOSYSTEM="1")
    return values


def local_objects(root: Path) -> None:
    for name in ("commondir", "objects/info/alternates", "objects/info/http-alternates", "info/grafts"):
        if (root / ".git" / name).exists():
            raise ValueError("Git metadata refers outside the private object store")


def git(root: Path, *args: str, input_file=None) -> bytes:
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                             "-C", str(root), *args], stdin=input_file, capture_output=True, timeout=120, check=False,
                            env=environment())
    if result.returncode or len(result.stdout) > 16 * 1024**2:
        raise ValueError("private source Git operation failed")
    return result.stdout


def apply(root: Path, pack: Path, revision: str, origin: str) -> None:
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("ship revision must be a full commit id")
    with opened(root, directory=True) as fd:
        if not (root / ".git").exists() and os.listdir(fd):
            raise ValueError("private source contains unidentified files")
    if not (root / ".git").exists():
        git(root, "init", "-q", "-b", "main")
        git(root, "remote", "add", "origin", origin)
    trusted_code(root)
    local_objects(root)
    if git(root, "status", "--porcelain", "--untracked-files=all", "--ignored"):
        raise ValueError("private source contains changes or untracked files")
    with opened(pack) as fd:
        if os.fstat(fd).st_size > 512 * 1024**2:
            raise ValueError("ship pack exceeds its size limit")
        with os.fdopen(os.dup(fd), "rb") as stream:
            git(root, "unpack-objects", "-q", input_file=stream)
    if git(root, "cat-file", "-t", revision).strip() != b"commit":
        raise ValueError("ship revision is not a commit")
    names = git(root, "ls-tree", "-r", "--name-only", "-z", revision).decode().split("\0")
    for name in filter(None, names):
        if Path(name).parts[0] in {".env", "config.toml", "data", ".setup-state", ".setup-identity", ".deployed"}:
            raise ValueError("shipped source overlaps private runtime state")
    boundary = root / ".git/shallow"
    try:
        old = read_safe(boundary).decode().splitlines()
    except FileNotFoundError:
        old = []
    write_safe(boundary, ("\n".join(sorted(set(old + [revision]))) + "\n").encode())
    git(root, "config", "gc.auto", "0")
    git(root, "reset", "--hard", revision)
    trusted_code(root)
