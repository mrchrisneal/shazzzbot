from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.parse
from pathlib import Path
from typing import Any

from . import constants as C
from . import footprint, formats, security
from .platforms.base import Platform


def load_config(path: Path, platform: Platform) -> tuple[dict[str, Any], bytes]:
    if os.geteuid() != 0:
        raise ValueError("helper must run as root")
    path = Path(os.path.abspath(str(path)))
    if path.name != "config.json":
        raise ValueError("helper config must be H/config.json")
    root = footprint.resolve_home(path.parent)
    with footprint.open_home(root) as fd:
        return _load_config(root / "config.json", platform, fd)


def _load_config(path: Path, platform: Platform, fd: int) -> tuple[dict[str, Any], bytes]:
    raw = formats.load(json.loads(footprint.read_at(fd, "config.json")), "helper config", 1)
    cfg: dict[str, Any] = {"format": 1, "key_file": str(path.parent / "api.key"),
                           "socket": str(footprint.WORK / "socket/helper.sock"), "socket_uid": 0, "socket_gid": 10001,
                           "peer_uids": [], "enabled": [], "scripts": {}, "script_binaries": [], "userscript_allow": [],
                           "read_timeout": 5, "action_timeout": 300, "output_bytes": 65536,
                           "flush_minutes": 60, "log_bytes": 1024 * 1024,
                           "log_file": str(path.parent / "helper.log"), "update_url": "",
                           "update_sha256": "", "update_version": "", "release_url": C.RELEASE_URL, "_comment": "",
                           "installation_policy": str(path.parent / "policy.json"), "accept_flash_share": False}
    if set(raw) - set(cfg):
        raise ValueError("unknown config setting")
    cfg.update(raw)
    cfg["_config"] = str(path)
    info = os.fstat(fd)
    cfg["_home_inode"] = [info.st_dev, info.st_ino]
    cfg["_install_id"] = footprint.identity(fd)["install_id"]
    if type(cfg["accept_flash_share"]) is not bool or not isinstance(cfg["installation_policy"], str):
        raise TypeError("invalid installation policy setting")
    for name in ("socket_uid", "socket_gid", "output_bytes", "log_bytes", "read_timeout", "action_timeout",
                 "flush_minutes"):
        if type(cfg[name]) is not int or cfg[name] < (0 if name.startswith("socket_") else 1):
            raise ValueError("invalid numeric setting: " + name)
    if cfg["output_bytes"] > C.MAX_UPDATE or not 4096 <= cfg["log_bytes"] <= 16 * C.MAX_UPDATE:
        raise ValueError("size setting too large")
    if cfg["action_timeout"] > C.MAX_ACTION_TIMEOUT:
        raise ValueError(f"action_timeout must be at most {C.MAX_ACTION_TIMEOUT} seconds")
    for name in ("peer_uids", "enabled", "userscript_allow", "script_binaries"):
        if not isinstance(cfg[name], list):
            raise TypeError("invalid list: " + name)
    if not all(type(v) is int and v >= 0 for v in cfg["peer_uids"]):
        raise ValueError("invalid peer uid")
    if not all(isinstance(v, str) and (v in platform.operations or v in platform.readonly) for v in cfg["enabled"]):
        raise ValueError("invalid enabled family")
    if not all(isinstance(v, str) and v.strip("*?") for v in cfg["userscript_allow"]):
        raise ValueError("invalid User Scripts allow glob")
    for name in ("key_file", "socket", "log_file"):
        if not isinstance(cfg[name], str) or not os.path.isabs(cfg[name]):
            raise ValueError("path must be absolute: " + name)
    footprint.validate_paths(cfg)
    if not isinstance(cfg["update_url"], str) or (cfg["update_url"] and
            urllib.parse.urlsplit(cfg["update_url"]).scheme != "https"):
        raise ValueError("update_url must use https")
    if not isinstance(cfg["release_url"], str) or not cfg["release_url"].startswith("https://"):
        raise ValueError("release_url must use https")
    pin = cfg["update_sha256"]
    if not isinstance(pin, str) or (pin and re.fullmatch(r"[0-9a-fA-F]{64}", pin) is None):
        raise ValueError("update_sha256 must be a pinned SHA-256")
    cfg["update_sha256"] = pin.lower()
    version = cfg["update_version"]
    if not isinstance(version, str):
        raise TypeError("update_version must be a version string")
    if version:
        try:
            if version != ".".join(map(str, C.version_number(version))):
                raise ValueError("noncanonical version")
        except ValueError:
            raise ValueError("update_version must be canonical MAJOR.MINOR.PATCH") from None
    if not isinstance(cfg["_comment"], str):
        raise TypeError("invalid config comment")
    if not isinstance(cfg["scripts"], dict):
        raise TypeError("scripts must map names to argv lists")
    for binary in cfg["script_binaries"]:
        security.checked_argv([binary], cfg["script_binaries"])
    for name, argv in cfg["scripts"].items():
        security.target_name(name)
        security.checked_argv(argv, cfg["script_binaries"])
    key = footprint.read_at(fd, "api.key", 4096).strip()
    if re.fullmatch(rb"[A-Za-z0-9_-]{32,}", key) is None:
        raise ValueError("invalid key; use openssl rand -hex 16")
    footprint.ensure_socket(cfg["socket_gid"])
    footprint.guard(cfg, active=False)
    asyncio.run(platform.ensure_safe(cfg["accept_flash_share"]))
    return cfg, key
