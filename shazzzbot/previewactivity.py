"""What the bot is doing right now, for the preview's activity line. Wraps the bot's own image render and send
calls on the instance, so it reports real work and needs no hook in the delivery code."""
import asyncio
import functools
import time
from collections.abc import Callable
from typing import Any

from . import delivery


def _where(message: Any) -> dict:
    board = message.owner or message
    return {"n": message.position + 1, "of": len(getattr(board, "messages", [message]))}


def _names(keys: Any) -> list[str]:
    return [key.replace("_", " ").title() for key in keys]


def _sent(bot: Any, board: Any, send: Callable) -> Callable:
    async def observed(*args: Any, **kwargs: Any) -> Any:
        files = {file.filename for file in kwargs.get("files") or ()}
        targets = []
        for key, embed in zip(board.embed_keys, kwargs.get("embeds", ())):
            mode = board.modes.get(key, "embed")
            if mode != "image" or delivery.image_name(embed) in files:
                targets.append(f"{key}:{mode}")
        try:
            result = await send(*args, **kwargs)
        except (Exception, asyncio.CancelledError) as exc:
            for key in targets:
                old = bot._preview_delivery.get(key, {})
                bot._preview_delivery[key] = {"last_success": old.get("last_success"), "last_failed": True,
                                               "reason": "cancelled" if isinstance(exc, asyncio.CancelledError)
                                               else type(exc).__name__}
            raise
        completed = time.time()
        for key in targets:
            bot._preview_delivery[key] = {"last_success": completed, "last_failed": False, "reason": ""}
        return result
    return observed


def watch(bot: Any) -> Callable[[], dict | None]:
    """Report running work or its last failure until new work supersedes it."""
    from .bot import IMAGE_WAIT, UPLOAD_WAIT

    active: list[tuple[int, dict]] = []
    latest = 0
    failed: dict | None = None
    bot._preview_delivery = {}
    send_inner = bot._send

    async def send_observer(board: Any, send: Any, embeds: Any, files: Any, keys: Any, edit: bool) -> Any:
        owner = board.owner or board
        callback = _sent(bot, board, send) if callable(send) and owner is getattr(bot, "main", None) else send
        return await send_inner(board, callback, embeds, files, keys, edit=edit)

    bot._send = send_observer

    def wrap(name: str, stage: Callable[..., dict]) -> None:
        inner = getattr(bot, name)

        @functools.wraps(inner)
        async def outer(*args: Any, **kwargs: Any) -> Any:
            nonlocal latest, failed
            entry = {**stage(*args, **kwargs), "since": time.time()}
            latest += 1
            running = (latest, entry)
            failed = None
            active.append(running)
            try:
                return await inner(*args, **kwargs)
            except (Exception, asyncio.CancelledError) as exc:
                if running[0] == latest:
                    failed = {**entry, "stage": "failed", "operation": entry["stage"],
                              "reason": "cancelled" if isinstance(exc, asyncio.CancelledError) else type(exc).__name__}
                raise
            finally:
                active.remove(running)
        setattr(bot, name, outer)

    wrap("image_files", lambda board=None: {"stage": "render", **({"targets": _names(delivery.keys(bot, board)),
                                            **_where(board)} if board is not None else {"targets": []})})
    wrap("_send", lambda board, _send, _embeds, files, keys, edit: {
        "stage": "upload" if files else "edit" if edit else "send",
        "timeout": UPLOAD_WAIT + (IMAGE_WAIT if files is not None else 0),
        "targets": _names(keys) if files else [], **_where(board)})
    def read() -> dict | None:
        return dict(failed) if failed is not None else dict(active[-1][1]) if active else None
    return read
