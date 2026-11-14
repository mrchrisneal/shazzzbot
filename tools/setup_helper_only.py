from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Any

from shazzzbot.helper import footprint, formats
from tools.setup_files import helper_home, read_safe, verify_pin
from tools.setup_schedule import active


def preflight(home: Path) -> dict[str, Any]:
    try:
        with helper_home(home) as fd:
            info = os.fstat(fd)
            try:
                identity = footprint.identity(fd)
            except FileNotFoundError:
                if os.listdir(fd):
                    raise ValueError("helper directory contains unidentified files") from None
                identity = None
            digest = hashlib.sha256()
            for name in (*footprint.CORE_FILES, ".identity.json", "helper.pyz.prev"):
                try:
                    data = footprint.read_at(fd, name)
                except FileNotFoundError:
                    continue
                digest.update(name.encode() + b"\0" + hashlib.sha256(data).digest())
            return {"install_id": identity["install_id"] if identity else secrets.token_hex(16),
                    "inode": [info.st_dev, info.st_ino], "fingerprint": digest.hexdigest(),
                    "fresh": identity is None}
    except FileNotFoundError:
        return {"install_id": secrets.token_hex(16), "inode": None, "fingerprint": "missing", "fresh": True}


def current(home: Path, stage: Path) -> bool:
    with helper_home(home) as fd:
        try:
            policy = formats.load(json.loads(footprint.read_at(fd, "policy.json", 65536)), "helper policy", 1)
            verify_pin(policy, hashlib.sha256(footprint.read_at(fd, "helper.pyz")).hexdigest())
            if policy.get("artifact") != str(home / "helper.pyz") or policy.get("config") != str(home / "config.json"):
                raise ValueError("helper installation policy leaves its private home")
            for name in ("config.json", "api.key"):
                footprint.read_at(fd, name)
            for name in ("helper.pyz", *footprint.BUNDLE_FILES):
                if footprint.read_at(fd, name) != read_safe(stage / name, private=True):
                    return False
            identity = footprint.identity(fd)
            if not identity["active"] or not active(Path(identity["hook"]).parent):
                return False
            footprint._hook(identity)
            return True
        except FileNotFoundError:
            return False


def main(argv: list[str]) -> None:
    action, home, *rest = argv
    if action == "preflight":
        print(json.dumps(preflight(Path(home))))
    elif action == "current":
        print(int(current(Path(home), Path(rest[0]))))
    elif action == "managed":
        from tools.setup_files import host_identity, install_identity
        from tools.setup_helper_env import managed_env

        app = Path(home)
        machine = Path(os.environ.get("SHAZZZBOT_MACHINE_ID", "/etc/machine-id"))
        print(install_identity(host_identity(machine), app) if managed_env(app) else "")
    elif action == "env-needed":
        from tools.setup_helper_env import needs_sync

        print(int(needs_sync(Path(home), Path(rest[0]))))
    else:
        raise ValueError("unknown helper-only inspection")


if __name__ == "__main__":
    import sys

    main(sys.argv[1:])
