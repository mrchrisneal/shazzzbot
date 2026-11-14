import logging

import pytest

from shazzzbot import renderbudget


def test_render_budget_accepts_nonnegative_byte_counts_through_the_exact_limit():
    assert renderbudget.SCALE == 2
    assert renderbudget.RENDER_BUDGET == 128 * 1024 * 1024
    assert renderbudget.fits(0) and renderbudget.fits(1)
    assert renderbudget.fits(renderbudget.RENDER_BUDGET)
    assert not renderbudget.fits(renderbudget.RENDER_BUDGET + 1)


@pytest.mark.parametrize("value", [None, -1, True, False, 1.0, "1", [], {}, float("nan"), float("inf"), 10**100])
def test_invalid_or_unbounded_byte_counts_fail_closed(value):
    assert not renderbudget.fits(value)


def test_each_section_warns_once_until_a_real_render_fits_again(monkeypatch, caplog):
    monkeypatch.setattr(renderbudget, "_budget", renderbudget._Budget())
    over = renderbudget.RENDER_BUDGET + 1
    with caplog.at_level(logging.WARNING, "shazzzbot"):
        assert renderbudget.check("forge_0", 1)
        assert not renderbudget.check("forge_0", over)
        assert not renderbudget.check("forge_0", over)
        assert not renderbudget.check("forge_1", over)
        assert renderbudget.check("forge_0", renderbudget.RENDER_BUDGET)
        assert not renderbudget.check("forge_1", over)
        assert not renderbudget.check("forge_0", over)
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 3
    assert sum("forge_0" in message for message in messages) == 2
    assert sum("forge_1" in message for message in messages) == 1


def test_serialized_renderers_and_budget_checks_can_nest_without_deadlocking():
    assert renderbudget.LOCK.acquire(blocking=False)
    try:
        nested = renderbudget.LOCK.acquire(blocking=False)
        try:
            assert nested
        finally:
            if nested:
                renderbudget.LOCK.release()
    finally:
        renderbudget.LOCK.release()

    @renderbudget.serialized
    def inner(value, *, extra):
        return value + extra

    @renderbudget.serialized
    def outer(value):
        assert renderbudget.check("nested", 1)
        return inner(value, extra=3)

    assert outer(4) == 7 and outer.__name__ == "outer" and inner.__name__ == "inner"


def test_retained_bytes_reduce_nested_render_limits_and_release_in_order(monkeypatch, caplog):
    monkeypatch.setattr(renderbudget, "_budget", renderbudget._Budget())
    limit = renderbudget.RENDER_BUDGET
    assert renderbudget.available() == limit
    with renderbudget.retained(10):
        assert renderbudget.available() == limit - 10
        assert renderbudget.fits(limit - 10)
        assert not renderbudget.fits(limit - 9)
        with caplog.at_level(logging.WARNING, "shazzzbot"):
            assert not renderbudget.check("retained", limit)
        with renderbudget.retained(limit - 10):
            assert renderbudget.available() == 0
            assert renderbudget.fits(0)
            assert not renderbudget.fits(1)
            with renderbudget.retained(0):
                assert renderbudget.available() == 0
            with pytest.raises(ValueError, match="available render budget"), renderbudget.retained(1):
                pytest.fail("an unavailable reservation was accepted")
        assert renderbudget.available() == limit - 10
    assert renderbudget.available() == limit
    assert renderbudget.check("retained", limit)
    assert "retained" not in renderbudget._budget.warned
    assert len(caplog.records) == 1


def test_retained_bytes_are_released_when_nested_rendering_raises(monkeypatch):
    monkeypatch.setattr(renderbudget, "_budget", renderbudget._Budget())
    with pytest.raises(RuntimeError, match="drawing failed"), renderbudget.retained(17), renderbudget.retained(23):
        assert renderbudget.available() == renderbudget.RENDER_BUDGET - 40
        raise RuntimeError("drawing failed")
    assert renderbudget._budget.held == 0
    assert renderbudget.available() == renderbudget.RENDER_BUDGET


@pytest.mark.parametrize("value", [None, -1, True, False, 1.0, "1", [], {}, 10**100])
def test_invalid_retained_byte_counts_fail_without_reserving(monkeypatch, value):
    monkeypatch.setattr(renderbudget, "_budget", renderbudget._Budget())
    with pytest.raises(ValueError, match="available render budget"), renderbudget.retained(value):
        pytest.fail("an invalid reservation was accepted")
    assert renderbudget._budget.held == 0
    assert renderbudget.available() == renderbudget.RENDER_BUDGET
