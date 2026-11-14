"""Docker through a docker-socket-proxy: list and info are read-only, restart is the only write the proxy allows."""
import ipaddress
import re
import time
from dataclasses import dataclass, field
from typing import NamedTuple

import aiohttp

from .tasktimes import elapsed

PROJECT = "com.docker.compose.project"
_EXITED = re.compile(r"Exited \((\d+)\)")


class Engine(NamedTuple):
    version: str
    driver: str
    images: int


@dataclass
class Inventory:
    updated: float | None = None
    failed: bool = False

    def success(self) -> None:
        self.updated = time.monotonic()
        self.failed = False

    def note(self) -> str:
        if not self.failed:
            return ""
        if self.updated is None:
            return "Inventory unavailable; no successful read"
        age = elapsed(max(0, time.monotonic() - self.updated))
        return f"Inventory stale; last updated {age} ago"


@dataclass
class Container:
    id: str
    name: str
    state: str
    status: str
    project: str = ""
    networks: tuple[str, ...] = ()
    ports: tuple[int, ...] = ()  # TCP ports published on a non-loopback host address
    image: str = ""
    command: str = field(default="", repr=False)

    @property
    def unhealthy(self) -> bool:
        return "(unhealthy)" in self.status

    @property
    def exit_code(self) -> int | None:
        m = _EXITED.match(self.status)
        return int(m[1]) if m else None


def parse(rows: list[dict]) -> list[Container]:
    return [Container(c["Id"], c["Names"][0].lstrip("/"), c["State"], c.get("Status", ""),
                      (c.get("Labels") or {}).get(PROJECT, ""),
                      tuple(sorted((c.get("NetworkSettings") or {}).get("Networks") or {})),
                      tuple(sorted({p["PublicPort"] for p in c.get("Ports") or () if p.get("PublicPort")
                                    and p.get("Type") == "tcp" and _reachable(p.get("IP", ""))})),
                      c.get("Image", "") if isinstance(c.get("Image", ""), str) else "",
                      c.get("Command", "") if isinstance(c.get("Command", ""), str) else "")
            for c in rows]


def _reachable(ip: str) -> bool:
    """A router can forward to any bind but loopback."""
    if not ip:
        return True
    a = ipaddress.ip_address(ip)
    return not (a.ipv4_mapped or a if isinstance(a, ipaddress.IPv6Address) else a).is_loopback


class Docker:
    def __init__(self, url: str, session: aiohttp.ClientSession):
        self.url = url.rstrip("/")
        self.http = session

    async def list(self) -> list[Container]:
        async with self.http.get(f"{self.url}/containers/json", params={"all": "1"}) as r:
            r.raise_for_status()
            return parse(await r.json())

    async def info(self) -> Engine:
        async with self.http.get(f"{self.url}/info") as r:
            r.raise_for_status()
            d = await r.json()
        return Engine(str(d.get("ServerVersion", "")), str(d.get("Driver", "")), int(d.get("Images", 0)))

    async def restart(self, cid: str, grace: int = 20) -> None:
        """Docker waits `grace` seconds before killing, so the request must outlive that."""
        async with self.http.post(f"{self.url}/containers/{cid}/restart", params={"t": str(grace)},
                                  timeout=aiohttp.ClientTimeout(total=grace + 30)) as r:
            r.raise_for_status()
