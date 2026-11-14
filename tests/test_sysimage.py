import io
from collections import Counter
from unittest.mock import patch

import pytest
from PIL import Image

from shazzzbot import charts, panel, sysimage, ups, view
from shazzzbot.config import DEFAULT_THRESHOLDS as TH
from shazzzbot.host import Snapshot


@pytest.mark.parametrize("state,level,colour", [("OL CHRG", panel.FINE, charts.GREEN),
                                               ("OB", panel.WARNING, charts.YELLOW),
                                               ("OB LB", panel.CRITICAL, charts.RED),
                                               ("OL RB", panel.CRITICAL, charts.RED),
                                               ("OL FUTURE", panel.WARNING, charts.YELLOW)])
def test_ups_reuses_the_system_bar_and_follows_array_in_every_view(state, level, colour):
    from unittest.mock import patch

    reading = ups.parse({"ups.status": state, "battery.charge": "97", "battery.runtime": "2520"})
    table = sysimage.rows(Snapshot(uptime_s=86400), TH, usage=(1, 2), ups=reading)
    labels = [r.label for r in table]
    assert labels[labels.index("Array") + 1] == "UPS"
    row = table[-1]
    assert row.fill == 0.97 and row.level == level and row.colour == colour
    assert row.marks == (panel.FINE, level) and row.lines == ("97% - 42m left", reading.words)
    section = sysimage.section("System", "", "soon", table, None)
    assert bool(section.issues) == bool(level)
    if level:
        assert section.issues == [(level, "UPS: " + row.text.replace("    ", "  "))]
    text = view.text(section).description
    rich = view.rich(section)
    field = next(f for f in rich.fields if f.name == "UPS")
    assert "97%" in text and "42m" in text and reading.words in text
    assert "97%" in field.value and "42m" in field.value and reading.words in field.value
    names = [f.name for f in rich.fields]
    assert names[names.index("Array") + 1] == "UPS"
    with patch.object(sysimage, "_text", wraps=sysimage._text) as draw, \
            patch.object(view, "draw_box", wraps=view.draw_box) as boxes:
        assert sysimage.render(table, now=123).startswith(b"\x89PNG")
    drawn = "".join(call.args[4] for call in draw.call_args_list)
    assert reading.words in drawn and "97%" in drawn and "42m" in drawn
    assert any(call.args[1].heading == view.ISSUES for call in boxes.call_args_list) == bool(level)


def test_ups_missing_or_partial_data_is_explicit_and_becomes_an_issue():
    table = sysimage.rows(Snapshot(uptime_s=86400), TH)
    assert "UPS" not in [r.label for r in table]
    missing = sysimage.rows(Snapshot(uptime_s=86400), TH, ups=ups.Reading())
    row = missing[-1]
    assert row.label == "UPS" and row.text == "no data" and row.fill is None and row.dot == panel.WARNING
    section = sysimage.section("System", "", "soon", missing, None)
    assert section.issues == [(panel.WARNING, "UPS: no data")]
    assert "no data" in view.text(section).description
    assert "no data" in next(f.value for f in view.rich(section).fields if f.name == "UPS")
    assert sysimage.render(missing, now=123).startswith(b"\x89PNG")
    partial = sysimage.ups_row(ups.Reading(("OB", "LB")), TH)
    assert partial.level == panel.CRITICAL and partial.fill is None
    assert partial.marks == (panel.WARNING, panel.CRITICAL)
    assert "charge no data" in partial.text and "runtime no data" in partial.text
    assert sysimage.ups_row(ups.Reading(("OL",), 100, None), TH).level == panel.WARNING
    assert sysimage.ups_row(ups.Reading(("OL",), 0, 45), TH).text == "0% - 45s left    Online"


def test_long_ups_status_fits_the_image_without_losing_words():
    from unittest.mock import patch

    row = sysimage.ups_row(ups.Reading(tuple(ups.STATUS), 100, 2520), TH)
    with patch.object(sysimage, "_text", wraps=sysimage._text) as draw:
        png = sysimage.render([row], now=123)
        assert png.startswith(b"\x89PNG")
    call = next(call for call in draw.call_args_list if "Waiting for data" in call.args[4])
    x, text, font = call.args[2], call.args[4], call.args[5]
    assert all(word in text for word in ups.STATUS.values())
    import io

    from PIL import Image

    width = Image.open(io.BytesIO(png)).width
    assert x + sum(font.getlength(ch) for ch in text) / sysimage.K <= width - 24
    assert font.size == sysimage._fonts()[2].size * sysimage.K


