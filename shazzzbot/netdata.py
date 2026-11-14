"""Active alarms from a Netdata agent's /api/v1/alarms."""
from collections.abc import Sequence
from fnmatch import fnmatchcase

import aiohttp

LEVEL = {"WARNING": 1, "CRITICAL": 2}


def parse(data: dict, ignore: Sequence[str] = ()) -> list[tuple[int, str]]:
    """Raised alarms as (level, text), critical first, minus those whose "chart name" matches an ignore glob."""
    rows = []
    for a in data.get("alarms", {}).values():
        lv = LEVEL.get(a.get("status", ""))
        if lv:
            where = a.get("chart", "").split(".", 1)[-1]
            if any(fnmatchcase(f"{where} {a.get('name', '?')}", g) for g in ignore):
                continue
            rows.append((lv, f"{where} {a.get('name', '?')} {a.get('value_string', '')}".strip()))
    return sorted(rows, key=lambda r: (-r[0], r[1]))


async def alarms(session: aiohttp.ClientSession, url: str, ignore: Sequence[str] = ()) -> list[tuple[int, str]]:
    async with session.get(url.rstrip("/") + "/api/v1/alarms") as r:
        r.raise_for_status()
        return parse(await r.json(), ignore)
