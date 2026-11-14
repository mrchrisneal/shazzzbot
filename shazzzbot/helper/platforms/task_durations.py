from __future__ import annotations

import re
from datetime import datetime, tzinfo
from typing import TypedDict


class Record(TypedDict):
    at: float
    seconds: float | None


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 10**12


def _stamp(text: str, fmt: str, zone: tzinfo | None) -> float | None:
    if zone is None:
        return None
    try:
        local = datetime.strptime(text, fmt)
    except ValueError:
        return None
    dt = local.replace(tzinfo=zone)
    if dt.utcoffset() != dt.replace(fold=1).utcoffset():
        return None
    return dt.timestamp()


def user_script(text: str, zone: tzinfo | None) -> Record | None:
    started = None
    result: Record | None = None
    for line in text.splitlines():
        if line.startswith("Script Starting "):
            started = _stamp(line.removeprefix("Script Starting "), "%b %d, %Y  %H:%M.%S", zone)
        elif line.startswith("Script Finished "):
            ended = _stamp(line.removeprefix("Script Finished "), "%b %d, %Y  %H:%M.%S", zone)
            result = None
            if started is not None and ended is not None and ended >= started:
                result = {"at": ended, "seconds": ended - started}
            started = None
    return result


def appdata_backup(text: str, zone: tzinfo | None, observed: float = 0.0) -> Record | None:
    started = None
    result: Record | None = None
    for line in text.splitlines():
        match = re.fullmatch(r"\[([^]]+)\]\[[^]]+\]\[Main\] (.*)", line)
        if not match:
            continue
        date, message = match.groups()
        if message.endswith("WELCOME TO APPDATA.BACKUP!! :D"):
            started = _stamp(date, "%d.%m.%Y %H:%M:%S", zone)
        elif message == "DONE! Thanks for using this plugin and have a safe day ;)":
            ended = _stamp(date, "%d.%m.%Y %H:%M:%S", zone)
            result = {"at": observed, "seconds": None}
            if started is not None and ended is not None and ended >= started:
                result = {"at": ended, "seconds": ended - started}
            started = None
    return result


def parity_history(text: str, observed: float) -> Record | None:
    for line in reversed(text.splitlines()):
        fields = line.split("|")
        if len(fields) not in (6, 7, 9) or fields[3] != "0" \
                or fields[5].lower() not in {"check", "check p", "check q", "check pq", "parity-check"}:
            continue
        value = fields[6] if len(fields) == 9 and fields[6] else fields[1]
        try:
            seconds = float(value)
        except ValueError:
            return {"at": observed, "seconds": None}
        return {"at": observed, "seconds": seconds if _number(seconds) and seconds > 0 else None}
    return None
