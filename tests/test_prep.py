import asyncio
import math
from unittest.mock import AsyncMock, MagicMock

import pytest

from shazzzbot import boards, prep
from shazzzbot import bot as B
from tests.test_bot import channel, edited, make_bot, stub_render, wire

BOUNDARY = 1_800_000_000.0  # a multiple of 60 and of TICK


@pytest.fixture
def clock(monkeypatch):
    now = [BOUNDARY - 4]
    sleeps: list[float] = []

    async def sleep(delay):
        sleeps.append(delay)
        now[0] += delay
    monkeypatch.setattr(B.time, "time", lambda: now[0])
    monkeypatch.setattr(prep, "hold", sleep)
    return now, sleeps


def posted(b: B.Bot, now: list[float], keys=()) -> MagicMock:
    stub_render(b, keys)
    seen = {}

    async def collect(at=None):
        seen["collect"] = now[0], at
        return []
    b.collect = collect  # type: ignore[method-assign]
    msg = edited(MagicMock(id=5))
    msg.edit.side_effect = lambda **kw: seen.setdefault("sent", (now[0], kw)) and msg
    b.main.message_id, b.main.msg = 5, msg
    b.main.edit_slot, b.main.image_slot = B.delivery.edit_slot(b, b.main), b._slot(b.main.image_every)
    b._snap, b._tick = B.Snapshot(), b._slot(B.TICK)
    wire(b, channel())
    msg.seen = seen
    return msg


def test_text_edit_collects_early_and_sends_at_the_boundary(tmp_path, clock):
    now, sleeps = clock
    b = make_bot(tmp_path, timezone="UTC")
    msg = posted(b, now)
    asyncio.run(b.panel_loop.coro(b))
    assert msg.seen["collect"] == (BOUNDARY - 4, BOUNDARY)
    assert msg.seen["sent"][0] == BOUNDARY + prep.LATE and sleeps == [4 + prep.LATE]
    assert "text" in b.lead.took and "image" not in b.lead.took


def test_image_is_drawn_early_and_uploaded_at_the_boundary(tmp_path, clock):
    now, _ = clock
    b = make_bot(tmp_path, timezone="UTC")
    msg = posted(b, now, keys=("history",))
    del b.image_files  # the real one, which takes the drawn images
    drawn = []

    async def draw(message, at):
        drawn.append((now[0], at))
        return [("history.png", b"png")]
    b.draw_images = draw  # type: ignore[method-assign]
    asyncio.run(b.panel_loop.coro(b))
    assert drawn == [(BOUNDARY - 4, BOUNDARY)]
    at, kw = msg.seen["sent"]
    assert at == BOUNDARY + prep.LATE and [f.filename for f in kw["files"]] == ["history.png"]
    assert "image" in b.lead.took and b._drawn == {}


def test_an_overrunning_render_sends_the_moment_it_completes(tmp_path, clock):
    now, sleeps = clock
    b = make_bot(tmp_path, timezone="UTC")
    msg = posted(b, now)
    real = b.collect

    async def slow(at=None):
        await real(at)
        now[0] += 7
        return []
    b.collect = slow  # type: ignore[method-assign]
    asyncio.run(b.panel_loop.coro(b))
    assert msg.seen["sent"][0] == BOUNDARY + 3 and sleeps == []


def test_a_budget_wait_past_the_boundary_holds_the_send(tmp_path, clock):
    now, _ = clock
    b = make_bot(tmp_path, timezone="UTC")
    msg = posted(b, now)
    now[0] = BOUNDARY - 1
    b.rate_budget.next = lambda route, at, media=False: max(at, BOUNDARY + 3)  # type: ignore[method-assign]
    asyncio.run(b.panel_loop.coro(b))
    assert msg.seen["collect"] == (BOUNDARY - 1, BOUNDARY + 3)
    assert msg.seen["sent"][0] == BOUNDARY + 3 + prep.LATE


