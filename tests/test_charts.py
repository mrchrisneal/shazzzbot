import io
import logging
import sqlite3
import time
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

from shazzzbot import charts
from shazzzbot.host import Snapshot


def test_parse_span_bounds():
    assert charts.parse_span("5m") == 300 and charts.parse_span(" 24H ") == 86400 and charts.parse_span("90m") == 5400
    assert charts.parse_span("3d") == 3 * 86400 and charts.parse_span("168h") == charts.MAX_WINDOW
    assert all(charts.parse_span(x) is None for x in ("4m", "169h", "8d", "", "abc", "-5m"))
    assert charts.span(5400) == "90m" and charts.span(7200) == "2h" and charts.span(86400) == "24h"
    assert charts.span(3 * 86400) == "3d"


def test_columns_keep_peaks_and_skip_old():
    s = [(0.0, 1.0), (2.0, 9.0), (3.0, 2.0), (6.0, 4.0), (-50.0, 99.0)]
    assert charts._columns(s, 1, 0.0, 10, 2) == [9.0, 4.0]


def test_render_24h_is_small_png():
    s = [(i * 10.0, i % 100, 50.0, i % 7, i % 3, i % 11, i % 5) for i in range(8640)]
    png = charts.render(s, 86400, now=86400.0)
    width = Image.open(io.BytesIO(png)).width
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) < 43_000 * width / charts.W
    assert charts.render([], 1800, now=1.0)[:4] == b"\x89PNG"


def test_history_keeps_normal_dimensions_at_the_shared_two_times_scale():
    from shazzzbot import view

    with patch.object(charts.Image, "new", wraps=charts.Image.new) as allocate, \
            patch.object(charts, "plot", wraps=charts.plot) as plot:
        png = charts.render([], 1800, now=123)
    expected_height = charts.HEAD_H + 2 * charts.CHART_H
    assert charts.SS == charts.renderbudget.SCALE == 2
    assert isinstance(png, view.Rendered) and (png.key, png.section_title, png.now) == ("history", "History (30m)", 123)
    assert Image.open(io.BytesIO(png)).size == (charts.W, expected_height)
    assert allocate.call_args_list[0].args[:2] == ("RGB", (charts.W, expected_height))
    assert allocate.call_args_list[1].args[:2] == ("RGBA", (charts.W * 2, expected_height * 2))
    assert all(call.args[-1] == 3 for call in plot.call_args_list)


def test_oversized_history_returns_a_bounded_placeholder_before_allocating_the_card(monkeypatch):
    from shazzzbot import view

    width = 100_000_000
    original = charts.Image.new
    allocations = []

    def allocate(mode, size, *args, **kwargs):
        assert 4 * size[0] * size[1] < charts.renderbudget.RENDER_BUDGET
        allocations.append(size)
        return original(mode, size, *args, **kwargs)

    monkeypatch.setattr(charts.Image, "new", allocate)
    with patch.object(view, "placeholder", wraps=view.placeholder) as placeholder:
        png = charts.render([], 1800, now=123, min_width=width)
    placeholder.assert_called_once_with("history", "History (30m)", width, 123, None)
    image = Image.open(io.BytesIO(png))
    assert allocations and image.width < width and image.height < 1000


def test_core_colours_share_one_lightness():
    light = [charts._lab(charts.core_colour(k, 12))[0] for k in range(12)]
    assert max(light) - min(light) < 0.01 and len({charts.core_colour(k, 12) for k in range(12)}) == 12


def test_render_draws_per_core_lines():
    s = [(float(t), 50.0, 40.0, 1.0, 2.0, 3.0, 4.0, *(10.0 * k for k in range(16))) for t in range(0, 1800, 10)]
    assert charts.render(s, 1800, now=1800.0)[:4] == b"\x89PNG"


def test_ramp_hits_green_yellow_red_at_thresholds():
    assert charts.ramp(40, 80, 90) == charts.GREEN and charts.ramp(80, 80, 90) == charts.YELLOW
    assert charts.ramp(85, 80, 90) == charts.ORANGE
    assert charts.ramp(90, 80, 90) == charts.RED == charts.ramp(500, 80, 90) == charts.ramp(90, 90, 90)


