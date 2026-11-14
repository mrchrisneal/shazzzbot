from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from zipimport import zipimporter

PROTOCOL = 1


def version_number(value: str) -> tuple[int, int, int]:
    part = r"(0|[1-9][0-9]{0,8})"
    match = re.fullmatch("v?" + part + r"\." + part + r"\." + part, value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("version must be canonical MAJOR.MINOR.PATCH with at most nine digits per component")
    return int(match[1]), int(match[2]), int(match[3])


def parse_version(data: bytes) -> str:
    try:
        value = data.decode("ascii").removesuffix("\n")
        if value != ".".join(map(str, version_number(value))):
            raise ValueError("noncanonical version")
        return value
    except ValueError:
        raise ValueError("VERSION must contain canonical MAJOR.MINOR.PATCH and one optional newline") from None


def read_version() -> str:
    loader = globals().get("__loader__")
    try:
        if isinstance(loader, zipimporter):
            data = loader.get_data(loader.archive + "/VERSION")
        else:
            with (Path(__file__).resolve().parents[2] / "VERSION").open("rb") as stream:
                data = stream.read(64)
    except OSError:
        raise ValueError("VERSION file is missing or unreadable") from None
    return parse_version(data)


VERSION = read_version()

MAX_REQUEST = 4096

MAX_UPDATE = 1024 * 1024

MAX_ACTION_TIMEOUT = 86400

REPO = "mrchrisneal/shazzzbot"
RELEASE_URL = f"https://api.github.com/repos/{REPO}/releases/latest"

RUN = Path("/run")

ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
       "LANG": "C", "LC_ALL": "C", "HOME": "/root"}

Send = Callable[[dict[str, Any]], Awaitable[None]]
