import asyncio
from unittest.mock import AsyncMock, MagicMock

from shazzzbot import bot as B
from shazzzbot import delivery, panel, view
from shazzzbot.config import IMAGE_FLOOR, Output
from tests.test_bot import PANEL, channel, edited, make_bot


def test_two_image_channels_due_on_one_boundary_use_distinct_ticks_and_real_footers(tmp_path, monkeypatch):
    start = 1_800_000_000.0
    clock = [start]
    monkeypatch.setattr(B.time, "time", lambda: clock[0])
    monkeypatch.setattr(B.time, "monotonic", lambda: clock[0])
    bot = make_bot(tmp_path, outputs=[Output(PANEL, {"apps": "text"}, image_every=300),
                                    Output(21, image_every=1800)])
    bot._sections = [panel.Section(key, key.title(), [panel.Part([(panel.FINE, ("Value", "1%"))])])
                     for key in ("apps", "tasks")]
    bot.collect = AsyncMock(return_value=bot._sections)
    bot.image_files = AsyncMock(return_value=[MagicMock()])
    channels = {board.channel_id: channel() for board in bot.boards}
    for ch in channels.values():
        ch.send.return_value = edited(MagicMock(id=777))
    monkeypatch.setattr(bot, "get_channel", channels.get)
    async def go():
        await bot.refresh()
        assert bot.image_files.await_count == 1
        first_plan = bot.schedule(clock[0])
        assert first_plan[21][0] == start + IMAGE_FLOOR
        queued = channels[21].send.await_args.kwargs["embeds"]
        assert all(embed.footer.text == view.footer(first_plan[21][0], bot.tz) for embed in queued)
        initial = channels[PANEL].send.await_args.kwargs["embeds"]
        assert all(embed.footer.text == view.footer(first_plan[PANEL][0], bot.tz) for embed in initial)
        clock[0] = start + delivery.TICK
        await bot.refresh(images_only=True)
        assert bot.image_files.await_count == 1
        clock[0] = first_plan[21][0]
        await bot.refresh(images_only=True)
        assert bot.image_files.await_count == 2
        second = channels[21].send.return_value.edit.await_args.kwargs["embeds"]
        assert all(embed.footer.text == view.footer(
            bot.schedule(clock[0])[21][0], bot.tz) for embed in second)
        assert bot.boards[1].image_at - bot.main.image_at >= IMAGE_FLOOR
        assert bot.boards[1].image_at // delivery.TICK != bot.main.image_at // delivery.TICK
    asyncio.run(go())