def test_nothing_is_prepared_outside_the_lead_or_without_a_schedule(tmp_path, clock):
    now, sleeps = clock
    now[0] = BOUNDARY - 30
    b = make_bot(tmp_path, timezone="UTC")
    msg = posted(b, now)
    asyncio.run(b.panel_loop.coro(b))
    assert msg.seen == {} and sleeps == []
    b.rate_budget.next = lambda route, at, media=False: math.inf  # type: ignore[method-assign]
    assert prep.upcoming(b, now[0]) is None
    assert asyncio.run(prep.ahead(b)) is None


def test_manual_refresh_does_not_hold(tmp_path, clock):
    now, sleeps = clock
    b = make_bot(tmp_path, timezone="UTC")
    msg = posted(b, now)
    asyncio.run(b.refresh(force_images=True))
    assert msg.seen["sent"][0] == BOUNDARY - 4 and msg.seen["collect"] == (BOUNDARY - 4, None) and sleeps == []


def test_lead_adapts_to_measured_prep_time_within_its_clamps():
    lead = prep.Lead()
    assert lead("image") == prep.DEFAULT
    lead.observe("image", 4)
    assert lead("image") == 7
    lead.observe("image", 14)
    assert lead("image") == pytest.approx((4 + 0.3 * 10) * 1.5 + 1)
    for _ in range(20):
        lead.observe("image", 100)
    assert lead("image") == prep.HIGH
    lead.observe("text", 0.1)
    assert lead("text") == prep.LOW and lead("image") == prep.HIGH


def test_drawn_images_for_other_keys_are_redrawn(tmp_path, clock):
    now, _ = clock
    b = make_bot(tmp_path, timezone="UTC")
    posted(b, now, keys=("history",))
    del b.image_files
    b.draw_images = AsyncMock(return_value=[("history.png", b"new")])  # type: ignore[method-assign]
    b._drawn = {B.delivery.identity(b.main): (("system",), [("system.png", b"old")])}
    files = asyncio.run(b.image_files(b.main))
    assert [f.filename for f in files] == ["history.png"] and b._drawn == {}


def test_preparation_does_not_hold_the_render_lock_during_collect_or_draw(tmp_path, clock, monkeypatch):
    b = make_bot(tmp_path, timezone="UTC")
    stub_render(b)
    snapshot = object()
    b._snapshot = snapshot
    monkeypatch.setattr(prep, "upcoming", lambda _bot, _now: (BOUNDARY, [b.main]))

    async def go():
        gathering, finish = asyncio.Event(), asyncio.Event()

        async def collect(at):
            gathering.set()
            assert at == BOUNDARY and not b._lock.locked()
            await finish.wait()
            return []

        async def draw(message, at):
            assert not b._lock.locked() and b.main.lock.locked()
            assert message.snapshot is snapshot and at == BOUNDARY
            async with b._lock:
                return [("history.png", b"png")]

        b.collect, b.draw_images = collect, draw
        pending = asyncio.create_task(prep.ahead(b))
        await gathering.wait()
        async with b._lock:
            finish.set()
        return await pending

    result = asyncio.run(go())
    assert result.snapshot is snapshot
    assert result.files == {B.delivery.identity(b.main): (("history",), [("history.png", b"png")])}


@pytest.mark.parametrize("all_busy", [False, True])
def test_preparation_skips_busy_board_without_changing_its_snapshot(tmp_path, clock, monkeypatch, all_busy):
    b = make_bot(tmp_path, timezone="UTC")
    stub_render(b)
    old, fresh = object(), object()
    b._snapshot = fresh
    busy = boards.child(b.main, 1)
    busy.snapshot = old
    idle = boards.Board(21)
    child = boards.child(idle, 1)
    uploads = [busy] if all_busy else [busy, child]
    monkeypatch.setattr(prep, "upcoming", lambda _bot, _now: (BOUNDARY, uploads))
    drawn = []

    async def draw(message, at):
        assert message.owner is idle and idle.lock.locked() and not b._lock.locked()
        assert message.snapshot is fresh and at == BOUNDARY
        drawn.append(message)
        return [("history.png", b"png")]

    b.draw_images = draw

    async def go():
        async with b.main.lock:
            return await prep.ahead(b)

    result = asyncio.run(go())
    assert busy.snapshot is old and result.snapshot is fresh
    assert drawn == ([] if all_busy else [child])
    assert bool(result.files) == (not all_busy)
