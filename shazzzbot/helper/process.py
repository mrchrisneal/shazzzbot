from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from asyncio.exceptions import TimeoutError as AsyncTimeoutError
from typing import cast

from . import footprint
from .constants import ENV


async def execute(argv: list[str], timeout: int, cap: int, errors: bool = True,
                  env: dict[str, str] | None = None, pass_fds: tuple[int, ...] = ()) -> tuple[int, list[str], bool]:
    lines: list[str] = []
    if env is None:
        work = str(footprint.ensure_workdir())
        env = {**ENV, "HOME": work, "TMPDIR": work}
    process = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT if errors else asyncio.subprocess.DEVNULL,
                env=env, pass_fds=pass_fds, start_new_session=True)

    completed, truncated = False, False

    async def drain() -> int:
        nonlocal completed, truncated
        size = 0
        pending = bytearray()
        output = cast(asyncio.StreamReader, process.stdout)
        while True:
            chunk = await output.read(1024)
            if not chunk:
                if pending:
                    lines.append(pending.decode("utf-8", "replace"))
                rc = await process.wait()
                completed = True
                return rc
            retained = chunk[:cap - size]
            truncated = truncated or len(retained) < len(chunk)
            size += len(retained)
            offset = len(pending)
            pending.extend(retained)
            start = 0
            for end in range(offset, len(pending)):
                if pending[end] == 10:
                    lines.append(pending[start:end].decode("utf-8", "replace"))
                    start = end + 1
            if start:
                del pending[:start]
    try:
        rc = await asyncio.wait_for(drain(), timeout)
    except AsyncTimeoutError:
        rc = 124
    finally:
        if not completed:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
    return rc, lines, truncated
