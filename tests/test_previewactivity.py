import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from shazzzbot import delivery, previewactivity
from shazzzbot.bot import IMAGE_WAIT, UPLOAD_WAIT


def test_the_reader_reports_the_running_stage_and_clears_after(monkeypatch):
    monkeypatch.setattr(delivery, "keys", lambda _bot, _m: ("disks", "app_history"))
    seen = []
    owner = SimpleNamespace(owner=None, position=0)
    owner.messages = [owner, SimpleNamespace(owner=owner, position=1), SimpleNamespace(owner=owner, position=2)]
    second = owner.messages[1]

    class Bot:
        async def image_files(self, board=None):
            seen.append(read())
            return ["png"]

        async def _send(self, board, send, embeds, files, keys, edit):
            seen.append(read())

    bot = Bot()
    read = previewactivity.watch(bot)
    assert read() is None

    async def go():
        await bot.image_files(second)
        await bot._send(second, None, [], ["f"], ("disks",), edit=True)
        await bot._send(second, None, [], None, ("disks",), edit=True)
        await bot._send(owner, None, [], None, (), edit=False)
    asyncio.run(go())
    strip = [{k: v for k, v in s.items() if k != "since"} for s in seen]
    assert strip == [{"stage": "render", "targets": ["Disks", "App History"], "n": 2, "of": 3},
                     {"stage": "upload", "targets": ["Disks"], "n": 2, "of": 3,
                      "timeout": IMAGE_WAIT + UPLOAD_WAIT},
                     {"stage": "edit", "targets": [], "n": 2, "of": 3, "timeout": UPLOAD_WAIT},
                     {"stage": "send", "targets": [], "n": 1, "of": 3, "timeout": UPLOAD_WAIT}]
    assert read() is None


@pytest.mark.parametrize("operation,files,edit", [("render", None, False), ("upload", ["png"], True),
                                                ("edit", None, True), ("send", None, False)])
def test_failures_remain_visible_without_exposing_exception_text_and_new_work_clears_them(
        monkeypatch, operation, files, edit):
    monkeypatch.setattr(delivery, "keys", lambda _bot, _m: ("system",))
    monkeypatch.setattr(previewactivity.time, "time", lambda: 42)
    board = SimpleNamespace(owner=None, position=0)
    board.messages = [board]
    inner = AsyncMock(side_effect=RuntimeError("private failure text"))
    bot = SimpleNamespace(image_files=inner, _send=inner)
    read = previewactivity.watch(bot)

    async def call():
        if operation == "render":
            return await bot.image_files(board)
        return await bot._send(board, None, [], files, ("system",), edit=edit)

    with pytest.raises(RuntimeError, match="private failure text"):
        asyncio.run(call())
    expected = {"stage": "failed", "operation": operation, "reason": "RuntimeError", "since": 42,
                "targets": ["System"] if operation in {"render", "upload"} else [], "n": 1, "of": 1}
    if operation != "render":
        expected["timeout"] = UPLOAD_WAIT + (IMAGE_WAIT if files is not None else 0)
    assert read() == expected and "private failure text" not in repr(read())
    changed = read()
    changed["reason"] = "changed"
    assert read() == expected
    inner.side_effect = None
    inner.return_value = ["rendered"]
    assert asyncio.run(call()) == ["rendered"]
    assert read() is None


def test_cancellation_finishes_the_stage_and_retains_only_a_safe_reason(monkeypatch):
    monkeypatch.setattr(previewactivity.time, "time", lambda: 42)
    entered = asyncio.Event()

    async def image_files():
        entered.set()
        await asyncio.Event().wait()

    bot = SimpleNamespace(image_files=image_files, _send=AsyncMock())
    read = previewactivity.watch(bot)

    async def go():
        task = asyncio.create_task(bot.image_files())
        await asyncio.wait_for(entered.wait(), 2)
        assert read() == {"stage": "render", "targets": [], "since": 42}
        task.cancel("private cancellation detail")
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(go())
    assert read() == {"stage": "failed", "operation": "render", "reason": "cancelled", "targets": [], "since": 42}


@pytest.mark.parametrize("files", [None, [], ["png"]])
def test_live_send_timeout_uses_the_bots_actual_waits_without_changing_the_operation(files):
    seen = []

    async def send(*_args, **_kwargs):
        seen.append(read())
        return "sent"

    bot = SimpleNamespace(image_files=AsyncMock(), _send=send)
    board = SimpleNamespace(owner=None, position=0)
    read = previewactivity.watch(bot)
    assert asyncio.run(bot._send(board, None, [], files, ("system",), edit=True)) == "sent"
    [stage] = seen
    assert stage["timeout"] == UPLOAD_WAIT + (IMAGE_WAIT if files is not None else 0)
    assert stage["stage"] == ("upload" if files else "edit")
    assert stage["targets"] == (["System"] if files else [])
    assert read() is None


def test_a_newer_failure_remains_visible_after_older_work_finishes():
    entered, release = asyncio.Event(), asyncio.Event()

    async def image_files():
        entered.set()
        await release.wait()

    bot = SimpleNamespace(image_files=image_files, _send=AsyncMock(side_effect=OSError("private detail")))
    read = previewactivity.watch(bot)
    board = SimpleNamespace(owner=None, position=0)

    async def go():
        older = asyncio.create_task(bot.image_files())
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(OSError, match="private detail"):
            await bot._send(board, None, [], ["png"], ("system",), edit=True)
        failure = read()
        assert failure["stage"] == "failed" and failure["operation"] == "upload"
        release.set()
        await asyncio.wait_for(older, 2)
        assert read() == failure
    asyncio.run(go())


