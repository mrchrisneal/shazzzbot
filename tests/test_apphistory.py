import pytest

from shazzzbot.apphistory import MAX_APPS, AppHistory
from shazzzbot.host import TOP_N, Snapshot


@pytest.mark.parametrize("interval", [0, -1])
def test_interval_must_be_positive(interval):
    with pytest.raises(ValueError, match="interval"):
        AppHistory(interval)


def test_rings_follow_the_window_and_have_a_hard_sample_limit():
    history = AppHistory(10)
    snapshot = Snapshot(top_cpu=[("app", 1)], top_ram=[("app", 64)])
    for t in range(0, 100, 5):
        history.add(snapshot, 30, t)
    assert list(history.samples["cpu"]["app"]) == [(t, 1) for t in (80, 85, 90, 95)]
    assert history.samples["cpu"]["app"].maxlen == 4
    history.add(snapshot, 10, 100)
    assert history.values("ram", "app", 90, 100) == [(95, 64), (100, 64)]
    assert history.samples["ram"]["app"].maxlen == 2
    history.add(snapshot, 300, 110)
    assert history.samples["ram"]["app"].maxlen == 31
    assert history.values("ram", "app", 100, 105) == [(100, 64)]
    history.add(snapshot, 30, 1000)
    assert history.values("cpu", "app", 0, 1000) == [(1000, 1)]


def test_absent_apps_stay_in_history_until_their_points_expire():
    history = AppHistory(10)
    history.add(Snapshot(top_cpu=[("a", 1)], top_ram=[("a", 64)]), 300, 0)
    history.add(Snapshot(top_cpu=[("b", 2)], top_ram=[("a", 128)]), 300, 10)
    assert set(history.samples["cpu"]) == {"a", "b"}
    assert history.values("cpu", "a", 0, 10) == [(0, 1)]
    assert history.values("ram", "a", 0, 10) == [(0, 64), (10, 128)]
    history.add(Snapshot(), 300, 311)
    assert history.samples == {"cpu": {}, "ram": {}}


def test_tracking_keeps_all_names_sums_duplicates_and_skips_invalid_values():
    history = AppHistory(10)
    apps = [(str(i), float(i)) for i in range(TOP_N * 2)]
    history.add(Snapshot(top_cpu=apps, top_ram=apps), 300, 0)
    assert all(len(metric) == TOP_N * 2 for metric in history.samples.values())
    bad = [("duplicate", 1), ("duplicate", 2), ("nan", float("nan")), ("inf", float("inf")), ("negative", -1)]
    history.add(Snapshot(top_cpu=bad), 300, 10)
    assert history.values("cpu", "duplicate", 0, 10) == [(10, 3)]
    assert not {"nan", "inf", "negative"} & set(history.samples["cpu"])
    assert len(history.samples["ram"]) == TOP_N * 2


def test_repeated_or_backward_clock_values_do_not_make_fake_samples():
    history = AppHistory(10)
    history.add(Snapshot(top_cpu=[("a", 1)]), 300, 10)
    history.add(Snapshot(top_cpu=[("a", 2)]), 300, 10)
    assert history.values("cpu", "a", 0, 20) == [(10, 2)]
    history.add(Snapshot(top_cpu=[("a", 3)]), 300, 5)
    assert history.values("cpu", "a", 0, 20) == [(5, 3)]
    assert AppHistory(10).samples == {"cpu": {}, "ram": {}}


def saved(samples):
    import json

    return json.dumps({"format": 2, "samples": samples, "instances": {"cpu": {}, "ram": {}}})


