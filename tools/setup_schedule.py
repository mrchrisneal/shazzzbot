from __future__ import annotations

import json
from pathlib import Path

from tools import setup_transaction as transaction
from tools.setup_files import read_safe


def hook_text(home: Path, hook: Path, source: bytes) -> bytes:
    if not home.is_absolute() or not hook.is_absolute() or ".." in home.parts or ".." in hook.parts:
        raise ValueError("helper home and hook paths must be absolute")
    source = source.decode()
    source = source.replace("HOME = None\n", "HOME = Path(" + repr(str(home)) + ")\n", 1)
    source = source.replace("HOOK = None\n", "HOOK = Path(" + repr(str(hook)) + ")\n", 1)
    return ("#!/bin/bash\nHOME=/tmp/shazzzbot-helper TMPDIR=/tmp/shazzzbot-helper "
            "XDG_CACHE_HOME=/tmp/shazzzbot-helper/cache XDG_CONFIG_HOME=/tmp/shazzzbot-helper/config "
            "XDG_DATA_HOME=/tmp/shazzzbot-helper/data "
            "exec /usr/bin/python3 -I -B - \"$@\" <<'SHAZZZBOT_HELPER_PY'\n" + source
            + "SHAZZZBOT_HELPER_PY\n").encode()


def active(helper: Path) -> bool:
    try:
        value = json.loads(read_safe(helper.parent.parent / "schedule.json"))
    except FileNotFoundError:
        return False
    if not isinstance(value, dict):
        raise TypeError("invalid User Scripts schedule")
    return value.get("shazzzbot-helper") == {"frequency": "boot", "script": str(helper / "script")}


def schedule(helper: Path, backup: Path) -> None:
    if helper.parent.name != "scripts" or helper.parent.parent.name != "user.scripts":
        raise ValueError("startup scheduling requires the User Scripts plugin directory")
    root = helper.parent.parent
    try:
        value = json.loads(read_safe(root / "schedule.json"))
    except FileNotFoundError:
        value = {}
    if not isinstance(value, dict):
        raise TypeError("invalid User Scripts schedule")
    key = "shazzzbot-helper"
    if key in value and (not isinstance(value[key], dict) or value[key].get("script") != str(helper / "script")):
        raise ValueError("User Scripts schedule name belongs to another script")
    value[key] = {"frequency": "boot", "script": str(helper / "script")}
    state = transaction.manifest(backup)
    state["schedule"] = str(root)
    transaction.save(backup, state)
    transaction.replace(backup, "schedule", "schedule.json", json.dumps(value, sort_keys=True).encode())