def test_nice_scales_below_one():
    assert [charts._nice(v) for v in (0, 0.3, 0.07, 1, 3, 7, 40, 101)] == [1, 0.5, 0.1, 1, 5, 10, 50, 200]


def test_smooth_passes_through_samples_without_overshoot():
    pts = [(0.0, 10.0), (10.0, 0.0), (20.0, 0.0), (30.0, 10.0)]
    out = charts._smooth(pts, 4)
    assert pts[0] in out and pts[-1] in out and (10.0, 0.0) in out
    assert all(0.0 <= y <= 10.0 for _, y in out)


def test_num_keeps_four_digits():
    assert [charts.num(v) for v in (0, 23.84, 999.94, 999.96, 1346.4)] == ["0.0", "23.8", "999.9", "1000", "1346"]


def test_short_lines_and_empty_disks():
    assert charts._smooth([(0.0, 1.0), (1.0, 2.0)]) == [(0.0, 1.0), (1.0, 2.0)]
    assert charts.disk_bars([("disk1", 0.0, 30, 90.0, 95.0)])[:4] == b"\x89PNG"
    assert charts.disk_bars([]) is None


def test_disk_bars_over_budget_draw_a_placeholder_at_the_frame_time(monkeypatch):
    from shazzzbot import view
    calls = []
    monkeypatch.setattr(charts.renderbudget, "check", lambda key, size: key != "disk_usage")
    monkeypatch.setattr(view, "placeholder", lambda *a, **k: calls.append(a) or b"\x89PNG placeholder")
    tz = ZoneInfo("America/Los_Angeles")
    assert charts.disk_bars([("disk1", 50.0, 30, 90.0, 95.0)], 123.0, tz) == b"\x89PNG placeholder"
    assert calls == [("disk_usage", "Disk usage", charts.W, 123.0, tz)]


def snap(cpu=1.0, cores=(2.0, 3.0)):
    return Snapshot(cpu_pct=cpu, cpu_cores=list(cores))


def test_history_keeps_only_its_span_in_memory_and_on_disk(tmp_path):
    h = charts.History(str(tmp_path / "d" / "h.db"), keep=100)
    for t in (1000.0, 1050.0, 1200.0):
        h.store(h.add(snap(), t))
    assert [r[0] for r in h.samples] == [1200.0] and list(h.samples[0]) == [1200.0, 1.0, 0.0, 0, 0, 0, 0, 2.0, 3.0]
    assert h._db.execute("SELECT t FROM samples").fetchall() == [(1200.0,)]
    h.close()


def test_history_skips_malformed_rows_and_moves_a_corrupt_file_aside(tmp_path, caplog):
    path = tmp_path / "h.db"
    h = charts.History(str(path), keep=10**9)
    h.store(h.add(snap()))
    h._db.execute("INSERT INTO samples VALUES (?, x'0102')", (time.time() + 1,))
    h._db.execute("INSERT INTO samples VALUES (?, 'text')", (time.time() + 2,))
    h.close()

    assert len(charts.History(str(path), keep=10**9).samples) == 1
    path.write_bytes(b"not a database" * 100)
    (tmp_path / "h.db-wal").write_bytes(b"junk")
    with caplog.at_level(logging.ERROR):
        h = charts.History(str(path), keep=10**9)
    assert not h.samples and (tmp_path / "h.db.bad").exists() and "unreadable" in caplog.text
    h.close()


@pytest.mark.parametrize("header", [0, 2])
def test_history_unsupported_format_keeps_the_existing_database_untouched(tmp_path, header):
    from shazzzbot.helper.formats import FormatError

    path = tmp_path / "history.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE samples (t REAL PRIMARY KEY, v BLOB NOT NULL)")
        db.execute(f"PRAGMA user_version={header}")
    db.close()
    before = path.read_bytes()
    with pytest.raises(FormatError):
        charts.History(str(path), keep=100)
    assert path.read_bytes() == before
    assert not path.with_suffix(".db.bad").exists()
    assert not (tmp_path / "history.db-wal").exists()


