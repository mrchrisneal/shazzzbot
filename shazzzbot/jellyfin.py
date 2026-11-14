"""Active Jellyfin streams, counted by how they play."""
from collections import Counter

import aiohttp

KINDS = ("direct play", "direct stream", "transcode")


def parse(sessions: list[dict]) -> Counter[str]:
    """Jellyfin reports a remux as a Transcode with video and audio both direct; that counts as direct stream."""
    out: Counter[str] = Counter()
    for s in sessions:
        if not s.get("NowPlayingItem"):
            continue
        method = (s.get("PlayState") or {}).get("PlayMethod", "")
        info = s.get("TranscodingInfo") or {}
        if method == "DirectPlay":
            out["direct play"] += 1
        elif method == "DirectStream" or (info.get("IsVideoDirect") and info.get("IsAudioDirect")):
            out["direct stream"] += 1
        else:
            out["transcode"] += 1
    return out


async def streams(session: aiohttp.ClientSession, url: str, key: str) -> Counter[str]:
    async with session.get(url.rstrip("/") + "/Sessions", params={"activeWithinSeconds": "120"},
                           headers={"Authorization": f'MediaBrowser Token="{key}"'}) as r:
        r.raise_for_status()
        return parse(await r.json())
