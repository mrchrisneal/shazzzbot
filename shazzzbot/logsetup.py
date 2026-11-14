"""Apply log_level from config.toml. Debug covers only Shazzzbot's own loggers and turns off after debug_hours."""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable

log = logging.getLogger("shazzzbot")


def configure(level: str, hours: int, timer: Callable[..., threading.Timer] = threading.Timer) -> None:
    value = logging.getLevelNamesMapping()[level.upper()]
    root = max(value, logging.INFO)
    logging.getLogger().setLevel(root)
    # delivery.py learns Discord cooldowns from discord.http WARNING records, so never filter those out.
    logging.getLogger("discord.http").setLevel(min(root, logging.WARNING))
    log.setLevel(value)
    if value != logging.DEBUG:
        return
    if not hours:
        log.info("debug logging is on with no time limit")
        return
    t = timer(hours * 3600, off, (hours,))
    t.daemon = True
    t.start()
    log.info("debug logging is on for %d hours", hours)


def off(hours: int) -> None:
    log.setLevel(logging.INFO)
    log.info("debug logging turned off after %d hours", hours)
