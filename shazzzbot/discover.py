"""One-shot Discord REST lookups for setup: the servers the bot is in, their text channels, and the bot icon.

    python -m shazzzbot.discover guilds
    python -m shazzzbot.discover channels GUILD_ID
    python -m shazzzbot.discover icon PNG_PATH

The token comes from DISCORD_TOKEN. Results are JSON lines on stdout. It never uses the gateway, never creates
channels, and never changes permissions."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import aiohttp

from . import SOURCE_URL, __version__

API = "https://discord.com/api/v10"
SNOWFLAKE = re.compile(r"[0-9]{17,20}")
TEXT_TYPES = {0, 5}
CATEGORY = 4
REQUEST_SECONDS = 20
TIMEOUT = aiohttp.ClientTimeout(total=REQUEST_SECONDS)


class DiscoverError(Exception):
    pass


def snowflake(value: str) -> str:
    if not SNOWFLAKE.fullmatch(value):
        raise DiscoverError("a Discord id has 17 to 20 digits")
    return value


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bot {token}", "User-Agent": f"DiscordBot ({SOURCE_URL}, {__version__})"}


async def call(http: Any, method: str, path: str, body: dict[str, str] | None = None) -> Any:
    async with http.request(method, API + path, json=body) as resp:
        if resp.status >= 300:
            raise DiscoverError(f"HTTP {resp.status} for {method} {path}")
        return await resp.json()


async def guilds(http: Any) -> list[dict[str, str]]:
    rows = await call(http, "GET", "/users/@me/guilds")
    return [{"id": str(g["id"]), "name": str(g["name"])} for g in rows]


async def channels(http: Any, guild_id: str) -> list[dict[str, str]]:
    rows = await call(http, "GET", f"/guilds/{snowflake(guild_id)}/channels")
    categories = {str(c["id"]): c for c in rows if c.get("type") == CATEGORY}

    def order(c: dict[str, Any]) -> tuple[int, int]:
        parent = categories.get(str(c.get("parent_id")))
        return (parent.get("position", 0) + 1 if parent else 0, c.get("position", 0))

    return [{"id": str(c["id"]), "name": str(c["name"]),
             "category": str(categories[str(c.get("parent_id"))]["name"]) if str(c.get("parent_id")) in categories
             else ""}
            for c in sorted((c for c in rows if c.get("type") in TEXT_TYPES), key=order)]


async def roles(http: Any, guild_id: str) -> list[dict[str, str]]:
    rows = await call(http, "GET", f"/guilds/{snowflake(guild_id)}/roles")
    return [{"id": str(r["id"]), "name": str(r["name"])}
            for r in sorted(rows, key=lambda r: -int(r.get("position", 0)))
            if str(r["id"]) != guild_id and not r.get("managed")]


async def owner(http: Any, guild_id: str) -> list[dict[str, str]]:
    guild = await call(http, "GET", f"/guilds/{snowflake(guild_id)}")
    return [{"id": snowflake(str(guild["owner_id"]))}]


async def set_icon(http: Any, png: bytes) -> list[dict[str, str]]:
    image = "data:image/png;base64," + base64.b64encode(png).decode()
    results = []
    for target, path, field in (("application", "/applications/@me", "icon"), ("avatar", "/users/@me", "avatar")):
        try:
            await call(http, "PATCH", path, {field: image})
            results.append({"target": target, "result": "ok"})
        except (DiscoverError, aiohttp.ClientError, TimeoutError) as e:
            results.append({"target": target, "result": str(e) or type(e).__name__})
    return results


def permissions(guild: dict, member: dict, roles: list[dict], channel: dict, user: str) -> int:
    role_ids = {str(r) for r in member["roles"]} | {str(guild["id"])}
    base = 0
    for role in roles:
        if str(role["id"]) in role_ids:
            base |= int(role["permissions"])
    if str(guild.get("owner_id")) == user or base & 8:
        return (1 << 53) - 1
    overwrites = channel.get("permission_overwrites", [])
    everyone: dict[str, Any] = next((o for o in overwrites if str(o["id"]) == str(guild["id"])), {})
    base = (base & ~int(everyone.get("deny", 0))) | int(everyone.get("allow", 0))
    allow = deny = 0
    for overwrite in overwrites:
        if overwrite["type"] == 0 and str(overwrite["id"]) in role_ids - {str(guild["id"])}:
            allow |= int(overwrite["allow"])
            deny |= int(overwrite["deny"])
    base = (base & ~deny) | allow
    own: dict[str, Any] = next((o for o in overwrites if o["type"] == 1 and str(o["id"]) == user), {})
    return (base & ~int(own.get("deny", 0))) | int(own.get("allow", 0))


async def ready(http: Any, path: str) -> list[dict[str, str]]:
    from . import config, health
    cfg = config.load(path)
    if not health.ok():
        raise DiscoverError("main loop is not healthy")
    guild_id = snowflake(str(cfg.guild_id))
    try:
        guild = await call(http, "GET", f"/guilds/{guild_id}")
        user = snowflake(str((await call(http, "GET", "/users/@me"))["id"]))
        member = await call(http, "GET", f"/guilds/{guild_id}/members/{user}")
        roles = await call(http, "GET", f"/guilds/{guild_id}/roles")
        targets = {o.channel: 1024 | 2048 | 16384 | 32768 | 65536 for o in cfg.outputs}
        for extra in (cfg.audit_channel_id, cfg.commands_channel_id):
            if extra:
                targets[extra] = targets.get(extra, 0) | 1024 | 2048
        if not targets or str(guild["id"]) != guild_id:
            raise DiscoverError("configured guild or output channels are not accessible")
        for target, required in targets.items():
            channel = await call(http, "GET", f"/channels/{snowflake(str(target))}")
            if channel["type"] not in TEXT_TYPES or str(channel["guild_id"]) != guild_id \
                    or permissions(guild, member, roles, channel, user) & required != required:
                raise DiscoverError("configured channel lacks required bot permissions")
    except (KeyError, ValueError, TypeError) as error:
        raise DiscoverError("invalid Discord readiness response") from error
    return [{"result": "ready"}]


async def ready_relay(http: Any) -> list[dict[str, str]]:
    from . import relay
    folder = Path(os.environ.get("RELAY_DIR", "/data"))
    node = relay.Relay(f"https://127.0.0.1:{relay.PORT}",
                       (folder / "api.key").read_text().strip(), relay.fingerprint(str(folder / "cert.pem")))
    level, _, _, _ = await relay.ping(http, node)
    if level != relay.FINE:
        raise DiscoverError("relay ping failed")
    return [{"result": "ready"}]


async def run(argv: Sequence[str], token: str) -> list[dict[str, str]]:
    async with aiohttp.ClientSession(headers=headers(token), timeout=TIMEOUT) as http:
        if list(argv) == ["ready-relay"]:
            return await ready_relay(http)
        if len(argv) == 2 and argv[0] == "ready":
            return await asyncio.wait_for(ready(http, argv[1]), REQUEST_SECONDS)
        if list(argv) == ["guilds"]:
            return await guilds(http)
        if len(argv) == 2 and argv[0] == "channels":
            return await channels(http, argv[1])
        if len(argv) == 2 and argv[0] == "roles":
            return await roles(http, argv[1])
        if len(argv) == 2 and argv[0] == "owner":
            return await owner(http, argv[1])
        if len(argv) == 2 and argv[0] == "icon":
            return await set_icon(http, await asyncio.to_thread(Path(argv[1]).read_bytes))
    raise DiscoverError("usage: discover guilds | channels GUILD_ID | roles GUILD_ID | owner GUILD_ID | icon PNG_PATH")


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    token = os.environ.get("DISCORD_TOKEN", "")
    if not token and args != ["ready-relay"]:
        print("DISCORD_TOKEN is not set", file=sys.stderr)
        return 2
    try:
        rows = asyncio.run(run(args, token))
    except (DiscoverError, aiohttp.ClientError, TimeoutError, OSError) as e:
        print(f"discover failed: {str(e) or type(e).__name__}", file=sys.stderr)
        return 1
    for row in rows:
        print(json.dumps(row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
