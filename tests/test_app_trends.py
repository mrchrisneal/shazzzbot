from unittest.mock import patch

import pytest

from shazzzbot import charts, panel, view
from shazzzbot.apphistory import AppHistory
from shazzzbot.glyphs import NO_DATA
from shazzzbot.host import Snapshot
from shazzzbot.spark import TREND_WIDTH


def history_table():
    history = AppHistory(10)
    for t in range(0, 301, 10):
        cpu = [("high", 2.5), ("low", 0.6)]
        ram = [("high", 100.0), ("low", 20.0)]
        if t >= 240:
            cpu.append(("late", 0.8))
            ram.append(("late", 10.0))
        if t >= 290:
            cpu.append(("twice", 0.4))
            ram.append(("twice", 15.0))
        if t == 300:
            cpu.append(("once", 0.3))
            ram.append(("once", 10.0))
        snapshot = Snapshot(top_cpu=cpu, top_ram=ram)
        history.add(snapshot, 300, t)
    return panel.apps_section(snapshot, history, 300, now=300), history


def test_each_app_uses_its_own_variation_and_even_one_sample_has_floor_dots():
    section, _ = history_table()
    for part in section.parts:
        high, low, late, twice, once = part.rows
        assert high[1][-1] == chr(0x28c0) * TREND_WIDTH
        assert low[1][-1] == chr(0x28c0) * TREND_WIDTH
        assert late[1][-1].startswith(NO_DATA * 12) and late[1][-1] != NO_DATA * TREND_WIDTH
        assert twice[1][-1] == once[1][-1] == NO_DATA * (TREND_WIDTH - 1) + chr(0x28c0)
        assert [len(trend.samples) for trend in part.trends[-2:]] == [2, 1]
        assert all(len(cells[-1]) == TREND_WIDTH and " " not in cells[-1] for _, cells in part.rows)
        assert all(0x2800 <= ord(ch) <= 0x28ff for _, cells in part.rows for ch in cells[-1])


def test_text_trend_cells_remain_fixed_width_and_keep_their_columns_with_short_history():
    section, _ = history_table()
    text = view.text(section).description
    lines = [line for line in text.splitlines() if any(0x2800 <= ord(ch) <= 0x28ff for ch in line)]
    assert len(lines) == 10
    assert len({len(line) - TREND_WIDTH for line in lines[:5]}) == 1
    assert len({len(line) - TREND_WIDTH for line in lines[5:]}) == 1
    assert all(len(line) <= panel.WIDTH + 3 for line in text.splitlines())
    for line in lines:
        trend = line[-TREND_WIDTH:]
        assert " " not in trend
        if "once" in line or "twice" in line:
            assert trend == NO_DATA * (TREND_WIDTH - 1) + chr(0x28c0)
        if "late" in line:
            assert trend.startswith(NO_DATA * 12)


def test_rich_view_keeps_every_trace_and_placeholder_at_the_same_width():
    section, _ = history_table()
    fields = view.rich(section).fields
    for field, part in zip(fields, section.parts):
        for line, (_, cells) in zip(field.value.splitlines(), part.rows):
            assert line.startswith("`" + cells[-1] + "`" + view.GAP2)
            assert len(cells[-1]) == TREND_WIDTH and " " not in cells[-1]


def test_image_uses_each_apps_range_and_a_constant_plot_width_for_cpu_and_ram():
    section, _ = history_table()
    with patch.object(charts, "plot", wraps=charts.plot) as plot:
        assert view.image(section, now=300).startswith(b"\x89PNG")
    assert plot.call_count == 10
    assert all(call.args[6] is None and call.kwargs == {"floor": True} for call in plot.call_args_list)
    assert {call.args[1][2] - call.args[1][0] for call in plot.call_args_list} == {120 * view.K}
    assert all(call.args[4:6] == (0, 300) for call in plot.call_args_list)


def test_scale_uses_all_listed_apps_over_the_window_and_ignores_hidden_apps():
    _, history = history_table()
    snapshot = Snapshot(top_cpu=[("low", 0.6)], top_ram=[("low", 20.0)])
    section = panel.apps_section(snapshot, history, 300, now=300)
    assert all(part.rows[0][1][-1] == chr(0x28c0) * TREND_WIDTH for part in section.parts)
    expired = panel.apps_section(snapshot, history, 300, now=1000)
    assert all(part.rows[0][1][-1] == NO_DATA * TREND_WIDTH for part in expired.parts)


