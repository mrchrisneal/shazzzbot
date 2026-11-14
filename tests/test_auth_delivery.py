import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import discord
import pytest
from yarl import URL

from shazzzbot import board_delivery, chartgen, discordauth, discordlog
from tests.test_bot import AUDIT, OPER, PANEL, channel, ctx, make_bot, wire


def failure(status):
    return discord.HTTPException(SimpleNamespace(status=status, reason="denied"), "denied")


def sdk(error=None):
    original = AsyncMock(side_effect=error, return_value="sent")
    http = SimpleNamespace(request=original)
    notice = AsyncMock()
    budget = discordlog.install(http, on_auth=notice)
    return http, budget, original, notice


def request(http, route, error):
    with pytest.raises(type(error)) as caught:
        asyncio.run(http.request(route))
    assert caught.value is error


@pytest.mark.parametrize("path", ["/users/@me", "/webhooks/1"])
def test_three_bot_token_failures_block_all_bot_routes_and_notify_once(path):
    error = failure(401)
    http, budget, original, notice = sdk(error)
    route = discord.http.Route("GET", path)
    for _ in range(3):
        request(http, route, error)
    notice.assert_awaited_once_with(None)
    for target in (route, discord.http.Route("GET", "/channels/{channel_id}", channel_id=20)):
        with pytest.raises(discordauth.Blocked):
            asyncio.run(http.request(target))
    assert original.await_count == 3 and budget.auth.blocked(20)


@pytest.mark.parametrize("path", ["/interactions/1/expired/callback", "/webhooks/1/expired/messages/@original"])
@pytest.mark.parametrize("status", [401, 404])
def test_expired_interaction_credentials_never_count_toward_bot_auth_failure(path, status):
    error = failure(status)
    http, budget, original, notice = sdk(error)
    route = discord.http.Route("POST", path)
    for _ in range(4):
        request(http, route, error)
    assert not budget.auth.blocked(20)
    notice.assert_not_awaited()
    for _ in range(3):
        ticket = budget.auth.begin("/users/@me")
        budget.auth.finish(ticket, 401)
    assert budget.auth.blocked(20)
    request(http, route, error)
    assert original.await_count == 5


def test_three_channel_forbiddens_do_not_block_other_channels():
    error = failure(403)
    http, budget, original, notice = sdk(error)
    route = discord.http.Route("PATCH", "/channels/{channel_id}/messages/1", channel_id=20)
    for _ in range(3):
        request(http, route, error)
    notice.assert_awaited_once_with(20)
    assert budget.auth.blocked(20) and not budget.auth.blocked(21)
    original.side_effect = None
    assert asyncio.run(http.request(discord.http.Route("GET", "/channels/{channel_id}", channel_id=21))) == "sent"
    assert budget.auth.blocked(20)


def test_forbidden_without_channel_is_logged_without_opening_any_circuit(caplog):
    error = failure(403)
    http, budget, original, notice = sdk(error)
    for _ in range(4):
        request(http, discord.http.Route("GET", "/guilds/10"), error)
    assert not budget.auth.blocked(20) and original.await_count == 4
    assert "403" in caplog.text
    notice.assert_not_awaited()


def test_success_resets_a_consecutive_auth_streak_and_other_errors_pass_through():
    error = failure(401)
    http, budget, original, notice = sdk(error)
    route = discord.http.Route("GET", "/users/@me")
    for _ in range(2):
        request(http, route, error)
    original.side_effect = None
    assert asyncio.run(http.request(route)) == "sent"
    original.side_effect = error
    for _ in range(2):
        request(http, route, error)
    assert not budget.auth.blocked(20)
    timeout = TimeoutError("read timed out")
    original.side_effect = timeout
    request(http, route, timeout)
    assert not budget.auth.blocked(20)
    notice.assert_not_awaited()


def test_blocked_board_skips_fetches_sends_and_cleanup(tmp_path):
    bot = make_bot(tmp_path)
    for _ in range(3):
        bot.rate_budget.auth.finish(bot.rate_budget.auth.begin(f"/channels/{PANEL}/messages"), 403)
    bot.get_channel = AsyncMock()
    bot._sweep = AsyncMock()
    asyncio.run(board_delivery.refresh(bot, bot.main, [], False, False))
    bot.get_channel.assert_not_called()
    bot._sweep.assert_not_awaited()


