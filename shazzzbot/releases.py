import asyncio
import json
import math
import time
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime

import aiohttp

from .helper.constants import version_number

MAX_BYTES = 65536
MAX_RETRY_AFTER = 3600.0
TIMEOUT = 10.0


@dataclass(frozen=True)
class Check:
    tag: str | None
    error: str | None
    retry_after: float = 0.0


def retry_after(raw: str | None, now: float) -> float:
    if raw is None:
        return 0.0
    try:
        seconds = float(raw)
    except ValueError:
        try:
            date = parsedate_to_datetime(raw)
            if date.tzinfo is None:
                date = date.replace(tzinfo=UTC)
            seconds = date.timestamp() - now
        except (ValueError, OverflowError):
            return 0.0
    return max(0.0, min(MAX_RETRY_AFTER, seconds)) if math.isfinite(seconds) else 0.0


async def fetch(http: aiohttp.ClientSession, repo: str) -> Check:
    retry = 0.0
    try:
        url = f"https://api.github.com/repos/{repo}/releases/latest"
        async with asyncio.timeout(TIMEOUT), http.get(url) as response:
            retry = retry_after(response.headers.get("Retry-After"), time.time())
            if response.status != 200:
                return Check(None, f"HTTP {response.status}", retry)
            data = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                if len(data) + len(chunk) > MAX_BYTES:
                    return Check(None, "response too large", retry)
                data.extend(chunk)
            body = json.loads(data)
            tag = body.get("tag_name") if isinstance(body, dict) else None
            if not isinstance(tag, str):
                return Check(None, "invalid release data", retry)
            version_number(tag)
            return Check(tag, None)
    except TimeoutError:
        return Check(None, "timeout", retry)
    except aiohttp.ClientError:
        return Check(None, "network error", retry)
    except (ValueError, RecursionError):
        return Check(None, "invalid release data", retry)
