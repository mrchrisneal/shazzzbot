import json

import pytest

from shazzzbot import apphistory
from shazzzbot.apphistory import AppHistory
from shazzzbot.host import Snapshot


def write_file(path, instances, samples=None):
    path.write_text(json.dumps({"format": 2, "samples": samples or {"cpu": {"a": [[10, 1]]}, "ram": {}},
                               "instances": instances}))


@pytest.mark.parametrize("instances", [None, [], {}, {"cpu": {}, "ram": {}, "extra": {}},
                                      {"cpu": [], "ram": {}}, {"cpu": {"other": []}, "ram": {}}])
def test_invalid_instance_shapes_never_restore_partial_histories(tmp_path, instances, caplog):
    path = tmp_path / "apps.json"
    write_file(path, instances)
    history = AppHistory(10, str(path), 300)
    assert history.samples == history.instances == {"cpu": {}, "ram": {}}
    assert "starting empty" in caplog.text


@pytest.mark.parametrize("records", [None, {}, [None], [[]], [["1", "1"]], [["1", "1", 1, 2]],
                                    [[1, "1", 10]], [["", "1", 10]], [["1" * 33, "1", 10]],
                                    [[chr(0x661), "1", 10]], [["one", "1", 10]], [["1", "one", 10]],
                                    [["1", "1", -1]], [["1", "1", True]], [["1", "1", float("inf")]],
                                    [["1", "1", float("nan")]], [["1", "1", 1e13]],
                                    [["1", "1", 10], ["1", "1", 10]],
                                    [[str(i), "1", 10] for i in range(apphistory.MAX_INSTANCES + 1)]])
def test_invalid_instance_records_never_restore_partial_histories(tmp_path, records, caplog):
    path = tmp_path / "apps.json"
    write_file(path, {"cpu": {"a": records}, "ram": {}})
    history = AppHistory(10, str(path), 300)
    assert history.samples == history.instances == {"cpu": {}, "ram": {}}
    assert "starting empty" in caplog.text


def test_instance_restore_drops_expired_future_and_sampleless_names(tmp_path, monkeypatch):
    path = tmp_path / "apps.json"
    monkeypatch.setattr(apphistory.time, "time", lambda: 100)
    samples = {"cpu": {"a": [[90, 1]], "old": [[1, 2]]}, "ram": {}}
    identities = {"cpu": {"a": [["1", "1", 1], ["2", "1", 90], ["3", "1", 101]],
                          "old": [["4", "1", 90]]}, "ram": {}}
    write_file(path, identities, samples)
    history = AppHistory(10, str(path), 20)
    assert history.instances == {"cpu": {"a": {("2", "1"): 90}}, "ram": {}}
    assert not history.multiple("cpu", "a", 80, 100)


def test_window_expansion_keeps_the_old_full_ring_and_invalid_identity_and_overflow_are_ignored():
    history = AppHistory(10)
    for t in (0, 10, 20):
        history.add(Snapshot(top_cpu=[("a", 1)], app_instances={"a": frozenset({("bad", "1")})}), 20, t)
    history.add(Snapshot(top_cpu=[("a", 2)]), 40, 30)
    assert history.values("cpu", "a", 0, 30) == [(0, 1), (10, 1), (20, 1), (30, 2)]
    assert history.samples["cpu"]["a"].maxlen == 5 and not history.multiple("cpu", "a", 0, 30)
    history.add(Snapshot(top_cpu=[("huge", 1e15), ("huge", 1e15)]), 40, 40)
    assert "huge" not in history.samples["cpu"]
