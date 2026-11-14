import asyncio
import io
from unittest.mock import AsyncMock

import pytest
from PIL import Image, ImageDraw

from shazzzbot import helperclient, panel, sysimage, ups, view
from tests.test_bot import make_bot


@pytest.mark.parametrize("mode,value,note", [("batt_level", 40, "(off at 40%)"),
                                          ("batt_timer", 600, "(off at 10m left)"),
                                          ("sec_timer", 3600, "(off after 1h on battery)")])
def test_shutdown_setting_is_on_the_ups_row_in_every_render_and_only_percent_draws_a_marker(
        monkeypatch, mode, value, note):
    reading = ups.Reading(("OL",), 95, 3600)
    row = sysimage.ups_row(reading, panel.DEFAULT_THRESHOLDS, {"mode": mode, "value": value})
    assert note in row.text and row.level == panel.FINE and not row.issues
    assert "95% - 1h left " + note in row.text and row.text.endswith("Online")
    card = sysimage.section("System", "", "", [row], None)
    assert note in view.text(card).description and "95% - 1h left" in view.text(card).description
    field = next(f for f in view.rich(card).fields if f.name == "UPS")
    assert field.value.startswith(f"95% - 1h left\n{note}\nOnline")
    lines, words = [], []
    original_line, original_text = ImageDraw.ImageDraw.line, sysimage._text
    def line(draw, xy, **kwargs):
        lines.append((xy, kwargs))
        return original_line(draw, xy, **kwargs)
    def text(*args):
        words.append(args[4])
        return original_text(*args)
    monkeypatch.setattr(ImageDraw.ImageDraw, "line", line)
    monkeypatch.setattr(sysimage, "_text", text)
    png = sysimage.render([row], now=123)
    image = Image.open(io.BytesIO(png))
    assert any(note in text for text in words)
    markers = [(xy, style) for xy, style in lines if style.get("fill") == (255, 255, 255)]
    assert len(markers) == (1 if mode == "batt_level" else 0)
    if markers:
        _, head, small = (font.font_variant(size=font.size * sysimage.K) for font in sysimage._fonts())
        layout = sysimage.layout([row], head, small)
        x = layout.bx0 + (layout.bx1 - layout.bx0) * value / 100
        cy = view.TOP + view.ROW_H + 4 + view.PAD + sysimage.ROW_H // 2
        xy, style = markers[0]
        assert xy == (x * sysimage.K, (cy - 11) * sysimage.K, x * sysimage.K, (cy + 11) * sysimage.K)
        assert style["width"] == 2 * sysimage.K
        assert (255, 255, 255) in [image.getpixel((int(x) + dx, cy)) for dx in (-1, 0, 1)]


def test_missing_settings_and_missing_ups_data_stay_optional():
    row = sysimage.ups_row(ups.Reading(), panel.DEFAULT_THRESHOLDS)
    assert row.text == "no data" and row.marker is None
    assert sysimage._ups_shutdown(row, {"mode": "unknown"}) == row
    configured = sysimage.ups_row(ups.Reading(), panel.DEFAULT_THRESHOLDS, {"mode": "batt_level", "value": 40})
    assert configured.text == "no data (off at 40%)" and configured.fill is None
    assert configured.issues == row.issues and sysimage.render([configured], now=123).startswith(b"\x89PNG")


def test_worst_case_shutdown_caption_grows_the_canvas_and_keeps_the_normal_bar_and_font():
    reading = ups.Reading(("OB", "LB", "RB"), 100, 99 * 3600 + 59 * 60)
    row = sysimage.ups_row(reading, panel.DEFAULT_THRESHOLDS, {"mode": "sec_timer", "value": 99 * 86400 + 23 * 3600})
    _, head, small = (font.font_variant(size=font.size * sysimage.K) for font in sysimage._fonts())
    layout = sysimage.layout([row], head, small)
    png = Image.open(io.BytesIO(sysimage.render([row], now=123)))
    assert layout.right_edges[0] <= png.width - 24 and png.width > 800
    assert layout.bx1 - layout.bx0 == 250 and small.size == 20 * sysimage.K
    assert "(off after 99d 23h on battery)" in row.text


def test_bot_reads_shutdown_settings_with_tasks_and_never_reuses_a_failed_optional_read(tmp_path, monkeypatch, caplog):
    settings = helperclient.Settings("/run/helper.sock", "TEST_HELPER_KEY")
    b = make_bot(tmp_path, helper=settings, ups="unit@nut")
    b._read_tasks = AsyncMock(return_value=None)
    b._ups = ups.Reading(("OL",), 95, 3600)
    ask = AsyncMock(return_value={"mode": "batt_level", "value": 40})
    monkeypatch.setattr(helperclient, "request", ask)
    asyncio.run(b.collect())
    ask.assert_awaited_once_with(settings, "ups_shutdown")
    assert "(off at 40%)" in next(row.text for row in b._system if row.label == "UPS")
    ask.side_effect = helperclient.Error("secret details")
    asyncio.run(b.collect())
    assert "off at" not in next(row.text for row in b._system if row.label == "UPS")
    assert "secret details" not in caplog.text
    b.cfg.helper = None
    assert asyncio.run(b._read_ups_shutdown()) == {}
    b.cfg.helper, b.cfg.ups = settings, ""
    assert asyncio.run(b._read_ups_shutdown()) == {}