def test_persistence_round_trip_throttle_atomic_replace_and_clean_shutdown(tmp_path, monkeypatch, caplog):
    import json
    import os
    from pathlib import Path

    from shazzzbot import apphistory

    path = tmp_path / "data" / "apps.json"
    clock = [0.0]
    monkeypatch.setattr(apphistory.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(apphistory.time, "time", lambda: 120)
    with caplog.at_level("INFO"):
        history = AppHistory(10, str(path), 60)
    assert "missing; starting empty" in caplog.text
    snapshot = Snapshot(top_cpu=[("app", 1)], top_ram=[("app", 64)])
    for t in (70, 90, 100):
        history.add(snapshot, 60, t)
    replaced = []
    original = os.replace
    def replace(source, target):
        assert str(target) == str(path) and os.stat(source).st_mode & 0o777 == 0o600
        assert json.loads(Path(source).read_text())["format"] == 2
        replaced.append(path.read_bytes() if path.exists() else None)
        return original(source, target)
    monkeypatch.setattr(os, "replace", replace)
    history.store()
    first = path.read_bytes()
    history.add(snapshot, 60, 110)
    clock[0] = 59
    history.store()
    assert path.read_bytes() == first and len(replaced) == 1
    clock[0] = 60
    history.store()
    assert len(replaced) == 2 and replaced[-1] == first
    history.add(snapshot, 60, 120)
    clock[0] = 61
    history.close()
    history.store(force=True)
    history.close()
    assert len(replaced) == 3 and not list(path.parent.glob(".apps-*"))
    restored = AppHistory(10, str(path), 30)
    assert restored.values("cpu", "app", 0, 120) == [(90, 1), (100, 1), (110, 1), (120, 1)]
    assert restored.values("ram", "app", 0, 120) == [(90, 64), (100, 64), (110, 64), (120, 64)]
    assert restored.samples["cpu"]["app"].maxlen == 4


@pytest.mark.parametrize("data", [
    {"format": 2}, {"format": 2, "samples": []},
    {"format": 2, "samples": {"cpu": {}}},
    {"format": 2, "samples": {"cpu": [], "ram": {}}},
    {"format": 2, "samples": {"cpu": {str(i): [] for i in range(MAX_APPS + 1)}, "ram": {}}},
    {"format": 2, "samples": {"cpu": {"": []}, "ram": {}}},
])
def test_corrupt_file_shapes_start_empty_with_a_log_line(tmp_path, caplog, data):
    import json

    path = tmp_path / "apps.json"
    data["instances"] = {"cpu": {}, "ram": {}}
    path.write_text(json.dumps(data))
    assert AppHistory(10, str(path), 300).samples == {"cpu": {}, "ram": {}}
    assert "corrupt or unreadable; starting empty" in caplog.text


@pytest.mark.parametrize("data", [None, [], {}, {"format": True}, {"format": 1}, {"format": 3}])
def test_unsupported_history_header_refuses_without_overwriting(tmp_path, data):
    import json

    from shazzzbot.helper.formats import FormatError

    path = tmp_path / "apps.json"
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    with pytest.raises(FormatError):
        AppHistory(10, str(path), 300)
    assert path.read_bytes() == before


@pytest.mark.parametrize("points", [{}, [None], [[1]], [[1, 2, 3]], [[True, 1]], [[-1, 1]], [[1e13, 1]],
                                     [["1", 1]], [[1, True]], [[1, -1]], [[1, 1e16]], [[1, None]],
                                     [[1, float("nan")]], [[1, float("inf")]], [[2, 1], [1, 2]], [[1, 1], [1, 2]]])
def test_corrupt_samples_never_restore_a_partial_file(tmp_path, caplog, points):
    path = tmp_path / "apps.json"
    path.write_text(saved({"cpu": {"good": [[1, 1]], "bad": points}, "ram": {}}))
    assert AppHistory(10, str(path), 300).samples == {"cpu": {}, "ram": {}}
    assert "starting empty" in caplog.text


def test_load_drops_expired_and_future_samples_and_empty_apps(tmp_path, monkeypatch):
    from shazzzbot import apphistory

    monkeypatch.setattr(apphistory.time, "time", lambda: 100)
    path = tmp_path / "apps.json"
    path.write_text(saved({"cpu": {"app": [[1, 9], [90, 2], [100, 3], [110, 4]]}, "ram": {"old": [[1, 8]]}}))
    restored = AppHistory(10, str(path), 30)
    assert restored.values("cpu", "app", 0, 200) == [(90, 2), (100, 3)]
    assert restored.samples["ram"] == {}
    monkeypatch.setattr(apphistory, "MAX_POINTS", 2)
    history = AppHistory(1)
    for t in range(20):
        history.add(Snapshot(top_cpu=[("app", 1)]), 300, t)
    assert history.samples["cpu"]["app"].maxlen == 2 and len(history.samples["cpu"]["app"]) == 2
    path.write_text(saved({"cpu": {"app": [[i, 1] for i in range(3)]}, "ram": {}}))
    assert AppHistory(10, str(path), 300).samples == {"cpu": {}, "ram": {}}


@pytest.mark.parametrize("raw", [b"broken JSON",
    b'{"format":2,"instances":{"cpu":{},"ram":{}},"samples":{"cpu":{"a":'
    + b"[" * 2000 + b"0" + b"]" * 2000 + b'},"ram":{}}}'])
def test_unparseable_or_excessively_deep_files_are_corrupt(tmp_path, caplog, raw):
    path = tmp_path / "apps.json"
    path.write_bytes(raw)
    assert AppHistory(10, str(path), 300).samples == {"cpu": {}, "ram": {}}
    assert "starting empty" in caplog.text


def test_file_byte_cap_and_unreadable_files_fail_cleanly(tmp_path, monkeypatch, caplog):
    from unittest.mock import Mock

    from shazzzbot import apphistory

    path = tmp_path / "apps.json"
    path.write_text(saved({"cpu": {}, "ram": {}}))
    monkeypatch.setattr(apphistory, "MAX_BYTES", 2)
    assert AppHistory(10, str(path), 300).samples == {"cpu": {}, "ram": {}}
    monkeypatch.setattr(apphistory, "open", Mock(side_effect=PermissionError("secret marker")), raising=False)
    assert AppHistory(10, str(path), 300).samples == {"cpu": {}, "ram": {}}
    assert "secret marker" not in caplog.text


@pytest.mark.parametrize("bad", ["", "x" * 1025, "line\nbreak", 3])
def test_invalid_names_are_not_loaded_or_sampled(tmp_path, bad):
    path = tmp_path / "apps.json"
    if isinstance(bad, str):
        path.write_text(saved({"cpu": {bad: [[1, 2]]}, "ram": {}}))
        assert AppHistory(10, str(path), 300).samples == {"cpu": {}, "ram": {}}
    history = AppHistory(10)
    history.add(Snapshot(top_cpu=[(bad, 1)]), 300, 1)
    assert history.samples == {"cpu": {}, "ram": {}}


@pytest.mark.parametrize("now", [-1, True, float("nan"), float("inf")])
def test_invalid_sample_times_are_rejected(now):
    with pytest.raises(ValueError, match="sample time"):
        AppHistory(10).add(Snapshot(), 300, now)


def test_positive_window_is_required_and_memory_only_histories_do_not_write(tmp_path):
    for window in (0, -1):
        with pytest.raises(ValueError, match="window"):
            AppHistory(10, str(tmp_path / "apps.json"), window)
        with pytest.raises(ValueError, match="window"):
            AppHistory(10).add(Snapshot(), window, 1)
    history = AppHistory(10)
    history.store()
    history.close()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failure", ["replace", "file-fsync", "dir-fsync", "too-large"])
def test_failed_saves_leave_complete_files_clean_up_and_obey_the_retry_period(tmp_path, monkeypatch, caplog, failure):
    import json
    import os
    from unittest.mock import Mock

    from shazzzbot import apphistory

    monkeypatch.chdir(tmp_path)
    clock = [0.0]
    monkeypatch.setattr(apphistory.time, "monotonic", lambda: clock[0])
    history = AppHistory(10, "apps.json", 300)
    history.add(Snapshot(top_cpu=[("app", 1)]), 300, 100)
    history.store()
    old = (tmp_path / "apps.json").read_bytes()
    history.add(Snapshot(top_cpu=[("app", 2)]), 300, 110)
    clock[0] = 60
    if failure == "replace":
        monkeypatch.setattr(os, "replace", Mock(side_effect=OSError("secret marker")))
    elif failure == "file-fsync":
        monkeypatch.setattr(os, "fsync", Mock(side_effect=OSError("secret marker")))
    elif failure == "dir-fsync":
        monkeypatch.setattr(os, "fsync", Mock(side_effect=[None, OSError("secret marker")]))
    else:
        monkeypatch.setattr(apphistory, "MAX_BYTES", 1)
    history.store()
    if failure != "dir-fsync":
        assert (tmp_path / "apps.json").read_bytes() == old
    else:
        assert json.loads((tmp_path / "apps.json").read_bytes())["samples"]["cpu"]["app"][-1] == [110, 2]
    assert not list(tmp_path.glob(".apps-*"))
    assert "could not save app history" in caplog.text and "secret marker" not in caplog.text
    caplog.clear()
    clock[0] = 61
    history.store()
    assert not caplog.text
