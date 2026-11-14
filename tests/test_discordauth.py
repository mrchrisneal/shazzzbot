import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import discord
import pytest

from shazzzbot import discordauth as auth


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(auth.time, "monotonic", lambda: now[0])
    return now


def fail(guard, status, channel=20):
    path = f"/api/v10/channels/{channel}/messages/30" if channel is not None else "/api/v10/gateway/bot"
    return guard.finish(guard.begin(path), status)


@pytest.mark.parametrize("status", [401, 403])
def test_three_auth_failures_block_once_and_reserve_one_probe(clock, caplog, status):
    guard = auth.Guard()
    assert not guard.blocked(20)
    assert [fail(guard, status) for _ in range(3)] == [False, False, True]
    assert guard.blocked(20)
    assert guard.blocked(21) == (status == 401)
    with pytest.raises(auth.Blocked, match="paused"):
        guard.begin("/channels/20/messages/30")
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1
    clock[0] += auth.PROBE_EVERY - 1
    assert guard.blocked(20)
    clock[0] += 1
    assert not guard.blocked(20)
    probe = guard.begin("/channels/20/messages/30")
    assert guard.blocked(20)
    with pytest.raises(auth.Blocked):
        guard.begin("/channels/20/messages/31")
    assert not guard.finish(probe, status)
    assert guard.blocked(20)
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1
    clock[0] += auth.PROBE_EVERY
    probe = guard.begin("/channels/20/messages/31")
    assert not guard.finish(probe, 204)
    assert not guard.blocked() and not guard.blocked(20)


def test_channel_failures_are_consecutive_and_isolated(clock):
    guard = auth.Guard()
    assert not fail(guard, 403)
    assert not fail(guard, 200, 21)
    assert not fail(guard, 403)
    assert fail(guard, 403)
    assert not guard.blocked(21)
    assert not fail(guard, 200, 21)
    assert guard.blocked(20)
    guard.reset()
    assert not fail(guard, 403)
    assert not fail(guard, 200)
    assert [fail(guard, 403) for _ in range(3)] == [False, False, True]


@pytest.mark.parametrize("status", [None, 500, 429])
@pytest.mark.parametrize("failure", [401, 403])
def test_failed_probe_keeps_its_deadline_without_resetting_failures(clock, status, failure):
    guard = auth.Guard()
    for _ in range(3):
        fail(guard, failure)
    clock[0] += auth.PROBE_EVERY
    ticket = guard.begin("/channels/20/messages/30")
    assert not guard.finish(ticket, status)
    assert guard.blocked(20)
    clock[0] += auth.PROBE_EVERY
    assert not guard.blocked(20)
    ticket = guard.begin("/channels/20/messages/30")
    assert not guard.finish(ticket, 200)
    assert not guard.blocked(20)


def test_a_probe_reserves_both_global_and_channel_circuits(clock):
    guard = auth.Guard()
    for _ in range(3):
        fail(guard, 403)
    for _ in range(3):
        fail(guard, 401, 21)
    clock[0] += auth.PROBE_EVERY
    ticket = guard.begin("/channels/20/messages/30")
    assert ticket.global_probe and ticket.local_probe
    with pytest.raises(auth.Blocked):
        guard.begin("/channels/21/messages/30")
    guard.finish(ticket, 200)
    assert not guard.blocked(20) and not guard.blocked(21)


def test_config_reset_invalidates_inflight_tickets(clock):
    guard = auth.Guard()
    ticket = guard.begin("/channels/20/messages/30")
    guard.reset()
    assert not guard.finish(ticket, 401)
    assert not guard.blocked(20)
    assert [fail(guard, 401) for _ in range(3)] == [False, False, True]
    guard.reset()
    assert not guard.blocked(20)


