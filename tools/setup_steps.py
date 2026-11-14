"""Wizard steps that start from what setup.sh found on the host: time zone, UPS, Netdata, Jellyfin, restarts,
command permissions, and service checks."""
from __future__ import annotations

import json
import os
import re
from functools import partial
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

from shazzzbot.helper.constants import REPO

NETDATA_HELP = f"https://github.com/{REPO}/wiki/Installation#optional-netdata"
PERMS_HELP = f"https://github.com/{REPO}/wiki/Configuration#permissions"
CONTAINER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
TIERS = {"admin": "every command", "operate": "restarts and other changes", "view": "read-only commands"}


def probe() -> dict[str, Any]:
    path = os.environ.get("SHAZZZBOT_PROBE", "")
    try:
        data = json.loads(Path(path).read_text()) if path else {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def clean(value: object) -> str:
    return value if isinstance(value, str) and value.isascii() and value.isprintable() else ""


def containers(found: dict[str, Any]) -> list[dict[str, str]]:
    rows = found.get("containers")
    return [{"name": c["name"], "webui": clean(c.get("webui"))} for c in rows if isinstance(c, dict)
            and CONTAINER.fullmatch(clean(c.get("name")))] if isinstance(rows, list) else []


def http(value: str) -> str | None:
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        parts = None
    good = parts and port != 0 and parts.scheme in {"http", "https"} and parts.hostname and not set("?#@ ") & set(value)
    return None if good and clean(value) else "use an http:// or https:// URL with no user name or query"


def choose(w: Any, env: str, detected: str, offer: str, question: str, check: Any) -> str:
    if detected and check(detected) is None and w.prompt and not os.environ.get(env):
        return detected if w.yes(offer.format(detected), True) else ""
    return w.ask(env, question, check, required=False)


def snowflakes(sc: ModuleType, env: str) -> list[int]:
    parts = [p for p in re.split(r"[\s,]+", os.environ.get(env, "")) if p]
    if not all(sc.SNOWFLAKE.fullmatch(p) for p in parts):
        raise SystemExit(f"{env}: Discord ids have 17 to 20 digits")
    return [int(p) for p in parts]


def jellyfin(sc: ModuleType, w: Any, found: dict[str, Any], env: list[str], create: set[str]) -> str:
    if ".env" not in create:
        if w.prompt:
            sc.say("Jellyfin: put JELLYFIN_KEY in .env, then set [jellyfin] url in config.toml.")
        return ""
    url = choose(w, "SHAZZZBOT_JELLYFIN_URL", clean(found.get("jellyfin")), "Show Jellyfin now-playing from {}?",
                 "Jellyfin URL (empty for none)", http)
    if not url:
        return ""
    key = w.ask("SHAZZZBOT_JELLYFIN_KEY", "Jellyfin API key, from Dashboard > API Keys (input is hidden)",
                lambda v: None, required=False, secret=True)
    if not key:
        sc.say("  Jellyfin skipped: it needs an API key.")
        return ""
    sc.set_line(env, "# JELLYFIN_KEY=", f"JELLYFIN_KEY={key}")
    os.environ.setdefault("JELLYFIN_KEY", key)
    return url


def restarts(sc: ModuleType, w: Any, names: list[str]) -> list[str]:
    if raw := os.environ.get("SHAZZZBOT_RESTART_ALLOW", ""):
        return [n for n in re.split(r"[\s,]+", raw) if n]
    if not (w.prompt and names):
        return []
    rows = [{"id": n, "name": n} for n in names]
    return sc.menu("Containers that /docker may restart:", rows, many=True, optional=True, manual=False) or []


def perms(sc: ModuleType, w: Any, cfg: list[str], token: str, guild: str) -> None:
    roles = sc.lookup(token, ["roles", guild]) if w.prompt and token else None
    if w.prompt and roles is None:
        sc.say(f"Set command permissions later: {PERMS_HELP}")
    for tier, scope in TIERS.items():
        ids = snowflakes(sc, f"SHAZZZBOT_PERMS_{tier.upper()}_ROLES")
        if not ids and roles:
            picked = sc.menu(f"Roles for the {tier} tier ({scope}):", roles, many=True, optional=True,
                             manual=False) or []
            ids = [int(i) for i in picked]
        if ids:
            sc.set_line(cfg, "roles = ", f"roles = {json.dumps(ids)}", section=f"[perms.{tier}]")
    owner = (sc.lookup(token, ["owner", guild]) or [{}])[0].get("id", "") if roles is not None else ""
    if not owner:
        return
    if w.yes("Give the server owner the admin tier?", True):
        sc.set_line(cfg, "users = ", f"users = [{owner}]", section="[perms.admin]")
    if w.yes("Let /blowup ping the server owner?", False):
        sc.set_line(cfg, "blowup_user_id = ", f"blowup_user_id = {owner}")


def checks(sc: ModuleType, w: Any, found: list[dict[str, str]]) -> list[dict[str, object]]:
    from shazzzbot import services
    found = [c for c in found if c["webui"] and http(c["webui"]) is None]
    rows = [{"id": c["name"], "name": f"{c['name']}  {c['webui']}"} for c in found]
    if not (w.prompt and rows):
        return []
    picked = sc.menu("Containers with a web page; choose the ones to check:", rows, many=True, optional=True,
                     manual=False) or []
    out: list[dict[str, object]] = []
    for c in found:
        entry: dict[str, object] = {"name": c["name"], "container": c["name"], "url": c["webui"]}
        if c["name"] not in picked:
            continue
        if (error := sc.validator(partial(services.validate, [dict(entry)]))) is not None:
            sc.say(f"  skipped {c['name']}: {error}")
            continue
        out.append(entry)
    return out


def run(sc: ModuleType, w: Any, cfg: list[str], env: list[str], create: set[str], token: str,
        guild: str) -> list[dict[str, object]]:
    from shazzzbot import ups
    found = probe()
    if found and w.prompt:
        sc.say("Found on this server. The bot must reach each address from its container.")
    tz = clean(found.get("timezone"))
    tz = w.ask("SHAZZZBOT_TIMEZONE", "Time zone", sc.timezone, default=tz if tz and not sc.timezone(tz) else "UTC")
    sc.set_line(cfg, "timezone = ", f"timezone = {json.dumps(tz)}")

    def nut_check(value: str) -> str | None:
        return sc.validator(lambda: ups.target(value))

    targets = [t for t in found.get("ups") or [] if clean(t) and nut_check(t) is None] \
        if isinstance(found.get("ups"), list) else []
    if nut := choose(w, "SHAZZZBOT_UPS", targets[0] if targets else "", "Show the UPS {}?",
                     "NUT UPS as name@host:port (empty for none)", nut_check):
        sc.set_line(cfg, "# [ups]", "[ups]")
        sc.set_line(cfg, "# target = ", f"target = {json.dumps(nut)}")
    netdata = clean(found.get("netdata"))
    if w.prompt and not netdata and not os.environ.get("SHAZZZBOT_NETDATA_URL"):
        sc.say(f"Netdata adds the detailed alerts. To install it: {NETDATA_HELP}")
    if url := choose(w, "SHAZZZBOT_NETDATA_URL", netdata, "Use Netdata at {} for alerts?",
                     "Netdata URL (empty for none)", http):
        sc.set_line(cfg, "url = ", f"url = {json.dumps(url)}", section="[netdata]")
    if url := jellyfin(sc, w, found, env, create):
        sc.set_line(cfg, "url = ", f"url = {json.dumps(url)}", section="[jellyfin]")
    rows = containers(found)
    if allow := restarts(sc, w, [c["name"] for c in rows]):
        sc.set_line(cfg, "allow = ", f"allow = {json.dumps(allow)}", section="[restarts]")
    perms(sc, w, cfg, token, guild)
    return checks(sc, w, rows)
