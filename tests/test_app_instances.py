import json
from collections import Counter

import pytest
from PIL import ImageDraw

from shazzzbot import apphistory, charts, panel, view
from shazzzbot.apphistory import AppHistory
from shazzzbot.glyphs import NO_DATA
from shazzzbot.host import TOP_N, Host, Snapshot
from shazzzbot.spark import braille, spark
from tests.test_host import _PAGE, CID, _stat, _tree


def snapshot(name="worker", value=1, ids=(), grouped=False):
    return Snapshot(apps_cpu={name: value}, apps_ram={name: value * 10},
                    top_cpu=[(name, value)], top_ram=[(name, value * 10)],
                    app_instances={name: frozenset(ids)}, grouped_apps=frozenset({name}) if grouped else frozenset())


def test_host_keeps_all_final_labels_sums_collisions_and_identifies_only_bare_processes(tmp_path, monkeypatch):
    _tree(tmp_path, [0] * 8, 0, 0)
    h = Host(str(tmp_path))
    def write_extra(ticks):
        for i in range(TOP_N + 3):
            pid = str(600 + i)
            folder = tmp_path / "proc" / pid
            folder.mkdir(exist_ok=True)
            comm = "same" if i < 2 else "worker" if i < 4 else f"extra{i}"
            (folder / "stat").write_text(_stat(pid, comm, ticks, 2**20 // _PAGE))
            (folder / "cgroup").write_text("0::/\n")
    clock = [0]
    original = h._raw
    def raw():
        data = original()
        data["t"] = clock[0]
        return data
    monkeypatch.setattr(h, "_raw", raw)
    write_extra(0)
    h.poll({CID: "same"})
    clock[0] = 10
    _tree(tmp_path, [10] * 8, 0, 100)
    write_extra(10)
    s = h.poll({CID: "same"})
    assert len(s.apps_cpu) > TOP_N and len(s.apps_ram) > TOP_N
    assert s.top_cpu == Counter(s.apps_cpu).most_common(TOP_N)
    assert s.top_ram == Counter(s.apps_ram).most_common(TOP_N)
    assert s.apps_ram["same"] == 7
    assert "same" in s.grouped_apps and "same" not in s.app_instances
    assert s.app_instances["worker"] == frozenset({("602", "7"), ("603", "7")})
    assert "*Parity/array I/O" in s.grouped_apps and "*Parity/array I/O" not in s.app_instances


def test_reentry_keeps_the_samples_collected_outside_the_top_list_and_sums_duplicate_names():
    history = AppHistory(10)
    for t, selected in ((0, "a"), (10, "b"), (20, "a")):
        s = Snapshot(apps_cpu={"a": t + 1, "b": 50}, apps_ram={"a": t + 2, "b": 60},
                     top_cpu=[(selected, 1)], top_ram=[(selected, 2)])
        history.add(s, 100, t)
    assert history.values("cpu", "a", 0, 20) == [(0, 1), (10, 11), (20, 21)]
    assert history.values("ram", "a", 0, 20) == [(0, 2), (10, 12), (20, 22)]
    section = panel.apps_section(s, history, 100, now=20)
    assert section.parts[0].trends[0].samples == ((0, 1), (10, 11), (20, 21))
    history.add(Snapshot(top_cpu=[("same", 1), ("same", 2)], top_ram=[("same", 5), ("same", 7)]), 100, 30)
    assert history.values("cpu", "same", 0, 30) == [(30, 3)]
    assert history.values("ram", "same", 0, 30) == [(30, 12)]
    assert history.values("cpu", "a", 0, 30) == [(0, 1), (10, 11), (20, 21)]


def test_names_evict_least_recently_seen_and_points_and_instance_sets_expire_together(monkeypatch):
    monkeypatch.setattr(apphistory, "MAX_APPS", 2)
    history = AppHistory(10)
    history.add(snapshot("a", ids=(("1", "1"),)), 100, 0)
    history.add(snapshot("b", ids=(("2", "1"),)), 100, 10)
    history.add(snapshot("a", ids=(("1", "1"),)), 100, 20)
    history.add(snapshot("c", ids=(("3", "1"),)), 100, 30)
    assert set(history.samples["cpu"]) == set(history.instances["cpu"]) == {"a", "c"}
    history.add(Snapshot(), 100, 130)
    assert set(history.samples["cpu"]) == {"c"}
    history.add(Snapshot(), 100, 131)
    assert history.samples == history.instances == {"cpu": {}, "ram": {}}


def test_global_point_budget_and_per_name_instance_caps_are_consistent(monkeypatch):
    monkeypatch.setattr(apphistory, "MAX_POINTS", 8)
    monkeypatch.setattr(apphistory, "MAX_APPS", 4)
    monkeypatch.setattr(apphistory, "MAX_INSTANCES", 2)
    history = AppHistory(1)
    values = {str(i): i for i in range(10)}
    ids = {name: frozenset({("1", "1"), ("2", "1"), ("3", "1")}) for name in values}
    for t in range(10):
        history.add(Snapshot(apps_cpu=values, apps_ram=values, app_instances=ids), 100, t)
    assert all(len(apps) == 4 for apps in history.samples.values())
    assert sum(len(points) for apps in history.samples.values() for points in apps.values()) == 8
    assert all(len(seen) == 2 for apps in history.instances.values() for seen in apps.values())
    assert history.multiple("cpu", "9", 0, 9)


def test_bare_process_marker_covers_current_sums_and_restarts_then_expires_and_persists(tmp_path, monkeypatch):
    path = tmp_path / "apps.json"
    monkeypatch.setattr(apphistory.time, "time", lambda: 20)
    history = AppHistory(10, str(path), 100)
    one = snapshot(ids=(("1", "1"),))
    history.add(one, 100, 0)
    assert all(" (+)" not in part.rows[0][1][1] for part in panel.apps_section(one, history, 100, now=0).parts)
    two = snapshot(value=3, ids=(("1", "1"), ("2", "1")))
    history.add(two, 100, 10)
    assert all(part.rows[0][1][1] == "worker (+)" for part in panel.apps_section(two, history, 100, now=10).parts)
    restarted = snapshot(ids=(("1", "2"),))
    history.add(restarted, 100, 20)
    history.close()
    restored = AppHistory(10, str(path), 100)
    assert restored.multiple("cpu", "worker", 0, 20)
    assert all(part.rows[0][1][1] == "worker (+)"
               for part in panel.apps_section(restarted, restored, 100, now=20).parts)
    restored.add(restarted, 100, 111)
    assert not restored.multiple("cpu", "worker", 11, 111)
    assert all(" (+)" not in part.rows[0][1][1]
               for part in panel.apps_section(restarted, restored, 100, now=111).parts)
    assert json.loads(path.read_text())["format"] == 2


@pytest.mark.parametrize("name,grouped", [("container", True), ("VM: guest", False), ("LXC: box", False),
                                         ("*Parity/array I/O", False), ("*User shares", False),
                                         ("*Docker shims", False)])
def test_grouped_and_synthetic_rows_never_get_a_process_marker(name, grouped):
    history = AppHistory(10)
    s = snapshot(name, ids=(("1", "1"), ("2", "1")), grouped=grouped)
    history.add(s, 100, 0)
    assert not history.multiple("cpu", name, 0, 100)
    section = panel.apps_section(s, history, 100, now=0)
    assert all(" (+)" not in cells[1] for part in section.parts for _, cells in part.rows)
    assert " (+)" not in view.text(section).description and " (+)" not in str(view.rich(section).to_dict())


def test_marker_survives_name_truncation_in_every_render_and_shared_columns(monkeypatch):
    history = AppHistory(10)
    name = "bare_process_" + "W" * 80
    s = snapshot(name, 999.9, (("1", "1"), ("2", "1")))
    history.add(s, 100, 0)
    section = panel.apps_section(s, history, 100, now=0)
    assert all(len(part.rows[0][1][1]) == 32 and part.rows[0][1][1].endswith("... (+)") for part in section.parts)
    lines = [line for line in view.text(section).description.splitlines() if "bare_process" in line]
    assert len(lines) == 2 and all(" (+) " in line and len(line) <= panel.WIDTH + 3 for line in lines)
    assert len({line.index(" (+)") for line in lines}) == 1
    assert all(" (+):**" in field.value for field in view.rich(section).fields)
    drawn = []
    original = ImageDraw.ImageDraw.text
    def text(draw, xy, text, **kwargs):
        drawn.append(text)
        return original(draw, xy, text, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", text)
    assert view.image(section, now=1).startswith(b"\x89PNG")
    assert sum(text.endswith("... (+)") for text in drawn) == 2


def test_renamed_image_names_are_italic_without_stars_and_measured_in_that_font(monkeypatch):
    name = "*Parity/array I/O"
    section = panel.apps_section(snapshot(name))
    part = section.parts[0]
    small = charts._fonts()[2].font_variant(size=charts._fonts()[2].size * view.K)
    italic = view._italic(small)
    assert view._layout(part, small)[1][1] == charts.text_width(italic, name[1:]) / view.K
    drawn = []
    original = ImageDraw.ImageDraw.text
    def text(draw, xy, text, **kwargs):
        drawn.append((text, kwargs["font"]))
        return original(draw, xy, text, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", text)
    assert view.image(section, now=1).startswith(b"\x89PNG")
    labels = [(text, font) for text, font in drawn if text == name[1:]]
    assert len(labels) == 2 and all("Italic" in font.getname()[1] for _, font in labels)
    assert all(not text.startswith("*") for text, _ in drawn)
    assert name in view.text(section).description and "**_Parity/array I/O:_**" in str(view.rich(section).to_dict())


def test_tiny_variation_is_not_flat_in_braille_or_the_image_and_no_render_emits_blank_braille(monkeypatch):
    values = [(0, 66.60), (10, 66.61), (20, 66.60)]
    assert spark(values, 2, start=0, end=20) == chr(0x28c0) + chr(0x28c7)
    assert chr(0x2800) not in braille(values, 15, start=0, end=20)
    history = AppHistory(10)
    for t, value in values:
        s = snapshot(value=value)
        history.add(s, 20, t)
    section = panel.apps_section(s, history, 20, now=20)
    history_section = charts.section([(t, v, v, v, v, v, v) for t, v in values], 20, now=20)
    texts, traces = [], []
    original_text, original_plot = ImageDraw.ImageDraw.text, charts.plot
    def text(draw, xy, text, **kwargs):
        texts.append(text)
        return original_text(draw, xy, text, **kwargs)
    def plot(draw, box, *args, **kwargs):
        original_plot(draw, box, *args, **kwargs)
        if kwargs.get("floor"):
            traces.append(draw._image.crop(tuple(map(int, box))))
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", text)
    monkeypatch.setattr(charts, "plot", plot)
    for card in (section, history_section):
        assert chr(0x2800) not in view.text(card).description
        assert chr(0x2800) not in str(view.rich(card).to_dict())
        assert view.image(card, now=20).startswith(b"\x89PNG")
    assert traces and all(trace.getbbox()[1] == 0 and trace.getbbox()[3] > 10 * view.K for trace in traces)
    assert all(chr(0x2800) not in text for text in texts)
    assert NO_DATA in view.text(section).description and "(partial data)" in section.note
