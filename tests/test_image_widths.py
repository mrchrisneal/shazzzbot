import io
from datetime import UTC

import pytest
from PIL import Image, ImageDraw

from shazzzbot import charts, panel, sysimage, view


def record_text(monkeypatch):
    records = []
    original = ImageDraw.ImageDraw.text
    def draw(self, xy, text, *args, **kwargs):
        font = kwargs["font"]
        box = self.textbbox(xy, text, font=font, anchor=kwargs.get("anchor"))
        records.append((self._image.size, text, font.size, box))
        return original(self, xy, text, *args, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", draw)
    return records


def worst_table():
    return [sysimage.Row("OS", "Unraid 7.3.4", note="(update available!)"),
            sysimage.Row("RAM", "999.9GB/999.9GB (100%)", 1, charts.RED),
            sysimage.Row("Array", "999.9TB/999.9TB (100%)", 1, charts.RED),
            sysimage.Row("Disk I/O", "R 999.9 MB/s  W 999.9 MB/s  (100% busiest)", 1, charts.RED),
            sysimage.Row("Net I/O", f"{sysimage.DOWN} 999.9 Mbps  {sysimage.UP} 999.9 Mbps (100% of link)",
                         1, charts.RED),
            sysimage.Row("Load", "", spans=(("99.99 (1m)  ", charts.TEXT), (-75.0, charts.YELLOW),
                                            ("        99.99 (5m)  ", charts.TEXT), (0.0, charts.TEXT),
                                            ("        99.99 (15m)  ", charts.TEXT), (75.0, charts.GREEN))),
            sysimage.Row("UPS", "100%  99h59m  On battery, Low battery, Replace battery", 1, charts.RED),
            sysimage.Row("Alerts", "", spans=((sysimage.DOT, charts.RED), ("123 critical  ", charts.TEXT),
                                              (sysimage.DOT, charts.YELLOW), ("456 warnings", charts.TEXT))),
            sysimage.Row("Tasks", "3 tasks running: Complete archive verification, Nightly array maintenance, "
                         "Scheduled remote backup", dot=panel.WARNING),
            sysimage.Row("Nodes", "2 nodes ok (~91ms)", note="(update available!)", dot=panel.WARNING)]


def test_system_width_follows_every_rows_full_drawing_without_shrinking(monkeypatch):
    table = sysimage.image_order(worst_table())
    _, head, small = (f.font_variant(size=f.size * sysimage.K) for f in charts._fonts())
    layout = sysimage.layout(table, head, small)
    records = record_text(monkeypatch)
    image = Image.open(io.BytesIO(sysimage.render(table, now=123, tz=UTC)))
    assert image.width == layout.width > sysimage.W
    assert layout.bx1 - layout.bx0 == 250 and layout.tx - layout.bx0 == 270
    assert max(layout.right_edges) <= image.width - 24
    assert all(box[2] <= size[0] - 24 * sysimage.K for size, _, _, box in records)
    assert {size for _, _, size, _ in records} <= {head.size, small.size}
    assert any(text == "(update available!)" and size == small.size for _, text, size, _ in records)


def test_normal_system_keeps_its_width_bars_and_fonts(monkeypatch):
    table = [sysimage.Row("OS", "Unraid 7.3.4"), sysimage.Row("CPU", "19%", 0.19, charts.GREEN)]
    _, head, small = (f.font_variant(size=f.size * sysimage.K) for f in charts._fonts())
    layout = sysimage.layout(table, head, small)
    records = record_text(monkeypatch)
    image = Image.open(io.BytesIO(sysimage.render(table, now=123, tz=UTC)))
    assert image.width == layout.width == 800
    assert layout.bx1 - layout.bx0 == 250 and layout.tx - layout.bx0 == 270
    assert {size for _, _, size, _ in records} == {head.size, small.size}


def test_generic_tables_headings_and_title_grow_without_cutting_cells(monkeypatch):
    cells = ("long_container_name_" * 12, "999.9GB", "long status " * 12)
    part = panel.Part([(panel.WARNING, cells)], "A long heading " * 20, "lrr", icons=("dot1",))
    section = panel.Section("wide", "A long title " * 30, [part])
    records = record_text(monkeypatch)
    image = Image.open(io.BytesIO(view.image(section, now=123, tz=UTC)))
    assert image.width > 1000
    assert all(cell in [text for _, text, _, _ in records] for cell in cells)
    assert part.heading in [text for _, text, _, _ in records]
    assert all(box[2] <= size[0] - 24 * view.K for size, _, _, box in records)
    _, head, small = (f.font_variant(size=f.size * view.K) for f in charts._fonts())
    assert {size for _, _, size, _ in records} <= {head.size, small.size}
    assert view.part_right(part, small) <= image.width - 24


def test_history_legends_and_disk_labels_grow_the_canvas_at_normal_font_sizes(monkeypatch):
    samples = [(123.0, 100.0, 100.0, 999.9, 999.9, 999.9, 999.9)]
    disks = [("a_long_cache_pool_name_" + str(i), 100.0, None, 90.0, 95.0) for i in range(3)]
    records = record_text(monkeypatch)
    image = Image.open(io.BytesIO(charts.render(samples, 1800, now=123, tz=UTC)))
    bars = Image.open(io.BytesIO(charts.disk_bars(disks, now=123, tz=UTC)))
    assert image.width > charts.W and bars.width > charts.W
    assert all(box[2] <= size[0] - 24 for size, _, _, box in records)
    assert all(box[0] >= 22 for _, _, _, box in records)
    assert {size for _, _, size, _ in records} <= {font.size for font in charts._fonts()}
    assert all(charts.chart_width(samples, spec, charts._fonts()) <= image.width // 2 for spec in charts.CHARTS)
    assert charts.disk_width(disks, charts._fonts()[2]) <= bars.width
    assert all(name in [text for _, text, _, _ in records] for name, *_ in disks)


def test_history_without_wide_content_keeps_the_default_width():
    assert Image.open(io.BytesIO(charts.render([], 1800, now=123, tz=UTC))).width == charts.W


@pytest.mark.parametrize("icon", ["", "github"])
def test_combined_image_footer_widens_instead_of_shrinking_its_font(monkeypatch, icon):
    stream = io.BytesIO()
    Image.new("RGB", (800, 40), charts.BG).save(stream, "PNG")
    records = record_text(monkeypatch)
    note = "Next images at 11:00 AM. " * 10
    image = Image.open(io.BytesIO(view.stack([stream.getvalue()], note, icon)))
    assert image.width > 800
    assert {size for _, _, size, _ in records} == {charts._fonts()[2].size}
    assert all(box[0] >= view.FOOT_PAD and box[2] <= size[0] - view.FOOT_PAD for size, _, _, box in records)
    normal = Image.open(io.BytesIO(view.stack([stream.getvalue()], "Next images at 11:00 AM", icon)))
    assert normal.width == 800
