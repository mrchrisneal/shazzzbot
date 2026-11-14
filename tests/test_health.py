import runpy
import sys

import pytest

from shazzzbot import health


def test_beat_then_ok_until_the_deadline(tmp_path, monkeypatch):
    monkeypatch.setenv("HEARTBEAT", str(tmp_path / "hb"))
    assert not health.ok()
    health.beat(10)
    assert health.ok() and 59 < float((tmp_path / "hb").read_text()) - __import__("time").time() <= 60
    (tmp_path / "hb").write_text("1")
    assert not health.ok()
    (tmp_path / "hb").write_text("junk")
    assert not health.ok()


def test_runs_as_a_healthcheck(tmp_path, monkeypatch):
    monkeypatch.setenv("HEARTBEAT", str(tmp_path / "hb"))
    monkeypatch.delitem(sys.modules, "shazzzbot.health", raising=False)
    with pytest.raises(SystemExit) as e:
        runpy.run_module("shazzzbot.health", run_name="__main__")
    assert e.value.code == 1
