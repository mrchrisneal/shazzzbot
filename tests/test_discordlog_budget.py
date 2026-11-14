import asyncio
import copy
import math
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import discord
import pytest
from yarl import URL

from shazzzbot import discordlog as d

ROUTE = d.route_key("PATCH", "/api/v10/channels/20/messages/42")


@pytest.fixture
def clock(monkeypatch):
    value = [1_800_000_000.0]
    monkeypatch.setattr(d.time, "time", lambda: value[0])
    return value


def observe(budget, status=200, media=False, path="/channels/20/messages/42", **overrides):
    headers = {"X-RateLimit-Limit": "5", "X-RateLimit-Remaining": "4", "X-RateLimit-Reset-After": "10",
               "X-RateLimit-Bucket": "bucket", "X-RateLimit-Scope": "user"}
    headers.update(overrides)
    response = SimpleNamespace(status=status, headers=headers)
    budget.observe(response, "PATCH", path, media)
    return response


def test_header_buffer_is_shared_by_classes_and_major_parameter(clock):
    budget = d.Budget()
    observe(budget)
    assert budget.next(ROUTE, clock[0]) == clock[0]
    for media in (False, True, False):
        budget.take(ROUTE, clock[0], media)
    assert budget.next(ROUTE, clock[0], True) == clock[0] + 10
    assert budget.next(ROUTE, clock[0]) == clock[0] + 10
    other = d.route_key("PATCH", "/channels/21/messages/99")
    observe(budget, path="/channels/21/messages/99")
    assert len(budget.buckets) == 2 and budget.next(other, clock[0]) == clock[0]
    observe(budget, path="/channels/20/messages/13", **{"X-RateLimit-Remaining": "3"})
    assert budget.buckets[budget.routes[ROUTE]].remaining == 1
    clock[0] += 10
    budget.take(ROUTE, clock[0])
    assert budget.buckets[budget.routes[ROUTE]].remaining == 4
    observe(budget)
    clock[0] += 20
    observe(budget)
    assert budget.buckets[budget.routes[ROUTE]].remaining == 4


def test_unknown_bucket_obeys_global_ten_percent_buffer_and_acquire_waits(clock, monkeypatch):
    budget = d.Budget()
    for _ in range(d.GLOBAL_USABLE):
        assert budget.next(ROUTE, clock[0]) == clock[0]
        budget.take(ROUTE, clock[0])
    assert budget.next(ROUTE, clock[0]) == clock[0] + 1
    async def sleep(delay):
        clock[0] += delay
    sleeper = AsyncMock(side_effect=sleep)
    monkeypatch.setattr(d.asyncio, "sleep", sleeper)
    asyncio.run(budget.acquire(ROUTE))
    sleeper.assert_awaited_once_with(1.0)
    assert budget.global_sends[-1] == clock[0]
    assert budget.clone().dump() == budget.dump()


def test_media_hidden_cap_records_text_headroom_and_backoff_probes_over_hours(clock):
    budget = d.Budget()
    observe(budget, **{"X-RateLimit-Limit": "100", "X-RateLimit-Remaining": "99"})
    for _ in range(6):
        budget.take(ROUTE, clock[0], True)
        clock[0] += 75
    observe(budget, 429, True, **{"Retry-After": "90", "X-RateLimit-Limit": "100", "X-RateLimit-Remaining": "90"})
    media = budget.summary()[ROUTE + "|media"]
    assert media["divergent"] and budget.summary()[ROUTE + "|text"]["divergent"]
    assert media["sends_before_429"] == 6 and media["sample_window"] == 540
    assert media["429_count"] == 1 and media["retry_after"] == 90 and media["scope"] == "user"
    target = media["target_rate"]
    assert target == pytest.approx(0.9 * 5 / 540)
    assert budget.media_spacing(600) == pytest.approx(120)
    assert budget.next(ROUTE, clock[0], True) >= clock[0] + 90
    clock[0] += 3 * d.PROBE_EVERY
    later = budget.summary()[ROUTE + "|media"]
    assert later["target_rate"] == pytest.approx(target * 1.05**3)
    assert later["evidence"] < media["evidence"]
    budget.take(ROUTE, clock[0], True)
    observe(budget, 429, True, **{"Retry-After": "90", "X-RateLimit-Limit": "100", "X-RateLimit-Remaining": "80"})
    assert budget.summary()[ROUTE + "|media"]["target_rate"] < later["target_rate"]
    for _ in range(10):
        clock[0] += d.DAY
        budget.summary()
    assert budget.summary()[ROUTE + "|media"]["target_rate"] <= 1 / 75
    assert len(budget.summary()[ROUTE + "|media"]["failures"]) == 2


