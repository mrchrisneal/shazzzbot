from datetime import UTC
from io import BytesIO
from unittest.mock import patch

import pytest
from PIL import Image

from shazzzbot import jobs, panel, services, sysimage, view
from shazzzbot.docker import Container
from shazzzbot.host import Snapshot
from shazzzbot.unraid import build


def sections():
    array = build({"mdState": "STOPPED", "sbSyncErrs": "2"}, {
        "cache": {"status": "DISK_OK"}, "disk3": {"status": "DISK_DSBL", "numErrors": "12"}})
    containers = [Container("a", "bad", "running", "Up (unhealthy)"),
                  Container("b", "up", "running", "Up", "stack"),
                  Container("c", "down", "exited", "Exited (1)", "stack")]
    return [panel.array_section(array, frozenset({"cache"})), panel.docker_section(containers, None),
            services.section([services.Service("web")],
                             [services.Result(2, "missing", "N/A", "N/A", "N/A", "container missing")]),
            jobs.tasks_section(None, None, UTC, [], {}),
            sysimage.section("System", "", "3:57 PM", [sysimage.Row("Jellyfin", "down", level=2)], None)]


@pytest.mark.parametrize("section", sections(), ids=lambda s: s.key)
def test_builder_issues_are_last_in_every_view(section):
    assert section.issues
    text = view.text(section).description
    assert "**Issues & Alerts**" in text
    footer = view.rich(section).fields[-1]
    assert footer.name == "Issues & Alerts"
    text_issues = text.split("**Issues & Alerts**\n```\n", 1)[1].removesuffix("\n```")
    plain = " ".join(line.removeprefix(panel.WARN + " ").removeprefix(panel.CRIT + " ").strip()
                     for line in text_issues.splitlines())
    for lv, reason in section.issues:
        assert all(line in plain and view._md(line) in footer.value for line in reason.split("\n"))
        assert panel.SLOT[lv] in footer.value
    with patch.object(view, "_rows", wraps=view._rows) as draw:
        assert view.image(section).startswith(b"\x89PNG")
    assert draw.call_args.args[1].heading == "Issues & Alerts"
    assert draw.call_args.args[1].cut is None


def test_all_builder_status_rows_have_reasons():
    array = build({"mdState": "STARTED"}, {})
    healthy = [panel.array_section(array), panel.docker_section([], None),
               services.section([], []), jobs.tasks_section({"generated": 1e20}, None, UTC, [], {}),
               sysimage.section("System", "", "3:57 PM", [sysimage.Row("CPU", "1%")], None)]
    apps = panel.apps_section(Snapshot())
    for section in sections() + healthy + [apps]:
        flagged = any(lv > panel.FINE for part in section.parts for lv, _ in part.rows)
        assert not flagged or section.issues or section.key == "apps"
    assert all(not s.issues for s in healthy + [apps])
    assert (1, "cache SMART warning") in sections()[0].issues
    assert (2, "disk3 12 read/write errors") in sections()[0].issues
    assert (2, "Array STOPPED") in sections()[0].issues


def test_image_footer_wraps_without_losing_reason():
    reason = "long warning " * 40
    section = panel.Section("test", "Test", [], issues=[(1, reason)])
    with patch.object(view, "_rows", wraps=view._rows) as draw:
        png = view.image(section)
    part = draw.call_args.args[1]
    assert len(part.rows) > 1
    assert "".join(cells[0] for _, cells in part.rows) == reason
    assert Image.open(BytesIO(png)).height > 100


def test_system_image_draws_same_reasons_as_section():
    rows = [sysimage.Row("CPU", "95%", level=2), sysimage.Row("Uptime", "2m", dot=1)]
    with patch.object(view, "_rows", wraps=view._rows) as draw:
        assert sysimage.render(rows, status=(2, "nodes down")).startswith(b"\x89PNG")
    footer = draw.call_args.args[1]
    assert footer.heading == "Issues & Alerts"
    assert footer.rows == [(2, ("Nodes: nodes down",)), (1, ("Uptime: 2m",))]  # CPU is in the Alerts feed


def test_system_footer_lists_the_alerts_not_the_alerts_row():
    alerts = [(1, "/var/lib/netdata disk_space_usage 96.4%"), (0, "cleared")]
    s = sysimage.section("", "", "3:57 PM", sysimage.status_rows(alerts, [], [], None, False), None)
    assert list(s.issues) == [(1, "/var/lib/netdata disk_space_usage 96.4%")]


@pytest.mark.parametrize("level", [panel.FINE, panel.WARNING, panel.CRITICAL])
def test_text_issue_footer_wraps_to_phone_width(level):
    reason = "a_really_long_container_name exited (1) with a detailed reason"
    section = panel.Section("test", "Test", [], issues=[(level, reason)])
    lines = view.text(section).description.split("```\n", 1)[1].removesuffix("\n```").splitlines()
    assert all(len(line) <= panel.WIDTH + 3 for line in lines)
    rows = view._lines(view.parts(section)[0], level > panel.FINE)
    assert "".join(text.replace(" ", "") for _, text in rows) == reason.replace(" ", "")
    assert rows[0][0] == level and all(lv == panel.CONT for lv, _ in rows[1:])


def test_alerts_field_marks_each_count():
    from shazzzbot import view
    rows = sysimage.status_rows([(1, "disk full")], [], [], None, False)
    field = view.rich(sysimage.section("", "", "3:57 PM", rows, None)).fields[0]
    assert field.value == f"{panel.SLOT[panel.WARNING]} 1 warning\n\u00a0"


def test_wrapped_issue_keeps_one_glyph_and_collapses_gaps():
    s = panel.Section("x", "X", [], issues=[(panel.CRITICAL, "Array: 22.1/22.7 TB    (97%) and more words to wrap")])
    lines = view.text(s).description.split("```\n", 1)[1].removesuffix("\n```").splitlines()
    glyph = panel.SLOT[panel.CRITICAL]
    assert lines[0].startswith(glyph + " ") and sum(glyph in x for x in lines) == 1
    assert all(x.startswith("  ") and "   " not in x[2:] for x in lines[1:]) and "   " not in lines[0]
