from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


class FormatError(ValueError):
    pass


def _number(value: object, kind: str) -> int:
    if type(value) is not int or not 1 <= value <= 2**31 - 1:
        raise FormatError(f"{kind} requires a bounded positive integer format")
    return value


def _steps(steps: Mapping[int, Callable] | None, kind: str) -> Mapping[int, Callable]:
    if steps is None:
        return {}
    if not isinstance(steps, Mapping) or any(type(key) is not int or key < 1 or not callable(step)
                                           for key, step in steps.items()):
        raise FormatError(f"{kind} has invalid format steps")
    return steps


def _supported(found: int, current: int, kind: str) -> None:
    if found > current:
        raise FormatError(f"{kind} was written by a newer Shazzzbot")


def load(value: object, kind: str, current: int, steps: Mapping[int, Callable] | None = None) -> dict[str, Any]:
    current = _number(current, kind)
    table = _steps(steps, kind)
    if not isinstance(value, dict):
        raise FormatError(f"{kind} must be an object with a format")
    found = _number(value.get("format"), kind)
    _supported(found, current, kind)
    result = dict(value)
    while found < current:
        if found not in table:
            raise FormatError(f"{kind} format is unsupported; no upgrade step is available")
        candidate = table[found](result)
        if not isinstance(candidate, dict) or type(candidate.get("format")) is not int \
                or candidate["format"] != found + 1:
            raise FormatError(f"{kind} format step did not advance exactly once")
        result, found = candidate, found + 1
    return result


def dump(value: Mapping[str, Any], current: int) -> dict[str, Any]:
    return {**value, "format": _number(current, "document")}


def sqlite(db: Any, kind: str, current: int, steps: Mapping[int, Callable] | None = None) -> None:
    current = _number(current, kind)
    table = _steps(steps, kind)
    found = db.execute("PRAGMA user_version").fetchone()[0]
    if found == 0:
        if db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is not None:
            raise FormatError(f"{kind} has no format; an existing database cannot be stamped")
    else:
        _number(found, kind)
    _supported(found, current, kind)
    if found == current:
        return
    db.execute("BEGIN")
    try:
        if found == 0:
            # PRAGMA takes no bound parameters; current is a bounded int from _number. reviewed 2026-11
            # nosemgrep: formatted-sql-query, sqlalchemy-execute-raw-query
            db.execute(f"PRAGMA user_version={current}")
        while 0 < found < current:
            if found not in table:
                raise FormatError(f"{kind} format is unsupported; no upgrade step is available")
            table[found](db)
            advanced = db.execute("PRAGMA user_version").fetchone()[0]
            if advanced != found + 1:
                raise FormatError(f"{kind} format step did not advance exactly once")
            found = advanced
        db.commit()
    except BaseException:
        db.rollback()
        raise