def test_global_scope_and_duplicate_response_are_recorded_once(clock):
    budget = d.Budget()
    response = observe(budget, 429, **{"Retry-After": "40", "X-RateLimit-Scope": "global"})
    budget.observe(response, "PATCH", "/channels/20/messages/42")
    assert budget.summary()[ROUTE + "|text"]["429_count"] == 1
    assert budget.next(d.route_key("GET", "/gateway/bot"), clock[0]) == clock[0] + 40


@pytest.mark.parametrize("value", [None, True, [], "nan", "inf", -1, 10**13, "bad"])
def test_invalid_numbers_are_rejected(value):
    with pytest.raises((ValueError, TypeError)):
        d.number(value)


@pytest.mark.parametrize("headers", [
    {"X-RateLimit-Limit": "1.5"}, {"X-RateLimit-Remaining": "1.5"}, {"X-RateLimit-Reset-After": "0"},
    {"X-RateLimit-Bucket": None}, {"X-RateLimit-Bucket": ""}, {"X-RateLimit-Remaining": "6"},
])
def test_malformed_headers_do_not_invent_a_bucket(clock, headers):
    budget = d.Budget()
    observe(budget, **headers)
    assert budget.routes == {} and budget.next(ROUTE, clock[0]) == clock[0]


def test_no_retry_delay_or_old_sends_does_not_invent_a_sustainable_rate(clock):
    budget = d.Budget()
    observe(budget, 429, **{"Retry-After": None, "X-RateLimit-Scope": "other"})
    state = budget.summary()[ROUTE + "|text"]
    assert state["scope"] == "unknown" and state["retry_after"] == 0 and state["sends_before_429"] == 0
    budget.take(ROUTE, clock[0] - 2 * d.DAY, True)
    observe(budget, 429, True, **{"Retry-After": "10", "X-RateLimit-Remaining": "0"})
    assert budget.summary()[ROUTE + "|media"]["sends_before_429"] == 0


def test_summary_near_buffer_logs_at_most_once_per_minute(clock, caplog):
    budget = d.Budget()
    with caplog.at_level("INFO", logger="shazzzbot"):
        observe(budget, **{"X-RateLimit-Remaining": "1", "X-RateLimit-Reset-After": "300"})
        observe(budget, **{"X-RateLimit-Remaining": "1", "X-RateLimit-Reset-After": "300"})
        clock[0] += 60
        observe(budget, **{"X-RateLimit-Remaining": "1", "X-RateLimit-Reset-After": "300"})
    assert sum("near buffer" in record.getMessage() for record in caplog.records) == 2


def test_persistence_restores_mid_window_and_learned_rate(clock):
    budget = d.Budget()
    observe(budget)
    budget.take(ROUTE, clock[0], True)
    observe(budget, 429, True, **{"Retry-After": "20", "X-RateLimit-Remaining": "1"})
    snapshot = copy.deepcopy(budget.dump())
    restored = d.Budget()
    restored.restore(snapshot)
    assert restored.dump() == snapshot
    assert restored.next(ROUTE, clock[0], True) == budget.next(ROUTE, clock[0], True)
    clock[0] += 20
    assert restored.next(ROUTE, clock[0], True) >= clock[0]


