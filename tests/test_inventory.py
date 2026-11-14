import asyncio
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from shazzzbot import view
from shazzzbot.docker import Engine
from tests.test_bot import WEB, make_bot


@pytest.mark.parametrize("error", [TimeoutError(), aiohttp.ClientError("proxy unavailable")])
def test_inventory_failure_keeps_cached_data_marks_its_age_and_recovers(tmp_path, monkeypatch, error):
    clock = [100.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    bot = make_bot(tmp_path)
    bot.docker = MagicMock(list=AsyncMock(side_effect=[[WEB], error, []]),
                           info=AsyncMock(return_value=Engine("27", "overlay2", 1)))

    async def collect():
        return next(section for section in await bot.collect() if section.key == "docker")

    asyncio.run(bot._poll_docker())
    assert asyncio.run(collect()).note == ""
    clock[0] += 125
    asyncio.run(bot._poll_docker())
    section = asyncio.run(collect())
    assert bot.containers == [WEB]
    assert section.note == "Inventory stale; last updated 2m ago"
    assert section.parts
    clock[0] += 60
    assert asyncio.run(collect()).note == "Inventory stale; last updated 3m ago"
    asyncio.run(bot._poll_docker())
    assert bot.containers == [] and asyncio.run(collect()).note == ""


@pytest.mark.parametrize("mode", ["image", "embed", "text"])
@pytest.mark.parametrize("previous", [True, False])
def test_failed_inventory_status_is_visible_in_every_mode(tmp_path, monkeypatch, mode, previous):
    clock = [100.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    bot = make_bot(tmp_path)
    bot.docker = MagicMock(list=AsyncMock(side_effect=[[WEB], TimeoutError()] if previous else [TimeoutError()]),
                           info=AsyncMock(return_value=Engine("27", "overlay2", 0)))
    if previous:
        asyncio.run(bot._poll_docker())
        clock[0] += 125

    async def collect():
        await bot._poll_docker()
        return next(section for section in await bot.collect() if section.key == "docker")

    section = asyncio.run(collect())
    expected = "Inventory stale; last updated 2m ago" if previous else "Inventory unavailable; no successful read"
    assert section.note == expected
    if mode == "image":
        drawn = []
        actual = view.ImageDraw.ImageDraw.text

        def text(draw, xy, value, *args, **kwargs):
            drawn.append(value)
            return actual(draw, xy, value, *args, **kwargs)

        monkeypatch.setattr(view.ImageDraw.ImageDraw, "text", text)
        assert view.image(section).startswith(b"\x89PNG")
        assert expected in drawn
    else:
        assert expected in view.embed(section, mode).description
