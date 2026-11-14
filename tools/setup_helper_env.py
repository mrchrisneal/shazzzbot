from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from shazzzbot.helper import formats
from shazzzbot.helper.footprint import WORK
from tools import setup_transaction as transaction
from tools.setup_config import dotenv
from tools.setup_files import helper_home, host_identity, install_identity, read_at, read_safe


def managed_env(app: Path) -> bool:
    try:
        value = formats.load(json.loads(read_safe(app / ".setup-identity", private=True)), "install identity", 2)
    except FileNotFoundError:
        return False
    identifier = install_identity(host_identity(), app)
    if value.get("role") != "bot":
        raise ValueError("unsupported bot install identity")
    expected = os.environ.get("SHAZZZBOT_BOT_INSTALL_ID", identifier)
    if identifier != expected:
        raise ValueError("bot install identity mismatch")
    try:
        read_safe(app / ".env", private=True)
    except FileNotFoundError:
        return False
    return True


def updated_env(original: bytes, key: str, socket: str) -> bytes:
    text = original.decode("utf-8")
    values = dotenv(text)
    desired = {"SHAZZZBOT_HELPER_KEY": key, "HELPER_SOCKET_DIR": socket}
    if all(values.get(name) == value for name, value in desired.items()):
        return original
    newline = "\r\n" if "\r\n" in text else "\n"
    remaining = dict(desired)
    lines = []
    for line in text.splitlines(keepends=True):
        found = re.match(r"\s*(?:export )?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if found is not None and found[1] in remaining:
            name = found[1]
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            lines.append(name + "=" + json.dumps(remaining.pop(name)) + ending)
        else:
            lines.append(line)
    result = "".join(lines)
    for name, value in remaining.items():
        if result and not result.endswith(("\r", "\n")):
            result += newline
        result += name + "=" + json.dumps(value) + newline
    candidate = result.encode("utf-8")
    dotenv(result)
    return candidate


def candidate_env(app: Path, home: Path, expected: list[int]) -> tuple[bytes, bytes] | None:
    if not managed_env(app):
        return None
    with helper_home(home, expected) as parent:
        raw, _ = read_at(parent, "api.key", private=True)
    key = raw.decode("ascii").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{32,}", key) is None:
        raise ValueError("invalid helper API key")
    original = read_safe(app / ".env", private=True)
    return original, updated_env(original, key, str(WORK / "socket"))


def needs_sync(app: Path, home: Path) -> bool:
    expected = json.loads(os.environ.get("SHAZZZBOT_HELPER_INODE", "null"))
    if expected is None:
        raise ValueError("helper environment needs a recorded helper directory identity")
    result = candidate_env(app, home, expected)
    return result is not None and result[0] != result[1]


def sync_env(app: Path, home: Path, folder: Path) -> bool:
    value = transaction.manifest(folder)
    if value.get("app") != str(app) or value.get("helper") != str(home):
        raise ValueError("helper environment transaction does not match its installation")
    expected = value.get("helper_inode")
    if expected is None:
        raise ValueError("helper environment needs a recorded helper directory identity")
    result = candidate_env(app, home, expected)
    if result is None:
        return False
    original, candidate = result
    if candidate == original:
        return False
    transaction.watch(folder, "app", ".env")
    watched = transaction.manifest(folder)["files"]["app/.env"]["before"]
    if not isinstance(watched, dict) or watched.get("sha256") != transaction.digest(original) \
            or read_safe(app / ".env", private=True) != original:
        raise ValueError("bot environment changed during setup; rerun")
    transaction.replace(folder, "app", ".env", candidate)
    return True


def main() -> None:
    try:
        print(int(sync_env(*map(Path, sys.argv[1:]))))
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
