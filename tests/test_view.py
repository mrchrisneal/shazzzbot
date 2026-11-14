import io
from unittest.mock import Mock

import pytest
from PIL import Image

from shazzzbot import charts, panel, renderbudget, view
from shazzzbot.glyphs import GREEN
from shazzzbot.panel import CRITICAL, FINE, WARNING, Part, Section

ELL = chr(0x2026)


def test_image_issue_wrapping_keeps_words_whole_and_bounds_unbroken_names(monkeypatch):
    monkeypatch.setattr(view, "W", 84)
    monkeypatch.setattr(view, "K", 1)
    font = Mock(getlength=len)
    part = Part([(WARNING, ("aa bb timeout cc",)), (CRITICAL, ("x" * 22,))])
    wrapped = view._wrap(part, font)
    assert wrapped.rows == [(WARNING, ("aa bb ",)), (panel.CONT, ("timeout cc",)),
                            (CRITICAL, ("x" * 10,)), (panel.CONT, ("x" * 10,)), (panel.CONT, ("xx",))]


def section(n: int = 3) -> Section:
    rows = [(FINE, ("Name", "Size")), (CRITICAL, ("a_very_long_name_" * 4, "1G"))] + [
        (FINE, (f"n{i}", "-")) for i in range(n)]
    return Section("k", "Title", [Part(rows, "Table", "lr", heads=True), Part([(WARNING, ("loose",))], cut=None),
                                  Part([(FINE, ("Only", "head"))], heads=True)], "a note", "foot", stamp=True)


def test_text_rich_and_image_share_one_section(monkeypatch):
    s = section()
    t = view.text(s)
    assert t.title == "Title" and t.footer.text == "foot" and t.timestamp is not None
    assert t.description.startswith("a note\n**Table**\n```") and ELL in t.description and "Only" not in t.description
    r = view.rich(s)
    assert r.description == f"a note\n\n{panel.SLOT[WARNING]} loose" and r.fields[0].name == "a_very_long_name_" * 4
    assert r.fields[0].value == f"{panel.CRIT} Size: 1G\n\u00a0"
    assert r.fields[1].name == "n0" and r.fields[1].value == "Size: -\n\u00a0"
    assert all(f.inline for f in r.fields)
    i = view.embed(s, "image")
    assert i.image.url == "attachment://k.png" and i.description == "a note" and not i.fields
    assert view.embed(s, "text").description == t.description and view.embed(s, "embed").fields
    drawn = []
    original = view.ImageDraw.ImageDraw.text
    def capture(draw, xy, text, **kwargs):
        drawn.append((xy, text, kwargs))
        return original(draw, xy, text, **kwargs)
    monkeypatch.setattr(view.ImageDraw.ImageDraw, "text", capture)
    assert view.image(s).startswith(b"\x89PNG")
    xy, _, style = next(row for row in drawn if row[1] == s.note)
    title = next(row for row in drawn if row[1] == s.title)
    heading = next(row for row in drawn if row[1] == "Table")
    assert title[0][1] < xy[1] < heading[0][1]
    assert style["fill"] == charts.DIM and style["font"].size < title[2]["font"].size


def test_rich_cuts_a_long_field():
    v = view.rich(section(200)).fields[0].value
    assert len(v) <= 1024 and v.endswith(" more")


def test_image_dot_can_mark_the_value():
    rows = [(WARNING, ("Stacks", "3 of 4 up")), (CRITICAL, ("gone", "-")), (FINE, ("Up", "1"))]
    for align in ("lr", "ll"):
        assert view.image(Section("d", "D", [Part(rows, align=align, on_value=True)])).startswith(b"\x89PNG")


def test_long_lines_keep_their_start_and_images_cap_rows():
    v = view.rich(Section("a", "A", [Part([(CRITICAL, ("x" * 2000,))], "Alerts", cut=None)])).fields[0].value
    assert "x" * 900 in v and v.endswith(ELL) and len(v) <= 1024
    big = Section("d", "D", [Part([(FINE, (str(i), "1")) for i in range(500)])])
    assert view._cap(big.parts[0]).rows[-1][1] == ("...and 461 more", "")
    assert view.image(big).startswith(b"\x89PNG")


def test_rich_rows_use_the_ok_glyph_set_at_startup(monkeypatch):
    monkeypatch.setattr(panel, "SLOT", panel.SLOT)
    panel.use_ok_glyph(GREEN)
    assert view.rich(section()).fields[1].value == f"{GREEN} Size: -\n\u00a0"