def test_history_write_failure_logs_once_and_recovers(tmp_path, caplog):
    h = charts.History(str(tmp_path / "h.db"), keep=10**9)
    real = h._db
    h._db = MagicMock(execute=MagicMock(side_effect=sqlite3.OperationalError("disk full")), in_transaction=False)
    with caplog.at_level(logging.INFO):
        h.store(h.add(snap()))
        h.store(h.add(snap()))
        h._db = real
        h.store(h.add(snap()))
    assert caplog.text.count("history write failed") == 1 and "work again" in caplog.text
    assert len(h.samples) == 3
    h._db = MagicMock(executemany=MagicMock(side_effect=sqlite3.OperationalError("locked")), in_transaction=True)
    h.store(h.add(snap()))
    h._db.execute.assert_called_with("ROLLBACK")
    h._db = real
    h.close()
    h.store(h.add(snap()))  # after close: dropped, never raises


def test_gaps_cover_missing_spans_and_both_ends():
    assert charts.gaps([10.0, 20.0, 80.0, 90.0], 0.0, 100.0, 30.0) == [(20.0, 80.0)]
    assert charts.gaps([50.0], 0.0, 100.0, 30.0) == [(0.0, 50.0), (50.0, 100.0)]
    assert charts.gaps([], 0.0, 100.0, 30.0) == [(0.0, 100.0)]


