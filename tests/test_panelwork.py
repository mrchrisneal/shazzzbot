import asyncio
import sys

import pytest

from shazzzbot.host import Snapshot
from tests.test_bot import make_bot


def test_render_snapshot_shares_read_only_history_buffers_and_survives_expiry(tmp_path):
    bot = make_bot(tmp_path)
    bot.history.keep = 60
    first = bot.history.add(Snapshot(cpu_pct=12, cpu_cores=[10, 20]), t=100)
    second = bot.history.add(Snapshot(cpu_pct=24, cpu_cores=[20, 30]), t=160)
    asyncio.run(bot.collect())
    snapshot = bot._snapshot
    assert snapshot is not None
    assert len(snapshot.samples) == 2
    for sample, row in zip(snapshot.samples, (first, second)):
        assert sample.readonly and sample.obj is row
        assert sample.format == "d" and sample.nbytes == len(row) * row.itemsize
        with pytest.raises(TypeError):
            sample[1] = 99
    expected = [list(sample) for sample in snapshot.samples]
    bot.history.store(second)
    bot.history.add(Snapshot(cpu_pct=48, cpu_cores=[40, 50]), t=221)
    assert len(bot.history.samples) == 1
    assert [list(sample) for sample in snapshot.samples] == expected
    asyncio.run(bot.collect())
    assert bot._snapshot is not snapshot and len(bot._snapshot.samples) == 1
    assert [list(sample) for sample in snapshot.samples] == expected
    bot.history.close()


def test_render_snapshot_adds_only_small_views_to_compact_high_core_history(tmp_path):
    bot = make_bot(tmp_path)
    for timestamp in range(1, 1001):
        bot.history.add(Snapshot(cpu_cores=[float(core) for core in range(128)]), t=timestamp)
    rows = list(bot.history.samples)
    asyncio.run(bot.collect())
    snapshot = bot._snapshot
    assert snapshot is not None
    assert all(sample.obj is row and sample.readonly for sample, row in zip(snapshot.samples, rows))
    raw_bytes = sum(len(row) * row.itemsize for row in rows)
    assert sum(sample.nbytes for sample in snapshot.samples) == raw_bytes
    view_bytes = sys.getsizeof(snapshot.samples) + sum(sys.getsizeof(sample) for sample in snapshot.samples)
    assert view_bytes <= len(rows) * 256 + 64
    assert raw_bytes + view_bytes < raw_bytes * 1.25
    bot.history.close()
