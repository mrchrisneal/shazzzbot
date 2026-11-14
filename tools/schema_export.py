"""Generate the example config, config reference, and static editor metadata."""
import argparse
import json
import math
import re
from pathlib import Path

from shazzzbot import schema, schemaview

ROOT = Path(__file__).resolve().parent.parent


def _key(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) else json.dumps(value, ensure_ascii=True)


def _toml(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("generated config numbers must be finite")
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_key(key)} = {_toml(item)}" for key, item in value.items()) + "}"
    raise TypeError("unsupported generated config value")


def _sample(spec: schema.Field) -> object:
    if spec.sensitive:
        return ""
    if spec.example is not schema.MISSING:
        return spec.example
    if spec.default is not schema.MISSING and spec.default is not None:
        return spec.default
    if spec.choices:
        return spec.choices[0]
    if spec.kind == "integer":
        return int(spec.minimum or 0)
    if spec.kind == "list":
        return list(spec.item.choices) if spec.item else []
    return "example"


def _active(spec: schema.Field, parent: bool) -> bool:
    if not parent:
        return False
    if spec.example_active is not None:
        return spec.example_active
    return spec.required or spec.default is not schema.MISSING and spec.default is not None


def _line(key: str, spec: schema.Field, active: bool, value: object = schema.MISSING) -> list[str]:
    chosen = _sample(spec) if value is schema.MISSING else "" if spec.sensitive else value
    return [f"# {spec.description}", ("" if active else "# ") + f"{_key(key)} = {_toml(chosen)}"]


def _table(section: str, path: tuple[str, ...], active: bool, values: dict | None = None,
           sparse: bool = False) -> list[str]:
    prefix = "" if active else "# "
    lines = [prefix + "[" + ".".join(map(_key, path)) + "]"]
    if section in schema.DYNAMIC:
        for key, value in (values or {}).items():
            spec = schema.DYNAMIC[section]
            if spec.kind == "table":
                lines += [""] + _table(spec.ref, (*path, key), active, value)
            else:
                lines += _line(key, spec, active, value)
        return lines
    nested = []
    for key, spec in schema.SCHEMAS[section].items():
        if spec.kind == "table" or _rows(spec):
            nested.append((key, spec))
        elif not sparse or key in (values or {}):
            value = values.get(key, schema.MISSING) if values else schema.MISSING
            lines += _line(key, spec, _active(spec, active), value)
    for key, spec in nested:
        lines += ["", f"# {spec.description}"] + _child((*path, key), spec, active)
    return lines


def _rows(spec: schema.Field) -> bool:
    return spec.kind == "list" and spec.item is not None and spec.item.kind == "table"


def _child(path: tuple[str, ...], spec: schema.Field, parent: bool) -> list[str]:
    """A table or array of tables under path, from its example when it has one."""
    if _rows(spec):
        assert spec.item is not None
        active = parent and spec.example_active is True
        lines: list[str] = []
        for n, row in enumerate(spec.example if spec.example is not schema.MISSING else [{}]):
            body = _table(spec.item.ref, path, active, row, sparse=n > 0)
            body[0] = ("" if active else "# ") + "[[" + ".".join(map(_key, path)) + "]]"
            lines += ([""] if lines else []) + body
        return lines
    active = _active(spec, parent) and (spec.ref not in schema.DYNAMIC or spec.example_active is True)
    return _table(spec.ref, path, active, spec.example if isinstance(spec.example, dict) else None)


def example() -> str:
    lines = ["# python -m tools.schema_export creates this file from shazzzbot/schema.py.",
             "# Replace the required Discord ids before startup. Secrets belong in .env.", ""]
    nested = []
    for key, spec in schema.SCHEMAS["config"].items():
        if spec.kind == "table" or _rows(spec):
            nested.append((key, spec))
        else:
            lines += _line(key, spec, _active(spec, True)) + [""]
    for key, spec in nested:
        lines += [f"# {spec.description}"] + _child((key,), spec, True) + [""]
    return "\n".join(lines).rstrip() + "\n"


def _escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _default(spec: schema.Field) -> str:
    if spec.sensitive:
        return "Not shown"
    if spec.default_by:
        return ", ".join(f"{key}: `{_escape(_toml(value))}`" for key, value in spec.default_by.items())
    if spec.default is schema.MISSING:
        return "Required" if spec.required else "Unset"
    return "Unset" if spec.default is None else "`" + _escape(_toml(spec.default)) + "`"


def _constraints(spec: schema.Field) -> str:
    parts = ["Required."] if spec.required and _default(spec) != "Required" else []
    if spec.sensitive:
        return " ".join(parts)
    if spec.choices:
        parts.append("Choices: " + ", ".join(f"`{_escape(_toml(value))}`" for value in spec.choices) + ".")
    for name, value in (("Minimum", spec.minimum), ("Maximum", spec.maximum),
                        ("Minimum length", spec.min_length or None), ("Maximum length", spec.max_length)):
        if value is not None:
            parts.append(f"{name}: {value}.")
    if spec.unique:
        parts.append("No duplicates.")
    if spec.pattern:
        parts.append(f"Pattern: `{_escape(spec.pattern)}`.")
    if spec.printable:
        parts.append("Printable text.")
    if spec.nonblank:
        parts.append("Must include a non-space character.")
    if spec.finite:
        parts.append("Finite number.")
    if spec.kind == "threshold":
        parts.append("One warning number or [warning, critical].")
    if spec.kind == "duration":
        parts.append("Finite non-negative number or false.")
    if spec.ref:
        parts.append(f"See [{spec.ref}](#{spec.ref}).")
    if spec.item:
        rules = _constraints(spec.item)
        if rules:
            parts.append("Each item: " + rules[0].lower() + rules[1:])
    return " ".join(parts)


def reference() -> str:
    lines = ["# Configuration", "", "`shazzzbot/schema.py` defines these settings.",
             "Config checks and editor metadata use this schema at startup.",
             "The example and this reference exist before startup, so the build creates them.", "",
             "Set secrets in environment variables. Do not put tokens in TOML.",
             "Replace the required Discord ids in `config.example.toml` before startup.",
             "Patterns must match the whole value.", ""]
    for section, entries in [*schema.SCHEMAS.items(), ("Environment variables", schema.ENVIRONMENT)]:
        lines += [f"## {section}", "", "| Key | Type | Default | Unit | Description |",
                  "|---|---|---|---|---|"]
        for key, spec in entries.items():
            description = _escape(spec.description + " " + _constraints(spec)).strip()
            lines.append(f"| `{key}` | {spec.kind} | {_default(spec)} | {spec.unit or '-'} | {description} |")
        lines += [""]
    lines += ["## Named tables", ""]
    for key, spec in schema.DYNAMIC.items():
        lines += [f"### {key}", "", f"Names map to {spec.kind} values. {_constraints(spec)}".rstrip(), ""]
    return "\n".join(lines).rstrip() + "\n"


def outputs() -> dict[str, str]:
    return {"config.example.toml": example(), "docs/Configuration.md": reference(),
            "docs/demo/schema.json": schemaview.dumps()}


def write(root: Path) -> None:
    for name, body in outputs().items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def check(root: Path) -> list[str]:
    return [name for name, body in outputs().items()
            if not (root / name).is_file() or (root / name).read_bytes() != body.encode("utf-8")]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.check:
        stale = check(args.out)
        if stale:
            parser.exit(1, "generated config files are missing or stale: " + ", ".join(stale) + "\n")
    else:
        write(args.out)


if __name__ == "__main__":
    main()
