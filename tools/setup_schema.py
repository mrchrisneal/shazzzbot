"""Inspect config settings and append an inactive catalogue of available defaults."""
from __future__ import annotations

import json
import math
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from shazzzbot import schema
from shazzzbot.helper import formats

HEADER = "# Available defaults (commented; existing settings are unchanged)"
PREFIX = "# default "
TOML_STEPS: dict[int, Callable[[bytes], bytes]] = {}


@dataclass(frozen=True)
class Addition:
    path: str
    value: str

    def line(self) -> str:
        return PREFIX + self.path + " = " + self.value


@dataclass(frozen=True)
class Report:
    errors: tuple[str, ...]
    additions: tuple[Addition, ...]
    candidate: bytes
    format_change: tuple[int, int] | None = None

    def summary(self) -> str:
        lines = ["! " + error for error in self.errors]
        if self.format_change is not None:
            previous, current = self.format_change
            lines.append(f"~ config_format = {previous} -> {current}")
        lines += ["+ " + addition.path + " = " + addition.value + " (commented)" for addition in self.additions]
        return "\n".join(lines) if lines else "Config is current."


def _key(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) else json.dumps(value, ensure_ascii=True)


def _toml(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("default numbers must be finite")
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(_key(key) + " = " + _toml(item) for key, item in value.items()) + "}"
    raise TypeError("unsupported default value")


def _catalogue(text: str) -> set[str]:
    lines = []
    for line in reversed(text.splitlines()):
        if line and line != HEADER and not line.startswith(PREFIX):
            break
        lines.append(line)
    return {line for line in lines if line.startswith(PREFIX)} if HEADER in lines else set()


def _issue(errors: list[str], function, *args, **kwargs) -> None:
    try:
        function(*args, **kwargs)
    except SystemExit as error:
        errors.append(str(error))


def _field(spec: schema.Field, value: Any, path: str, errors: list[str], additions: list[Addition]) -> None:
    if value is None and spec.nullable:
        return
    if spec.kind == "table" and isinstance(value, dict):
        _table(spec.ref, value, path, errors, additions)
    elif spec.kind == "list" and isinstance(value, list) and spec.item is not None:
        from dataclasses import replace

        before = len(errors)
        for index, item in enumerate(value):
            _field(spec.item, item, f"{path}[{index}]", errors, additions)
        checked = replace(spec, item=None, unique=spec.unique and len(errors) == before)
        _issue(errors, schema._value, checked, value, path)
    else:
        _issue(errors, schema._value, spec, value, path)


def _table(section: str, raw: dict, path: str, errors: list[str], additions: list[Addition]) -> None:
    if section in schema.DYNAMIC:
        for key, value in raw.items():
            if not key.strip():
                errors.append(f"{path}: names must be non-empty text")
            else:
                _field(schema.DYNAMIC[section], value, path + "." + _key(key), errors, additions)
        return
    fields = schema.SCHEMAS[section]
    for key, value in raw.items():
        if key not in fields:
            _issue(errors, schema.unknown, section, {key: value}, path or "config")
    discriminator = raw.get("type")
    for key, spec in fields.items():
        full = path + "." + key if path else key
        if key in raw:
            _field(spec, raw[key], full, errors, additions)
        elif spec.required:
            errors.append(f"{path or 'config'}: missing required setting {key}")
        elif not spec.sensitive and spec.default is not schema.MISSING and spec.default is not None:
            provider = discriminator if isinstance(discriminator, str) else None
            value = schema.default(section, key, discriminator=provider)
            additions.append(Addition(full, _toml(value)))


def _text_step(function: Callable[[bytes], bytes]) -> Callable[[dict], dict]:
    def advance(value: dict) -> dict:
        candidate = function(value["data"])
        if not isinstance(candidate, bytes):
            raise formats.FormatError("configuration format step must return TOML bytes")
        try:
            raw = tomllib.loads(candidate.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError):
            raise formats.FormatError("configuration format step produced invalid TOML") from None
        return {"format": raw.get("config_format"), "data": candidate}
    return advance


def upgrade(data: bytes, raw: dict) -> bytes:
    current = schema.default("config", "config_format")
    formats.load({"format": current}, "configuration", current, TOML_STEPS)
    steps = {number: _text_step(function) for number, function in TOML_STEPS.items()}
    value = formats.load({"format": raw.get("config_format"), "data": data}, "configuration", current, steps)
    return value["data"]


def audit(data: bytes) -> Report:
    try:
        text = data.decode("utf-8")
        raw = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return Report(("config: invalid TOML",), (), data)
    previous = raw.get("config_format")
    data = upgrade(data, raw)
    text = data.decode("utf-8")
    raw = tomllib.loads(text)
    current = raw["config_format"]
    changed = (previous, current) if previous != current else None
    errors: list[str] = []
    available: list[Addition] = []
    _table("config", raw, "", errors, available)
    known = _catalogue(text)
    additions = tuple(item for item in available if item.line() not in known)
    if errors or not additions:
        return Report(tuple(errors), additions, data, changed)
    newline = "\r\n" if "\r\n" in text else "\n"
    separator = newline if text.endswith("\n") else newline + newline
    candidate = data + (separator + HEADER + newline + newline.join(item.line() for item in additions)
                        + newline).encode("utf-8")
    return Report((), additions, candidate, changed)
