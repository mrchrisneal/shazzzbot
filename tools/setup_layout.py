from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from shazzzbot.helper import formats
from tools.setup_config import dotenv
from tools.setup_files import read_safe, write_safe


def project(role: str) -> str:
    if role not in {"bot", "relay"}:
        raise ValueError("invalid runtime role")
    return "shazzzbot" if role == "bot" else "shazzzbot_relay"


def image_repository(identifier: str) -> str:
    return "shazzzbot-" + hashlib.sha256(identifier.encode()).hexdigest()[:16]


def context() -> dict:
    identifier = os.environ.get("SHAZZZBOT_INSTALL_ID", "")
    role = os.environ.get("SHAZZZBOT_INSTALL_ROLE", "")
    if re.fullmatch(r"[0-9a-f]{32}", identifier) is None:
        raise ValueError("invalid install id")
    repository = image_repository(identifier)
    return {"install_id": identifier, "project": project(role), "role": role,
            "dev_image": repository + ":dev", "prev_image": repository + ":prev"}


def state(app: Path) -> str:
    try:
        value = formats.load(json.loads(read_safe(app / ".setup-state", private=True)), "deployment state", 1)
    except FileNotFoundError:
        return ""
    return " ".join(state_values(value))


def state_values(value: dict) -> tuple[str, str, str]:
    if set(value) != {"format", "method", "role", "ref"} \
            or value.get("method") not in {"compose-build", "compose-image", "docker-run", "bot-only"} \
            or value.get("role") not in {"bot", "relay"} \
            or value["role"] == "relay" and value["method"] == "bot-only" \
            or not isinstance(value.get("ref"), str) \
            or re.fullmatch(r"(?:v[0-9]+\.[0-9]+\.[0-9]+|[0-9a-f]{40})", value["ref"]) is None:
        raise ValueError("invalid deployment state")
    return value["method"], value["role"], value["ref"]


def state_data(method: str, role: str, ref: str) -> bytes:
    value = formats.dump({"method": method, "role": role, "ref": ref}, 1)
    state_values(value)
    return json.dumps(value).encode()


def normalize_environment(root: Path, folder: Path, role: str) -> None:
    from shazzzbot.helper.footprint import WORK

    if role != "bot":
        return
    try:
        env = dotenv(read_safe(folder / ".env", private=True).decode())
    except FileNotFoundError:
        return
    env["HELPER_SOCKET_DIR"] = str(WORK / "socket")
    write_safe(folder / ".env", "".join(f"{key}={json.dumps(item, ensure_ascii=False)}\n"
                                       for key, item in env.items()).encode())


def compose(text: str, value: dict) -> str:
    lines, section = [], ""
    for line in text.splitlines():
        if line and not line.startswith((" ", "#")):
            section = line.split(":", 1)[0]
        if line == "    build: .":
            continue
        if line == "    image: shazzzbot:dev":
            line = "    image: " + value["dev_image"]
        if section == "networks":
            if line.startswith("    name:"):
                line = "    name: " + json.dumps(value["project"] + "_default")
            if "org.shazzzbot.install:" in line:
                line = "    labels: " + json.dumps({"org.shazzzbot.install": value["install_id"]})
        lines.append(line)
    return "\n".join(lines) + "\n"


def rollback_compose(root: Path, file: Path, image: str) -> None:
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None:
        raise ValueError("invalid rollback image")
    value = context()
    text = compose(read_safe(file).decode(), value)
    service = "shazzzbot" if value["role"] == "bot" else "relay"
    inside, changed, lines = False, False, []
    for line in text.splitlines():
        if line.startswith("  ") and not line.startswith("   "):
            inside = line == "  " + service + ":"
        if inside and line.startswith("    image:"):
            line, changed = "    image: " + image, True
        if inside and line.startswith("    build:"):
            continue
        lines.append(line)
    if not changed:
        raise ValueError("rollback template has no application image")
    write_safe(file, ("\n".join(lines) + "\n").encode())
