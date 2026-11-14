import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import discord
import pytest
from yarl import URL

from shazzzbot import discordlog


def response(status=429, **headers):
    return SimpleNamespace(status=status, reason="limited", headers=headers)


@pytest.mark.parametrize("scope", ["user", "shared", "global"])
def test_rate_limit_line_has_scope_bucket_delay_and_secret_free_route(caplog, scope):
    reply = response(**{"X-RateLimit-Scope": scope, "X-RateLimit-Bucket": "bucket42", "Retry-After": "47.5",
                        "Authorization": "do-not-log-this"})
    route = "https://name:password@discord.com/api/v10/webhooks/42/private-token/messages/1?token=other-secret"
    discordlog.report(reply, "PATCH", route)
    discordlog.report(reply, "PATCH", route)
    records = [record.getMessage() for record in caplog.records]
    assert len(records) == 1
    assert records[0] == (f"Discord 429 scope='{scope}' bucket='bucket42' retry_after_header='47.5' "
                          "retry_after_body='unknown' "
                          "route=PATCH '/api/v10/webhooks/42/[redacted]/messages/1'")
    assert all(secret not in records[0] for secret in ("password", "private-token", "other-secret", "do-not-log-this"))


def test_missing_headers_are_explicit_and_header_newlines_do_not_forge_logs(caplog):
    discordlog.report(response(), "POST", "/interactions/42/private-token/callback")
    assert "scope='unknown' bucket='unknown' retry_after_header='unknown' retry_after_body='unknown'" in caplog.text
    assert "private-token" not in caplog.text
    discordlog.report(response(**{"X-RateLimit-Bucket": "bucket\nforged"}), "GET", "/gateway/bot")
    assert "bucket\\nforged" in caplog.records[-1].getMessage()
    assert "\n" not in caplog.records[-1].getMessage()


def test_sdk_trace_records_each_retry_before_it_is_hidden_and_installs_once(caplog):
    route = discord.http.Route("PATCH", "/channels/{channel_id}/messages/{message_id}", channel_id=1, message_id=2)
    replies = [response(**{"X-RateLimit-Scope": "user", "X-RateLimit-Bucket": "b", "Retry-After": str(i)})
               for i in (16, 47)]
    success = response(200)

    async def go():
        async with aiohttp.ClientSession() as session:
            async def original(_route, *, fail=False):
                for reply in [*replies, success]:
                    params = SimpleNamespace(method="PATCH", url=URL("https://discord.com/api/v10/channels/1/messages/2"),
                                             response=reply)
                    for trace in session.trace_configs:
                        await trace.on_request_end.send(session, SimpleNamespace(), params)
                if fail:
                    raise discord.HTTPException(replies[-1], {"message": "limited", "code": 400009})
                return "sent"
            http = SimpleNamespace(request=original, _HTTPClient__session=session)
            discordlog.install(http)
            with pytest.raises(discord.HTTPException):
                await http.request(route, fail=True)
            assert len(session.trace_configs) == 1
            assert await http.request(route) == "sent"
            assert len(session.trace_configs) == 1
    asyncio.run(go())
    records = [record.getMessage() for record in caplog.records]
    assert len(records) == 2
    assert "retry_after_header='16'" in records[0] and "retry_after_header='47'" in records[1]
    assert all("route=PATCH '/api/v10/channels/1/messages/2'" in line for line in records)


@pytest.mark.parametrize("status", [429, 403])
def test_exception_without_session_trace_still_reports_429_and_propagates(caplog, status):
    error = discord.HTTPException(response(status), {"message": "no", "code": 0})
    http = SimpleNamespace(request=AsyncMock(side_effect=error))
    discordlog.install(http)
    with pytest.raises(discord.HTTPException) as caught:
        asyncio.run(http.request(discord.http.Route("GET", "/users/@me")))
    assert caught.value is error
    assert ("Discord 429" in caplog.text) == (status == 429)


def test_observer_attaches_to_a_recreated_sdk_session():
    http = SimpleNamespace(request=AsyncMock(return_value="ok"))
    discordlog.install(http)
    async def go():
        for _ in range(2):
            async with aiohttp.ClientSession() as session:
                http._HTTPClient__session = session
                assert await http.request(discord.http.Route("GET", "/users/@me")) == "ok"
                assert len(session.trace_configs) == 1
    asyncio.run(go())


