from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import Any

from ..files import trusted_open

CONFIG = Path("/boot/config/plugins/nut-dw/nut-dw.cfg")
MAX_BYTES = 65536
FIELDS = {"batt_level": "BATTERYLEVEL", "batt_timer": "RTVALUE", "sec_timer": "TIMEOUT"}
LINE = re.compile(r'^\s*(SHUTDOWN|BATTERYLEVEL|RTVALUE|TIMEOUT)\s*=\s*"([^"\r\n]*)"\s*$')


def parse(text: str) -> dict[str, Any]:
    values = {}
    for line in text.splitlines():
        match = LINE.fullmatch(line)
        if match:
            name, value = match.groups()
            if name in values:
                return {}
            values[name] = value
    mode = values.get("SHUTDOWN", "")
    if mode not in FIELDS:
        return {}
    try:
        value = float(values.get(FIELDS[mode], ""))
    except ValueError:
        return {}
    return {"mode": mode, "value": value} if math.isfinite(value) and value >= 0 else {}


def read(path: Path = CONFIG, cap: int = MAX_BYTES) -> dict[str, Any]:
    try:
        with trusted_open(path.parent, (path.name,)) as fd, os.fdopen(os.dup(fd), "rb") as stream:
            if os.fstat(fd).st_size > min(cap, MAX_BYTES):
                return {}
            raw = stream.read(min(cap, MAX_BYTES) + 1)
        return parse(raw.decode("utf-8")) if len(raw) <= min(cap, MAX_BYTES) else {}
    except (OSError, ValueError):
        return {}
