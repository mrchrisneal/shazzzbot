from __future__ import annotations

import logging
from typing import ClassVar

import pytest

from shazzzbot import logsetup, schema

NAMES = ("", "shazzzbot", "discord.http", "discord.gateway")


class FakeTimer:
    made: ClassVar[list[FakeTimer]] = []

    def __init__(self, delay, fn, args):
        self.delay, self.fn, self.args, self.daemon, self.started = delay, fn, args, False, False
        FakeTimer.made.append(self)

    def start(self):
        self.started = True


@pytest.fixture(autouse=True)
def restore_levels():
    saved = {n: logging.getLogger(n).level for n in NAMES}
    FakeTimer.made = []
    yield
    for n, level in saved.items():
        logging.getLogger(n).setLevel(level)


def levels():
    return {n: logging.getLogger(n).getEffectiveLevel() for n in NAMES}


def test_defaults():
    assert schema.default("config", "log_level") == "info"
    assert schema.default("config", "debug_hours") == 24


def test_info_sets_no_timer():
    logsetup.configure("info", 24, FakeTimer)
    assert set(levels().values()) == {logging.INFO}
    assert FakeTimer.made == []


def test_debug_is_own_loggers_only_and_reverts():
    logsetup.configure("debug", 24, FakeTimer)
    assert levels() == {"": logging.INFO, "shazzzbot": logging.DEBUG, "discord.http": logging.INFO,
                        "discord.gateway": logging.INFO}
    assert not logging.getLogger("discord.gateway").isEnabledFor(logging.DEBUG)
    [t] = FakeTimer.made
    assert (t.delay, t.daemon, t.started) == (24 * 3600, True, True)
    t.fn(*t.args)
    assert logging.getLogger("shazzzbot").getEffectiveLevel() == logging.INFO


def test_debug_zero_hours_has_no_limit():
    logsetup.configure("debug", 0, FakeTimer)
    assert logging.getLogger("shazzzbot").getEffectiveLevel() == logging.DEBUG
    assert FakeTimer.made == []


def test_error_keeps_discord_rate_limit_warnings():
    logsetup.configure("error", 24, FakeTimer)
    assert levels()[""] == logging.ERROR
    assert logging.getLogger("discord.http").isEnabledFor(logging.WARNING)
    assert not logging.getLogger("discord.gateway").isEnabledFor(logging.WARNING)


@pytest.mark.parametrize(("key", "value"), [("log_level", "verbose"), ("log_level", "DEBUG"),
                                            ("debug_hours", -1), ("debug_hours", 169), ("debug_hours", True)])
def test_schema_rejects(key, value):
    with pytest.raises(SystemExit):
        schema.validate_field("config", key, value, key)


@pytest.mark.parametrize(("key", "value"), [("log_level", "warning"), ("debug_hours", 0), ("debug_hours", 168)])
def test_schema_accepts(key, value):
    schema.validate_field("config", key, value, key)