@pytest.mark.parametrize("session", [None, {}, "private-session-value", object()])
def test_unsupported_trace_session_warns_once_without_exposing_values_or_changing_requests(caplog, session):
    route = discord.http.Route("POST", "/webhooks/{webhook_id}/{webhook_token}",
                               webhook_id=42, webhook_token="private-route-value")
    result = object()
    original = AsyncMock(return_value=result)
    http = SimpleNamespace(request=original)
    if session is not None:
        http._HTTPClient__session = session
    discordlog.install(http)
    caplog.set_level(logging.INFO, "shazzzbot")
    payload = {"secret": "private-body-value"}
    async def go():
        assert await http.request(route, payload, reason="private-reason-value") is result
        assert await http.request(route, payload, reason="private-reason-value") is result
    asyncio.run(go())
    assert original.await_count == 2
    original.assert_awaited_with(route, payload, reason="private-reason-value")
    records = [record for record in caplog.records if record.name == "shazzzbot"]
    assert len(records) == 1 and records[0].levelno == logging.WARNING
    assert "tracing unavailable" in records[0].getMessage()
    assert "pinned py-cord/aiohttp versions" in records[0].getMessage()
    assert "budgets remain conservative" in records[0].getMessage()
    assert "private-" not in records[0].getMessage()


def test_lazy_sdk_session_is_attached_after_the_first_completed_request_without_warning(caplog):
    route = discord.http.Route("GET", "/users/@me")
    async def go():
        async with aiohttp.ClientSession() as session:
            async def original(_route):
                http._HTTPClient__session = session
                assert not session.trace_configs
                return "sent"
            http = SimpleNamespace(request=original)
            discordlog.install(http)
            assert await http.request(route) == "sent"
            assert len(session.trace_configs) == 1
    asyncio.run(go())
    assert not caplog.records


def test_session_replaced_inside_a_request_is_attached_afterwards(caplog):
    route = discord.http.Route("GET", "/users/@me")
    async def go():
        async with aiohttp.ClientSession() as old, aiohttp.ClientSession() as new:
            async def original(_route):
                assert len(old.trace_configs) == 1 and not new.trace_configs
                http._HTTPClient__session = new
                return "sent"
            http = SimpleNamespace(request=original, _HTTPClient__session=old)
            discordlog.install(http)
            assert await http.request(route) == "sent"
            assert len(new.trace_configs) == 1
            assert old.trace_configs[0] is new.trace_configs[0]
    asyncio.run(go())
    assert not caplog.records


def test_trace_attachment_retries_later_and_logs_recovery_once(caplog):
    route = discord.http.Route("GET", "/users/@me")
    http = SimpleNamespace(request=AsyncMock(return_value="sent"))
    discordlog.install(http)
    caplog.set_level(logging.INFO, "shazzzbot")
    async def go():
        assert await http.request(route) == "sent"
        assert await http.request(route) == "sent"
        async with aiohttp.ClientSession() as session:
            http._HTTPClient__session = session
            assert await http.request(route) == "sent"
            assert await http.request(route) == "sent"
            assert len(session.trace_configs) == 1
    asyncio.run(go())
    assert [record.getMessage() for record in caplog.records] == [
        ("Discord response tracing unavailable; verify the pinned py-cord/aiohttp versions and restart. "
         "Rate budgets remain conservative."), "Discord response tracing recovered"]


@pytest.mark.parametrize("kind", ["immutable", "append-error"])
def test_unsupported_trace_list_preserves_delegation_and_can_recover(caplog, kind):
    class Unavailable(list):
        def append(self, _value):
            raise RuntimeError("private-adapter-value")
    route = discord.http.Route("GET", "/users/@me")
    caplog.set_level(logging.INFO, "shazzzbot")
    async def go():
        async with aiohttp.ClientSession() as session:
            session._trace_configs = () if kind == "immutable" else Unavailable()
            http = SimpleNamespace(request=AsyncMock(return_value="sent"), _HTTPClient__session=session)
            discordlog.install(http)
            assert await http.request(route) == "sent"
            session._trace_configs = []
            assert await http.request(route) == "sent"
            assert len(session.trace_configs) == 1
    asyncio.run(go())
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 2
    assert "tracing unavailable" in messages[0] and messages[1] == "Discord response tracing recovered"
    assert "private-adapter-value" not in " ".join(messages)


@pytest.mark.parametrize("failure", [OSError("private-error-value"), RuntimeError("private-error-value")])
def test_an_uncompleted_request_does_not_claim_unsupported_tracing_or_replace_its_error(caplog, failure):
    route = discord.http.Route("GET", "/users/@me")
    http = SimpleNamespace(request=AsyncMock(side_effect=failure))
    discordlog.install(http)
    with pytest.raises(type(failure)) as caught:
        asyncio.run(http.request(route))
    assert caught.value is failure
    assert not caplog.records