@pytest.mark.parametrize("change", [
    lambda v: v.update(routes=[]),
    lambda v: v.update(buckets={"x": []}),
    lambda v: v["buckets"].update({"x" * 1025: next(iter(v["buckets"].values()))}),
    lambda v: v["routes"].update(bad="missing"),
    lambda v: next(iter(v["evidence"].values())).update(kind="bad"),
    lambda v: next(iter(v["evidence"].values())).update(sends="bad"),
    lambda v: next(iter(v["evidence"].values())).update(sends=[0] * (d.MAX_EVENTS + 1)),
    lambda v: next(iter(v["evidence"].values())).update(failures="bad"),
    lambda v: next(iter(v["evidence"].values())).update(failures=[[]] * (d.MAX_EVENTS + 1)),
    lambda v: v.update(global_sends="bad"), lambda v: v.update(global_sends=[0] * (d.GLOBAL_LIMIT + 1)),
])
def test_corrupt_or_unknown_schema_starts_fresh(clock, caplog, change):
    budget = d.Budget()
    observe(budget)
    snapshot = copy.deepcopy(budget.dump())
    change(snapshot)
    restored = d.Budget()
    restored.restore(snapshot)
    assert restored.dump() is None and "starting fresh" in caplog.text


def test_missing_state_and_restart_bounds(clock):
    budget = d.Budget()
    budget.restore(None)
    assert budget.dump() is None

    observe(budget)
    budget.take(ROUTE, clock[0])
    snapshot = copy.deepcopy(budget.dump())
    state = next(iter(snapshot["evidence"].values()))
    state.update(target=10**10, count=10**10, before=10**10, weight=10**10, limit=10**10,
                 last_429=10**10, probe_at=10**10, until=10**10, retry=10**10, sample_window=10**10,
                 sends=[clock[0] - 2 * d.DAY, clock[0], clock[0] + 1], scope="other")
    snapshot["global_sends"] = [clock[0] - 2, clock[0], clock[0] + 1]
    snapshot["global_until"] = 10**10
    budget.restore(snapshot)
    restored = next(iter(budget.evidence.values()))
    assert restored.count == 10**6 and restored.before == d.MAX_EVENTS
    assert list(restored.sends) == [clock[0]] and list(budget.global_sends) == [clock[0]]
    assert restored.until == budget.global_until == clock[0] + d.DAY
    assert restored.last_429 == restored.probe_at == clock[0]
    assert restored.scope == "unknown" and restored.target <= d.GLOBAL_USABLE


@pytest.mark.parametrize("value", [[], {}, {"format": True}, {"format": 0}, {"format": 2}])
def test_unsupported_rate_budget_refuses_without_resetting_live_evidence(clock, value):
    from shazzzbot.helper.formats import FormatError

    budget = d.Budget()
    observe(budget)
    before = copy.deepcopy(budget.dump())
    with pytest.raises(FormatError):
        budget.restore(value)
    assert budget.dump() == before


def test_record_caps_and_tiny_bucket_can_progress(clock):
    budget = d.Budget()
    for index in range(d.MAX_RECORDS + 1):
        observe(budget, path=f"/channels/{index}/messages/1", **{"X-RateLimit-Bucket": str(index)})
    assert len(budget.routes) == len(budget.buckets) == len(budget.evidence) == d.MAX_RECORDS
    assert ROUTE not in d.Budget().routes
    observe(budget, **{"X-RateLimit-Limit": "1", "X-RateLimit-Remaining": "1"})
    assert budget.next(ROUTE, clock[0]) == clock[0]
    state = budget._state(ROUTE, True)
    state.target = 0
    assert budget.next(ROUTE, clock[0], True) == clock[0]
    assert budget.media_spacing(600) == 75
    assert math.isinf(budget.next(ROUTE, math.inf))
    empty = d.Budget()
    empty._state(ROUTE, False).target = 1
    assert empty.next(ROUTE, clock[0]) == clock[0]


def test_trace_reserves_every_attempt_and_consumes_one_panel_permit(clock, monkeypatch):
    async def sleep(delay):
        clock[0] += delay
    monkeypatch.setattr(d.asyncio, "sleep", sleep)
    async def go():
        async with aiohttp.ClientSession() as session:
            async def original(route, **kwargs):
                for _ in range(2):
                    params = SimpleNamespace(method=route.method, url=URL(route.url))
                    for trace in session.trace_configs:
                        await trace.on_request_start.send(session, SimpleNamespace(), params)
                return "ok"
            http = SimpleNamespace(request=original, _HTTPClient__session=session)
            budget = d.install(http)
            async with budget.permit(ROUTE, True):
                assert await http.request(discord.http.Route("PATCH", "/channels/20/messages/42"), files=["f"]) == "ok"
            assert len(budget.global_sends) == 2
            assert len(budget.evidence[ROUTE + "|media"].sends) == 2
    asyncio.run(go())


