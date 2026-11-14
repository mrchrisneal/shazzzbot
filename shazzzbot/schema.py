"""Config fields and constraints shared by parsing, the editor, and reference outputs."""
from __future__ import annotations

import copy
import difflib
import math
import re
from dataclasses import asdict, dataclass, is_dataclass, replace
from typing import Any

SECTIONS = ("system", "history", "disks", "services", "docker", "apps", "tasks", "forges")
MODES = ("text", "embed", "image")
HEAVY_KINDS = ("parity", "file_integrity", "appdata_backup_plugin", "appdata_backup_script")
MISSING = object()


@dataclass(frozen=True)
class Field:
    kind: str
    description: str
    default: Any = MISSING
    required: bool = False
    choices: tuple[Any, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    unit: str = ""
    sensitive: bool = False
    editable: bool = False
    nullable: bool = False
    ref: str = ""
    item: Field | None = None
    min_length: int = 0
    max_length: int | None = None
    pattern: str = ""
    printable: bool = False
    nonblank: bool = False
    unique: bool = False
    finite: bool = False
    default_by: dict[str, Any] | None = None
    example: Any = MISSING
    example_active: bool | None = None


def text(description: str, value: Any = "", **options: Any) -> Field:
    return Field("string", description, value, **options)


def integer(description: str, value: Any = MISSING, **options: Any) -> Field:
    return Field("integer", description, value, **options)


def flag(description: str, value: bool = False, **options: Any) -> Field:
    return Field("boolean", description, value, **options)


def sequence(description: str, item: Field, value: Any = MISSING, **options: Any) -> Field:
    if value is MISSING and not options.get("required", False):
        value = []
    return Field("list", description, value, item=item, **options)


def table(description: str, ref: str, value: Any = MISSING, **options: Any) -> Field:
    return Field("table", description, {} if value is MISSING else value, ref=ref, **options)


ID = integer("Discord id.", minimum=1)
NAME = text("Name.")
ENV_NAME = text("Name of an environment variable.", MISSING, required=True,
                pattern=r"[A-Za-z_][A-Za-z0-9_]{0,127}")
REPO = text("Repository as owner/name.", pattern=r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
THRESHOLDS = {
    "resource": Field("threshold", "CPU and RAM use.", [80, 90], unit="percent"),
    "load": Field("threshold", "Load average per CPU core.", [0.7, 1.0], unit="per core"),
    "iowait": Field("threshold", "CPU time waiting for disk I/O.", [13, 24], unit="percent"),
    "temp": Field("threshold", "CPU temperature.", [80, 95], unit="C"),
    "disk_full": Field("threshold", "Disk space used.", [90, 95], unit="percent"),
    "hdd_temp": Field("threshold", "Hard disk temperature.", [45, 55], unit="C"),
    "ssd_temp": Field("threshold", "SSD temperature.", [60, 70], unit="C"),
    "disk_busy": Field("threshold", "Time a disk is busy. One number sets warning only.", 95, unit="percent"),
}
TASKS = {
    "shown": integer("Number of upcoming tasks to show.", 20, minimum=1),
    "ignore": sequence("Task names or globs to hide.", NAME),
    "long_running_minutes": Field("duration", "Emphasize long tasks. Zero or false disables the time trigger.",
                                  60, unit="minutes"),
    "heavy_kinds": sequence("Task kinds to emphasize. An empty list disables this trigger.",
                            text("Task kind.", choices=HEAVY_KINDS), list(HEAVY_KINDS), unique=True),
    "rename": table("Task names mapped to display names.", "text_map", example={"backup": "Nightly backup"}),
}
SCHEMAS: dict[str, dict[str, Field]] = {
    "config": {
        "config_format": integer("Configuration format.", 1, required=True, minimum=1, maximum=1),
        "guild_id": integer("Discord server id.", required=True, minimum=1, example=0),
        "audit_channel_id": integer("Audit channel id. Zero disables the channel.", None, minimum=0, nullable=True,
                                    example=0, example_active=True),
        "commands_channel_id": integer("Extra slash-command channel id. Zero disables it.", None,
                                       minimum=0, nullable=True, example=0, example_active=True),
        "blowup_user_id": integer("User to ping with /blowup. Zero disables it.", 0, minimum=0),
        "interval": integer("Seconds between host samples.", 10, minimum=5, unit="seconds"),
        "edit_every": integer("Default time between edits of a message without images.", 60, minimum=5,
                              unit="seconds"),
        "image_every": integer("Default time between image uploads. A message with images changes only when they "
                               "upload. The learned budget can delay uploads.", 60, minimum=60, unit="seconds"),
        "state_file": text("State file in the bot data volume.", "/data/state.json"),
        "timezone": text("IANA time zone for displayed times.", "UTC", editable=True),
        "green_ok": flag("Use a green dot for healthy rows.", editable=True),
        "order": sequence("Section order. Unlisted sections follow.", text("Section.", choices=SECTIONS),
                          list(SECTIONS), unique=True, editable=True),
        "history": table("Chart history.", "history", example_active=True),
        "system": table("System card text.", "system", example_active=True, editable=True),
        "host": table("Read-only host access.", "host", example_active=True),
        "jellyfin": table("Jellyfin now-playing. Requires JELLYFIN_KEY.", "jellyfin", example_active=True),
        "ups": table("Read-only NUT UPS.", "ups", example_active=False),
        "netdata": table("Netdata alarms.", "netdata", example_active=True),
        "tasks": table("Scheduled and running tasks. Running-task colors do not add alerts.", "tasks",
                       example_active=True, editable=True),
        "services": table("Container and external service checks.", "services", example_active=True),
        "restarts": table("Container restarts by /docker.", "restarts", example_active=True),
        "forges": sequence("Code forges to monitor.", table("Forge.", "forges"), max_length=3),
        "helper": table("Signed host-helper connection. Its key stays in the environment.", "helper", None,
                        nullable=True),
        "render": table("Render mode per section. Unset sections use image mode.", "render",
                        example={key: "image" for key in SECTIONS}, example_active=True, editable=True),
        "thresholds": table("Warning and critical levels. One number sets warning only.", "thresholds",
                               {k: v.default for k, v in THRESHOLDS.items()}, editable=True),
        "perms": table("Command permissions. Unlisted users and roles have no tier.", "perms"),
        "node_updates": table("Trusted terminal commands printed by /update. The bot never runs them.", "node_updates",
                              example={"bot": {"hostname": "server.example.com", "run_on": "workstation.example.com",
                                               "command": "sudo ./setup.sh --upgrade"}}),
        "preview": table("Authenticated config preview. Omit it to disable the page.", "preview", None, nullable=True),
        "outputs": sequence("One block per output channel. Channel ids must be different.",
                            table("Output.", "outputs"), required=True, min_length=1,
                            example=[{"channel": 0}], example_active=True),
    },
    "outputs": {
        "channel": integer("Discord channel id.", required=True, minimum=1, example=0),
        "render": table("Per-section overrides of the top-level [render].", "render", example_active=False),
        "sections": sequence("Sections to show, in the top-level order. Omit to show all.",
                             text("Section.", choices=SECTIONS), None, nullable=True, min_length=1, unique=True,
                             example=list(SECTIONS)),
        "image_every": integer("Image period. Omit to inherit the global value.", None, nullable=True,
                               minimum=60, unit="seconds", example=60),
        "edit_every": integer("Edit period. Omit to inherit the global value.", None, nullable=True,
                              minimum=5, unit="seconds", example=60),
    },
    "preview": {
        "enabled": flag("Enable the authenticated preview page.", example=True),
        "bind": text(
            "Container listen IP. compose.yaml publishes no port; add one on a single host address.",
            "0.0.0.0",  # container side; compose picks the host address. reviewed 2026-11  # nosec B104
            pattern=r"[0-9A-Fa-f:.]{2,45}"),
        "port": integer("Preview listen port.", 8780, minimum=1024, maximum=65535),
        "user": text("Login name. Omit both credentials to generate a login.", pattern=r"[A-Za-z0-9_.-]{0,64}"),
        "password_hash": text("Password hash from python -m shazzzbot.preview hash.", sensitive=True),
    },
    "history": {
        "days": integer("History retained in the data volume.", 7, minimum=1, maximum=7, unit="days"),
    },
    "system": {
        "title": text("System card title.", "Unraid System Info", editable=True),
        "subtitle": text("System card subtitle.", editable=True),
    },
    "host": {
        "root": text("Read-only host mount.", "/host"),
        "emhttp": text("Read-only emhttp data directory.", "/host/emhttp"),
        "docker_proxy": text("Docker socket proxy URL. Omit it to disable Docker checks.", None, nullable=True,
                             example="http://docker-proxy:2375", example_active=True),
    },
    "jellyfin": {
        "url": text("Jellyfin base URL. Requires JELLYFIN_KEY."),
    },
    "ups": {
        "target": text("Read-only NUT target as name@host[:port]. The default port is 3493.",
                       example="ups@nut:3493"),
    },
    "netdata": {
        "url": text("Netdata base URL. Empty disables Netdata."),
        "ignore": sequence("Netdata chart and alarm globs to hide.", NAME),
    },
    "services": {
        "columns": sequence("Labels for Local, VPN and Public checks.", text("Column label.", min_length=1),
                            ["Local", "VPN", "Public"], min_length=3, max_length=3, editable=True),
        "ignore": sequence("Service names or globs to skip entirely.", NAME),
        "public_host": text("Public name or IP to probe through configured relays."),
        "checks": sequence("One block per service check.", table("Service check.", "service_check"),
                           example=[{"name": "Jellyfin", "container": "jellyfin",
                                     "url": "http://server.example.com:8096/health",
                                     "public_url": "https://jellyfin.example.com/health"},
                                    {"name": "Nextcloud", "container": "nextcloud",
                                     "url": "http://server.example.com:8443/status.php", "expect": "installed"},
                                    {"name": "Router", "tcp": "192.0.2.1:443", "every": 300}],
                           example_active=False),
    },
    "restarts": {
        "allow": sequence("Container-name globs /docker restart may touch. Empty disables restarts.", NAME),
        "groups": table("Group names mapped to container-name globs.", "restart_groups",
                        example={"media": ["jellyfin*", "reader"]}),
    },
    "service_check": {
        "name": text("Display name.", MISSING, required=True, min_length=1, example="Jellyfin"),
        "container": text("Docker container name. Omit it for an external check.", example="jellyfin"),
        "url": text("Local HTTP or HTTPS probe URL.", example="http://server.example.com:8096/health"),
        "expect": text("Text that an HTTP response must contain."),
        "public_url": text("Public HTTP or HTTPS URL, checked through a relay when configured."),
        "vpn_url": text("VPN HTTP or HTTPS probe URL."),
        "tcp": text("Local TCP target as host:port or [IPv6]:port."),
        "vpn_tcp": text("VPN TCP target."),
        "public_tcp": text("Public TCP target, checked through a relay when configured."),
        "every": integer("Check period. External checks need at least 60 seconds. Omit for the global cycle.",
                         None, nullable=True, minimum=1, unit="seconds", example=120),
    },
    "forges": {
        "type": text("Forge provider.", MISSING, required=True, choices=("forgejo", "github"), example="forgejo"),
        "url": text("Forge API base URL. Forgejo needs its server URL without /api/v1.",
                    default_by={"forgejo": "", "github": "https://api.github.com"}, example="https://forge.example.com"),
        "token_env": replace(ENV_NAME, example="FORGEJO_TOKEN"),
        "repos": sequence("Tracked owner/name repositories.", REPO, min_length=1, max_length=10, required=True,
                          example=["owner/repository"]),
        "show": sequence("Features to show. The default includes every feature the provider supports.",
                         text("Feature.", choices=("ci", "runs", "runners", "pushes", "issues")),
                         min_length=1, unique=True,
                         default_by={"forgejo": ["ci", "runs", "runners", "pushes", "issues"],
                                     "github": ["ci", "runs", "pushes", "issues"]}),
        "name": text("Display name. Do not include a private address.", max_length=64,
                     printable=True, nonblank=True, default_by={"forgejo": "Forgejo", "github": "GitHub"}),
        "system_row": flag("Add a short System row. At most one forge may enable it."),
        "latest": integer("Maximum displayed items per activity list.", 5, minimum=1, maximum=5),
        "pinned": sequence("Tracked repos shown first. They count toward latest.", REPO),
        "filter": table("Enabled activity kinds mapped to tracked repos to monitor.", "forge_filter"),
    },
    "helper": {
        "socket": text("Unix socket path inside the bot container.", MISSING, required=True,
                       example="/run/shazzzbot-helper/helper.sock"),
        "key_env": replace(ENV_NAME, example="SHAZZZBOT_HELPER_KEY"),
    },
    "tasks": TASKS,
    "thresholds": THRESHOLDS,
    "permission": {
        "roles": sequence("Allowed Discord role ids.", ID),
        "users": sequence("Allowed Discord user ids.", ID),
    },
    "node_update": {
        "hostname": text("Node hostname or IP.", MISSING, required=True,
                         pattern=r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,252}"),
        "run_on": text("Host where the command is pasted. Omit to use hostname.", None,
                       pattern=r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,252}"),
        "command": text("One trusted ASCII command line. The bot only prints it.", MISSING, required=True,
                        min_length=1, max_length=1000, nonblank=True),
    },
}
SCHEMAS["render"] = {key: text("text, embed, or image.", "image", choices=MODES) for key in SECTIONS}
SCHEMAS["perms"] = {key: table("Allowed users and roles.", "permission") for key in ("view", "operate", "admin")}
SCHEMAS["forge_filter"] = {key: sequence("Repos to monitor.", REPO, min_length=1)
                           for key in ("ci", "runs", "pushes", "issues")}
DYNAMIC = {
    "text_map": text("Display name."),
    "restart_groups": sequence("Container-name globs.", text("Container-name glob.", nonblank=True), min_length=1),
    "node_updates": table("Trusted update command.", "node_update"),
}
ENVIRONMENT = {
    "DISCORD_TOKEN": text("Discord bot token. Never put it in TOML.", MISSING, required=True, sensitive=True),
    "JELLYFIN_KEY": text("Jellyfin API key.", sensitive=True),
    "RELAY_URL": text("Relay HTTPS URL. Add _2, _3 and so on for more relays."),
    "RELAY_KEY": text("Relay API key. Use the same suffix as its URL.", sensitive=True),
    "RELAY_FINGERPRINT": text("Relay TLS certificate SHA-256. Use the same suffix as its URL."),
}


def names(section: str = "config") -> tuple[str, ...]:
    return tuple(SCHEMAS[section])


def default(section: str, key: str, *, discriminator: str | None = None) -> Any:
    spec = SCHEMAS[section][key]
    value = spec.default
    if spec.default_by and discriminator is not None:
        value = spec.default_by.get(discriminator, value)
    if value is MISSING:
        raise KeyError(f"{section}.{key} has no default")
    return copy.deepcopy(value)


def defaults(section: str = "config") -> dict[str, Any]:
    return {key: default(section, key) for key, spec in SCHEMAS[section].items()
            if spec.default is not MISSING and spec.default is not None}


def unknown(section: str, raw: dict, path: str) -> None:
    if section in DYNAMIC:
        return
    extra = set(raw) - set(names(section))
    if extra:
        parts = []
        for key in sorted(extra):
            near = difflib.get_close_matches(key, names(section), n=1)
            hint = f"; did you mean {near[0]!r}?" if near else ""
            parts.append(f"unknown setting {key!r}{hint}")
        raise SystemExit(f"{path}: " + "; ".join(parts))


def _number(value: object, finite: bool = False) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return not finite or math.isfinite(value)
    except OverflowError:
        return False


def _value(spec: Field, value: Any, path: str) -> None:
    if value is None and spec.nullable:
        return
    kind = spec.kind
    good = True
    if kind == "string":
        good = isinstance(value, str) and len(value) >= spec.min_length \
            and (spec.max_length is None or len(value) <= spec.max_length) \
            and (not spec.pattern or re.fullmatch(spec.pattern, value) is not None) \
            and (not spec.printable or value.isprintable()) and (not spec.nonblank or bool(value.strip()))
    elif kind == "integer":
        good = type(value) is int
    elif kind == "boolean":
        good = type(value) is bool
    elif kind == "number":
        good = _number(value, spec.finite)
    elif kind == "duration":
        good = value is False or _number(value, True) and value >= 0
    elif kind == "threshold":
        good = _number(value) or isinstance(value, list) and len(value) == 2 \
            and all(_number(v) for v in value) and value[0] <= value[1]
    elif kind == "list":
        good = isinstance(value, list) and len(value) >= spec.min_length \
            and (spec.max_length is None or len(value) <= spec.max_length)
        if good:
            if spec.item is not None:
                for index, item in enumerate(value):
                    _value(spec.item, item, f"{path}[{index}]")
            if spec.unique and len(set(value)) != len(value):
                raise SystemExit(f"{path}: items must be unique")
    elif kind == "table":
        validate(spec.ref, value, path=path)
    else:
        raise ValueError(f"unknown schema field kind: {kind}")
    if not good and kind == "list" and isinstance(value, list):
        low, high = spec.min_length, spec.max_length
        count = f"exactly {low}" if low == high else f"at least {low}" if high is None \
            else f"at most {high}" if not low else f"{low} to {high}"
        last = low if high is None or low == high else high
        raise SystemExit(f"{path}: expected a list of {count} item{'' if last == 1 else 's'}; {spec.description}")
    if not good:
        raise SystemExit(f"{path}: expected {kind}; {spec.description}")
    if spec.choices and value not in spec.choices:
        raise SystemExit(f"{path}: choose from {list(spec.choices)}")
    if kind in {"integer", "number"} and (spec.minimum is not None and value < spec.minimum
                                         or spec.maximum is not None and value > spec.maximum):
        if spec.minimum is None:
            bounds = f"at most {spec.maximum}"
        elif spec.maximum is None:
            bounds = f"at least {spec.minimum}"
        else:
            bounds = f"from {spec.minimum} to {spec.maximum}"
        raise SystemExit(f"{path}: value must be {bounds}")


def validate(section: str, raw: object, path: str = "config") -> None:
    if not isinstance(raw, dict):
        raise SystemExit(f"{path}: expected a table")
    unknown(section, raw, path)
    if section in DYNAMIC:
        for key, value in raw.items():
            if not isinstance(key, str) or not key.strip():
                raise SystemExit(f"{path}: names must be non-empty text")
            _value(DYNAMIC[section], value, f"{path}.{key}")
        return
    for key, spec in SCHEMAS[section].items():
        if key not in raw:
            if spec.required:
                raise SystemExit(f"{path}: missing required setting {key}")
        else:
            _value(spec, raw[key], f"{path}.{key}")


def validate_field(section: str, key: str, value: object, path: str, *, items: bool = True) -> None:
    spec = SCHEMAS[section][key]
    _value(spec if items else replace(spec, item=None), value, path)


def model_values(value: Any, *, root: bool = True) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return model_values(asdict(value), root=root)
    if isinstance(value, dict):
        return {key: model_values(item, root=False) for key, item in value.items()
                if not root or key in SCHEMAS["config"]}
    if isinstance(value, (list, tuple)):
        return [model_values(item, root=False) for item in value]
    return value


def threshold_defaults() -> dict[str, tuple[float, float]]:
    return {key: (float(value[0]), float(value[1])) if isinstance(value, list) else (float(value), math.inf)
            for key, value in defaults("thresholds").items()}
