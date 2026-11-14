"""Guided configuration, schema checks, and confirmed config updates for setup.sh."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable
from functools import partial
from pathlib import Path

sys.path.insert(0, os.environ.get("SHAZZZBOT_APP", "/app"))
from shazzzbot.helper.constants import REPO

SNOWFLAKE = re.compile(r"[0-9]{17,20}")
TOKEN = re.compile(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{20,}")
NAME = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?|[0-9a-fA-F:]+")


APPS = "https://discord.com/developers/applications"
INVITE = "https://discord.com/oauth2/authorize?client_id={}&scope=bot+applications.commands&permissions=117760"
ICON_URL = f"https://raw.githubusercontent.com/{REPO}/main/assets/icon.png"
COPY_ID = ("Turn on User Settings > Advanced > Developer Mode in Discord, then right-click the server or channel "
           "and click Copy ID.")


def say(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


class Missing(Exception):
    pass


class Wizard:
    def __init__(self, prompt: bool) -> None:
        self.prompt = prompt

    def ask(self, env: str, question: str, check: Callable[[str], str | None], *, required: bool = True,
            secret: bool = False, default: str = "") -> str:
        value = os.environ.get(env, "").strip()
        if value:
            if (error := check(value)) is not None:
                raise SystemExit(f"{env}: {error}")
            return value
        if not self.prompt:
            if required and not default:
                raise Missing(env)
            return default
        for _ in range(3):
            error = None
            label = f"{question} [{default}]: " if default else f"{question}: "
            answer = (getpass.getpass(label) if secret and sys.stdin.isatty() else input_line(label)).strip() or default
            if not answer and not required:
                return ""
            if answer and (error := check(answer)) is None:
                return answer
            print(f"  {error or 'a value is needed'}", file=sys.stderr)
        raise SystemExit(f"no valid answer for: {question}")

    def yes(self, question: str, default: bool = False) -> bool:
        if not self.prompt:
            return False
        answer = input_line(f"{question} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        return answer in {"y", "yes"} or (default and not answer)


def input_line(label: str) -> str:
    print(label, end="", file=sys.stderr, flush=True)
    # One byte at a time: setup.sh reads the same stdin after this process ends, so nothing past the line is taken.
    data = b""
    while not data.endswith(b"\n") and (byte := os.read(0, 1)):
        data += byte
    if not data:
        raise SystemExit("no input")
    return data.decode(errors="replace")


def pattern(regex: re.Pattern[str], message: str) -> Callable[[str], str | None]:
    return lambda v: None if regex.fullmatch(v) else message


def validator(fn: Callable[[], object]) -> str | None:
    try:
        fn()
    except (SystemExit, ValueError, TypeError) as e:
        return str(e) or type(e).__name__
    return None


def timezone(value: str) -> str | None:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError):
        return 'use a name like "America/Los_Angeles"'
    return None


def set_line(lines: list[str], prefix: str, new: str) -> None:
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            lines[i] = new
            return
    raise SystemExit(f"example file has no line starting with {prefix!r}")


def toml_block(table: str, entry: dict[str, object]) -> str:
    return f"\n[[{table}]]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in entry.items())


def services(w: Wizard) -> list[dict[str, object]]:
    from shazzzbot import services as checks
    out: list[dict[str, object]] = []
    while w.yes("Add a service check?"):
        entry: dict[str, object] = {"name": w.ask("", "  Name", lambda v: None)}
        container = w.ask("", "  Container name (empty for none)", lambda v: None, required=False)
        if container:
            entry["container"] = container
        url = w.ask("", "  Local URL, or empty for a TCP target", lambda v: None, required=False)
        if url:
            entry["url"] = url
        elif tcp := w.ask("", "  TCP target host:port (empty for none)", lambda v: None, required=False):
            entry["tcp"] = tcp
        if (error := validator(partial(checks.validate, [dict(entry)]))) is not None:
            print(f"  skipped: {error}", file=sys.stderr)
            continue
        out.append(entry)
    return out


def forges(w: Wizard, secrets: dict[str, str]) -> list[dict[str, object]]:
    from shazzzbot import forges as forge_config
    out: list[dict[str, object]] = []
    while len(out) < 3 and w.yes("Add a forge (Forgejo or GitHub)?"):
        kind = w.ask("", "  Type: forgejo or github", lambda v: None if v in {"forgejo", "github"} else
                     "type forgejo or github")
        entry: dict[str, object] = {"type": kind}
        if kind == "forgejo":
            entry["url"] = w.ask("", "  Server URL, without /api/v1", lambda v: None)
        env = f"{kind.upper()}_TOKEN" + (f"_{len(out) + 1}" if len(out) else "")
        entry["token_env"] = env
        token = w.ask("", "  Access token (input is hidden)", lambda v: None, secret=True)
        entry["repos"] = [r.strip() for r in w.ask("", "  Repositories, owner/name, separated by commas",
                                                   lambda v: None).split(",") if r.strip()]
        os.environ[env] = token
        if (error := validator(partial(forge_config.validate, [dict(entry)]))) is not None:
            print(f"  skipped: {error}", file=sys.stderr)
            continue
        secrets[env] = token
        out.append(entry)
    return out


def relay(w: Wizard, env: list[str]) -> None:
    from shazzzbot import config
    url = w.ask("SHAZZZBOT_RELAY_URL", "Relay URL from the relay's first-start log (empty for none)",
                lambda v: None, required=False)
    if not url:
        return
    key = w.ask("SHAZZZBOT_RELAY_KEY", "Relay key (input is hidden)", lambda v: None, secret=True)
    fp = w.ask("SHAZZZBOT_RELAY_FINGERPRINT", "Relay fingerprint", lambda v: None)
    os.environ.update(RELAY_URL=url, RELAY_KEY=key, RELAY_FINGERPRINT=fp)
    if (error := validator(config._relays)) is not None:
        raise SystemExit(error)
    set_line(env, "# RELAY_URL=", f"RELAY_URL={url}")
    set_line(env, "# RELAY_KEY=", f"RELAY_KEY={key}")
    set_line(env, "# RELAY_FINGERPRINT=", f"RELAY_FINGERPRINT={fp}")


def lookup(token: str, argv: list[str]) -> list[dict[str, str]] | None:
    import asyncio

    import aiohttp

    from shazzzbot import discover
    try:
        return asyncio.run(discover.run(argv, token))
    except (discover.DiscoverError, aiohttp.ClientError, OSError) as e:
        say(f"  cannot ask Discord: {str(e) or type(e).__name__}")
        return None


def menu(title: str, rows: list[dict[str, str]], *, many: bool = False,
         optional: bool = False) -> list[str] | None:
    say(title)
    for i, row in enumerate(rows, 1):
        say(f"  {i}) {row['category'] + ' / ' if row.get('category') else ''}{row['name']}")
    extra = ", m to type an id" + (", or empty for none" if optional else "")
    for _ in range(3):
        answer = input_line(f"Choose {'numbers separated by commas' if many else 'a number'}{extra}: ").strip()
        if not answer and optional:
            return []
        if answer.lower() == "m":
            return None
        picks = [p.strip() for p in answer.split(",")] if many else [answer]
        if picks and all(p.isdigit() and 1 <= int(p) <= len(rows) for p in picks):
            return [rows[int(p) - 1]["id"] for p in dict.fromkeys(picks)]
        say("  not a choice")
    raise SystemExit("no valid choice")


def pick_guild(w: Wizard, token: str, app_id: str) -> str:
    snowflake = pattern(SNOWFLAKE, "a Discord id has 17 to 20 digits")
    while w.prompt and not os.environ.get("SHAZZZBOT_GUILD_ID") and token:
        rows = lookup(token, ["guilds"])
        if rows is None:
            break
        if rows:
            picked = menu("Servers the bot is in:", rows)
            if picked:
                return picked[0]
            break
        say("The bot is in no server yet." + (f" Invite it: {INVITE.format(app_id)}" if app_id else ""))
        answer = input_line("r to recheck, m to type an id, or c to cancel: ").strip().lower()
        if answer == "c":
            raise SystemExit("setup cancelled")
        if answer == "m":
            break
    if w.prompt and not os.environ.get("SHAZZZBOT_GUILD_ID"):
        say(COPY_ID)
    return w.ask("SHAZZZBOT_GUILD_ID", "Server (guild) id", snowflake)


def pick_channels(w: Wizard, token: str, guild: str) -> tuple[list[str], str, str]:
    snowflake = pattern(SNOWFLAKE, "a Discord id has 17 to 20 digits")

    def ids(value: str) -> str | None:
        parts = [p.strip() for p in value.split(",")]
        return None if all(SNOWFLAKE.fullmatch(p) for p in parts) else "Discord ids have 17 to 20 digits"

    rows = None
    if w.prompt and token and not os.environ.get("SHAZZZBOT_STATUS_CHANNEL_ID"):
        rows = lookup(token, ["channels", guild])
    if rows:
        status = menu("Text channels:", rows, many=True)
        commands = menu("Commands channel (slash commands also answer here):", rows, optional=True) \
            if status is not None else None
        audit = menu("Audit channel (a private channel for the change log):", rows, optional=True) \
            if commands is not None else None
        if status is not None and commands is not None and audit is not None:
            return status, (commands or [""])[0], (audit or [""])[0]
    if w.prompt and not os.environ.get("SHAZZZBOT_STATUS_CHANNEL_ID"):
        say(COPY_ID)
    status_ids = w.ask("SHAZZZBOT_STATUS_CHANNEL_ID", "Status channel id (more than one: separate with commas)", ids)
    commands_id = w.ask("SHAZZZBOT_COMMANDS_CHANNEL_ID", "Commands channel id (empty for none)", snowflake,
                        required=False)
    audit_id = w.ask("SHAZZZBOT_AUDIT_CHANNEL_ID", "Audit channel id (empty for none)", snowflake, required=False)
    return [p.strip() for p in status_ids.split(",")], commands_id, audit_id


def set_icon(w: Wizard, token: str, app_id: str) -> None:
    if not (os.environ.get("SHAZZZBOT_SET_ICON") == "1" or w.yes("Set the shazzzbot icon on the app and bot?", False)):
        return
    icon = os.path.join(os.environ.get("SHAZZZBOT_APP", "/app"), "assets", "icon.png")
    rows = lookup(token, ["icon", icon]) or []
    for row in rows:
        say(f"  icon for the {row['target']}: {row['result']}")
    if any(row["result"] != "ok" for row in rows) or not rows:
        say(f"  Upload it by hand: download {ICON_URL}" + (f" and add it on {APPS}/{app_id}/information"
                                                          if app_id else ""))


def bot(w: Wizard, examples: Path, create: set[str], bot_only: bool) -> dict[str, str]:
    from shazzzbot import config, ups
    files: dict[str, str] = {}
    env = (examples / ".env.example").read_text().splitlines()
    secrets: dict[str, str] = {}
    snowflake = pattern(SNOWFLAKE, "a Discord id has 17 to 20 digits")
    token = os.environ.get("DISCORD_TOKEN", "")
    if w.prompt and not os.environ.get("SHAZZZBOT_APP_ID"):
        say(f"Create the Discord application: open {APPS} and click New Application.")
    app_id = w.ask("SHAZZZBOT_APP_ID", "Application ID, from the General Information page", snowflake,
                   required=False)
    if app_id:
        say(f"General Information: {APPS}/{app_id}/information")
        say(f"Bot page, where Reset Token shows the token: {APPS}/{app_id}/bot")
        say("Leave all three privileged intents off: the bot needs none of them.")
    if ".env" in create:
        token = w.ask("SHAZZZBOT_DISCORD_TOKEN", "Bot token (input is hidden)",
                      pattern(TOKEN, "a bot token has three parts with dots between them"), secret=True)
        set_line(env, "DISCORD_TOKEN=", f"DISCORD_TOKEN={token}")
        if key := os.environ.get("SHAZZZBOT_HELPER_KEY", ""):
            set_line(env, "# SHAZZZBOT_HELPER_KEY=", f"SHAZZZBOT_HELPER_KEY={key}")
        if socket_dir := os.environ.get("SHAZZZBOT_HELPER_SOCKET_DIR", ""):
            set_line(env, "HELPER_SOCKET_DIR=", f"HELPER_SOCKET_DIR={socket_dir}")
        relay(w, env)
    if app_id:
        say(f"Invite the bot to your server: {INVITE.format(app_id)}")
    if token:
        set_icon(w, token, app_id)
    if "config.toml" in create:
        cfg = (examples / "config.example.toml").read_text().splitlines()
        guild = pick_guild(w, token, app_id)
        status, commands, audit = pick_channels(w, token, guild)
        if len(set(status)) != len(status) or audit and audit in status:
            raise SystemExit("each status channel and the audit channel must be different")
        for channel in [*status, commands, audit]:
            if channel:
                say(f"  check: https://discord.com/channels/{guild}/{channel}")
        tz = w.ask("SHAZZZBOT_TIMEZONE", "Time zone", timezone, default="UTC")
        nut = w.ask("SHAZZZBOT_UPS", "NUT UPS as name@host:port (empty for none)",
                    lambda v: validator(lambda: ups.target(v)), required=False)
        set_line(cfg, "guild_id = ", f"guild_id = {guild}")
        set_line(cfg, "audit_channel_id = ", f"audit_channel_id = {audit or 0}")
        set_line(cfg, "commands_channel_id = ", f"commands_channel_id = {commands or 0}")
        set_line(cfg, "channel = ", f"channel = {status[0]}")
        set_line(cfg, "timezone = ", f"timezone = {json.dumps(tz)}")
        if nut:
            set_line(cfg, "# [ups]", "[ups]")
            set_line(cfg, "# target = ", f"target = {json.dumps(nut)}")
        if bot_only:
            set_line(cfg, "docker_proxy = ", "# docker_proxy = \"http://docker-proxy:2375\"")
        if os.environ.get("SHAZZZBOT_HELPER_KEY"):
            set_line(cfg, "# [helper]", "[helper]")
            set_line(cfg, "# socket = ", 'socket = "/run/shazzzbot-helper/helper.sock"')
            set_line(cfg, "# key_env = ", 'key_env = "SHAZZZBOT_HELPER_KEY"')
        text = "\n".join(cfg) + "\n"
        text += "".join(toml_block("outputs", {"channel": int(c)}) for c in status[1:])
        text += "".join(toml_block("services.checks", s) for s in services(w))
        if ".env" in create:
            text += "".join(toml_block("forges", f) for f in forges(w, secrets))
        elif w.prompt:
            say("Add forges after repair: put their token environment variables in the existing .env first.")
        env.extend(f"{name}={value}" for name, value in secrets.items())
        os.environ["DISCORD_TOKEN"] = token or "unset"
        for line in env:
            name, sep, value = line.partition("=")
            if sep and not name.startswith("#") and value:
                os.environ.setdefault(name, value)
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(text)
        try:
            config.load(f.name)
        finally:
            os.unlink(f.name)
        files["config.toml"] = text
    if ".env" in create:
        files[".env"] = "\n".join(env) + "\n"
    return files


def relay_node(w: Wizard) -> dict[str, str]:
    from shazzzbot import relay as relay_mod

    def names(value: str) -> str | None:
        allow = relay_mod.allowlist(value)
        return None if allow and all(NAME.fullmatch(a) for a in allow) else "list host names or IPs"

    allow = w.ask("SHAZZZBOT_RELAY_ALLOW", "Names this relay may check, separated by spaces", names)
    lines = ["# Relay settings. Never commit this file.", f"RELAY_ALLOW={' '.join(relay_mod.allowlist(allow))}"]

    def extra(value: str) -> str | None:
        os.environ["RELAY_KEYS"] = value
        return validator(lambda: relay_mod.accepted_keys("k" * 32))

    keys = w.ask("SHAZZZBOT_RELAY_KEYS", "Extra bot keys, separated by spaces (empty for none)", extra,
                 required=False, secret=True)
    if keys:
        lines.append(f"RELAY_KEYS={keys}")
    return {".env": "\n".join(lines) + "\n"}


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] in {"--audit-config", "--migrate-config"}:
        return schema_command(argv)
    if argv and argv[0] == "--validate":
        validate_candidate(Path(argv[1]), argv[2])
        return 0
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--examples", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--role", choices=("bot", "relay"), default="bot")
    parser.add_argument("--create", action="append", choices=(".env", "config.toml"), default=[])
    parser.add_argument("--bot-only", action="store_true")
    parser.add_argument("--no-prompt", action="store_true")
    args = parser.parse_args(argv)
    wizard = Wizard(prompt=not args.no_prompt)
    try:
        files = (relay_node(wizard) if args.role == "relay" else
                 bot(wizard, args.examples, set(args.create), args.bot_only))
    except Missing as e:
        print(f"missing required value: set {e} or run without --unattended", file=sys.stderr)
        return 1
    for name, text in files.items():
        if name in args.create:
            fd = os.open(args.out / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(text)
    return 0


def schema_command(argv: list[str]) -> int:
    from tools.setup_files import read_safe
    from tools.setup_schema import audit
    from tools.setup_transaction import migrate_config

    parser = argparse.ArgumentParser(description="Check config settings against the current schema.",
                                     allow_abbrev=False)
    parser.add_argument("action", choices=("audit-config", "migrate-config"))
    parser.add_argument("config", type=Path)
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--transaction", type=Path)
    args = parser.parse_args([argv[0].removeprefix("--"), *argv[1:]])
    if args.action == "migrate-config" and not args.dry_run and args.transaction is None:
        parser.error("--migrate-config needs --transaction for a real migration")
    try:
        if args.config.name != "config.toml":
            raise ValueError("select the installation's config.toml")
        original = read_safe(args.config, private=True)
        report = audit(original)
        print(report.summary())
        if report.errors:
            return 1
        validate_config_bytes(report.candidate, read_safe(args.config.parent / ".env", private=True))
        if args.action == "audit-config" or args.dry_run or report.candidate == original:
            return 0
        if not args.confirm:
            say("Config migration needs explicit confirmation; use --confirm after reviewing the summary.")
            return 1
        from shazzzbot import __version__

        migrate_config(args.config.parent, original, report.candidate, __version__, args.transaction)
        return 0
    except (OSError, ValueError, SystemExit) as error:
        say("! " + str(error))
        return 1


def validate_config_bytes(data: bytes, env: bytes) -> None:
    from shazzzbot import config

    if not hasattr(os, "memfd_create"):
        raise ValueError("config validation requires os.memfd_create; no files were changed")
    values = dotenv(env.decode("utf-8"))
    if values.get("SHAZZZBOT_ROLE", "bot") != "bot":
        raise ValueError("SHAZZZBOT_ROLE in .env must be bot")
    previous = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(values)
        with os.fdopen(os.memfd_create("shazzzbot-config", flags=os.MFD_CLOEXEC), "w+b") as stream:
            stream.write(data)
            stream.flush()
            config.load(f"/proc/self/fd/{stream.fileno()}")
    finally:
        os.environ.clear()
        os.environ.update(previous)


def validate_candidate(folder: Path, role: str) -> None:
    values = dotenv((folder / ".env").read_text())
    if values.get("SHAZZZBOT_ROLE", role) != role:
        raise ValueError("SHAZZZBOT_ROLE in .env must match the selected role")
    os.environ.update(values)
    if role == "bot":
        from shazzzbot import config
        config.load(str(folder / "config.toml"))
    else:
        from shazzzbot import relay as relay_mod
        if not relay_mod.allowlist(os.environ.get("RELAY_ALLOW", "")):
            raise SystemExit("RELAY_ALLOW is required")
        relay_mod.accepted_keys("k" * 32)


def dotenv(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.removeprefix("export ").partition("=")
        name, value = name.strip(), value.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or name in values:
            raise ValueError("invalid or duplicate environment variable name")
        if value.startswith(("'", '"')):
            quote = value[0]
            found = re.fullmatch(r"'([^']*)'\s*(?:#.*)?", value) if quote == "'" else \
                re.fullmatch(r'"((?:[^"\\]|\\[\\"nrt])*)"\s*(?:#.*)?', value)
            if found is None:
                raise ValueError("invalid quoted environment value")
            value = found[1]
            if quote == '"':
                value = re.sub(r'\\([\\"nrt])', lambda m: {"n": "\n", "r": "\r", "t": "\t"}.get(m[1], m[1]), value)
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        if "$" in value or any(c in value for c in "\n\r\0"):
            raise ValueError("environment interpolation and multiline values are not supported by every install method")
        values[name] = value
    return values


if __name__ == "__main__":
    sys.exit(main())