@pytest.mark.parametrize("fails", [False, True])
def test_each_response_persists_budget_without_crashing_on_disk_error(clock, caplog, fails):
    callback = (lambda: (_ for _ in ()).throw(OSError("read only"))) if fails else AsyncMock()
    calls = []
    def save():
        calls.append(True)
        if fails:
            callback()
    async def go():
        async with aiohttp.ClientSession() as session:
            async def original(route):
                params = SimpleNamespace(method=route.method, url=URL(route.url),
                                         response=SimpleNamespace(status=200, headers={}))
                await session.trace_configs[0].on_request_end.send(session, SimpleNamespace(), params)
            http = SimpleNamespace(request=original, _HTTPClient__session=session)
            d.install(http, save)
            await http.request(discord.http.Route("GET", "/gateway/bot"))
    asyncio.run(go())
    assert calls == [True] and ("could not persist" in caplog.text) == fails


def test_sdk_body_retry_delay_completes_missing_header_evidence_once(clock):
    budget = d.Budget()
    budget.take(ROUTE, clock[0], True)
    observe(budget, 429, True, **{"Retry-After": None, "X-RateLimit-Scope": "global"})
    budget.defer(ROUTE, 120, True)
    state = budget.evidence[ROUTE + "|media"]
    target = state.target
    assert state.retry == 120 and state.until == budget.global_until == clock[0] + 120
    assert state.failures[-1][2:4] == (0, 120) and state.count == 1
    budget.defer(ROUTE, 120, True)
    assert state.target == target and state.count == 1
    old = d.Budget()
    observe(old, 429, **{"Retry-After": None})
    clock[0] += 2
    old.defer(ROUTE, 10, False)
    assert old.evidence[ROUTE + "|text"].target is None


def test_reservations_move_to_actual_send_and_cannot_refund_a_new_window(clock):
    budget = d.Budget()
    observe(budget)
    async def go():
        async with budget.permit(ROUTE):
            ticket = d.PERMIT.get()
            assert budget.buckets[ticket.bucket].remaining == 3
            budget.release(ticket)
            assert budget.buckets[ticket.bucket].remaining == 4
            assert not budget.global_sends
            budget.release(ticket)
            assert budget.buckets[ticket.bucket].remaining == 4
        async with budget.permit(ROUTE):
            ticket = d.PERMIT.get()
            clock[0] += 11
            observe(budget)
            remaining = budget.buckets[ticket.bucket].remaining
            budget.global_sends.clear()
            budget.evidence[ROUTE + "|text"].sends.clear()
            budget.release(ticket)
            assert budget.buckets[ticket.bucket].remaining == remaining
    asyncio.run(go())


@pytest.mark.parametrize("table,field", [("buckets", "limit"), ("buckets", "remaining"), ("evidence", "count")])
def test_fractional_counters_are_corrupt_state(clock, caplog, table, field):
    budget = d.Budget()
    observe(budget)
    snapshot = copy.deepcopy(budget.dump())
    next(iter(snapshot[table].values()))[field] = 1.5
    budget.restore(snapshot)
    assert budget.dump() is None and "starting fresh" in caplog.text