@pytest.mark.parametrize("older_first", [False, True])
def test_an_older_failure_never_replaces_a_newer_stage_or_its_success(older_first):
    entered = [asyncio.Event(), asyncio.Event()]
    releases = [asyncio.Event(), asyncio.Event()]

    async def image_files():
        entered[0].set()
        await releases[0].wait()
        raise ValueError("private detail")

    async def send(*_args, **_kwargs):
        entered[1].set()
        await releases[1].wait()

    bot = SimpleNamespace(image_files=image_files, _send=send)
    read = previewactivity.watch(bot)
    board = SimpleNamespace(owner=None, position=0)

    async def go():
        older = asyncio.create_task(bot.image_files())
        await asyncio.wait_for(entered[0].wait(), 2)
        newer = asyncio.create_task(bot._send(board, None, [], None, (), edit=True))
        await asyncio.wait_for(entered[1].wait(), 2)
        assert read()["stage"] == "edit"
        if older_first:
            releases[0].set()
            with pytest.raises(ValueError, match="private detail"):
                await asyncio.wait_for(older, 2)
            assert read()["stage"] == "edit"
            releases[1].set()
            await asyncio.wait_for(newer, 2)
        else:
            releases[1].set()
            await asyncio.wait_for(newer, 2)
            assert read()["stage"] == "render"
            releases[0].set()
            with pytest.raises(ValueError, match="private detail"):
                await asyncio.wait_for(older, 2)
        assert read() is None
    asyncio.run(go())


def observed_bot(inner=None):
    board = SimpleNamespace(owner=None, position=0, embed_keys=["system", "services"],
                            modes={"system": "image", "services": "text"})
    board.messages = [board]

    async def invoke(_board, send, embeds, files, _keys, edit):
        return await send(embeds=embeds, files=files)

    bot = SimpleNamespace(main=board, image_files=AsyncMock(), _send=inner or invoke)
    previewactivity.watch(bot)
    image = discord.Embed().set_image(url="attachment://system.png")
    text = discord.Embed(description="Services")
    return bot, board, [image, text]


def test_only_successful_sdk_completion_records_per_embed_success(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(previewactivity.time, "time", lambda: now[0])
    bot, board, embeds = observed_bot()
    answer = object()

    async def sdk(**kwargs):
        assert bot._preview_delivery == {}
        assert kwargs["embeds"] is embeds
        now[0] = 12.0
        return answer

    files = [SimpleNamespace(filename="system.png")]
    assert asyncio.run(bot._send(board, sdk, embeds, files, (), edit=True)) is answer
    assert bot._preview_delivery == {
        "system:image": {"last_success": 12, "last_failed": False, "reason": ""},
        "services:text": {"last_success": 12, "last_failed": False, "reason": ""},
    }


@pytest.mark.parametrize("previous", [False, True])
@pytest.mark.parametrize("cancelled", [False, True])
def test_sdk_failures_preserve_last_success_and_exception_identity(monkeypatch, previous, cancelled):
    now = [10.0]
    monkeypatch.setattr(previewactivity.time, "time", lambda: now[0])
    bot, board, embeds = observed_bot()
    files = [SimpleNamespace(filename="system.png")]
    if previous:
        asyncio.run(bot._send(board, AsyncMock(), embeds, files, (), edit=True))
    now[0] = 20
    error = asyncio.CancelledError("private detail") if cancelled else RuntimeError("private detail")
    with pytest.raises(type(error)) as caught:
        asyncio.run(bot._send(board, AsyncMock(side_effect=error), embeds, files, (), edit=True))
    assert caught.value is error
    for name in ("system:image", "services:text"):
        assert bot._preview_delivery[name] == {"last_success": 10 if previous else None, "last_failed": True,
                                              "reason": "cancelled" if cancelled else "RuntimeError"}
    assert "private detail" not in repr(bot._preview_delivery)


def test_text_only_success_keeps_the_images_last_success_and_failed_state(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(previewactivity.time, "time", lambda: now[0])
    bot, board, embeds = observed_bot()
    files = [SimpleNamespace(filename="system.png")]
    asyncio.run(bot._send(board, AsyncMock(), embeds, files, (), edit=True))
    with pytest.raises(OSError):
        asyncio.run(bot._send(board, AsyncMock(side_effect=OSError()), embeds, files, (), edit=True))
    old_image = dict(bot._preview_delivery["system:image"])
    now[0] = 30
    asyncio.run(bot._send(board, AsyncMock(), embeds, None, (), edit=True))
    assert bot._preview_delivery["system:image"] == old_image
    assert bot._preview_delivery["services:text"] == {"last_success": 30, "last_failed": False, "reason": ""}


def test_rescheduling_without_an_sdk_call_is_not_a_success():
    old = object()
    bot, board, embeds = observed_bot(AsyncMock(return_value=old))
    sdk = AsyncMock()
    assert asyncio.run(bot._send(board, sdk, embeds, None, (), edit=True)) is old
    sdk.assert_not_awaited()
    assert bot._preview_delivery == {}


def test_other_outputs_do_not_enter_the_main_outputs_history():
    bot, board, embeds = observed_bot()
    other = SimpleNamespace(owner=None, position=0, messages=[], embed_keys=board.embed_keys, modes=board.modes)
    sdk = AsyncMock(return_value="other")
    files = [SimpleNamespace(filename="system.png")]
    assert asyncio.run(bot._send(other, sdk, embeds, files, (), edit=True)) == "other"
    sdk.assert_awaited_once()
    assert bot._preview_delivery == {}
