"""The display settings the preview may edit: diagnostics from the real config loader, and a safe merge into
config.toml that leaves every other setting, comment, and table exactly as it was."""
import difflib
import json
import logging
import math
import os
import re
import tempfile
import threading
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest import mock
from zoneinfo import ZoneInfo

from . import config, panel, schema, schemaview
from .glyphs import GREEN, OK
from .helper import formats

KEYS = schemaview.editable()
TABLES = schemaview.tables()
LEAVES = schemaview.leaves()
ATTRS = (*(k for k in KEYS if k in config.Config.__dataclass_fields__ and k not in LEAVES),
         *(attr for attr, (table, key) in config.PATHS.items() if table in TABLES or key in LEAVES.get(table, ())))
MAX_TEXT = 32768
SENTINEL = "#####SET#####"
SECRETS = schemaview.sensitive()
BASE = "config_format = 1\nguild_id = 1\n\n[[outputs]]\nchannel = 2\n"  # the minimum the loader accepts, for previews
_BARE = re.compile(r"[A-Za-z0-9_-]+")
_HEADER = re.compile(r"\s*\[\[?\s*([A-Za-z0-9_-]+)[^\]]*\]\]?\s*(#.*)?$")
_ASSIGN = re.compile(r"\s*([A-Za-z0-9_-]+)\s*[.=]")
log = logging.getLogger("shazzzbot")


def _key(k: str) -> str:
    return k if _BARE.fullmatch(k) else json.dumps(k, ensure_ascii=False)