def test_rows_flag_uptime_and_os_update():
    dots = [{r.label: r.dot for r in sysimage.rows(Snapshot(uptime_s=up), TH, "Unraid 7.3.4", new)}
            for up, new in ((600, ""), (1800, "7.3.5"), (7200, ""))]
    assert [d["Uptime"] for d in dots] == [panel.CRITICAL, panel.WARNING, panel.FINE]
    notes = [r.note for r in sysimage.rows(Snapshot(), TH, "Unraid 7.3.4", "7.3.5") if r.label == "OS"]
    assert notes == ["(update available!)"]


def test_render_png():
    s = Snapshot(uptime_s=99999, cpu_pct=95, ram_used_mb=1, ram_total_mb=10, temp_c=60, net_speed_mbps=1000)
    table = sysimage.rows(s, TH, "Unraid 7.3.4", "", (5.0, 10.0))
    cpu = next(r for r in table if r.label == "CPU")
    assert cpu.fill == 0.95 and [r.label for r in table][-1] == "Array"
    png = sysimage.render(table, 0)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) < 64000


def test_system_render_keeps_normal_dimensions_at_the_shared_two_times_scale():
    row = sysimage.Row("CPU", "25%", 0.25, charts.GREEN)
    with patch.object(sysimage.Image, "new", wraps=sysimage.Image.new) as allocate:
        png = sysimage.render([row], now=123)
    image = Image.open(io.BytesIO(png))
    expected_height = view.TOP + view.ROW_H + 4 + 2 * view.PAD + sysimage.ROW_H + 6
    assert sysimage.K == view.K == sysimage.renderbudget.SCALE == 2
    assert isinstance(png, view.Rendered) and (png.key, png.section_title, png.now) == ("system", "System", 123)
    assert image.size == (sysimage.W, expected_height)
    assert allocate.call_args_list[0].args[:2] == ("RGB", (sysimage.W * 2, expected_height * 2))


def test_oversized_system_returns_a_bounded_placeholder_before_allocating_the_card(monkeypatch):
    width = 100_000_000
    original = sysimage.Image.new
    allocations = []

    def allocate(mode, size, *args, **kwargs):
        assert 4 * size[0] * size[1] < sysimage.renderbudget.RENDER_BUDGET
        allocations.append(size)
        return original(mode, size, *args, **kwargs)

    monkeypatch.setattr(sysimage.Image, "new", allocate)
    with patch.object(view, "placeholder", wraps=view.placeholder) as placeholder:
        png = sysimage.render([sysimage.Row("CPU", "25%")], now=123, min_width=width)
    placeholder.assert_called_once_with("system", "System", width, 123, None)
    image = Image.open(io.BytesIO(png))
    assert allocations and image.width < width and image.height < 1000


def test_load_has_a_trend_and_level_per_value():
    c = panel.CORES
    row = next(r for r in sysimage.rows(Snapshot(load=(1.2 * c, 0.8 * c, 0.2 * c)), TH,
                                        load_then=(1.0 * c, 0.8 * c, 0.3 * c))
               if r.label == "Load")
    up, flat, down = (t for t, _ in row.spans[2::4])
    assert row.fill is None and -75 <= up < -10 and flat == 0 and 10 < down <= 75
    assert [c for _, c in row.spans[0::4]] == [charts.RED, charts.YELLOW, charts.TEXT]
    assert sysimage.swivel_colour(0) == charts.TEXT and sysimage.swivel_colour(-60) == charts.RED
    assert sysimage.swivel_colour(-30) == charts.YELLOW and sysimage.swivel_colour(75) == charts.GREEN


def test_status_rows():
    alerts, tasks, jf = sysimage.status_rows([(panel.CRITICAL, "a"), (panel.WARNING, "b")], [],
                                             [(4000.0, "Cache scrub")],
                                             Counter({"transcode": 1, "direct play": 2}), True)
    assert alerts.text == "1 critical    1 warning" and alerts.level == panel.CRITICAL
    assert [c for t, c in alerts.spans if t == sysimage.DOT] == [charts.RED, charts.YELLOW]
    assert tasks.text == "next:  Cache scrub in 1h06m"
    assert jf.text == "2 direct play, 1 transcoding (3 total)"
    busy = sysimage.status_rows([], [(None, "File integrity", panel.FINE)], [], None, True)
    assert busy[0].text == "none" and busy[1].text == "1 task running:  File integrity"
    assert busy[2].dot == panel.CRITICAL
    active = [(None, "File integrity", panel.FINE), (None, "Mover", panel.FINE)]
    assert sysimage.status_rows([], active, [], None, False)[1].text == "2 tasks running:  File integrity, Mover"