def test_shared_scope_keeps_separate_evidence_and_learns_its_share(clock, caplog):
    budget = d.Budget()
    observe(budget, **{"X-RateLimit-Limit": "100", "X-RateLimit-Remaining": "99"})
    for _ in range(6):
        budget.take(ROUTE, clock[0], True)
        clock[0] += 75
    reply = SimpleNamespace(status=429, headers={"Retry-After": "1", "X-RateLimit-Scope": "shared",
                                                "X-RateLimit-Bucket": "bucket", "X-RateLimit-Limit": "100",
                                                "X-RateLimit-Remaining": "90", "X-RateLimit-Reset-After": "5"},
                            _shazzzbot_body_retry=37.6)
    budget.observe(reply, "PATCH", "/channels/20/messages/42", True)
    shared = budget.summary()[ROUTE + "|media|shared"]
    assert shared["header_retry_after"] == 1 and shared["body_retry_after"] == 37.6
    assert shared["retry_after"] == 37.6 and "assumed_consumers" not in shared
    assert shared["target_rate"] <= 0.9 / 75
    assert budget.next(ROUTE, clock[0], True) >= clock[0] + 37.6
    assert "retry_after_header='1' retry_after_body=37.6" in caplog.text
    first_shared = copy.deepcopy(shared)
    clock[0] += 1000
    budget.take(ROUTE, clock[0], True)
    observe(budget, 429, True, **{"Retry-After": "10", "X-RateLimit-Scope": "user"})
    snapshot = budget.summary()
    assert snapshot[ROUTE + "|media|user"]["429_count"] == 1
    assert snapshot[ROUTE + "|media|shared"]["429_count"] == first_shared["429_count"] == 1
    assert snapshot[ROUTE + "|media"]["429_count"] == 2
    restored = d.Budget()
    restored.restore(copy.deepcopy(budget.dump()))
    assert restored.summary() == snapshot


@pytest.mark.parametrize("body", [{"retry_after": 37.6}, {}, [], {"retry_after": "bad"}, ValueError("bad JSON")])
def test_trace_reads_retry_body_before_sdk_and_logs_both_delays(clock, caplog, body):
    reply = SimpleNamespace(status=429, headers={"Retry-After": "1", "X-RateLimit-Scope": "shared"},
                            json=AsyncMock(side_effect=body if isinstance(body, Exception) else None,
                                           return_value=body))
    params = SimpleNamespace(response=reply, method="PATCH", url=URL("https://discord.com/api/v10/channels/20/messages/42"))
    budget = d.Budget()
    asyncio.run(d._response(None, None, params, budget=budget))
    summary = budget.summary()[ROUTE + "|text"]
    assert summary["header_retry_after"] == 1
    assert summary["body_retry_after"] == (37.6 if body == {"retry_after": 37.6} else 0)
    assert sum("Discord 429" in record.getMessage() for record in caplog.records) == 1


def test_shared_target_recovers_slowly_and_relearns_when_other_consumer_changes(clock):
    budget = d.Budget()
    observe(budget, media=True)
    budget.take(ROUTE, clock[0], True)
    observe(budget, 429, True, **{"Retry-After": "60", "X-RateLimit-Scope": "shared"})
    initial = budget.summary()[ROUTE + "|media|shared"]["target_rate"]
    for _ in range(8):
        clock[0] += d.PROBE_EVERY
        budget.summary()
    recovered = budget.summary()[ROUTE + "|media|shared"]["target_rate"]
    assert initial < recovered <= initial * 1.05**8
    budget.take(ROUTE, clock[0], True)
    observe(budget, 429, True, **{"Retry-After": "600", "X-RateLimit-Scope": "shared"})
    assert budget.summary()[ROUTE + "|media|shared"]["target_rate"] <= recovered * 0.9
    assert budget.media_spacing(600) <= 600


def test_unknown_scope_rate_is_clamped_when_a_header_limit_is_learned(clock):
    budget = d.Budget()
    budget.take(ROUTE, clock[0])
    observe(budget, 429, **{"Retry-After": "1", "X-RateLimit-Scope": "unknown"})
    assert budget.evidence[ROUTE + "|text"].target is not None
    observe(budget, **{"X-RateLimit-Limit": "2", "X-RateLimit-Remaining": "1", "X-RateLimit-Reset-After": "300"})
    assert budget.evidence[ROUTE + "|text"].target == pytest.approx(1 / 300)


def test_repeated_shared_backoffs_restore_the_previous_target(clock):
    budget = d.Budget()
    budget.take(ROUTE, clock[0], True)
    observe(budget, 429, True, **{"Retry-After": "60", "X-RateLimit-Scope": "shared"})
    clock[0] += 300
    budget.take(ROUTE, clock[0], True)
    observe(budget, 429, True, **{"Retry-After": "60", "X-RateLimit-Scope": "shared"})
    snapshot = copy.deepcopy(budget.dump())
    restored = d.Budget()
    restored.restore(snapshot)
    assert restored.evidence[ROUTE + "|media|shared"].prior_target is not None
    assert restored.dump() == snapshot


