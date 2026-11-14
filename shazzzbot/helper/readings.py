from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import multiprocessing
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .auth import canonical
from .platforms.tasks import Collector

log = logging.getLogger(__name__)


def _worker(paths: Mapping[str, Path], cap: int, incoming: int, outgoing: int) -> None:
    os.close(incoming)
    with contextlib.suppress(BrokenPipeError), os.fdopen(outgoing, "wb") as stream:
        try:
            data = canonical({"tasks": Collector(paths, strict=True).collect()})
            if len(data) > cap:
                raise ValueError("tasks output limit exceeded")
        except Exception as error:
            log.exception("tasks collection failed (%s)", type(error).__name__, exc_info=False)
            data = b'{"error":"tasks collection failed"}'
        stream.write(data)


async def _read(reader: asyncio.StreamReader, cap: int) -> bytes:
    try:
        return await reader.readexactly(cap + 1)
    except asyncio.IncompleteReadError as error:
        return error.partial


async def gather(paths: Mapping[str, Path], timeout: float, cap: int) -> dict[str, Any]:
    incoming, outgoing = os.pipe()
    with os.fdopen(incoming, "rb", buffering=0) as source, os.fdopen(outgoing, "wb", buffering=0) as sink:
        child = multiprocessing.get_context("fork").Process(target=_worker, args=(paths, cap, incoming, outgoing),
                                                           daemon=True)
        transport = None
        try:
            child.start()
            sink.close()
            reader = asyncio.StreamReader()
            transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(reader), source)
            limit = max(cap, 128)
            raw = await asyncio.wait_for(_read(reader, limit), timeout)
            if len(raw) > limit:
                raise ValueError("tasks output limit exceeded")
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {"tasks"} or not isinstance(value["tasks"], dict):
                raise ValueError("tasks collection failed")
            return value["tasks"]
        finally:
            if transport is not None:
                transport.close()
            if child.pid is not None:
                if child.is_alive():
                    child.kill()
                child.join(timeout=1)
                if not child.is_alive():
                    child.close()
                else:
                    log.error("tasks worker did not exit within cleanup budget")