def test_channel_less_403_only_logs_and_never_blocks(clock, caplog):
    guard = auth.Guard()
    for _ in range(5):
        assert not fail(guard, 403, None)
    assert not guard.blocked()
    assert len(caplog.records) == 5
    assert all("without a channel" in record.getMessage() for record in caplog.records)
    assert not fail(guard, 200, None)
    assert guard.begin("https://discord.com/api/v10/channels/20/messages/30?token=private").channel == 20
    assert "private" not in caplog.text


@pytest.mark.parametrize("status", [401, 403])
def test_queued_request_cannot_pass_a_new_circuit_or_reserve_its_probe(clock, status):
    guard = auth.Guard()
    queued = guard.begin("/channels/20/messages/30")
    guard.check(queued)
    for _ in range(3):
        fail(guard, status)
    with pytest.raises(auth.Blocked):
        guard.check(queued)
    clock[0] += auth.PROBE_EVERY
    with pytest.raises(auth.Blocked):
        guard.check(queued)
    probe = guard.begin("/channels/20/messages/30")
    guard.check(probe)
    guard.finish(probe, None)
    with pytest.raises(auth.Blocked):
        guard.check(probe)


def test_trace_checks_use_current_states_after_a_config_reset(clock):
    guard = auth.Guard()
    channel_less = guard.begin("/gateway/bot")
    guard.check(channel_less)
    for status in (401, 403):
        guard.reset()
        for _ in range(3):
            fail(guard, status)
        clock[0] += auth.PROBE_EVERY
        old_probe = guard.begin("/channels/20/messages/30")
        guard.reset()
        guard.check(old_probe)
        for _ in range(3):
            fail(guard, status)
        with pytest.raises(auth.Blocked):
            guard.check(old_probe)
    guard.reset()
    queued = guard.begin("/channels/21/messages/30")
    for _ in range(3):
        fail(guard, 403)
    guard.check(queued)


def notice_bot(channel=30, guild=10):
    audit = SimpleNamespace(id=channel, guild=SimpleNamespace(id=guild), send=AsyncMock())
    return SimpleNamespace(audit_channel=lambda: audit, cfg=SimpleNamespace(guild_id=10),
                           rate_budget=SimpleNamespace(auth=auth.Guard())), audit


@pytest.mark.parametrize("reason", ["global", "missing", "same", "guild", "blocked"])
def test_notice_only_uses_a_different_working_guild_audit_channel(clock, reason):
    bot, audit = notice_bot()
    target = 20
    if reason == "global":
        target = None
    elif reason == "missing":
        bot.audit_channel = lambda: None
    elif reason == "same":
        target = audit.id
    elif reason == "guild":
        audit.guild.id = 11
    else:
        for _ in range(3):
            fail(bot.rate_budget.auth, 403, audit.id)
    asyncio.run(auth.notify(bot, target))
    audit.send.assert_not_awaited()


def test_notice_has_suppressed_mentions_and_a_bounded_wait(clock, monkeypatch):
    bot, audit = notice_bot()
    wait = AsyncMock(wraps=asyncio.wait_for)
    monkeypatch.setattr(auth.asyncio, "wait_for", wait)
    asyncio.run(auth.notify(bot, 20))
    audit.send.assert_awaited_once()
    assert audit.send.await_args.args == (
        "Output <#20> blocked after 3 Discord 403 replies; checking again in 15 minutes.",)
    assert audit.send.await_args.kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
    assert wait.await_args.args[1] == auth.NOTICE_WAIT


@pytest.mark.parametrize("error", [auth.Blocked("paused"), TimeoutError(), aiohttp.ClientError(),
                                  discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "secret")])
def test_notice_failure_is_safe_and_does_not_expose_error_content(clock, caplog, error):
    bot, audit = notice_bot()
    audit.send.side_effect = error
    asyncio.run(auth.notify(bot, 20))
    assert len(caplog.records) == 1
    assert caplog.records[0].getMessage() == "could not post the Discord authorization notice to the audit channel"
    assert "secret" not in caplog.text