def test_policy_deferral_does_not_rewrite_observed_retry_headers_or_body(clock):
    budget = d.Budget()
    reply = SimpleNamespace(status=429, headers={"Retry-After": "1", "X-RateLimit-Scope": "shared"},
                            _shazzzbot_body_retry=37.6)
    budget.observe(reply, "PATCH", "/channels/20/messages/42", True)
    budget.defer(ROUTE, 75, True, body=False)
    state = budget.summary()[ROUTE + "|media"]
    assert state["header_retry_after"] == 1 and state["body_retry_after"] == state["retry_after"] == 37.6
    assert budget.next(ROUTE, clock[0], True) == clock[0] + 75


@pytest.mark.parametrize(("limit", "remaining", "wait"), [(0, 0, 10), (1, 0, 10), (1, 1, 0), (2, 1, 10), (2, 2, 0)])
def test_tiny_buckets_allow_a_send_and_preserve_the_available_buffer(clock, monkeypatch, limit, remaining, wait):
    budget = d.Budget()
    observe(budget, **{"X-RateLimit-Limit": str(limit), "X-RateLimit-Remaining": str(remaining)})
    start = clock[0]
    async def sleep(delay):
        clock[0] += delay
    monkeypatch.setattr(d.asyncio, "sleep", sleep)
    assert asyncio.run(budget.acquire(ROUTE)) == start + wait
    bucket = budget.buckets[budget.routes[ROUTE]]
    assert bucket.buffer == (1 if limit >= 2 else 0)
    assert bucket.remaining >= bucket.buffer
    assert budget.next(ROUTE, clock[0]) <= clock[0] + 10


@pytest.mark.parametrize("limit", [0, 1, 2])
def test_restored_zero_target_and_tiny_limit_recover_after_reset(clock, monkeypatch, limit):
    budget = d.Budget()
    observe(budget, **{"X-RateLimit-Limit": str(limit), "X-RateLimit-Remaining": "0"})
    raw = copy.deepcopy(budget.dump())
    state = next(iter(raw["evidence"].values()))
    state["target"] = 0
    state["sends"] = [clock[0]]
    restored = d.Budget()
    restored.restore(raw)
    start = clock[0]
    async def sleep(delay):
        clock[0] += delay
    monkeypatch.setattr(d.asyncio, "sleep", sleep)
    assert asyncio.run(restored.acquire(ROUTE)) == start + 10
    assert math.isfinite(next(iter(restored.evidence.values())).target)


@pytest.mark.parametrize("delay", [math.inf, d.MAX_IMAGE_SPACING + 1, 400.0, d.MAX_IMAGE_SPACING])
def test_request_wait_is_bounded_even_when_a_deadline_never_advances(clock, monkeypatch, caplog, delay):
    budget = d.Budget()
    monkeypatch.setattr(budget, "next", lambda *args: clock[0] + delay)
    sleeper = AsyncMock()
    monkeypatch.setattr(d.asyncio, "sleep", sleeper)
    with pytest.raises(TimeoutError, match="rate budget wait exceeded"):
        asyncio.run(budget.acquire(ROUTE))
    waits = [call.args[0] for call in sleeper.await_args_list]
    assert all(math.isfinite(wait) and wait <= d.MAX_IMAGE_SPACING for wait in waits)
    assert sum(waits) <= d.MAX_IMAGE_SPACING
    assert "Discord rate budget wait exceeds" in caplog.text


@pytest.mark.parametrize("bad", [0.0, -1.0, math.inf, math.nan])
def test_invalid_live_target_recovers_to_a_finite_rate(clock, bad):
    budget = d.Budget()
    state = budget._state(ROUTE, True, "shared")
    state.target = bad
    budget.take(ROUTE, clock[0], True)
    assert math.isfinite(budget.next(ROUTE, clock[0], True))
    assert state.target == 1 / 75