def test_render_tiny_bar_and_note():
    assert sysimage.render([sysimage.Row("Disk", "1%", 0.01, (0, 200, 0), note="new")], 0)[:4] == b"\x89PNG"


def test_temperature_bar_starts_at_room_temperature_and_is_green_at_normal_load():
    from shazzzbot import charts
    from shazzzbot.config import DEFAULT_THRESHOLDS
    from shazzzbot.host import Snapshot
    (temp,) = [r for r in sysimage.rows(Snapshot(load=(1.0, 1.0, 1.0), temp_c=54.0), DEFAULT_THRESHOLDS)
               if r.label == "Temp"]
    assert abs(temp.fill - (54 - 16) / (95 - 16)) < 1e-9 and temp.colour == charts.GREEN
    (hot,) = [r for r in sysimage.rows(Snapshot(load=(1.0, 1.0, 1.0), temp_c=95.0), DEFAULT_THRESHOLDS)
              if r.label == "Temp"]
    assert hot.fill == 1.0 and hot.colour == charts.RED


def test_node_status_line():
    F, W, C = panel.FINE, panel.WARNING, panel.CRITICAL
    assert sysimage.node_status([(F, 42.4)]) == (F, "1 node ok (42ms)")
    assert sysimage.node_status([(F, 30.0), (F, 46.0), (F, None)]) == (F, "3 nodes ok (~38ms)")
    assert sysimage.node_status([(F, None)]) == (F, "1 node ok")
    assert sysimage.node_status([(F, 9000.0), (W, 0.0), (F, 1.0)]) == (W, "2 of 3 nodes ok (see logs)")
    assert sysimage.node_status([(F, 1.0), (W, 0.0), (C, 0.0)]) == (C, "1 of 3 nodes ok (see logs)")


def test_render_with_status_issue_adds_footer_height():
    from io import BytesIO

    from PIL import Image
    t = [sysimage.Row("OS", "Unraid 7.3.4")]
    sizes = [Image.open(BytesIO(sysimage.render(t, 0, None, st))).size
             for st in (None, (panel.FINE, "1 node ok"), (panel.CRITICAL, "0 of 2 nodes ok"))]
    assert sizes[1][1] > sizes[0][1] and sizes[2][1] > sizes[1][1]


def test_image_puts_load_above_cpu():
    rows = [sysimage.Row(x, "1") for x in ("OS", "CPU", "RAM", "Load", "Temp")]
    assert [r.label for r in sysimage.image_order(rows)] == ["OS", "Load", "CPU", "RAM", "Temp"]
    assert sysimage.image_order(rows[:2]) == rows[:2]


def test_image_colours_the_measured_value():
    from shazzzbot.charts import RED, TEXT
    from shazzzbot.config import DEFAULT_THRESHOLDS
    from shazzzbot.host import Snapshot
    rows = {r.label: r for r in sysimage.rows(Snapshot(cpu_pct=95, ram_used_mb=1, ram_total_mb=10, temp_c=50),
                                              DEFAULT_THRESHOLDS)}
    cpu = rows["CPU"]
    assert "".join(t for t, _ in cpu.spans) == cpu.text and cpu.spans[0] == ("95%", RED)
    assert cpu.spans[1][1] == TEXT and rows["Temp"].spans[0][1] == TEXT  # fine values stay plain, bar keeps its ramp


def test_sig3_keeps_three_characters():
    f = "\u2007"
    assert sysimage.sig3(0.29, 1000, "B/s") == "290 KB/s"
    assert sysimage.sig3(4.0, 1000, "B/s") == "4.0 MB/s"
    assert sysimage.sig3(0.061, 1000, "bps") == f"{f}61 Kbps"
    assert sysimage.sig3(0.9996, 1000, "bps") == "1.0 Mbps"
    assert sysimage.sig3(5e6, 1000, "bps") == "5000 Gbps"


def test_hostname_precedes_os_in_all_views():
    from unittest.mock import patch

    from shazzzbot import unraid, view

    array = unraid.build({"NAME": "test-tower", "version": "7.3.4"}, {})
    table = sysimage.rows(Snapshot(uptime_s=7200), TH, f"Unraid {array.version}", hostname=array.name)
    assert [(r.label, r.text) for r in table[:2]] == [("Hostname", "test-tower"), ("OS", "Unraid 7.3.4")]
    section = sysimage.section("System", "", "soon", table, None)
    text = view.text(section).description
    assert text.index("Hostname") < text.index("OS") and "test-tower" in text
    fields = view.rich(section).fields
    assert [f.name for f in fields[:2]] == ["Hostname", "OS"]
    assert fields[0].value.startswith("test-tower")
    with patch.object(sysimage, "_text", wraps=sysimage._text) as draw:
        assert sysimage.render(table).startswith(b"\x89PNG")
    values = [call.args[4] for call in draw.call_args_list]
    assert values.index("test-tower") < values.index("Unraid 7.3.4")
    assert table[0].dot == panel.FINE and table[0].spans == ()


