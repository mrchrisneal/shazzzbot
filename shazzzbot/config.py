"""Settings from a TOML file. Secrets come only from the environment (DISCORD_TOKEN, JELLYFIN_KEY, RELAY_*), so the
file can be shared."""
import math
import os
import re
import tomllib
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import forges as forge_config
from . import helperclient, schema, taskstyle, ups
from .helper import formats
from .relay import Relay
from .services import Service, validate

SECTIONS = schema.SECTIONS
MODES = schema.MODES
DEFAULT_THRESHOLDS = schema.threshold_defaults()
# Attachment edits have shown shared-scope cooldowns; the body retry delay can exceed the header's delay.
# IMAGE_FLOOR is a per-process minimum; learned request budgets and observed retries can defer it further.
IMAGE_FLOOR = 75
MAX_IMAGE_SPACING = 600.0
# Flat Config attributes and where each lives in the TOML tables.
PATHS = {
    "history_days": ("history", "days"), "title": ("system", "title"), "subtitle": ("system", "subtitle"),
    "host_root": ("host", "root"), "emhttp": ("host", "emhttp"), "docker_proxy": ("host", "docker_proxy"),
    "jellyfin_url": ("jellyfin", "url"), "ups": ("ups", "target"),
    "netdata_url": ("netdata", "url"), "netdata_ignore": ("netdata", "ignore"),
    "tasks_shown": ("tasks", "shown"), "tasks_ignore": ("tasks", "ignore"), "tasks_rename": ("tasks", "rename"),
    "service_columns": ("services", "columns"), "services_ignore": ("services", "ignore"),
    "public_host": ("services", "public_host"), "services": ("services", "checks"),
    "restart_allow": ("restarts", "allow"), "restart_groups": ("restarts", "groups"),
}


@dataclass
class Output:
    channel: int
    render: dict[str, str] = field(default_factory=lambda: schema.default("outputs", "render"))
    sections: list[str] | None = field(default_factory=lambda: schema.default("outputs", "sections"))
    image_every: int | None = schema.default("outputs", "image_every")
    edit_every: int | None = schema.default("outputs", "edit_every")


@dataclass(frozen=True)
class Preview:
    bind: str = schema.default("preview", "bind")
    port: int = schema.default("preview", "port")
    user: str = schema.default("preview", "user")
    password_hash: str = schema.default("preview", "password_hash")


def _preview(raw: object) -> Preview | None:
    if raw is None:
        return None
    schema.validate("preview", raw, "[preview]")
    values = cast(dict[str, Any], raw)
    p = Preview(**{k: v for k, v in values.items() if k != "enabled"})
    if bool(p.user) != bool(p.password_hash):
        raise SystemExit("preview.user and preview.password_hash go together; make the hash with "
                         "python -m shazzzbot.preview hash")
    return p if values.get("enabled", False) else None


@dataclass
class Config:
    token: str
    guild_id: int
    outputs: list[Output]
    config_format: int = schema.default("config", "config_format")
    audit_channel_id: int | None = schema.default("config", "audit_channel_id")
    commands_channel_id: int | None = schema.default("config", "commands_channel_id")
    blowup_user_id: int = schema.default("config", "blowup_user_id")
    interval: int = schema.default("config", "interval")
    edit_every: int = schema.default("config", "edit_every")
    image_every: int = schema.default("config", "image_every")
    log_level: str = schema.default("config", "log_level")
    debug_hours: int = schema.default("config", "debug_hours")
    history_days: int = schema.default("history", "days")
    title: str = schema.default("system", "title")
    subtitle: str = schema.default("system", "subtitle")
    host_root: str = schema.default("host", "root")
    emhttp: str = schema.default("host", "emhttp")
    docker_proxy: str | None = schema.default("host", "docker_proxy")
    state_file: str = schema.default("config", "state_file")
    timezone: str = schema.default("config", "timezone")
    green_ok: bool = schema.default("config", "green_ok")
    netdata_url: str = schema.default("netdata", "url")
    netdata_ignore: list[str] = field(default_factory=lambda: schema.default("netdata", "ignore"))
    jellyfin_url: str = schema.default("jellyfin", "url")
    jellyfin_key: str = ""
    ups: str = schema.default("ups", "target")
    forges: list[forge_config.Settings] = field(default_factory=lambda: schema.default("config", "forges"))
    helper: helperclient.Settings | None = field(default_factory=lambda: schema.default("config", "helper"))
    tasks_ignore: list[str] = field(default_factory=lambda: schema.default("tasks", "ignore"))
    tasks_rename: dict[str, str] = field(default_factory=lambda: schema.default("tasks", "rename"))
    tasks_shown: int = schema.default("tasks", "shown")
    tasks: taskstyle.Settings = field(default_factory=taskstyle.Settings)
    order: list[str] = field(default_factory=lambda: schema.default("config", "order"))
    render: dict[str, str] = field(default_factory=lambda: schema.default("config", "render"))
    thresholds: dict[str, tuple[float, float]] = field(default_factory=schema.threshold_defaults)
    perms: dict[str, dict[str, list[int]]] = field(default_factory=lambda: schema.default("config", "perms"))
    services: list[Service] = field(default_factory=list)
    services_ignore: list[str] = field(default_factory=lambda: schema.default("services", "ignore"))
    service_columns: list[str] = field(default_factory=lambda: schema.default("services", "columns"))
    restart_allow: list[str] = field(default_factory=lambda: schema.default("restarts", "allow"))
    restart_groups: dict[str, list[str]] = field(default_factory=lambda: schema.default("restarts", "groups"))
    public_host: str = schema.default("services", "public_host")
    relays: list[Relay] = field(default_factory=list)  # from RELAY_URL, RELAY_KEY, RELAY_FINGERPRINT (+ _2, _3..)
    node_updates: dict[str, dict[str, str]] = field(default_factory=lambda: schema.default("config", "node_updates"))
    preview: Preview | None = field(default_factory=lambda: schema.default("config", "preview"))


