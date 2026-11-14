import json
from pathlib import Path

from shazzzbot import panel, previewvary

ROOT = Path(__file__).resolve().parent.parent
FIELDS = (("cpu_pct", "resource"), ("iowait_pct", "iowait"), ("disk_util_pct", "disk_busy"), ("temp_c", "temp"))


def levels(data: dict) -> list[int]:
    s = data["snap"]
    th = panel.DEFAULT_THRESHOLDS
    out = [panel.level(s[k], th[key]) for k, key in FIELDS]
    out.append(panel.level(s["ram_used_mb"] / s["ram_total_mb"] * 100, th["resource"]))
    out.append(panel.level(s["load"][0] / data["cores"], th["load"]))
    for d in data["array"]["disks"]:
        out.append(panel.level(d["temp"], th["hdd_temp" if d["rotational"] else "ssd_temp"]))
    return out


def test_each_step_stays_in_its_profile_bands_changes_every_metric_and_repeats_exactly():
    names = json.loads((ROOT / "docs" / "demo" / "snapshots.json").read_text())["names"]
    snaps = json.loads((ROOT / "docs" / "demo" / "snapshots.json").read_text())["sets"]["helper"]
    for i, base in enumerate(snaps):
        if names[i] == "Max width":
            continue
        text = json.dumps(base)
        assert previewvary.vary(text, 0, "p") == text
        prev = base
        for step in range(1, 5):
            out = json.loads(previewvary.vary(text, step, f"helper:{i}"))
            assert levels(out) == levels(base), (names[i], step)
            for k, _ in FIELDS:
                assert out["snap"][k] != prev["snap"][k], (names[i], step, k)
            assert out["snap"]["ram_used_mb"] != prev["snap"]["ram_used_mb"]
            assert out["snap"]["top_cpu"] != prev["snap"]["top_cpu"]
            assert out["history"][-1] != prev["history"][-1]
            assert previewvary.vary(text, step, f"helper:{i}") == json.dumps(out)
            prev = out


def test_worst_case_widths_survive_variety():
    snaps = json.loads((ROOT / "docs" / "demo" / "snapshots.json").read_text())
    base = snaps["sets"]["helper"][snaps["names"].index("Max width")]
    out = json.loads(previewvary.vary(json.dumps(base), 3, "w"))
    for k in ("cpu_pct", "temp_c", "iowait_pct", "disk_util_pct"):
        assert out["snap"][k] == base["snap"][k]


def test_fresh_history_and_unknown_disk_temperature_stay_absent_while_metrics_vary():
    snapshots = json.loads((ROOT / "docs/demo/snapshots.json").read_text())
    base = snapshots["sets"]["helper"][0]
    base["history"] = []
    base["array"]["disks"][0]["temp"] = None
    snapshot = json.dumps(base)
    varied = previewvary.vary(snapshot, 1, "fresh")
    out = json.loads(varied)
    assert out["history"] == []
    assert out["array"]["disks"][0]["temp"] is None
    assert out["array"]["disks"][0]["name"] == base["array"]["disks"][0]["name"]
    assert out["snap"]["cpu_pct"] != base["snap"]["cpu_pct"]
    assert out["snap"]["uptime_s"] == base["snap"]["uptime_s"] + 60
    assert json.loads(snapshot) == base
    assert previewvary.vary(snapshot, 1, "fresh") == varied
