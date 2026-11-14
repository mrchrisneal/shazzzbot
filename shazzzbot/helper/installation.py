from __future__ import annotations

import hashlib
import hmac
import json
import re
from pathlib import Path
from typing import Any

from . import constants as C
from . import files, footprint, formats


def guard(cfg: dict[str, Any], *, active: bool = True) -> None:
    footprint.guard(cfg, active=active)


def socket_root(cfg: dict[str, Any]) -> Path:
    footprint.validate_paths(cfg)
    return footprint.WORK


def policy(cfg: dict[str, Any], runtime: Path, *, active: bool = True) -> dict[str, Any]:
    guard(cfg, active=active)
    root = footprint.home(cfg)
    if runtime != root / "helper.pyz":
        raise ValueError("helper must run from H/helper.pyz")
    with footprint.open_home(cfg) as fd:
        value = formats.load(json.loads(footprint.read_at(fd, "policy.json", 65536)), "helper policy", 1)
    required = {"format", "artifact", "sha256", "config"}
    if set(value) not in (required, required | {"previous_sha256"}) \
            or not all(isinstance(item, str) for key, item in value.items() if key != "format") \
            or any(re.fullmatch(r"[0-9a-f]{64}", value[name]) is None
                   for name in ("sha256", "previous_sha256") if name in value):
        raise ValueError("invalid installation policy")
    if value["artifact"] != str(runtime) or value["config"] != cfg["_config"]:
        raise ValueError("installation policy must stay inside the helper home")
    return value


def commit(cfg: dict[str, Any], runtime: Path, data: bytes) -> None:
    value = policy(cfg, runtime)
    if len(data) > C.MAX_UPDATE:
        raise ValueError("helper update exceeds its size limit")
    with footprint.open_home(cfg) as fd:
        current = footprint.read_at(fd, "helper.pyz", C.MAX_UPDATE)
        previous = footprint.read_at(fd, "helper.pyz.prev", C.MAX_UPDATE)
        previous_sha = hashlib.sha256(previous).hexdigest()
        allowed = [value["sha256"], value.get("previous_sha256", "")]
        if not hmac.compare_digest(hashlib.sha256(current).digest(), hashlib.sha256(data).digest()) \
                or not any(hmac.compare_digest(previous_sha, digest) for digest in allowed):
            raise ValueError("helper update no longer matches its verified artifacts")
        value["previous_sha256"] = previous_sha
        value["sha256"] = hashlib.sha256(data).hexdigest()
        files.write_private(footprint.fd_path(fd) / "policy.json", json.dumps(formats.dump(value, 1)).encode())