def test_rate_body_read_has_an_explicit_timeout(clock, monkeypatch):
    observed = []
    native = d.asyncio.wait_for
    async def wait_for(awaitable, timeout):
        observed.append(timeout)
        return await native(awaitable, timeout)
    monkeypatch.setattr(d.asyncio, "wait_for", wait_for)
    reply = SimpleNamespace(status=429, headers={"Retry-After": "1"}, json=AsyncMock(side_effect=TimeoutError))
    params = SimpleNamespace(response=reply, method="PATCH", url=URL("https://discord.com/api/v10/channels/20/messages/42"))
    budget = d.Budget()
    asyncio.run(d._response(None, None, params, budget=budget))
    assert observed == [d.MAX_IMAGE_SPACING]
    assert budget.summary()[ROUTE + "|text"]["429_count"] == 1


def test_budget_persistence_is_debounced_but_429s_force_it(clock, monkeypatch):
    monkeypatch.setattr(d.time, "monotonic", lambda: clock[0])
    async def sleep(delay):
        clock[0] += delay
    monkeypatch.setattr(d.asyncio, "sleep", sleep)
    saves = []
    route = discord.http.Route("PATCH", "/channels/20/messages/42")
    reply = SimpleNamespace(status=200, headers={})
    async def go():
        nonlocal reply
        async with aiohttp.ClientSession() as session:
            async def original(route):
                params = SimpleNamespace(method=route.method, url=URL(route.url), response=reply)
                await session.trace_configs[0].on_request_start.send(session, SimpleNamespace(), params)
                await session.trace_configs[0].on_request_end.send(session, SimpleNamespace(), params)
            http = SimpleNamespace(request=original, _HTTPClient__session=session)
            d.install(http, lambda: saves.append(clock[0]))
            await http.request(route)
            start = clock[0]
            clock[0] += 10
            await http.request(route)
            clock[0] += 49
            await http.request(route)
            assert saves == [start]
            clock[0] += 1
            await http.request(route)
            assert saves == [start, start + 60]
            reply = SimpleNamespace(status=429, headers={"Retry-After": "1"})
            clock[0] += 1
            await http.request(route)
            assert saves[-1] == clock[0] and len(saves) == 3
            await http.request(route)
            assert len(saves) == 3
    asyncio.run(go())


def test_an_older_snapshot_cannot_regain_unrecorded_allowance(clock):
    budget = d.Budget()
    observe(budget)
    raw = copy.deepcopy(budget.dump())
    clock[0] += 3
    restored = d.Budget()
    restored.restore(raw)
    bucket = restored.buckets[restored.routes[ROUTE]]
    assert bucket.remaining == bucket.buffer
    assert restored.next(ROUTE, clock[0]) == next(iter(raw["buckets"].values()))["reset"]
    assert restored.global_until == clock[0] + 1
    clock[0] += 20
    restored.restore(raw)
    reset = restored.buckets[restored.routes[ROUTE]].reset
    assert reset == clock[0] + bucket.window
    snapshot = copy.deepcopy(restored.dump())
    clock[0] += 1
    restored.restore(snapshot)
    assert restored.buckets[restored.routes[ROUTE]].reset == reset


@pytest.mark.parametrize("limit", [0, 1, 2])
def test_tiny_bucket_reservation_reaches_the_wire_at_its_planned_time(clock, monkeypatch, limit):
    async def sleep(delay):
        clock[0] += delay
    monkeypatch.setattr(d.asyncio, "sleep", sleep)
    sent = []
    async def go():
        async with aiohttp.ClientSession() as session:
            async def original(route):
                params = SimpleNamespace(method=route.method, url=URL(route.url))
                await session.trace_configs[0].on_request_start.send(session, SimpleNamespace(), params)
                sent.append(clock[0])
            http = SimpleNamespace(request=original, _HTTPClient__session=session)
            budget = d.install(http)
            observe(budget, **{"X-RateLimit-Limit": str(limit), "X-RateLimit-Remaining": "0"})
            planned = budget.next(ROUTE, clock[0])
            async with budget.permit(ROUTE):
                await http.request(discord.http.Route("PATCH", "/channels/20/messages/42"))
            assert sent == [planned] and len(budget.global_sends) == 1
    asyncio.run(go())