def test_render_shades_missing_spans_at_half_black():
    s = [(float(t), 50.0, 40.0, 1.0, 2.0, 3.0, 4.0) for t in (*range(0, 600, 10), *range(1200, 1800, 10))]
    im = Image.open(io.BytesIO(charts.render(s, 1800, now=1800.0)))
    y = charts.HEAD_H + charts.CHART_H // 2 + 20
    hole_x = 22 + int((im.width // 2 - 44) * 0.5)  # 900s into the CPU chart
    assert im.getpixel((hole_x, y))[:3] == charts.SHADE
    assert im.getpixel((22 + 10, y))[:3] != charts.SHADE
    assert charts.SHADE == tuple(v // 2 for v in charts.PANEL)


def test_history_section_has_phone_width_sparklines_and_braille_in_text():
    from shazzzbot import panel, view

    samples = [(float(t), t % 100, 40.0, 1.0, 2.0, 3.0, 4.0) for t in range(0, 301, 10)]
    section = charts.section(samples, 300, now=300.0)
    assert [p.heading for p in section.parts[:3]] == ["Load (%)", "Disk (B/s)", "Net (bit/s)"]
    assert section.parts[3].heading == "CPU 5m"
    assert section.parts[4].heading == "RAM 5m"
    assert all(len(part.rows) == 1 for part in section.parts[3:5])
    assert section.parts[4].rows == [(panel.FINE, (chr(0x28c0) * 15,))]
    assert all(p.only == "text" for p in section.parts[:5]) and section.parts[5].only == "embed"
    text = view.text(section).description
    assert all(len(line) <= panel.WIDTH + 3 for line in text.splitlines())
    assert "Now" in text and "Peak" not in text and any(0x2800 < ord(ch) <= 0x28ff for ch in text)
    assert any(0x2800 < ord(ch) <= 0x28ff for ch in text) and chr(0x2026) not in text


def test_history_uses_bucket_width_in_text_and_rich_views():
    from shazzzbot import view
    from shazzzbot.spark import TREND_WIDTH

    section = charts.section([(1800.0, 90, 40, 1, 2, 3, 4)], 1800, now=1800.0)
    assert section.parts[3].heading == "CPU 30m"
    assert len(section.parts[3].rows) == 1 and len(section.parts[4].rows) == 1
    assert len(section.parts[3].rows[0][1][0]) == 15
    assert section.parts[3].rows[0][1][0][-1] != " "
    assert section.parts[3].rows[0][1][0] == chr(0x2840) * 14 + chr(0x28c0)
    assert len(view.rich(section).fields[0].value.splitlines()[1]) == TREND_WIDTH


def test_history_embed_is_a_grid_with_units_after_values():
    from shazzzbot import view

    samples = [(float(t), 19, 75, 27, 48, 80, 120) for t in range(0, 301, 10)]
    fields = view.rich(charts.section(samples, 300, now=300)).fields
    assert [f.name for f in fields] == ["CPU", "Disk read", "Net down", "RAM", "Disk write", "Net up"]
    assert all(f.inline for f in fields)
    assert fields[0].value.splitlines()[0] == "**19%**"
    assert fields[1].value.splitlines()[0] == "**27 MB/s**"
    assert fields[2].value.splitlines()[0] == "**80 Mbps**"
    assert all(any(0x2800 < ord(ch) <= 0x28ff for ch in f.value) for f in fields)


def test_history_section_empty_window_excludes_old_and_future_samples():
    from shazzzbot import view
    from shazzzbot.glyphs import NO_DATA

    section = charts.section([(-1, 99, 99, 99, 99, 99, 99), (301, 99, 99, 99, 99, 99, 99)], 300, now=300)
    cells = [ch for ch in view.text(section).description if 0x2800 <= ord(ch) <= 0x28ff]
    assert cells and set(cells) == {NO_DATA}
    assert all(all(ch == NO_DATA for ch in cells[0]) for part in section.parts[3:5] for _, cells in part.rows)
    assert all(part.heading.endswith("5m") for part in section.parts[3:5])
    assert all(f.value.splitlines()[0] == "-" for f in view.rich(section).fields)


def test_history_views_share_inclusive_window_edges_and_exclude_future_samples():
    from shazzzbot import view

    valid = [(0.0, 10, 20, 1, 2, 3, 4), (300.0, 30, 40, 5, 6, 7, 8)]
    samples = [(-0.1, 999, 999, 999, 999, 999, 999), *valid, (300.1, 999, 999, 999, 999, 999, 999)]
    with patch.object(charts, "_chart", wraps=charts._chart) as draw:
        image = charts.render(samples, 300, now=300)
    assert image.startswith(b"\x89PNG")
    assert len(draw.call_args_list) == 4
    assert all(call.args[4] == valid for call in draw.call_args_list)
    assert all(call.args[8] == [(0.0, 300.0)] for call in draw.call_args_list)
    actual, expected = charts.section(samples, 300, now=300), charts.section(valid, 300, now=300)
    assert view.text(actual).description == view.text(expected).description
    assert view.rich(actual).to_dict() == view.rich(expected).to_dict()


def test_history_startup_preserves_the_requested_window_and_leading_gap():
    from shazzzbot.glyphs import NO_DATA

    samples = [(290.0, 10, 20, 1, 2, 3, 4), (300.0, 30, 40, 5, 6, 7, 8)]
    with patch.object(charts, "_chart", wraps=charts._chart) as draw:
        image = charts.render(samples, 300, now=300)
    assert image.startswith(b"\x89PNG") and image.section_title == "History (5m)"
    assert all(call.args[5:7] == (300, 300) for call in draw.call_args_list)
    assert all(call.args[8] == [(0, 290.0)] for call in draw.call_args_list)
    section = charts.section(samples, 300, now=300)
    assert section.title == "History, last 5m"
    for part in section.parts[3:5]:
        trend = part.rows[0][1][0]
        assert part.heading.endswith("5m") and trend.startswith(NO_DATA) and trend[-1] != NO_DATA


def test_history_table_stays_narrow_with_large_rates():
    from shazzzbot import panel, view

    section = charts.section([(300, 1e12, 100, 1e12, 1e12, 1e12, 1e12)], 300, now=300)
    assert all(len(line) <= panel.WIDTH + 3 for line in view.text(section).description.splitlines())
    assert all(chr(0x2026) not in f.value for f in view.rich(section).fields)


def test_history_text_values_keep_units_and_right_align_percent_signs():
    from shazzzbot import view

    section = charts.section([(300, 9, 69, 27, 48, 80, 120)], 300, now=300)
    cpu, ram = [cells[0] for _, cells in section.parts[0].rows[1:]]
    assert "9%" in cpu and "69%" in ram and cpu.index("%") == ram.index("%")
    text = view.text(section).description
    assert all(value in text for value in ("27MB/s", "48MB/s", "80Mbps", "120Mbps"))


def test_history_image_labels_measurements_and_keeps_core_identifiers_unitless(monkeypatch):
    from PIL import ImageDraw

    original = ImageDraw.ImageDraw.text
    labels = []
    def text(draw, xy, value, *args, **kwargs):
        labels.append(value)
        return original(draw, xy, value, *args, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", text)
    png = charts.render([(300, 9, 69, 27, 48, 80, 120, 5, 13)], 300, now=300)
    assert png.startswith(b"\x89PNG")
    assert {"avg 9.0%", "69.0%", "R 27.0 MB/s", "W 48.0 MB/s", "DL 80.0 Mbps", "UL 120.0 Mbps"} <= set(labels)
    assert {"100%", "50 MB/s", "200 Mbps", "1", "2"} <= set(labels)
    assert {value for value in labels if value.replace(".", "").isdigit()} == {"1", "2"}


def test_large_history_values_keep_units_in_scientific_notation():
    from shazzzbot import view

    section = charts.section([(300, 1e12, 100, 1e12, 1e12, 1e12, 1e12)], 300, now=300)
    text = view.text(section).description
    assert "1e12%" in text and "1e12MB/s" in text and "1e12Mbps" in text
    assert all(len(line) <= 53 for line in text.splitlines())
    assert charts._measure(1e12, "%") == "1.0e12%"
    assert charts._measure(100, "%", axis=True) == "100%"


def test_scale_labels_have_a_reserved_band_above_every_plot(monkeypatch):
    from PIL import Image, ImageDraw

    labels, plots = [], []
    original_text, original_plot = ImageDraw.ImageDraw.text, charts.plot
    def text(draw, xy, text, **kwargs):
        if kwargs.get("anchor") == "ld":
            labels.append((xy, text, kwargs["font"]))
        return original_text(draw, xy, text, **kwargs)
    def plot(draw, box, *args):
        plots.append(box)
        return original_plot(draw, box, *args)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", text)
    monkeypatch.setattr(charts, "plot", plot)
    png = charts.render([(100, 100, 100, 500, 500, 1000, 1000)], 300, now=100)
    assert png.startswith(b"\x89PNG") and len(labels) == 4
    for xy, label, font in labels:
        top = min(box[1] / charts.SS for box in plots if box[0] / charts.SS <= xy[0] <= box[2] / charts.SS
                  and box[1] / charts.SS > xy[1])
        assert xy[1] + font.getbbox(label, anchor="ld")[3] < top
    expected = max(charts.W, 2 * max(charts.chart_width(
        [(100, 100, 100, 500, 500, 1000, 1000)], spec, charts._fonts()) for spec in charts.CHARTS))
    assert Image.open(io.BytesIO(png)).width == expected


def test_floor_lines_keep_missing_spans_empty_and_draw_isolated_samples():
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (100, 40), charts.BG)
    charts.plot(ImageDraw.Draw(image), (0, 5, 100, 35), [(0, 5), (9, 15)], 1, 0, 10, None,
                charts.BLUE, holes=[(0, 9)], floor=True)
    assert image.getpixel((0, 35)) == charts.BLUE
    assert image.getpixel((90, 5)) == charts.BLUE
    assert all(image.getpixel((50, y)) == charts.BG for y in range(image.height))
    assert all(image.getpixel((x, 36)) == charts.BG for x in range(image.width))
    assert all(image.getpixel((x, 4)) == charts.BG for x in range(image.width))