def _format(raw: dict) -> None:
    current = schema.default("config", "config_format")
    found = raw.get("config_format")
    try:
        formats.load({"format": found}, "configuration", current)
    except formats.FormatError:
        if type(found) is int and 0 < found < current:
            raise formats.FormatError("configuration format is unsupported; run setup.sh --action migrate") from None
        raise


# Path comes from the operator's config, not from a Discord user or the network. reviewed 2026-11
# bearer:disable python_lang_path_traversal
def load(path: str) -> Config:
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except (FileNotFoundError, IsADirectoryError):
        raise SystemExit(f"{path} is missing (Docker makes an empty folder in its place). Copy config.example.toml "
                         "to config.toml beside compose.yaml, delete any config.toml folder, and start again") from None
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"{path}: {e}") from None
    _format(raw)
    token = os.environ.get("DISCORD_TOKEN", "")
    if not token:
        raise SystemExit("DISCORD_TOKEN is not set")
    renamed = raw.get("tasks", {}).get("rename", {}) if isinstance(raw.get("tasks"), dict) else {}
    if isinstance(renamed, dict) and (misplaced := {k for k, v in renamed.items()
                                                    if k in schema.names("tasks") and not isinstance(v, str)}):
        raise SystemExit(f"{sorted(misplaced)} sit under [tasks.rename]; move them above it, into [tasks]")
    schema.unknown("config", raw, path)
    missing = [key for key, spec in schema.SCHEMAS["config"].items() if spec.required and key not in raw]
    if missing:
        raise SystemExit(f"{path}: missing required setting(s) {missing}")
    for name in {table for table, _ in PATHS.values()}:
        if name in raw:
            schema.validate(name, raw[name], f"{path}: [{name}]")
    raw = flatten(raw)
    output_raw = raw.pop("outputs")
    th = _thresholds(raw.pop("thresholds", {}), path)
    cfg = Config(token=token, outputs=[], jellyfin_key=os.environ.get("JELLYFIN_KEY", "").strip(),
                 relays=_relays(), thresholds=th,
                 services=validate(raw.pop("services", [])), forges=forge_config.validate(raw.pop("forges", [])),
                 helper=helperclient.validate(raw.pop("helper", None)),
                 tasks=taskstyle.validate(raw.pop("tasks", {})), preview=_preview(raw.pop("preview", None)), **raw)
    _reject_external(cfg.order, f"{path}: order")
    schema.validate_field("config", "order", cfg.order, f"{path}: order")
    cfg.outputs = _outputs(output_raw, cfg, path)
    _check(cfg, path)
    cfg.services = [s for s in cfg.services if not any(fnmatchcase(s.name, g) for g in cfg.services_ignore)]
    return cfg


def flatten(raw: dict) -> dict:
    """raw with every PATHS leaf moved from its table to its flat name; [tasks] keeps the running-task keys."""
    out = {key: dict(value) if key in schema.SCHEMAS and isinstance(value, dict) else value
           for key, value in raw.items()}
    for attr, (table, key) in PATHS.items():  # services.checks is last of its table: it replaces it
        if key in out.get(table, {}):
            out[attr] = out[table].pop(key)
    empty = {table for table, _ in PATHS.values()} - {"tasks"}
    return {key: value for key, value in out.items() if not (key in empty and value == {})}


def nested(values: dict) -> dict:
    """The TOML layout of flat values: the inverse of flatten."""
    out = {key: value for key, value in values.items() if key not in PATHS}
    for attr, (table, key) in PATHS.items():
        if attr in values:
            out.setdefault(table, {})[key] = values[attr]
    return out