@pytest.mark.parametrize("stage", ["fetch", "send"])
def test_board_stops_quietly_when_a_queued_request_hits_the_auth_circuit(tmp_path, monkeypatch, caplog, stage):
    bot = make_bot(tmp_path)
    wire(bot, channel())
    bot.main.layout_ready = True
    fetch = AsyncMock(side_effect=discordauth.Blocked() if stage == "fetch" else None)
    monkeypatch.setattr(board_delivery, "_fetch", fetch)
    send = AsyncMock(side_effect=discordauth.Blocked())
    monkeypatch.setattr(board_delivery, "send", send)
    bot._sweep = AsyncMock()
    caplog.clear()
    asyncio.run(board_delivery.refresh(bot, bot.main, [], False, False))
    assert send.await_count == (stage == "send")
    bot._sweep.assert_not_awaited()
    assert not caplog.records


def test_audit_refuses_a_blocked_channel_without_attempting_to_send(tmp_path):
    bot = make_bot(tmp_path)
    audit = channel()
    audit.id = AUDIT
    wire(bot, audit_ch=audit)
    for _ in range(3):
        bot.rate_budget.auth.finish(bot.rate_budget.auth.begin(f"/channels/{AUDIT}/messages"), 403)
    assert not asyncio.run(bot.audit(ctx(OPER).author, "restart web"))
    audit.send.assert_not_awaited()


def test_audit_returns_false_when_its_request_hits_a_new_auth_block(tmp_path):
    bot = make_bot(tmp_path)
    audit = channel()
    audit.send.side_effect = discordauth.Blocked()
    wire(bot, audit_ch=audit)
    assert not asyncio.run(bot.audit(ctx(OPER).author, "restart web"))


def test_chartgen_reports_a_blocked_dm_without_raising():
    caller = ctx(OPER)
    caller.author.send = AsyncMock(side_effect=discordauth.Blocked())
    assert asyncio.run(chartgen.deliver(caller, [], None)) == "error"
    assert caller.respond.await_args.kwargs == {"ephemeral": True}


def test_trace_can_budget_a_request_without_an_sdk_auth_ticket():
    import aiohttp
    from yarl import URL

    async def run():
        async with aiohttp.ClientSession() as session:
            http = SimpleNamespace(request=AsyncMock(return_value="ok"), _HTTPClient__session=session)
            budget = discordlog.install(http)
            route = discord.http.Route("GET", "/users/@me")
            assert await http.request(route) == "ok"
            assert discordlog.AUTH.get() is None
            await session.trace_configs[0].on_request_start.send(
                session, SimpleNamespace(), SimpleNamespace(method="GET", url=URL(route.url)))
            assert len(budget.global_sends) == 1

    asyncio.run(run())


def test_sdk_trace_rechecks_auth_after_waiting_for_a_rate_budget():
    async def run():
        queued, release = asyncio.Event(), asyncio.Event()
        sent, observed = [], []
        route = discord.http.Route("POST", "/channels/{channel_id}/messages", channel_id=20)
        async with aiohttp.ClientSession() as session:
            async def original(_route):
                assert discordlog.AUTH.get() is not None
                params = SimpleNamespace(method="POST", url=URL(route.url))
                try:
                    for trace in session.trace_configs:
                        await trace.on_request_start.send(session, SimpleNamespace(), params)
                except discordauth.Blocked as error:
                    observed.append(error)
                    raise
                sent.append("POST")
                return "sent"

            async def acquire(*_args):
                queued.set()
                await release.wait()

            http = SimpleNamespace(request=original, _HTTPClient__session=session)
            budget = discordlog.install(http)
            budget.acquire = acquire

            async def attempt():
                with pytest.raises(discordauth.Blocked) as caught:
                    await http.request(route)
                assert caught.value is observed[0]
                assert discordlog.AUTH.get() is None

            task = asyncio.create_task(attempt())
            await queued.wait()
            for _ in range(3):
                budget.auth.finish(budget.auth.begin("/channels/20/messages"), 403)
            release.set()
            await task
            assert not sent and len(observed) == 1
            assert budget.auth.blocked(20) and discordlog.AUTH.get() is None

    asyncio.run(run())
