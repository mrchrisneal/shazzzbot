from __future__ import annotations

import json
import os
from pathlib import Path

from shazzzbot.helper import formats
from tools.setup_files import opened, read_safe, write_safe


def answers(root: Path, out: Path, app: Path, role: str, method: str, *, save: bool) -> None:
    path = root / "wizard-answers.json"
    identity = formats.dump({"app": str(app), "role": role, "method": method}, 1)
    try:
        value = formats.load(json.loads(read_safe(path, private=True)), "wizard answers", 1)
    except FileNotFoundError:
        value = {**identity, "files": {}}
    if {k: value.get(k) for k in identity} != identity \
            or not isinstance(value.get("files"), dict) or set(value["files"]) - {".env", "config.toml"} \
            or any(not isinstance(v, str) for v in value["files"].values()):
        raise ValueError("saved wizard answers do not belong to this install")
    for name in (".env", "config.toml"):
        if save:
            try:
                value["files"][name] = read_safe(out / name, private=True).decode()
            except FileNotFoundError:
                pass
        elif not (app / name).exists() and name in value["files"]:
            text = value["files"][name]
            if name == ".env" and os.environ.get("SHAZZZBOT_HELPER_KEY"):
                from tools.setup_config import dotenv

                fields = dotenv(text)
                fields["SHAZZZBOT_HELPER_KEY"] = os.environ["SHAZZZBOT_HELPER_KEY"]
                fields["HELPER_SOCKET_DIR"] = os.environ["SHAZZZBOT_HELPER_SOCKET_DIR"]
                text = "".join(f"{key}={item}\n" for key, item in fields.items())
            write_safe(out / name, text.encode())
    if save:
        with opened(root, directory=True, create=True):
            pass
        write_safe(path, json.dumps(formats.dump(value, 1), sort_keys=True).encode())
