"""Shared read-only forge transport and normalized panel entries."""
import asyncio
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from urllib.parse import urlsplit

import aiohttp

from .panel import CRITICAL, FINE, clip

TIMEOUT = aiohttp.ClientTimeout(total=10, connect=3)
BODY_LIMIT = 1024 * 1024


class Problem(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def credential(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 4096 or not value.isascii() or not all(33 <= ord(c) <= 126 for c in value):
        raise Problem("auth failed")
    return value


@dataclass(frozen=True)
class Entry:
    name: str
    values: tuple[str, ...]
    at: float | None = None
    level: int = FINE
    finished: float | None = None


def clean(value: str, token: str, limit: int = 80) -> str:
    value = value.replace(token, "[redacted]") if token else value
    value = value.splitlines()[0] if value.splitlines() else ""
    return clip("".join(c if c.isprintable() else " " for c in value).replace("`", "'").replace("@", "(at)"), limit)


def display(value: str, token: str, base: str, limit: int = 80) -> str:
    value = value.replace(token, "[redacted]") if token else value
    url = urlsplit(base)
    for private in (base, f"{url.scheme}://{url.netloc}", url.netloc, url.hostname or ""):
        if private:
            value = value.replace(private, "[redacted]")
    return clean(value, token, limit)


def obj(value: object) -> dict:
    if not isinstance(value, dict):
        raise Problem("invalid data")
    return value


def items(value: object) -> list[dict]:
    if not isinstance(value, list) or len(value) > 100 or not all(isinstance(row, dict) for row in value):
        raise Problem("invalid data")
    return value


def count(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 10**9:
        raise Problem("invalid data")
    return value


def stamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        date = datetime.fromisoformat(value)
        return date.timestamp() if date.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def ci(value: object) -> tuple[str, int]:
    if value in ("success", "neutral"):
        return "pass", FINE
    if value in ("failure", "timed_out", "action_required", "stale", "startup_failure"):
        return "fail", CRITICAL
    if value in ("cancelled", "canceled", "skipped"):
        return "cancelled", FINE
    if value in ("running", "in_progress", "queued", "waiting", "pending", "blocked", "requested"):
        return "running", FINE
    raise Problem("invalid data")


class API:
    def __init__(self, session: aiohttp.ClientSession, base: str, token: str, scheme: str):
        self.session, self.base, self.token = session, base, credential(token)
        self.headers = {"Authorization": f"{scheme} {self.token}", "Accept": "application/json"}
        self.cache: dict[tuple, object] = {}
        self.locks: dict[tuple, asyncio.Lock] = {}
        self.slots = asyncio.Semaphore(4)
        self.auth_failed = False
        self.recent_runs = False

    def text(self, value: object, limit: int = 80) -> str:
        if not isinstance(value, str):
            raise Problem("invalid data")
        return display(value, self.token, self.base, limit)

    async def get(self, path: str, params: Mapping[str, str | int] | None = None) -> object:
        key = (path, tuple(sorted((k, str(v)) for k, v in (params or {}).items())))
        async with self.locks.setdefault(key, asyncio.Lock()):
            if key not in self.cache:
                try:
                    self.cache[key] = await self._get(path, params)
                except Problem as error:
                    self.cache[key] = error
            result = self.cache[key]
        if isinstance(result, Problem):
            raise result
        return result

    async def _get(self, path: str, params: Mapping[str, str | int] | None) -> object:
        async with self.slots:
            if self.auth_failed:
                raise Problem("auth failed", 401)
            try:
                async with self.session.get(self.base + path, params=params, headers=self.headers,
                                           timeout=TIMEOUT, allow_redirects=False) as response:
                    if response.status in (401, 403):
                        self.auth_failed = response.status == 401
                        raise Problem("auth failed", response.status)
                    if response.status != 200:
                        raise Problem(f"HTTP {response.status}", response.status)
                    if response.content_length is not None and response.content_length > BODY_LIMIT:
                        raise Problem("response too large")
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(8192):
                        body.extend(chunk)
                        if len(body) > BODY_LIMIT:
                            raise Problem("response too large")
                    return json.loads(body)
            except TimeoutError:
                raise Problem("timeout") from None
            except aiohttp.ClientError:
                raise Problem("unreachable") from None
            except (ValueError, RecursionError):
                raise Problem("invalid data") from None


class Client(Protocol):
    name: str
    label: str
    default_url: str
    prefix: str
    scheme: str
    features: tuple[str, ...]

    async def ci(self, api: API, repo: str) -> list[Entry]: ...
    async def runs(self, api: API, repo: str) -> list[Entry]: ...
    async def pushes(self, api: API, repo: str) -> list[Entry]: ...
    async def issues(self, api: API, repo: str) -> list[Entry]: ...


async def latest_run(api: API, repo: str, size_key: str, fields: tuple[str, ...]) -> tuple[dict | None, float | None]:
    path = f"/repos/{repo}/actions/runs"
    body = obj(await api.get(path, {"page": 1, size_key: 1}))
    rows = items(body.get("workflow_runs"))
    total = count(body.get("total_count"))
    if not rows:
        if total:
            raise Problem("invalid data")
        return None, None
    if total < len(rows):
        raise Problem("invalid data")
    if total > len(rows):
        tail = obj(await api.get(path, {"page": total, size_key: 1}))
        last = items(tail.get("workflow_runs"))
        if not last:
            raise Problem("invalid data")
        rows = [*rows, *last]
    candidates = []
    for row in rows:
        at = max((value for key in fields if (value := stamp(row.get(key))) is not None), default=None)
        run_id = row.get("id", 0)
        if type(run_id) is not int or run_id < 0 or len(rows) > 1 and at is None:
            raise Problem("invalid data")
        candidates.append((at if at is not None else -float("inf"), run_id, row, at))
    _, _, row, at = max(candidates, key=lambda candidate: candidate[:2])
    return row, at


async def commits(api: API, repo: str, path: str, params: Mapping[str, str | int]) -> list[Entry]:
    try:
        rows = items(await api.get(path, params))
    except Problem as error:
        if error.status != 409:
            raise
        rows = []
    out = []
    for row in rows[:3]:
        sha = api.text(row.get("sha"), 64)
        if not re.fullmatch(r"[a-fA-F0-9]{40}(?:[a-fA-F0-9]{24})?", sha):
            raise Problem("invalid data")
        commit = obj(row.get("commit"))
        message = api.text(commit.get("message"), 64) or "(empty message)"
        at = stamp(obj(commit.get("committer")).get("date"))
        out.append(Entry(api.text(f"{repo} {sha[:7]}", 210), (message,), at))
    return out or [Entry(api.text(repo, 210), ("no commits",))]