def test_image_fits_right_aligned_columns_and_frames_the_crop():
    import io

    from PIL import Image

    from shazzzbot.charts import BG, PANEL
    rows = [(0, ("Service", "Local", "Public")), (0, ("A long service name to set width", "5ms", "103ms")),
            (2, ("Auth", "-", "refused"))]
    p = Part(rows, align="lrr", heads=True, marks=[(), (), (0, 0, 2)], icons=["", "run", "stop"])
    im = Image.open(io.BytesIO(view.image(Section("s", "S", [p], footer="", issues=())))).convert("RGB")
    w, h = im.size
    assert view.MIN_W <= w < view.W
    ink = [x for x in range(w) for y in range(60, h) if sum(im.getpixel((x, y))) > 300]  # text, not box edges
    assert w - 60 < max(ink) < w - 16  # the right-aligned column ends at the panel's padding
    assert im.getpixel((1, 1)) == BG and im.getpixel((12, h // 2)) == PANEL


def test_service_state_icons_draw_in_their_colours():
    import io

    from PIL import Image

    from shazzzbot.charts import GREEN, RED, YELLOW
    rows = [(0, ("Service", "Local"))] + [(0, (k, "1ms")) for k in ("run", "pause", "stop", "warn")]
    p = Part(rows, align="lr", heads=True, icons=["", "run", "pause", "stop", "warn"])
    im = Image.open(io.BytesIO(view.image(Section("s", "S", [p])))).convert("RGB")
    gutter = {im.getpixel((x, y)) for x in range(24, 24 + view.ICON_W) for y in range(im.size[1])}
    assert {GREEN, RED, YELLOW} <= gutter


def test_rich_grid_fields_keep_marks_qualifiers_and_mobile_spacing():
    rows = [(WARNING, ("CPU", "95%  (iowait 3%)")), (FINE, ("Empty", ""))]
    s = Section("s", "System", [Part(rows, grid=True, marks=[(CRITICAL, FINE)])])
    fields = view.rich(s).fields
    assert fields[0].name == "CPU" and fields[0].inline
    assert fields[0].value == f"95% {panel.CRIT}\n(iowait 3%)\n\u00a0"
    assert fields[1].value == "-\n\u00a0"
    fallback = view.rich(s._replace(parts=[s.parts[0]._replace(marks=())])).fields[0]
    assert fallback.value == f"95% {panel.WARN}\n(iowait 3%)\n\u00a0"


def test_rich_grid_dots_follow_values_except_alerts_and_tasks():
    rows = [(WARNING, ("Disk I/O", "R 1 MB/s  W 2 MB/s  (98% busiest)")),
            (CRITICAL, ("Alerts", "9 critical  11 warnings")),
            (WARNING, ("Tasks", "2 tasks running:  my_sync  Mover"))]
    marks = [(FINE, FINE, WARNING), (CRITICAL, WARNING), (FINE, WARNING, FINE)]
    s = Section("s", "System", [Part(rows, grid=True, marks=marks)])
    values = [f.value for f in view.rich(s).fields]
    assert values[0] == f"R 1 MB/s\nW 2 MB/s\n(98% busiest) {panel.WARN}\n\u00a0"
    assert values[1] == f"{panel.CRIT} 9 critical\n{panel.WARN} 11 warnings\n\u00a0"
    assert values[2] == f"2 tasks running:\n{panel.WARN} my\\_sync\nMover\n\u00a0"


def test_rich_grid_fallback_keeps_per_value_dots():
    rows = [(CRITICAL, ("Alerts", "9 critical  11 warnings"))] + [(FINE, (f"Row{i}", "x" * 300)) for i in range(30)]
    s = Section("s", "System", [Part(rows, "System", grid=True, marks=[(CRITICAL, WARNING)])])
    e = view.rich(s)
    text = (e.description or "") + "".join(f.value for f in e.fields)
    g = view.GAP2
    assert f"**Alerts:**{g}{panel.CRIT} 9 critical{g}{panel.WARN} 11 warnings" in text


def test_rich_escapes_markdown_in_data():
    s = Section("d", "Disks", [Part([(FINE, ("Disk",))], cut=None)], issues=[(CRITICAL, "disk2 DISK_INVALID")])
    e = view.rich(s)
    text = (e.description or "") + "".join(f.value for f in e.fields)
    assert "DISK\\_INVALID" in text
    assert view._md("[x](https://e.org) `a`") == "\\[x\\](https://e.org) \\`a\\`"


def test_multiline_issues_become_continuation_rows():
    s = Section("d", "Docker", [], issues=[(CRITICAL, "media degraded:\nkavita exited (1)\nphotoprism exited (137)")])
    rows = view.parts(s)[-1].rows
    assert rows == [(CRITICAL, ("media degraded:",)), (panel.CONT, ("kavita exited (1)",)),
                    (panel.CONT, ("photoprism exited (137)",))]
    assert "photoprism exited (137)" in (view.text(s).description or "")


def test_rich_table_grid_marks_values_and_skips_empty_cells():
    p = Part([(FINE, ("Disk", "Temp", "Free")), (WARNING, ("disk1", "31C", "72G")),
              (FINE, ("disk2", "", "-"))], heads=True, marks=[(), (FINE, FINE, CRITICAL)])
    fields = view.rich(Section("d", "Disks", [p])).fields
    assert fields[0].name == f"{panel.CRIT} disk1" and fields[0].inline
    assert fields[0].value == f"```ansi\nTemp: 31C\nFree: \x1b[1;31m72G\x1b[0m {panel.CRIT}\n```"
    assert fields[1].value == "Free: -\n\u00a0"


def test_rich_tables_reserve_space_for_issues_and_fall_back_as_a_whole():
    p = Part([(FINE, ("Disk", "Free"))] + [(FINE, (f"disk{i}", "72G")) for i in range(25)],
             "Disks", heads=True)
    e = view.rich(Section("d", "D", [p], issues=[(WARNING, "disk issue")]))
    assert [f.name for f in e.fields] == ["Disks", "Issues & Alerts"]
    assert not any(f.inline for f in e.fields)
    assert "**disk0**" in e.fields[0].value
    e = view.rich(Section("d", "D", [p._replace(rows=p.rows[:-1])], issues=[(WARNING, "disk issue")]))
    assert len(e.fields) == 25 and e.fields[0].inline and e.fields[-1].name == "Issues & Alerts"


def test_rich_limits_values_names_descriptions_and_total_size():
    p = Part([(FINE, ("x" * 300, "v" * 2000)) for _ in range(6)], "Grid", grid=True)
    e = view.rich(Section("s", "T", [p], note="n" * 3000))
    assert len(e) <= 6000 and not e.fields[0].inline
    assert len(e.description) <= 4096 and len(e.fields[0].value) <= 1024
    e = view.rich(Section("s", "T", [p._replace(rows=p.rows[:1])]))
    assert len(e.fields[0].name) == 256 and len(e.fields[0].value) <= 1024
    assert e.fields[0].value.endswith("\n\u00a0")
    parts = [Part([(FINE, ("v" * 2000,))], "n" * 300) for _ in range(30)]
    e = view.rich(Section("s", "t" * 1000, parts, "d" * 10000, "f" * 3000))
    assert len(e.fields) == 25 and len(e) <= 6000
    assert len(e.title) == 256 and len(e.footer.text) == 2048
    assert all(len(f.name) <= 256 and len(f.value) <= 1024 for f in e.fields)


def test_rich_table_without_values_uses_flat_fallback():
    p = Part([(FINE, ("Disk",)), (WARNING, ("disk1",))], heads=True)
    e = view.rich(Section("d", "D", [p]))
    assert not e.fields and "disk1" in e.description


def test_disk_read_errors_do_not_mark_healthy_temperature_critical():
    from shazzzbot import unraid

    disk = unraid.Disk("disk1", "Data", "sdb", "DISK_OK", 31, 100, 50, 12, True)
    array = unraid.Array("tower", "7", "STARTED", False,
                         unraid.Parity("", False, False, 0, 0, 0, 0, 0, 0), [disk])
    embed = view.rich(panel.array_section(array))
    field = next(f for f in embed.fields if f.name.endswith("disk1"))
    assert field.value.startswith("```ansi\nTemp:   31C\n") and "Errors: \x1b[1;31m12" in field.value
    assert "disk1 12 read/write errors" in embed.fields[-1].value


def test_image_draws_placeholder_cells_at_half_strength():
    import io

    from PIL import Image

    from shazzzbot.charts import PANEL, TEXT
    assert view.FAINT == tuple((t + b) // 2 for t, b in zip(TEXT, PANEL))
    rows = [(FINE, ("Service", "Local")), (FINE, ("Kavita", "N/A"))]
    im = Image.open(io.BytesIO(view.image(Section("s", "S", [Part(rows, align="lr", heads=True)])))).convert("RGB")
    assert view.FAINT in {im.getpixel((x, y)) for x in range(im.size[0]) for y in range(im.size[1])}


def test_stacks_render_with_a_leading_status_icon():
    from shazzzbot.docker import Container

    cs = [Container("1", "a", "running", "Up", "apps_stack"), Container("2", "b", "exited", "Exited (1)", "apps_stack")]
    field = [f for f in view.rich(panel.docker_section(cs, None)).fields if f.name == "Stacks"][-1]
    assert field.value.startswith(f"{panel.CRIT} **apps\\_stack:** Up: ")
    assert "`" not in field.value and "\x1b" not in field.value
    assert field.value.split("Up: ")[1].split()[0] == "1/2"


def test_stack_draws_coloured_note_spans():
    import io

    from PIL import Image as I

    from shazzzbot import charts
    png = view.image(Section("d", "D", [Part([(FINE, ("x",))])]))
    out = I.open(io.BytesIO(view.stack([png], [("repo v1", charts.DIM), (" (update available)", charts.GREEN)])))
    assert any(px[:3] == charts.GREEN for px in out.crop((0, out.height - 30, out.width, out.height)).getdata())


def test_rich_table_hides_not_applicable_checks():
    p = Part([(FINE, ("Service", "Status", "Local", "VPN")), (FINE, ("Kavita", "ok", "3ms", "N/A"))], heads=True)
    assert "N/A" not in view.rich(Section("s", "S", [p])).fields[0].value


def test_renamed_apps_are_italic_not_starred():
    from shazzzbot.host import Snapshot
    v = view.rich(panel.apps_section(Snapshot(top_cpu=[("*Docker shims", 0.3)]))).fields[0].value
    assert "**_Docker shims:_**" in v and "*D" not in v


def test_rich_heading_after_another_part_starts_its_own_row():
    first = Part([(FINE, ("CPU", "5%"))], grid=True)
    second = Part([(FINE, ("Disk", "Free")), (FINE, ("disk1", "72G"))], "Disks", heads=True)
    names = [f.name for f in view.rich(Section("s", "S", [first, second])).fields]
    assert names[names.index("Disks") + 1] == "disk1"


def test_wide_tables_widen_the_image_instead_of_cutting_names():
    import io

    from PIL import Image

    heads = ("Disk", "Temp", "Used", "Free", "Read", "Write", "Busy", "Errors", "SMART")
    row = ("cache2", "38C", "63%", "170.9GB", "132KB/s", "671KB/s", "2%", "0", "3 warnings")
    p = Part([(FINE, heads), (FINE, row)], align="l" + "r" * 8, heads=True)
    s = Section("d", "D", [p])
    w = Image.open(io.BytesIO(view.image(s))).size[0]
    assert view.W < w
    x0, widths, gap = view._layout(p, view._fonts()[2].font_variant(size=view._fonts()[2].size * view.K))
    assert x0 + sum(widths) + gap * 8 + 24 <= w
    huge = p._replace(rows=[p.rows[0], (FINE, ("x" * 200, *row[1:]))])
    huge_width = Image.open(io.BytesIO(view.image(s._replace(parts=[huge])))).size[0]
    assert huge_width > w and huge_width > 1000


def test_issues_put_errors_first_and_summarise_the_rest():
    def body(issues):
        return view.rich(Section("s", "S", [Part([(FINE, ("CPU", "5%"))], grid=True)], issues=issues)).fields[-1].value
    v = body([(WARNING, f"w{i}") for i in range(9)] + [(CRITICAL, "e1"), (CRITICAL, "e2")])
    assert v.splitlines()[:3] == [f"{panel.CRIT} e1", f"{panel.CRIT} e2", f"{panel.WARN} w0"]
    assert v.splitlines()[-1] == "+ _3 additional warnings not visible_"
    v = body([(CRITICAL, f"e{i}") for i in range(8)] + [(WARNING, "w"), (CRITICAL, "e8")])
    assert v.splitlines()[-1] == "+ _1 additional warning and 1 error not visible_"
    v = body([(CRITICAL, f"e{i}") for i in range(10)])
    assert v.splitlines()[-1] == "+ _2 additional errors not visible_"
    s = Section("s", "S", [Part([(FINE, ("CPU", "5%"))], grid=True)], issues=[(WARNING, f"w{i}") for i in range(10)])
    assert "+ 2 additional warnings not visible" in view.text(s).description
    assert view.image(s).startswith(b"\x89PNG") and len(body([(WARNING, "w")] * 8).splitlines()) == 8


def test_text_grid_marks_colour_value_pieces_not_labels():
    p = Part([(CRITICAL, ("Alerts", "2 critical  1 warning")), (FINE, ("OS", "Unraid"))], cut=None, grid=True,
             marks=[(CRITICAL, WARNING)])
    d = view.text(Section("s", "S", [p])).description
    assert "Alerts  \x1b[1;31m2 critical\x1b[0m  \x1b[1;33m1 warning\x1b[0m" in d and "OS      Unraid" in d


@pytest.mark.parametrize("icon", ["unknown", "../github", "/tmp/github"])
@pytest.mark.parametrize("note", ["", "repo"])
def test_stack_rejects_unknown_icons_before_opening_any_image(monkeypatch, icon, note):
    png = view.image(Section("d", "D", [Part([(FINE, ("x",))])]))
    opened = Mock()
    monkeypatch.setattr(view.Image, "open", opened)
    with pytest.raises(KeyError) as error:
        view.stack([png], note, icon=icon)
    assert error.value.args == (icon,)
    opened.assert_not_called()


def test_stack_leads_the_note_with_a_tinted_icon():
    import io

    from PIL import Image as I

    from shazzzbot import charts
    png = view.image(Section("d", "D", [Part([(FINE, ("x" * 60,))])]))
    plain = I.open(io.BytesIO(view.stack([png], [("repo v1", charts.DIM)]))).convert("RGB")
    marked = I.open(io.BytesIO(view.stack([png], [("repo v1", charts.DIM)], icon="github"))).convert("RGB")
    strip = (0, plain.height - 30, plain.width, plain.height)
    assert list(marked.crop(strip).getdata()) != list(plain.crop(strip).getdata())
    assert sum(px == charts.DIM for px in marked.crop(strip).getdata()) > sum(
        px == charts.DIM for px in plain.crop(strip).getdata())


def test_stack_shrinks_a_long_note_to_fit_inside_the_padding():
    import io

    from PIL import Image as I

    from shazzzbot import charts
    png = view.image(Section("d", "D", [Part([(FINE, ("x",))])]))
    out = I.open(io.BytesIO(view.stack([png], [("r" * 120, charts.DIM)], icon="github"))).convert("RGB")
    ink = [x for x in range(out.width) for y in range(out.height - 30, out.height) if sum(out.getpixel((x, y))) > 200]
    assert min(ink) >= view.FOOT_PAD - 2 and out.width - 1 - max(ink) >= view.FOOT_PAD - 2


def test_text_stacks_show_all_four_status_icons():
    from shazzzbot.glyphs import PLAY, STOP
    from tests.test_docker import stack_cases

    text = view.text(panel.docker_section(stack_cases(), None)).description
    assert f"\x1b[32m{PLAY}\x1b[0m  healthy" in text
    assert any("warning" in line and line.startswith(panel.WARN + " ") for line in text.splitlines())
    assert any("critical" in line and line.startswith(panel.CRIT + " ") for line in text.splitlines())
    assert f"{STOP}  off" in text and f"\x1b[30m{STOP}" not in text


def test_rich_stacks_show_all_four_status_icons():
    from shazzzbot.glyphs import PLAY, STOP
    from tests.test_docker import stack_cases

    fields = view.rich(panel.docker_section(stack_cases(), None)).fields
    matching = [field for field in fields if field.name == "Stacks"]
    assert len(matching) == 2
    summary, stacks = matching
    assert summary.inline and not stacks.inline
    assert summary.value == f"1 of 4 stacks up {panel.CRIT}\n\u00a0"
    text = stacks.value
    assert "`" not in text and "\x1b" not in text
    assert f"{PLAY} **healthy:**" in text
    assert f"{panel.WARN} **warning:**" in text
    assert f"{panel.CRIT} **critical:**" in text
    assert f"{STOP} **off:**" in text


@pytest.mark.parametrize("heads", [False, True])
def test_rich_blocks_without_icons_are_plain_and_keep_every_value(heads):
    rows = [(WARNING, ("legacy", "1/2"))]
    if heads:
        rows.insert(0, (FINE, ("Stack", "Up")))
    part = Part(rows, heading="Stacks", heads=heads, block=True)
    rich = view.rich(Section("s", "S", [part]))
    assert rich.fields[0].name == "Stacks"
    assert rich.fields[0].value.startswith(f"{panel.WARN} **legacy:** ")
    assert "1/2" in rich.fields[0].value and "`" not in rich.fields[0].value


def test_image_stacks_show_all_four_status_icons_and_muted_stop():
    import io
    from unittest.mock import patch

    from PIL import Image

    from shazzzbot.charts import DIM, GREEN, RED, YELLOW
    from tests.test_docker import stack_cases

    with patch.object(view, "_icon", wraps=view._icon) as icons:
        png = view.image(panel.docker_section(stack_cases(), None))
    assert [(call.args[1], call.kwargs["muted"]) for call in icons.call_args_list] == [
        ("dot2", False), ("dot1", False), ("run", False), ("stop", True)]
    image = Image.open(io.BytesIO(png)).convert("RGB")
    gutter = {image.getpixel((x, y)) for x in range(24, 24 + view.ICON_W) for y in range(image.height)}
    assert {GREEN, YELLOW, RED, DIM} <= gutter


@pytest.mark.parametrize("healthy", [False, True])
def test_system_text_splits_disk_net_and_load_values_without_changing_other_views(monkeypatch, healthy):
    import re
    import unicodedata

    from shazzzbot import sysimage
    from shazzzbot.host import Snapshot

    monkeypatch.setattr(sysimage, "CORES", 1)
    snapshot = Snapshot(uptime_s=86400, load=(0.2, 0.3, 0.4) if healthy else (0.2, 0.8, 1.2),
                        disk_read_mbs=97 / 1024, disk_write_mbs=3.2, disk_util_pct=3 if healthy else 95,
                        net_rx_mbps=0.012, net_tx_mbps=1.2, net_speed_mbps=1000 if healthy else 1)
    table = sysimage.rows(snapshot, panel.DEFAULT_THRESHOLDS)
    section = sysimage.section("System", "", "soon", table, None)._replace(stamp=False)
    original = section.parts[0]
    rows = [(lv, (label, value + "  (per core)" if label == "Load" else value))
            for lv, (label, value) in original.rows]
    section = section._replace(parts=[original._replace(rows=rows)])
    rich = view.rich(section).to_dict()
    image = sysimage.render(table, now=123.0)
    text = view.text(section).description
    lines = [line for line in text.splitlines() if not line.startswith(("```", "**"))]
    plain = [re.sub("\x1b\\[[0-9;]*m", "", line) for line in lines]
    for label, tokens in (("Disk I/O", ("R ", "W ", "busiest")),
                          ("Net I/O", (sysimage.DOWN, sysimage.UP, "of link")),
                          ("Load", ("0.20", "0.30" if healthy else "0.80", "0.40" if healthy else "1.20", "per core"))):
        start = next(i for i, line in enumerate(plain) if label in line)
        assert all(token in plain[start + j] for j, token in enumerate(tokens))
        assert all(label not in plain[start + j] for j in range(1, len(tokens)))
        columns = [sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in
                       plain[start + j][:plain[start + j].index(token)])
                   for j, token in enumerate(tokens[:2])]
        assert columns[0] == columns[1]
        if not healthy:
            assert ("\x1b[1;33m" if label == "Disk I/O" else "\x1b[1;31m") in lines[start + 2]
    load = next(i for i, line in enumerate(plain) if "Load" in line)
    if not healthy:
        assert "\x1b[1;33m" in lines[load + 1]
    expanded = view._system_text(section.parts[0]).rows
    for i, (_, cells) in enumerate(expanded):
        if cells[0] in {"Disk I/O", "Net I/O", "Load"}:
            assert expanded[i + 1][0] == expanded[i + 2][0] == panel.CONT
            assert expanded[i + 1][1][0] == expanded[i + 2][1][0] == ""
    assert section.parts[0].rows == rows
    assert view.rich(section).to_dict() == rich
    assert sysimage.render(table, now=123.0) == image


def test_system_text_split_does_not_change_other_cards_or_non_grid_parts():
    value = "R 1 MB/s  W 2 MB/s  (0% busiest)"
    part = Part([(FINE, ("Disk I/O", value))], grid=True)
    assert value in view.text(Section("other", "Other", [part])).description
    assert value in view.text(Section("system", "System", [part._replace(grid=False)])).description


def app_trends():
    from shazzzbot.apphistory import AppHistory
    from shazzzbot.host import Snapshot

    history = AppHistory(60)
    for t, value in ((0, 0), (120, 20), (300, 5)):
        snapshot = Snapshot(top_cpu=[("known", value)], top_ram=[("known", value * 10)])
        if t == 300:
            snapshot.top_cpu.append(("new", 1))
            snapshot.top_ram.append(("new", 16))
        history.add(snapshot, 300, t)
    return panel.apps_section(snapshot, history, 300, now=300)


def test_apps_text_has_right_aligned_usage_and_one_sample_app_trends():
    from shazzzbot.glyphs import NO_DATA
    from shazzzbot.spark import TREND_WIDTH

    section = app_trends()
    text = view.text(section).description
    assert section.parts[0].rows[0][1][-1] in text
    assert section.parts[1].rows[0][1][-1] in text
    assert all(len(line) <= panel.WIDTH + 3 for line in text.splitlines())
    assert all(line.endswith(NO_DATA * (TREND_WIDTH - 1) + chr(0x28c0)) for line in text.splitlines() if "new" in line)
    assert "5.0%" in text and "50MB" in text
    cpu = [line for line in text.splitlines() if "%" in line and "**" not in line]
    assert cpu[0].index("%") == cpu[1].index("%")


def test_apps_rich_keeps_usage_then_inline_braille_and_no_fake_trends():
    from shazzzbot.glyphs import NO_DATA
    from shazzzbot.spark import TREND_WIDTH

    section = app_trends()
    fields = view.rich(section).fields
    for field, part in zip(fields, section.parts):
        known, new = field.value.splitlines()
        trend = part.rows[0][1][-1]
        assert known.startswith(f"`{trend}`" + view.GAP2) and "**known:**" in known
        assert "**new:**" in new and new.startswith("`" + NO_DATA * (TREND_WIDTH - 1) + chr(0x28c0) + "`" + view.GAP2)
    assert "5.0%" in fields[0].value and "50MB" in fields[1].value


def test_rich_keeps_legacy_ranks_and_does_not_treat_other_four_cell_rows_as_trends():
    legacy = Part([(FINE, ("1", "app", "5%"))], heading="Apps")
    assert view.rich(Section("apps", "Apps", [legacy])).fields[0].value == "**app:**\u00a0 5%"
    other = Part([(FINE, ("1", "name", "5%", "detail"))], heading="Other")
    value = view.rich(Section("other", "Other", [other])).fields[0].value
    assert "detail" in value and "`" not in value and "**name:**" not in value


def test_apps_image_uses_the_shared_chart_plot_only_for_apps_with_history():
    from unittest.mock import patch

    section = app_trends()
    with patch.object(view.charts, "plot", wraps=view.charts.plot) as plot:
        png = view.image(section, now=300)
    assert png.startswith(b"\x89PNG") and plot.call_count == 4
    for call, trend in zip(plot.call_args_list, [t for part in section.parts for t in part.trends]):
        assert call.args[2] == trend.samples
        assert call.args[4:6] == (0, 300)
        assert call.args[7] == trend.colour


def test_zero_app_usage_draws_a_floor_trend_from_its_first_sample():
    from unittest.mock import patch

    from shazzzbot.apphistory import AppHistory
    from shazzzbot.glyphs import NO_DATA
    from shazzzbot.host import Snapshot

    snapshot = Snapshot(top_cpu=[("idle", 0)])
    history = AppHistory(60)
    history.add(snapshot, 300, 0)
    assert len(panel.apps_section(snapshot, history, 300, now=0).parts[0].trends[0].samples) == 1
    history.add(snapshot, 300, 60)
    pending = panel.apps_section(snapshot, history, 300, now=60).parts[0]
    assert len(pending.trends[0].samples) == 2
    assert pending.rows[0][1][-1] == NO_DATA * 12 + chr(0x28c0) + NO_DATA + chr(0x28c0)
    history.add(snapshot, 300, 120)
    section = panel.apps_section(snapshot, history, 300, now=120)
    assert any(c != NO_DATA for c in section.parts[0].rows[0][1][-1])
    assert "0.0%" in view.text(section).description and "0.0%" in view.rich(section).fields[0].value
    with patch.object(view.charts, "plot", wraps=view.charts.plot) as plot:
        assert view.image(section, now=120).startswith(b"\x89PNG")
    assert plot.call_count == 1 and plot.call_args.args[6] is None


def test_trend_metadata_is_capped_with_rows_and_missing_entries_stay_blank():
    from unittest.mock import patch

    trend = panel.Trend(((0, 0), (100, 5)), 0, 100, 30, view.charts.BLUE)
    part = Part([(FINE, (str(i), "app", "5%", "")) for i in range(50)], align="llrl", cut=1, trends=[trend] * 50)
    capped = view._cap(part)
    assert len(capped.trends) == view.MAX_ROWS and capped.trends[-1] is None
    short = part._replace(rows=part.rows[:2], trends=[trend])
    with patch.object(view.charts, "plot", wraps=view.charts.plot) as plot:
        assert view.image(Section("apps", "Apps", [short])).startswith(b"\x89PNG")
    assert plot.call_count == 1


def test_legacy_flat_zero_trends_use_the_floor_without_dividing_by_zero():
    from unittest.mock import patch

    trend = panel.Trend(((0, 0), (100, 0)), 0, 100, 30, view.charts.BLUE)
    part = Part([(FINE, ("1", "idle", "0%", ""))], align="llrl", trends=[trend])
    with patch.object(view.charts, "plot", wraps=view.charts.plot) as plot:
        assert view.image(Section("apps", "Apps", [part]), now=100).startswith(b"\x89PNG")
    assert plot.call_args.args[6] is None


def test_stacked_sections_and_nested_footer_redraw_every_frame_at_the_widest_width(monkeypatch):
    from shazzzbot import sysimage
    from shazzzbot.host import Snapshot

    table = sysimage.rows(Snapshot(disk_util_pct=100, disk_read_mbs=999.9, disk_write_mbs=999.9),
                          panel.DEFAULT_THRESHOLDS)
    system = sysimage.render(table, now=123, status=(panel.WARNING, "2 nodes ok (~999ms)  (update available!)"))
    narrow = view.image(panel.Section("tasks", "Tasks", [panel.Part([(panel.FINE, ("Short task",))], "Running")]),
                        now=123)
    history = charts.render([(123, 100, 100, 999.9, 999.9, 999.9, 999.9)], 300, now=123)
    bars = charts.disk_bars([(f"disk{i}", 99, 40, 90, 95) for i in range(30)])
    assert bars is not None
    sources = [system, bars, narrow, history]
    natural = [Image.open(io.BytesIO(p)) for p in sources]
    assert len({im.width for im in natural}) > 1
    expected = max(im.width for im in natural)
    widths = []
    original = view.frame
    def frame(draw, spans, width):
        widths.append(width)
        return original(draw, spans, width)
    monkeypatch.setattr(view, "frame", frame)
    inner = view.stack(sources)
    output = Image.open(io.BytesIO(view.stack([inner], "Footer " + "W" * 130)))
    assert output.width > expected and all(w in (expected, output.width) for w in widths)
    assert output.height >= sum(im.height for im in natural) + 30
    rows = [y for y in range(3, output.height - 33)
            if all(output.getpixel((12, y + dy)) == charts.PANEL for dy in range(-3, 4))]
    assert rows
    for y in rows:
        assert output.getpixel((output.width - 12, y)) == charts.PANEL
        assert output.getpixel((output.width - 2, y)) == charts.BG
    assert output.width in widths
    _, head, small = (f.font_variant(size=f.size * sysimage.K) for f in charts._fonts())
    layout = sysimage.layout(table, head, small)
    assert layout.bx1 - layout.bx0 == 250


def test_generated_images_keep_their_natural_width_when_a_stack_needs_no_expansion():
    section = panel.Section("short", "Short", [panel.Part([(panel.FINE, ("row",))])])
    png = view.image(section, now=123)
    image = Image.open(io.BytesIO(png))
    assert Image.open(io.BytesIO(view.stack([png]))).width == image.width
    assert Image.open(io.BytesIO(png.draw(image.width))).size == image.size


def test_image_notes_reserve_full_width_and_space_even_without_body_rows(monkeypatch):
    notes = ["W" * 100, "Second line"]
    section = Section("note", "Note", [], note="\n".join(notes))
    drawn = []
    original = view.ImageDraw.ImageDraw.text
    def capture(draw, xy, text, **kwargs):
        drawn.append((xy, text, kwargs))
        return original(draw, xy, text, **kwargs)
    monkeypatch.setattr(view.ImageDraw.ImageDraw, "text", capture)
    png = Image.open(io.BytesIO(view.image(section, now=123)))
    assert png.height == view.TOP + view.ROW_H * 3 + 6
    for xy, text, style in (row for row in drawn if row[1] in notes):
        assert (xy[0] + style["font"].getbbox(text, anchor="lm")[2]) / view.K <= png.width - 24
        assert xy[1] / view.K < png.height - 6


def test_section_issue_limits_share_severity_order_and_preserve_the_full_feed():
    issues = [(WARNING, f"warning-{i}") for i in range(6)] + [(CRITICAL, f"error-{i}") for i in range(3)]
    primary = Section("system", "System", [], issues=issues)
    forge = Section("forge_0", "Forgejo", [], issues=issues, issues_limit=5)
    shown = view.parts(forge)[0].rows
    expected = [(lv, (text,)) for lv, text in [*issues[6:], *issues[:2]]]
    assert shown == [*expected, (panel.MORE, ("4 additional warnings not visible",))]
    assert view.parts(primary)[0].rows == [*expected, *((lv, (text,)) for lv, text in issues[2:5]),
                                         (panel.MORE, ("1 additional warning not visible",))]
    assert primary.issues is forge.issues is issues and len(forge.issues) == 9
    assert view._shown(issues[:5], 5) == [issues[i] for i in range(5)]
    omitted_errors = view._shown([(CRITICAL, f"e{i}") for i in range(7)] + issues[:2], 5)
    assert omitted_errors[-1] == (panel.MORE, "2 additional warnings and 2 errors not visible")
    for renderer in (view.text, view.rich):
        output = str(renderer(forge).to_dict())
        assert "4 additional warnings not visible" in output
        assert "error-0" in output and "error-2" in output and "warning-1" in output and "warning-2" not in output
        assert "1 additional warning not visible" in str(renderer(primary).to_dict())


def test_image_wrapped_issues_draw_one_dot_and_keep_every_text_line_aligned(monkeypatch):
    issues = [(CRITICAL, "Critical issue " + "long detailed reason " * 18),
              (WARNING, "Warning issue " + "unbroken-name" * 28)]
    section = Section("forge_0", "Forgejo", [], issues=issues, issues_limit=5)
    font = view._fonts()[2].font_variant(size=view._fonts()[2].size * view.K)
    wrapped = view._wrap(view.parts(section)[0], font)
    parents = [(i, lv) for i, (lv, _) in enumerate(wrapped.rows) if lv > FINE]
    assert [lv for _, lv in parents] == [CRITICAL, WARNING]
    assert len(wrapped.rows) > 6 and all(lv == panel.CONT for i, (lv, _) in enumerate(wrapped.rows)
                                       if i not in {n for n, _ in parents})
    dots, texts = [], []
    content = {cells[0] for _, cells in wrapped.rows}
    ellipse, text = view.ImageDraw.ImageDraw.ellipse, view.ImageDraw.ImageDraw.text

    def draw_dot(draw, box, **kwargs):
        dots.append((box, kwargs["fill"]))
        return ellipse(draw, box, **kwargs)

    def draw_text(draw, xy, value, **kwargs):
        if value in content:
            texts.append((xy, value))
        return text(draw, xy, value, **kwargs)

    monkeypatch.setattr(view.ImageDraw.ImageDraw, "ellipse", draw_dot)
    monkeypatch.setattr(view.ImageDraw.ImageDraw, "text", draw_text)
    assert view.image(section, now=123).startswith(b"\x89PNG")
    assert len(dots) == 2 and [colour for _, colour in dots] == [charts.RED, charts.YELLOW]
    assert len(texts) == len(wrapped.rows) and {xy[0] for xy, _ in texts} == {50 * view.K}
    assert [((box[1] + box[3]) / 2, colour) for box, colour in dots] == [
        (texts[i][0][1], charts.RED if lv == CRITICAL else charts.YELLOW) for i, lv in parents]


def test_image_wrapped_issue_summary_has_only_one_plus_marker(monkeypatch):
    monkeypatch.setattr(view, "W", 84)
    monkeypatch.setattr(view, "K", 1)
    summary = "4 additional warnings and 2 errors not visible"
    wrapped = view._wrap(Part([(panel.MORE, (summary,))], view.ISSUES), Mock(getlength=len))
    assert wrapped.rows[0][0] == panel.MORE and len(wrapped.rows) > 2
    assert all(lv == panel.CONT for lv, _ in wrapped.rows[1:])
    assert "".join(cells[0] for _, cells in wrapped.rows) == summary


@pytest.mark.parametrize("metadata", [False, True])
def test_text_table_summaries_span_columns_without_changing_data_alignment(metadata):
    rows = [(FINE, ("SHA", "Commit", "Age")), (FINE, ("abcdef0", "message", "1m ago"))]
    kwargs = {"marks": [(), (FINE, WARNING, FINE)], "colours": [(), (FINE, FINE, CRITICAL)],
              "icons": ["", "run"], "trends": [None]} if metadata else {}
    part = Part(rows, "Pushes", "lll", heads=True, cut=1, **kwargs)
    message = "3 additional repos not visible"
    extended = part._replace(rows=[*rows, (panel.MORE, (message, "", ""))])
    assert view._lines(extended, metadata) == [*view._lines(part, metadata), (panel.MORE, message)]
    output = view.text(Section("f", "Forgejo", [extended])).description
    assert f"+ {message}" in output and "abcdef0" in output and "message" in output
    assert all(len(view._plain(line)) <= panel.WIDTH + 3 for line in output.splitlines()
               if not line.startswith(("**", "```")))


def test_long_text_summary_wraps_once_per_logical_row_without_losing_its_marker():
    message = "3 additional repos not visible because " + "a detailed explanation " * 8
    part = Part([(panel.MORE, (message,))], "Pushes", cut=None)
    rows = view._text_rows(part, view._lines(part, False), False)
    assert rows[0][0] == panel.MORE and all(lv == panel.CONT for lv, _ in rows[1:])
    output = panel.table(rows).splitlines()[1:-1]
    assert len(output) > 2 and output[0].startswith("+ ")
    assert sum(line.startswith("+") for line in output) == 1
    assert all(line.startswith("  ") for line in output[1:])
    assert all(len(line) <= panel.WIDTH + 3 for line in output)
    assert " ".join(" ".join(output).removeprefix("+ ").split()) == " ".join(message.split())


@pytest.mark.parametrize("grid", [False, True])
def test_rich_summary_is_nonempty_prose_below_the_unchanged_inline_values(grid):
    rows = [(FINE, ("Repo", "State")), (FINE, ("owner/repo", "pass"))] if not grid else [
        (FINE, ("owner/repo", "pass"))]
    part = Part(rows, "CI", heads=not grid, grid=grid)
    message = "3 additional repos not visible"
    section = Section("forge_0", "Forgejo", [part._replace(rows=[*rows, (panel.MORE, (message, ""))])])
    fields = view.rich(section).fields
    assert fields[0].name == "owner/repo" and fields[0].inline and "pass" in fields[0].value
    assert not fields[-1].inline and fields[-1].value == f"+ _{message}_"
    assert "```" not in fields[-1].value and fields[-1].value.strip()
    previous = view.rich(section._replace(parts=[part])).fields[0]
    assert (fields[0].name, fields[0].value, fields[0].inline) == (previous.name, previous.value, previous.inline)


def test_rich_summaries_survive_field_fallback_and_whole_push_group_order():
    message = "4 additional repos not visible"
    rows = [(FINE, ("Repo", "State")), *((FINE, (f"repo{i}", "pass")) for i in range(25)),
            (panel.MORE, (message, ""))]
    part = Part(rows, "CI", heads=True)
    fallback = view.rich(Section("forge_0", "Forgejo", [part]))
    assert len(fallback.fields) == 1 and not fallback.fields[0].inline
    assert fallback.fields[0].value.endswith(f"+ _{message}_")
    block = view.rich(Section("forge_0", "Forgejo", [part._replace(block=True)]))
    assert block.fields[0].value.endswith(f"+ _{message}_")
    pushes = Part([(FINE, ("SHA", "Commit")), (FINE, ("abcdef0", "message"))], "Pushes - owner/repo", heads=True)
    summary = Part([(panel.MORE, (message,))], "Pushes", cut=None)
    output = view.rich(Section("forge_0", "Forgejo", [pushes, summary]))
    assert output.description is None and output.fields[-1].name == "Pushes"
    assert output.fields[-1].value == f"+ _{message}_"
    assert len(output) <= 6000 and len(output.fields) <= 25
    assert all(field.name and field.value and len(field.value) <= 1024 for field in output.fields)


@pytest.mark.parametrize("headed", [False, True])
def test_image_summary_spans_columns_and_keeps_the_original_cell_positions(monkeypatch, headed):
    rows = [(FINE, ("SHA", "Commit", "Age")), (FINE, ("abcdef0", "message", "1m ago"))] if headed else [
        (FINE, ("original row",))]
    part = Part(rows, "Results", "lll" if headed else "l", heads=headed, cut=1 if headed else 0)
    message = "3 additional repos not visible -- " + "W" * 45
    extended = part._replace(rows=[*rows, (panel.MORE, (message, *("",) * (len(rows[0][1]) - 1)))])
    font = view._fonts()[2].font_variant(size=view._fonts()[2].size * view.K)
    assert view._layout(extended, font) == view._layout(part, font)
    assert view.part_right(extended, font) >= 50 + view.text_width(view._italic(font), message) / view.K
    texts, icons = [], []
    draw_text, draw_icon = view.ImageDraw.ImageDraw.text, view._icon

    def capture_text(draw, xy, value, **kwargs):
        texts.append((value, xy, kwargs))
        return draw_text(draw, xy, value, **kwargs)

    def capture_icon(draw, kind, x, cy, **kwargs):
        icons.append((kind, x, cy))
        return draw_icon(draw, kind, x, cy, **kwargs)

    monkeypatch.setattr(view.ImageDraw.ImageDraw, "text", capture_text)
    monkeypatch.setattr(view, "_icon", capture_icon)
    view.image(Section("forge_0", "Forgejo", [part]), now=123)
    names = {value for _, cells in rows for value in cells}
    original = [(value, xy[0]) for value, xy, _ in texts if value in names]
    texts.clear()
    png = view.image(Section("forge_0", "Forgejo", [extended]), now=123)
    assert [(value, xy[0]) for value, xy, _ in texts if value in names] == original
    summary = [(xy, style) for value, xy, style in texts if value == message]
    assert len(summary) == 1 and summary[0][0][0] == 50 * view.K
    assert icons == [("plus", 31, summary[0][0][1] / view.K)]
    with Image.open(io.BytesIO(png)) as image:
        assert view.part_right(extended, font) <= image.width - 24
        assert summary[0][1]["fill"] == charts.DIM


def test_summary_only_parts_have_nonempty_renderings_and_one_image_marker(monkeypatch):
    message = "2 additional runs not visible"
    part = Part([(panel.MORE, (message,))], "Recent runs", cut=None)
    section = Section("forge_0", "Forgejo", [part])
    assert f"+ {message}" in view.text(section).description
    assert view.rich(section).fields[0].value == f"+ _{message}_"
    icons = []
    draw_icon = view._icon

    def capture(draw, kind, x, cy, **kwargs):
        icons.append((kind, x, cy))
        return draw_icon(draw, kind, x, cy, **kwargs)

    monkeypatch.setattr(view, "_icon", capture)
    assert view.image(section, now=123).startswith(b"\x89PNG")
    assert len(icons) == 1 and icons[0][:2] == ("plus", 31)


@pytest.fixture
def tiny_png():
    buffer = io.BytesIO()
    Image.new("RGB", (12, 8), charts.BG).save(buffer, "PNG")
    return buffer.getvalue()


def sized_header(png, width, height):
    import struct
    import zlib

    data = bytearray(png)
    data[16:24] = struct.pack(">II", width, height)
    data[29:33] = struct.pack(">I", zlib.crc32(data[12:29]))
    return bytes(data)


def test_oversized_cards_return_bounded_metadata_placeholders_before_allocating_the_card(monkeypatch, caplog):
    section = Section("forge_budget", "Forgejo", [Part([(FINE, ("row",))])])
    allocations, texts = [], []
    create, draw_text = Image.new, view.ImageDraw.ImageDraw.text

    def allocate(mode, size, *args, **kwargs):
        allocations.append((mode, size))
        return create(mode, size, *args, **kwargs)

    def capture(draw, xy, text, **kwargs):
        texts.append(text)
        return draw_text(draw, xy, text, **kwargs)

    monkeypatch.setattr(view.Image, "new", allocate)
    monkeypatch.setattr(view.ImageDraw.ImageDraw, "text", capture)
    png = view.image(section, now=123, min_width=10**9)
    assert isinstance(png, view.Rendered) and png.key == section.key and png.section_title == section.title
    assert png.now == 123 and png.tz is None
    assert allocations and all(mode == "RGB" for mode, _ in allocations)
    assert all(renderbudget.fits(view._image_bytes(w // view.K, h // view.K)) for _, (w, h) in allocations)
    assert "Lower latest" in " ".join(texts) and "Updated" in " ".join(texts)
    assert not renderbudget.fits(view._image_bytes(10**9, 100))
    view.stack([png], "version footer")
    png.draw(10**12)
    view.image(section, now=123, min_width=10**9)
    assert sum("image forge_budget exceeds" in record.message for record in caplog.records) == 1
    view.image(section, now=123)
    view.image(section, now=123, min_width=10**9)
    assert sum("image forge_budget exceeds" in record.message for record in caplog.records) == 2


def test_placeholder_is_nonrecursive_keeps_notes_and_fails_closed_if_the_budget_cannot_hold_it(monkeypatch):
    text_calls = []
    draw_text = view.ImageDraw.ImageDraw.text

    def capture(draw, xy, text, **kwargs):
        text_calls.append(text)
        return draw_text(draw, xy, text, **kwargs)

    monkeypatch.setattr(view.ImageDraw.ImageDraw, "text", capture)
    png = view.placeholder("tasks", "Tasks", 800, now=123, note="version footer")
    assert "Reduce entries" in " ".join(text_calls) and "version footer" in text_calls
    assert png.key == "tasks" and png.section_title == "Tasks" and png.now == 123
    monkeypatch.setattr(renderbudget, "fits", lambda _required: False)
    create = Mock()
    monkeypatch.setattr(view.Image, "new", create)
    with pytest.raises(ValueError, match="cannot hold a placeholder"):
        view.placeholder("tasks", "Tasks", 800)
    create.assert_not_called()


def test_two_times_scale_preserves_normal_output_dimensions(monkeypatch):
    section = Section("scale", "Scale", [Part([(FINE, ("Name", "Value")), (FINE, ("short", "12%"))], heads=True)])
    with monkeypatch.context() as context:
        context.setattr(view, "K", 3)
        old = Image.open(io.BytesIO(view.image(section, now=123, min_width=800))).size
    assert view.K == renderbudget.SCALE == 2
    current = Image.open(io.BytesIO(view.image(section, now=123, min_width=800)))
    assert current.size == old


def test_stack_rejects_header_dimensions_before_decoding_and_preserves_source_metadata(tiny_png, monkeypatch):
    source = view.Rendered(sized_header(tiny_png, 20000, 1000), lambda width: pytest.fail("redraw before budget"),
                           key="forge_large", section_title="Forgejo", now=123)
    convert = Mock(side_effect=AssertionError("decoded oversized input"))
    monkeypatch.setattr(view.Image.Image, "convert", convert)
    png = view.stack([source], "version footer")
    assert png.key == "stack:forge_large" and png.section_title == "Forgejo" and png.now == 123
    with Image.open(io.BytesIO(png)) as output:
        assert output.width == 20000
        assert 4 * (view.K * view.K + 3) * output.width * output.height < renderbudget.RENDER_BUDGET
    convert.assert_not_called()


def test_stack_guards_encoded_bytes_and_decompression_bombs_without_decoding(tiny_png, monkeypatch):
    fits = renderbudget.fits
    monkeypatch.setattr(renderbudget, "fits", lambda required: required != len(tiny_png) and fits(required))
    sizes = Mock(side_effect=AssertionError("opened rejected encoded data"))
    monkeypatch.setattr(view, "_sizes", sizes)
    assert view.stack([tiny_png]).startswith(b"\x89PNG")
    sizes.assert_not_called()
    monkeypatch.setattr(renderbudget, "fits", fits)
    sizes.side_effect = Image.DecompressionBombError("too many pixels")
    assert view.stack([tiny_png]).startswith(b"\x89PNG")
    sizes.assert_called_once()
    sizes.reset_mock()
    sizes.side_effect = [[(12, 8)], Image.DecompressionBombError("redrawn pixels")]
    assert view.stack([tiny_png]).startswith(b"\x89PNG")
    assert sizes.call_count == 2


def test_stack_rechecks_the_redrawn_header_before_conversion(tiny_png, monkeypatch):
    calls = []

    def redraw(width):
        calls.append(width)
        return sized_header(tiny_png, width, 50000)

    source = view.Rendered(tiny_png, redraw, key="child", section_title="Child", now=123)
    convert = Mock(side_effect=AssertionError("decoded rejected redraw"))
    monkeypatch.setattr(view.Image.Image, "convert", convert)
    output = view.stack([source], min_width=900)
    assert calls == [900] and output.key == "stack:child"
    assert Image.open(io.BytesIO(output)).height < 50000
    convert.assert_not_called()


def test_stack_redraws_sources_before_decoding_and_supports_nested_serialization(tiny_png, monkeypatch):
    events = []

    def redraw(width):
        events.append("redraw")
        return view.image(Section("child", "Child", [Part([(FINE, ("row",))])]), now=123, min_width=width)

    source = view.Rendered(tiny_png, redraw, key="child", section_title="Child", now=123)
    convert = Image.Image.convert

    def decode(image, *args, **kwargs):
        if image.format == "PNG":
            events.append("decode")
        return convert(image, *args, **kwargs)

    monkeypatch.setattr(view.Image.Image, "convert", decode)
    output = view.stack([source, tiny_png], min_width=900)
    assert events[0] == "redraw" and events[1:] == ["decode", "decode"]
    assert output.key == "stack:child,image" and output.section_title == "Child" and output.now == 123
    assert Image.open(io.BytesIO(output)).width == 900
    assert view.stack([output], "nested footer").startswith(b"\x89PNG")


def test_stack_accounts_for_original_and_redrawn_encoded_buffers_once(tiny_png, monkeypatch):
    source = view.Rendered(tiny_png, lambda width: tiny_png)
    assert view._encoded_size([source, source]) == len(source)
    measured = []
    estimate = view._stack_bytes

    def capture(width, sizes, foot, encoded):
        measured.append(encoded)
        return estimate(width, sizes, foot, encoded)

    monkeypatch.setattr(view, "_stack_bytes", capture)
    assert view.stack([source], min_width=900).startswith(b"\x89PNG")
    assert measured == [len(source), len(source) + len(tiny_png)]


def test_stack_reserves_original_and_previous_redraws_and_releases_them_on_failure(tiny_png):
    available, observed = renderbudget.available(), []
    replacement = bytes(bytearray(tiny_png))

    def redraw(width):
        observed.append(renderbudget.available())
        return replacement

    def fail(width):
        observed.append(renderbudget.available())
        raise RuntimeError("cancelled redraw")

    first, second = view.Rendered(tiny_png, redraw), view.Rendered(tiny_png, fail)
    original = len(first) + len(second)
    with pytest.raises(RuntimeError, match="cancelled redraw"):
        view.stack([first, second], min_width=900)
    assert observed == [available - original, available - original - len(replacement)]
    assert renderbudget.available() == available