def model(c: Config) -> dict:
    """c in its TOML layout, without runtime-only fields."""
    return cast(dict, schema.model_values(nested(schema.model_values(c, root=False))))


def _thresholds(raw: object, path: str) -> dict[str, tuple[float, float]]:
    schema.validate("thresholds", raw, path)
    values = schema.default("config", "thresholds")
    values.update(cast(dict[str, Any], raw))
    out = {}
    for name, value in values.items():
        if isinstance(value, (int, float)):
            out[name] = (float(value), math.inf)
        else:
            out[name] = (float(value[0]), float(value[1]))
    return out


def _reject_external(value: object, name: str) -> None:
    if isinstance(value, (dict, list)) and "external" in value:
        raise SystemExit(f"{name}: 'external' is merged into 'services'; use 'services' instead")


def _outputs(raw: object, c: Config, path: str) -> list[Output]:
    if not isinstance(raw, list) or not raw:
        raise SystemExit(f"{path}: [[outputs]] must contain at least one channel block")
    out = []
    seen: set[int] = set()
    order = list(dict.fromkeys([*c.order, *SECTIONS]))
    for i, entry in enumerate(raw, 1):
        prefix = f"{path}: [[outputs]] entry {i}"
        if not isinstance(entry, dict):
            raise SystemExit(f"{prefix} must be a channel block")
        _reject_external(entry.get("render"), f"{prefix}: render")
        _reject_external(entry.get("sections"), f"{prefix}: sections")
        schema.validate("outputs", entry, prefix)
        channel = entry["channel"]
        if channel in seen:
            raise SystemExit(f"{prefix}: duplicate channel {channel}")
        seen.add(channel)
        render = entry.get("render", {})
        selected = entry.get("sections")
        periods = {name: entry.get(name, getattr(c, name)) for name in ("image_every", "edit_every")}
        out.append(Output(channel, dict(render), None if selected is None else [s for s in order if s in selected],
                          periods["image_every"], periods["edit_every"]))
    return out


def _relays() -> list[Relay]:
    """One relay per suffix: RELAY_URL, RELAY_KEY, RELAY_FINGERPRINT, then the same names ending _2, _3 and so on."""
    sets: dict[str, dict[str, str]] = {}
    for k, v in os.environ.items():
        if (m := re.fullmatch(r"RELAY_(URL|KEY|FINGERPRINT)(_\d+)?", k)) and v.strip():
            sets.setdefault(m[2] or "", {})[m[1]] = v.strip()
    out = []
    for sfx in sorted(sets, key=lambda s: int(s[1:] or 1)):
        url, key, fp = (sets[sfx].get(k, "") for k in ("URL", "KEY", "FINGERPRINT"))
        if not (url and key and fp):
            raise SystemExit(f"RELAY_URL{sfx}, RELAY_KEY{sfx} and RELAY_FINGERPRINT{sfx} go together; "
                             "copy all three from the relay's log")
        fp = fp.replace(":", "").lower()
        if not url.startswith("https://") or not re.fullmatch(r"[0-9a-f]{64}", fp):
            raise SystemExit(
                f"RELAY_URL{sfx} must start with https:// and RELAY_FINGERPRINT{sfx} must be 64 hex digits")
        out.append(Relay(url, key, fp))
    return out


def _check(c: Config, path: str) -> None:
    _reject_external(c.order, f"{path}: order")
    schema.validate("config", model(c), path)

    errors = []
    try:
        ZoneInfo(c.timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        errors.append(f"timezone must be a name like \"America/Los_Angeles\", got {c.timezone!r}")
    if c.jellyfin_url and not c.jellyfin_key:
        errors.append("jellyfin.url needs JELLYFIN_KEY in .env (Jellyfin Dashboard, API Keys)")
    try:
        if c.ups != "":
            ups.target(c.ups)
    except ValueError:
        errors.append("ups.target must be name@host[:port], with a port from 1 to 65535; IPv6 hosts need brackets")
    if c.public_host and not c.relays:
        errors.append("services.public_host needs a relay: set RELAY_URL, RELAY_KEY and RELAY_FINGERPRINT in .env")
    if errors:
        raise SystemExit(f"{path}:\n  " + "\n  ".join(errors))
    _node_updates(c, path)


def _node_updates(c: Config, path: str) -> None:
    allowed = {"bot", "helper", *(r.url for r in c.relays)}
    if not isinstance(c.node_updates, dict) or set(c.node_updates) - allowed:
        raise SystemExit(f"{path}: node_updates keys must be bot, helper, or a configured RELAY_URL")
    for key, entry in c.node_updates.items():
        prefix = f"{path}: node_updates.{key}"
        schema.validate("node_update", entry, prefix)
        command = entry["command"]
        if "```" in command or any(
                ord(ch) < 32 or ord(ch) > 126 for ch in command):
            raise SystemExit(f"{prefix}: command must be one ASCII line without code fences")