def _value(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return ("inf" if v > 0 else "-inf") if math.isinf(v) else repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    raise ValueError(f"unsupported value {type(v).__name__}")


def dumps(values: dict[str, Any], path: tuple[str, ...] = ()) -> str:
    top = [f"{_key(k)} = {_value(v)}\n" for k, v in values.items() if not isinstance(v, dict)]
    tables = [f"\n[{'.'.join(map(_key, (*path, k)))}]\n" + dumps(v, (*path, k)).lstrip("\n")
              for k, v in values.items() if isinstance(v, dict)]
    return "".join(top + tables)


def _editable(raw: dict) -> dict:
    out = {k: raw[k] for k in KEYS if k in raw and k not in LEAVES}
    for k, names in LEAVES.items():
        if isinstance(raw.get(k), dict) and (part := {n: raw[k][n] for n in names if n in raw[k]}):
            out[k] = part
    return out


def subset(text: str) -> str:
    """The editable settings from a whole config file, as TOML, with secrets masked."""
    return dumps(mask(_editable(tomllib.loads(text)))).lstrip("\n")


def mask(values: Any) -> Any:
    """values with each secret shown as SENTINEL when set and blank when not; the real value never leaves."""
    if isinstance(values, dict):
        return {k: (SENTINEL if v else "") if k in SECRETS else mask(v) for k, v in values.items()}
    if isinstance(values, list):
        return [mask(value) for value in values]
    return values


def unmask(new: dict, old: dict) -> dict:
    """new with SENTINEL kept as the stored secret, a blank secret removed, and any other value replacing it."""
    out: dict = {}
    for k, v in new.items():
        if isinstance(v, dict):
            out[k] = unmask(v, old[k] if isinstance(old.get(k), dict) else {})
        elif k not in SECRETS:
            out[k] = v
        elif v == SENTINEL:
            if k in old:
                out[k] = old[k]
        elif isinstance(v, str) and SENTINEL in v:
            raise ValueError(f"{k} contains {SENTINEL}; keep it exactly, clear it, or replace it")
        elif v != "":
            out[k] = v
    return out


def _statement(lines: list[str], i: int, stop: int) -> int:
    for j in range(i + 1, stop + 1):
        try:
            tomllib.loads("\n".join(lines[i:j]))
            return j
        except tomllib.TOMLDecodeError:
            continue
    raise ValueError("could not find the end of a setting")


def splice(base: str, values: dict[str, Any]) -> str:
    """base with every editable setting replaced by values; refuses any merge whose result is not exactly that."""
    lines = base.splitlines()
    heads = [i for i, ln in enumerate(lines) if _HEADER.match(ln)]
    first = heads[0] if heads else len(lines)
    keep: list[str] = []
    i = 0
    while i < first:
        m = _ASSIGN.match(lines[i])
        if m and m[1] in KEYS:
            i = _statement(lines, i, first)
            continue
        keep.append(lines[i])
        i += 1
    while keep and not keep[-1].strip():
        keep.pop()
    top = [ln for ln in dumps({k: v for k, v in values.items() if not isinstance(v, dict)}).splitlines()]
    rest: list[str] = []
    placed = set()
    for n, h in enumerate(heads):
        block = lines[h:heads[n + 1] if n + 1 < len(heads) else len(lines)]
        m = _HEADER.match(block[0])
        if m and m[1] in TABLES:
            continue
        exact = re.fullmatch(r"\s*\[\s*([A-Za-z0-9_-]+)\s*\]\s*(#.*)?", block[0])
        if exact and exact[1] in LEAVES and exact[1] not in placed:
            placed.add(exact[1])
            block = [block[0], *dumps({k: v for k, v in values.get(exact[1], {}).items()}).splitlines(),
                     *_drop(block[1:], LEAVES[exact[1]])]
        rest.extend(block)
    tables = dumps({k: v for k, v in values.items() if isinstance(v, dict) and k not in placed})
    out = "\n".join(keep + top + ([""] if rest else []) + rest).rstrip("\n") + "\n" + tables
    old = tomllib.loads(base)
    want = {**{k: v for k, v in old.items() if k not in KEYS}, **{k: v for k, v in values.items() if k not in LEAVES}}
    for k, names in LEAVES.items():
        part = {n: v for n, v in old.get(k, {}).items() if n not in names} | values.get(k, {})
        if part or isinstance(old.get(k), dict):
            want[k] = part
    if tomllib.loads(out) != want:
        raise ValueError("config.toml has a layout this editor cannot merge safely; edit it by hand")
    return out


def _drop(lines: list[str], names: tuple[str, ...]) -> list[str]:
    keep, i = [], 0
    while i < len(lines):
        m = _ASSIGN.match(lines[i])
        if m and m[1] in names:
            i = _statement(lines, i, len(lines))
            continue
        keep.append(lines[i])
        i += 1
    return keep


def _where(text: str, message: str) -> tuple[int, int]:
    lines = text.splitlines() or [""]
    message = re.sub(r"\[([A-Za-z0-9_.]+)\]\.", r"\1.", message)
    words = re.findall(r"'([A-Za-z0-9_.-]+)'", message)
    words += re.findall(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", message)
    for word in (part for w in words for part in reversed(w.split("."))):
        for n, ln in enumerate(lines, 1):
            if re.match(rf"\s*(\[\s*)?{re.escape(word)}\s*[\].=]", ln):
                return n, len(ln) + 1
    return 1, len(lines[0]) + 1


def _diag(text: str, message: str, severity: str = "error", line: int = 0, col: int = 1) -> dict:
    lines = text.splitlines()
    end = (len(lines[line - 1]) + 1 if line <= len(lines) else 1) if line else 0
    if not line:
        line, end = _where(text, message)
    return {"line": line, "col": col, "end_line": line, "end_col": max(end, col + 1), "message": message,
            "severity": severity}


def _unknown(k: str) -> str:
    if k in schema.names("config"):
        return f"{k} cannot be edited here; change it in config.toml"
    near = difflib.get_close_matches(k, KEYS, 1)
    hint = f"; did you mean {near[0]!r}?" if near else f"; this editor accepts {', '.join(KEYS)}"
    return f"unknown setting {k!r}{hint}"


class _Warnings(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.module == "config" and record.thread == threading.get_ident():
            self.messages.append(record.getMessage())


@contextmanager
def _token() -> Iterator[None]:
    if os.environ.get("DISCORD_TOKEN"):
        yield
        return
    with mock.patch.dict(os.environ, {"DISCORD_TOKEN": "preview"}):
        yield


def check(text: str, base: str) -> tuple[list[dict], config.Config | None]:
    """Diagnostics for the edited settings merged into base, from tomllib and config.load; the Config when valid."""
    if len(text) > MAX_TEXT:
        return [_diag(text, f"the settings are over {MAX_TEXT} characters", line=1)], None
    try:
        values = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return [_diag(text, e.msg, line=e.lineno or 1, col=e.colno or 1)], None
    out = [_diag(text, _unknown(k), line=_where(text, k)[0]) for k in values if k not in KEYS]
    out += [_diag(text, f"{k}.{n} cannot be edited here; change it in config.toml", line=_where(text, n)[0])
            for k, names in LEAVES.items() if isinstance(values.get(k), dict) for n in values[k] if n not in names]
    out += [_diag(text, f"{k} must be a table", line=_where(text, k)[0])
            for k in LEAVES if k in values and not isinstance(values[k], dict)]
    if out:
        return out, None
    try:
        values = unmask(values, tomllib.loads(base))
        merged = splice(base, values)
    except ValueError as e:
        return [_diag(text, str(e), line=1)], None
    fd, path = tempfile.mkstemp(suffix=".toml")
    handler = _Warnings()
    log.addHandler(handler)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(merged)
        with _token():
            cfg = config.load(path)
    except formats.FormatError as e:
        return [_diag(text, str(e), line=1)], None
    except SystemExit as e:
        lines = (msg.strip().removeprefix(path + ":").removeprefix(path + ".").strip()
                 for msg in str(e.code).split("\n"))
        return [_diag(text, msg) for msg in lines if msg], None
    finally:
        log.removeHandler(handler)
        os.unlink(path)
    return [_diag(text, msg, "warning") for msg in handler.messages], cfg


def apply(bot: Any, cfg: config.Config) -> None:
    """Copy the display settings onto a running bot; they take effect on its next render."""
    for k in ATTRS:
        # Attribute names come only from the fixed display-setting allowlist. reviewed 2026-11
        # bearer:disable python_lang_code_injection
        setattr(bot.cfg, k, getattr(cfg, k))
    for board, output in zip(bot.boards, bot.cfg.outputs):
        board.render = {**cfg.render, **output.render}
    bot.tz = ZoneInfo(cfg.timezone)
    panel.use_ok_glyph(GREEN if cfg.green_ok else OK)
    bot.rate_budget.auth.reset()


def write(path: str, text: str) -> None:
    """Replace the file atomically when its folder is writable, else rewrite it in place (a single-file mount)."""
    parent = os.path.dirname(path) or "."
    try:
        fd, tmp = tempfile.mkstemp(prefix=".config-", dir=parent)
    except OSError:
        # ponytail: in-place rewrite is not atomic; mount the config folder writable to get os.replace
        # Operator-selected config path; HTTP input supplies only its content. reviewed 2026-11
        # bearer:disable python_lang_path_traversal
        with open(path, "r+", encoding="utf-8") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(text)
            f.truncate()
            f.flush()
            os.fsync(f.fileno())
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
