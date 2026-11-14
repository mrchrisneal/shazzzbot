"""Public config metadata, without runtime values or credentials."""
import json
import math
from dataclasses import fields
from typing import Any

from . import __version__, schema


def leaves() -> dict[str, tuple[str, ...]]:
    """Tables the editor may change only in part, mapped to the keys it may change."""
    return {key: names for key, spec in schema.SCHEMAS["config"].items()
            if spec.kind == "table" and not spec.editable and spec.ref in schema.SCHEMAS
            and (names := tuple(k for k, v in schema.SCHEMAS[spec.ref].items() if v.editable))}


def editable() -> tuple[str, ...]:
    return tuple(key for key, spec in schema.SCHEMAS["config"].items() if spec.editable or key in leaves())


def tables() -> tuple[str, ...]:
    """Tables the editor replaces whole."""
    return tuple(key for key in editable() if schema.SCHEMAS["config"][key].kind == "table" and key not in leaves())


def sensitive() -> frozenset[str]:
    return frozenset(key for section in [*schema.SCHEMAS.values(), schema.ENVIRONMENT]
                     for key, spec in section.items() if spec.sensitive)


def _data(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("schema metadata must contain only finite numbers")
        return value
    if isinstance(value, (list, tuple)):
        return [_data(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _data(item) for key, item in value.items()}
    raise TypeError("unsupported schema metadata value")


def describe(spec: schema.Field) -> dict[str, Any]:
    out = {}
    for field in fields(spec):
        key, value = field.name, getattr(spec, field.name)
        if value is schema.MISSING:
            continue
        if spec.sensitive and key in {"default", "example", "default_by", "choices"}:
            continue
        if key == "default" and spec.default_by:
            continue
        out[key] = describe(value) if isinstance(value, schema.Field) else _data(value)
    return out


def document() -> dict[str, Any]:
    return {"format": 1, "version": __version__, "root": "config",
            "sections": {name: {key: describe(spec) for key, spec in section.items()}
                         for name, section in schema.SCHEMAS.items()},
            "dynamic": {key: describe(spec) for key, spec in schema.DYNAMIC.items()},
            "environment": {key: describe(spec) for key, spec in schema.ENVIRONMENT.items()},
            "editor": {"keys": list(editable()), "tables": list(tables()),
                       "leaves": {key: list(names) for key, names in leaves().items()}}}


def dumps() -> str:
    return json.dumps(document(), ensure_ascii=True, allow_nan=False, sort_keys=True, indent=1) + "\n"