def test_section_marks_the_hot_value_from_its_spans():
    from shazzzbot import view
    rows = [sysimage.Row("Array", "22.1/22.7 TB    (97%)", level=2,
                         spans=(("22.1/22.7 TB    (", sysimage.TEXT), ("97%", sysimage.RED), (")", sysimage.TEXT))),
            sysimage.Row("Uptime", "2m", dot=1)]
    s = sysimage.section("t", "sub", "9:55", rows, None)
    assert s.parts[0].marks == [(), (0, 2)]  # Uptime sorts first and has no coloured span
    assert "\x1b[1;31m(97%)\x1b[0m" in view.text(s).description


def test_system_capacity_keeps_units_and_load_stays_a_count_in_all_views(monkeypatch):
    from unittest.mock import patch

    from shazzzbot import view

    monkeypatch.setattr(sysimage, "CORES", 1)
    table = sysimage.rows(Snapshot(uptime_s=7200, ram_used_mb=1024, ram_total_mb=2048, load=(1.0, 0.5, 0.1)),
                          TH, usage=(2**30, 2**31))
    rows = {r.label: r for r in table}
    assert rows["RAM"].text.startswith("1.0GB/2.0GB")
    assert rows["Array"].text.startswith("1.0TB/2.0TB")
    assert rows["Load"].cols == ("1.00 (1m)", "0.50 (5m)", "0.10 (15m)")
    section = sysimage.section("System", "", "soon", table, None)
    text, rich = view.text(section).description, str(view.rich(section).to_dict())
    assert all(value in text and value in rich for value in ("1.0GB/2.0GB", "1.0TB/2.0TB", "1.00 (1m)"))
    with patch.object(sysimage, "_text", wraps=sysimage._text) as draw:
        assert sysimage.render(table, now=123.0).startswith(b"\x89PNG")
    labels = [call.args[4] for call in draw.call_args_list]
    assert "1.00" in labels and any("1.0GB/2.0GB" in label for label in labels)


def test_tasks_row_lists_at_most_three_running_tasks_one_per_line():
    names = ["Parity - 72% done", "Mover", "A very long nightly photo sync job name", "Four", "Five"]
    active = [(60.0, n, panel.FINE) for n in names]
    alerts, tasks, _ = sysimage.status_rows([(panel.CRITICAL, "media degraded:\na exited (1)\nb exited (1)")],
                                            active, [], None, True, {"Mover": panel.WARNING})
    assert alerts.text == "1 critical"
    assert tasks.lines == ("5 tasks running:", "Parity - 72% done", "Mover",
                           "A very long nightly photo syn" + panel.ELL)
    assert tasks.marks == (panel.FINE, panel.FINE, panel.WARNING, panel.FINE)
    field = next(f for f in view.rich(sysimage.section("System", "", "", [tasks], None)).fields if f.name == "Tasks")
    assert field.value.startswith(f"5 tasks running:\nParity - 72% done\n{panel.WARN} Mover\nA very long")


def test_tasks_row_flags_a_hidden_hot_task_on_its_count_line():
    active = [(60.0, n, panel.FINE) for n in ("A", "B", "C", "Hot")]
    tasks = sysimage.status_rows([], active, [], None, False, {"Hot": panel.WARNING})[1]
    assert tasks.marks == (panel.WARNING, panel.FINE, panel.FINE, panel.FINE)
    text = view.text(sysimage.section("System", "", "", [tasks], None)).description
    import re
    lines = [re.sub("\x1b\\[[0-9;]*m", "", line).strip() for line in text.splitlines()]
    assert any(line.endswith("4 tasks running:") for line in lines) and "A" in lines and "C" in lines
    assert "Hot" not in text


def test_ups_without_data_keeps_its_shutdown_note_on_its_own_line():
    row = sysimage.ups_row(ups.Reading(), TH, {"mode": "sec_timer", "value": 600})
    assert row.lines == ("no data", "(off after 10m on battery)")
    field = next(f for f in view.rich(sysimage.section("System", "", "", [row], None)).fields if f.name == "UPS")
    assert field.value.startswith("no data")
    assert "\n(off after 10m on battery)" in field.value