def test_cpu_and_ram_system_trends_use_the_same_fixed_cell_count_as_top_apps():
    samples = [(float(t), 10.0, 20.0, 0.0, 0.0, 0.0, 0.0) for t in range(0, 301, 10)]
    section = charts.section(samples, 300, now=300)
    load = section.parts[0]
    assert all(len(cells[0][-TREND_WIDTH:]) == TREND_WIDTH
               and all(0x2800 <= ord(ch) <= 0x28ff for ch in cells[0][-TREND_WIDTH:])
               for _, cells in load.rows[1:])
    cards = section.parts[-1]
    assert all(len(cards.rows[i][1][-1].split("  ")[-1]) == TREND_WIDTH for i in (0, 3))


def test_worst_case_cpu_and_ram_names_values_and_trends_share_columns(monkeypatch):
    from PIL import ImageDraw

    cpu = [("cpu_" + "W" * 40, 100.0), ("short_cpu", 9.5)]
    ram = [("ram_" + "m" * 40, 999999.9), ("short_ram", 1.0)]
    snapshot = Snapshot(top_cpu=cpu, top_ram=ram)
    history = AppHistory(10)
    for t in (0, 10, 20):
        history.add(snapshot, 300, t)
    section = panel.apps_section(snapshot, history, 300, now=20)
    text = view.text(section).description
    lines = [line for line in text.splitlines() if any(0x2800 <= ord(ch) <= 0x28ff for ch in line)]
    starts = [next(i for i, ch in enumerate(line) if 0x2800 <= ord(ch) <= 0x28ff) for line in lines]
    assert len(lines) == 4 and len(set(starts)) == 1
    assert len({line.index("short_") for line in lines if "short_" in line}) == 1
    for line in lines:
        start = starts[0]
        assert line[start - 1] == " " and line[start - 2] != " "
        assert len(line) <= panel.WIDTH + 3
    drawn = []
    original = ImageDraw.ImageDraw.text
    def capture(draw, xy, text, **kwargs):
        drawn.append((xy, text, kwargs))
        return original(draw, xy, text, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    with patch.object(charts, "plot", wraps=charts.plot) as plot:
        assert view.image(section, now=20).startswith(b"\x89PNG")
    names = {cells[1] for part in section.parts for _, cells in part.rows}
    values = {cells[2] for part in section.parts for _, cells in part.rows}
    name_x = {xy[0] for xy, text, _ in drawn if text in names}
    value_x = {xy[0] for xy, text, kw in drawn if text in values and kw["anchor"] == "rm"}
    trend_x = {call.args[1][0] for call in plot.call_args_list}
    assert len(name_x) == len(value_x) == len(trend_x) == 1 and plot.call_count == 4
    small = view._fonts()[2].font_variant(size=view._fonts()[2].size * view.K)
    assert abs(next(iter(trend_x)) - next(iter(value_x)) - small.getlength(" ")) < 0.01


def test_flat_image_trends_are_lines_at_each_apps_own_baseline_with_no_fill_below(monkeypatch):
    import math

    from PIL import Image, ImageChops, ImageDraw

    snapshot = Snapshot(top_cpu=[("high_cpu", 100.0), ("low_cpu", 1.0)],
                        top_ram=[("high_ram", 100.0), ("low_ram", 1.0)])
    history = AppHistory(10)
    for t in (0, 10, 20):
        history.add(snapshot, 20, t)
    section = panel.apps_section(snapshot, history, 20, now=20)
    text, rich = view.text(section).to_dict(), view.rich(section).to_dict()
    names = [cells[1] for part in section.parts for _, cells in part.rows]
    labels, graphs = {}, []
    original_text, original_plot = ImageDraw.ImageDraw.text, charts.plot
    def draw_text(draw, xy, text, **kwargs):
        if text in names:
            labels[text] = (xy, kwargs["font"])
        return original_text(draw, xy, text, **kwargs)
    def plot(draw, box, *args, **kwargs):
        original_plot(draw, box, *args, **kwargs)
        bounds = (math.floor(box[0]), math.floor(box[1]), math.ceil(box[2]) + 1, math.ceil(box[3]) + 2)
        graphs.append((box, bounds, draw._image.crop(bounds), args[4], kwargs))
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", draw_text)
    monkeypatch.setattr(charts, "plot", plot)
    assert view.image(section, now=20).startswith(b"\x89PNG")
    assert len(graphs) == len(names) == 4
    for name, (box, bounds, pixels, scale, options) in zip(names, graphs):
        xy, font = labels[name]
        baseline = xy[1] + font.getbbox("0", anchor="lm")[3] - font.getbbox("0", anchor="ls")[3]
        occupied = ImageChops.difference(pixels, Image.new("RGB", pixels.size, view.PANEL)).getbbox()
        assert occupied is not None and options == {"floor": True} and scale is None
        assert bounds[1] + occupied[3] - 1 == box[3] == baseline
        row_top = xy[1] - view.ROW_H * view.K / 2
        assert row_top <= box[1] < baseline < row_top + view.ROW_H * view.K
        assert bounds[1] + occupied[1] >= baseline - view.K
        assert all(pixels.getpixel((x, pixels.height - 1)) == view.PANEL for x in range(pixels.width))
        assert all(pixels.getpixel((x, 0)) == view.PANEL for x in range(pixels.width))
    assert view.text(section).to_dict() == text and view.rich(section).to_dict() == rich


@pytest.mark.parametrize("partial", [False, True])
def test_top_apps_note_has_the_same_information_in_text_embed_and_image(monkeypatch, partial):
    section, history = history_table()
    if not partial:
        section = panel.apps_section(Snapshot(top_cpu=[("high", 2.5)], top_ram=[("high", 100)]),
                                     history, 300, now=300)
    expected = "Trends, last 5m" + (" (partial data)" if partial else "")
    assert section.note == expected
    assert expected in view.text(section).description and expected in view.rich(section).description
    drawn = []
    original = view.ImageDraw.ImageDraw.text
    def capture(draw, xy, text, **kwargs):
        drawn.append((xy, text, kwargs))
        return original(draw, xy, text, **kwargs)
    monkeypatch.setattr(view.ImageDraw.ImageDraw, "text", capture)
    assert view.image(section, now=300).startswith(b"\x89PNG")
    notes = [row for row in drawn if row[1] == expected]
    assert len(notes) == 1 and notes[0][2]["fill"] == charts.DIM
    assert next(row[0][1] for row in drawn if row[1] == section.title) < notes[0][0][1]
    assert notes[0][0][1] < next(row[0][1] for row in drawn if row[1] == section.parts[0].heading)


def test_image_variation_spans_the_band_with_no_fill_below_the_line(monkeypatch):
    import math

    history = AppHistory(10)
    for t, value in ((0, 1), (10, 2), (20, 1)):
        snapshot = Snapshot(top_cpu=[("cpu", value)], top_ram=[("ram", value * 100)])
        history.add(snapshot, 20, t)
    section = panel.apps_section(snapshot, history, 20, now=20)
    layers = []
    original = charts.plot
    def plot(draw, box, *args, **kwargs):
        original(draw, box, *args, **kwargs)
        bounds = (math.floor(box[0]), math.floor(box[1]), math.ceil(box[2]), math.ceil(box[3]) + 2)
        layers.append((draw._image.crop(bounds), args[5]))
    monkeypatch.setattr(charts, "plot", plot)
    assert view.image(section, now=20).startswith(b"\x89PNG") and len(layers) == 2
    for pixels, colour in layers:
        assert pixels.getpixel((0, pixels.height - 2)) == colour
        assert pixels.getpixel((pixels.width - 1, pixels.height - 2)) == colour
        assert pixels.getpixel((pixels.width // 2, 0)) == colour
        assert all(pixels.getpixel((pixels.width // 2, y)) == view.PANEL
                   for y in range(4 * view.K, pixels.height))
    assert [min(v for _, v in t.samples) for part in section.parts for t in part.trends] == [1, 100]
    assert [max(v for _, v in t.samples) for part in section.parts for t in part.trends] == [2, 200]
