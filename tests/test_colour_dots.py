import re
import unicodedata

import pytest

from shazzzbot import jobs, panel, services, sysimage, unraid, view
from shazzzbot.docker import Container
from shazzzbot.glyphs import PLAY
from tests.test_jobs import NOW, TZ, Frozen
from tests.test_unraid import DISKS, VAR

SGR = re.compile(r"\x1b\[([0-9;]*)m")


def visible(text):
    return SGR.sub("", text)


def columns(text):
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in visible(text))


def assert_colour_markers(text):
    parent = panel.FINE
    for line in text.splitlines():
        dots = [lv for lv in (panel.WARNING, panel.CRITICAL) if panel.SLOT[lv] in line]
        assert sum(line.count(panel.SLOT[lv]) for lv in (panel.WARNING, panel.CRITICAL)) <= 1, repr(line)
        if dots:
            parent = max(dots)
        codes = {code for match in SGR.finditer(line) for code in match[1].split(";")}
        for level, colours in ((panel.WARNING, {"33", "93"}), (panel.CRITICAL, {"31", "91"})):
            if codes & colours:
                assert parent >= level, repr(line)
        if codes & {"32", "92"}:
            assert PLAY in line, repr(line)


def assert_both_modes(section):
    text = view.text(section)
    embed = view.rich(section)
    assert_colour_markers(text.description or "")
    assert_colour_markers(embed.description or "")
    for field in embed.fields:
        assert_colour_markers(field.value)
    assert all(len(visible(line)) <= panel.WIDTH + 3 for line in (text.description or "").splitlines()
               if not line.startswith(("**", "```")))
    assert len(embed.fields) <= 25 and len(embed) <= 6000
    assert all(len(field.value) <= 1024 for field in embed.fields)
    return text, embed


def test_worst_case_real_sections_always_pair_colours_with_level_dots(monkeypatch):
    monkeypatch.setattr(jobs, "datetime", Frozen)
    array = unraid.build(unraid.parse_ini(VAR)[""], unraid.parse_ini(DISKS))
    disks = panel.array_section(array, ["disk1"], {d.device: (999.9, 999.9, 100) for d in array.disks})
    containers = [Container("a", "long-running-service-name", "running", "Up", project="healthy"),
                  Container("b", "unhealthy-service-name", "running", "Up (unhealthy)", project="warning"),
                  Container("c", "partial-running-member", "running", "Up", project="critical"),
                  Container("d", "partial-stopped-member", "exited", "Exited", project="critical")]
    names = ["A service name much longer than the narrow phone table", "Warn", "Critical"]
    results = [services.Result(panel.FINE, "running", "12ms", "N/A", "N/A"),
               services.Result(panel.WARNING, "running", "HTTP 502", "HTTP 503", "HTTP 504", "local HTTP 502"),
               services.Result(panel.CRITICAL, "unhealthy", "HTTP 500", "N/A", "N/A", "container unhealthy")]
    service = services.section([services.Service(name, name) for name in names], results)
    name = "A maintenance task with the longest supported display name"
    data = {"generated": NOW.timestamp(), "running": {name: NOW.timestamp() - 7200}, "jobs": []}
    tasks = jobs.tasks_section(data, None, TZ, [], {}, last={name: 14 * 3600 + 12 * 60})
    system = sysimage.section("System", "", "", [
        sysimage.Row("Load", "99.99 (1m)  88.88 (5m)  77.77 (15m)", marks=(panel.WARNING, panel.CRITICAL, 0)),
        sysimage.Row("OS", "Unraid version with a long update-available qualifier (update available!)"),
        *sysimage.status_rows([], jobs.active(data, None, frozenset(), NOW.timestamp(), [], {}), [], None, False,
                              {name: panel.WARNING})], None)
    for section in (disks, panel.docker_section(containers, None), service, tasks, system):
        assert_both_modes(section)
    assert tasks.issues == [] and system.issues == []


def test_running_dot_leads_the_parent_value_and_the_last_note_stays_under_the_name(monkeypatch):
    monkeypatch.setattr(jobs, "datetime", Frozen)
    array = unraid.Array("host", "7", "STARTED", False,
                         unraid.Parity("check", True, False, 61, 0, int(NOW.timestamp()) - 9 * 3600 - 25 * 60, 0, 0, 0))
    section = jobs.tasks_section({"generated": NOW.timestamp()}, array, TZ, [], {}, last={"Parity check": 7200})
    text, embed = assert_both_modes(section)
    lines = [visible(line) for line in text.description.splitlines()]
    main = next(line for line in lines if "Parity - 61% done" in line)
    note = next(line for line in lines if "Last took" in line)
    assert main.startswith(panel.WARN + " 9h25m") and main.count(panel.WARN) == 1
    assert panel.WARN not in note
    assert columns(main[:main.index("Parity")]) == columns(note[:note.index("Last took")])
    running = next(field.value for field in embed.fields if field.name == "Running (elapsed)")
    assert "*Last took 2h*" in running
    assert "`" not in running and "\x1b" not in running and running.count(panel.WARN) == 1
    assert section.issues == []


@pytest.mark.parametrize("level", [panel.WARNING, panel.CRITICAL])
def test_cosmetic_system_grid_colours_replace_the_neutral_text_marker_without_adding_issues(level):
    section = panel.Section("system", "System", [panel.Part([(panel.FINE, ("Tasks", "1 task running:  Parity check"))],
                                                          grid=True, cut=None, colours=[(panel.FINE, level)])])
    text, embed = assert_both_modes(section)
    task_line = next(line for line in text.description.splitlines() if "Tasks" in line)
    assert task_line.startswith(panel.SLOT[level] + " Tasks") and task_line.count(panel.SLOT[level]) == 1
    assert embed.fields[0].value.splitlines()[1] == panel.SLOT[level] + " Parity check"
    assert embed.fields[0].value.count(panel.SLOT[level]) == 1
    assert "`" not in embed.fields[0].value and "\x1b" not in embed.fields[0].value
    assert section.parts[0].rows[0][0] == panel.FINE and not section.issues


@pytest.mark.parametrize("icons", [False, True])
def test_mixed_level_table_reserves_one_leading_marker_for_the_highest_level(icons):
    part = panel.Part([(panel.FINE, ("Name", "Warn", "Error")),
                       (panel.WARNING, ("a name that has to be truncated to fit the phone", "999.9MB/s", "999.9MB/s"))],
                      align="lrr", heads=True, marks=[(), (0, panel.WARNING, panel.CRITICAL)],
                      icons=["", "warn"] if icons else ())
    text, _ = assert_both_modes(panel.Section("mixed", "Mixed", [part]))
    line = next(line for line in text.description.splitlines() if "999.9MB/s" in line)
    assert panel.WARN not in line and line.count(panel.CRIT) == 1
    assert visible(line).startswith(panel.CRIT + " ")
    assert len(visible(line)) <= panel.WIDTH + 3


def test_raw_colour_codes_and_wrapped_grid_values_keep_markers_and_colours():
    value = "alpha " * 12 + "omega"
    part = panel.Part([(panel.FINE, ("Very long label for a deliberately narrow card", "\x1b[33m" + value + "\x1b[m")),
                       (panel.FINE, ("Other", "\x1b[31m" + "x" * 120 + "\x1b[0m plain"))], grid=True, cut=None)
    section = panel.Section("generic", "Generic", [part], note="\x1b[93mNotice\x1b[0m")
    text, _ = assert_both_modes(section)
    assert visible(text.description).count("alpha") == 12
    assert "omega" in text.description and "plain" in text.description
    assert view._ansi_levels("\x1b[91mred\x1b[0m \x1b[92mrun") == {panel.CRITICAL}
    assert view._prose("\x1b[33mwarning\x1b[0m and \x1b[31merror\x1b[0m") == panel.CRIT + " warning and error"


def test_rich_markers_survive_field_clipping_and_global_embed_limits():
    parts = [panel.Part([(panel.FINE, ("Metric", "value " * 300))], f"Part {n}", grid=True,
                        colours=[(panel.WARNING,)]) for n in range(30)]
    section = panel.Section("many", "Many", parts)
    embed = view.rich(section)
    assert len(embed) <= 6000 and len(embed.fields) <= 25
    assert_colour_markers(embed.description or "")
    for field in embed.fields:
        assert len(field.value) <= 1024
        assert_colour_markers(field.value)


def test_wrapping_control_whitespace_preserves_words_and_repeats_the_active_colour():
    value = "\x1b[33mone\ttwo\nthree\rfour\vfive\fsix\x1b[0m plain"
    lines = view._ansi_wrap(value, 12, 0)
    assert " ".join(visible(line) for line in lines) == "one two three four five six plain"
    assert all(len(visible(line)) <= 12 for line in lines)
    assert all("\x1b[33m" in line for line in lines[:-1])


def test_system_alert_counts_have_one_highest_dot_and_keep_both_counts():
    issues = [(panel.CRITICAL, f"critical {n}") for n in range(2)] + [(panel.WARNING, f"warning {n}") for n in range(5)]
    rows = sysimage.status_rows(issues, [], [], None, False)
    section = sysimage.section("System", "", "", rows, None)
    text, rich = assert_both_modes(section)
    line = visible(next(line for line in text.description.splitlines() if "Alerts" in line))
    assert "2 critical" in line and "5 warnings" in line
    assert line.startswith(panel.CRIT + " Alerts") and line.count(panel.CRIT) == 1 and panel.WARN not in line
    value = next(field.value for field in rich.fields if field.name == "Alerts")
    assert value.splitlines()[:2] == [panel.CRIT + " 2 critical", panel.WARN + " 5 warnings"]
    assert "2 critical" in value and "5 warnings" in value and "`" not in value and "\x1b" not in value
    assert len(section.issues) == 7


def test_a_logical_row_wrapping_twice_lifts_late_severity_to_its_parent_only():
    value = "R " + "read " * 20 + "999.9 MB/s  W " + "write " * 20 + "999.9 MB/s  (100% busiest)"
    part = panel.Part([(panel.FINE, ("Disk I/O", value))], grid=True, cut=None,
                      marks=[(panel.FINE, panel.WARNING, panel.CRITICAL)])
    section = panel.Section("system", "System", [part])
    text, rich = assert_both_modes(section)
    lines = [line for line in text.description.splitlines() if not line.startswith("```")]
    assert len(lines) >= 3
    assert visible(lines[0]).startswith(panel.CRIT + " Disk I/O")
    assert sum(line.count(panel.CRIT) + line.count(panel.WARN) for line in lines) == 1
    assert any(panel.ANSI[panel.WARNING] in line for line in lines[1:])
    assert any(panel.ANSI[panel.CRITICAL] in line for line in lines[1:])
    assert visible(text.description).count("read") == 20 and visible(text.description).count("write") == 20
    assert "(100% busiest)" in text.description
    assert rich.fields[0].value.count(panel.CRIT) == 1 and rich.fields[0].value.count(panel.WARN) == 1
    assert [line[-1] for line in rich.fields[0].value.splitlines()[1:3]] == [panel.WARN, panel.CRIT]
    assert "`" not in rich.fields[0].value and "\x1b" not in rich.fields[0].value
    assert rich.fields[0].value.count("read") == 20 and rich.fields[0].value.count("write") == 20
    assert part.rows == [(panel.FINE, ("Disk I/O", value))] and not section.issues


@pytest.mark.parametrize("long", [False, True])
def test_disk_tile_dots_follow_values_and_never_shift_label_or_value_columns(long):
    temperature = "Temperature" * 8 if long else "Temp"
    value = "49C" + ("v" * 200 if long else "")
    part = panel.Part([(panel.FINE, ("Disk", temperature, "Used", "Busy")),
                       (panel.CRITICAL, ("disk1", value, "98%", "100%"))], heads=True,
                      marks=[(), (panel.FINE, panel.WARNING, panel.CRITICAL, panel.WARNING)])
    field = view.rich(panel.Section("disks", "Disks", [part])).fields[0]
    assert field.value.startswith("```ansi\n")
    lines = [visible(line) for line in field.value.splitlines()[1:-1]]
    column = min(len(temperature) + 1, panel.WIDTH // 2) + 1
    assert [line.index(value) for line, value in zip(lines, ("49C", "98%", "100%"))] == [column] * 3
    assert all(line[0].isalpha() for line in lines)
    for line, dot in zip(lines, (panel.WARN, panel.CRIT, panel.WARN)):
        assert line.endswith(dot)
        assert sum(line.count(mark) for mark in (panel.WARN, panel.CRIT)) == 1
        assert columns(line) <= panel.WIDTH + 3
    if long:
        assert panel.ELL in lines[0]


@pytest.mark.parametrize("grid", [False, True])
def test_explicit_continuations_share_the_parent_dot_in_text_and_rich(grid):
    part = panel.Part([(panel.FINE, ("Parent", "first")), (panel.CONT, ("", "detail"))], "Context", grid=grid,
                      marks=[(), (panel.CRITICAL, panel.CRITICAL)])
    section = panel.Section("context", "Context", [part])
    text, rich = assert_both_modes(section)
    assert text.description.count(panel.CRIT) == 1
    values = "\n".join(field.value for field in rich.fields)
    assert values.count(panel.CRIT) == 1 and "detail" in values
    assert "`" not in values and "\x1b" not in values


def test_orphan_continuations_and_muted_notes_are_plain_context():
    section = panel.Section("context", "Context", [panel.Part([(panel.CONT, ("", "detail"))], "Context")])
    rich = view.rich(section)
    assert rich.fields[0].value == "detail"
    for level in (panel.CONT, panel.NOTE):
        assert view._prose("\x1b[33mcontext\x1b[0m", level) == "context"


def test_single_value_tiles_are_prose_instead_of_inline_code():
    part = panel.Part([(panel.FINE, ("Service", "Status")), (panel.WARNING, ("Service", "paused"))], heads=True)
    field = view.rich(panel.Section("services", "Services", [part])).fields[0]
    assert field.value.startswith(panel.WARN + " Status: paused")
    assert "`" not in field.value and "\x1b" not in field.value


def test_explicit_icon_severity_raises_the_single_parent_marker():
    part = panel.Part([(panel.FINE, ("Name", "Value")), (panel.FINE, ("Marked", "12%"))], heads=True,
                      icons=["", "dot2"])
    text, rich = assert_both_modes(panel.Section("icons", "Icons", [part]))
    line = visible(next(line for line in text.description.splitlines() if "Marked" in line))
    assert line.startswith(panel.CRIT + "    Marked")
    assert line.count(panel.CRIT) == 1
    assert rich.fields[0].value.startswith(panel.CRIT)


def test_optional_fine_dots_lead_values_when_the_table_has_status(monkeypatch):
    from shazzzbot.glyphs import GREEN

    monkeypatch.setattr(panel, "SLOT", (GREEN, panel.WARN, panel.CRIT))
    part = panel.Part([(panel.FINE, ("Name", "Value")), (panel.FINE, ("Metric", "12%")),
                       (panel.WARNING, ("Warning", "90%"))], heads=True)
    section = panel.Section("fine", "Fine", [part], note="A note")
    text = view.text(section).description
    assert text.startswith("A note\n")
    lines = [line for line in text.splitlines() if not line.startswith("```")]
    assert next(line for line in lines if "Name" in line).startswith(GREEN + " ")
    assert next(line for line in lines if "Metric" in line).startswith(GREEN + " ")
    assert next(line for line in lines if "Warning" in line).startswith(panel.WARN + " ")
    assert all(len(visible(line)) <= panel.WIDTH + 3 for line in lines)


def test_text_system_alerts_start_with_one_critical_dot_and_no_warning_dot():
    issues = [(panel.CRITICAL, "critical")] * 2 + [(panel.WARNING, "warning")] * 5
    section = sysimage.section("System", "", "", sysimage.status_rows(issues, [], [], None, False), None)
    line = visible(next(line for line in view.text(section).description.splitlines() if "Alerts " in line))
    assert line.startswith(panel.CRIT + " Alerts")
    assert line.count(panel.CRIT) == 1 and panel.WARN not in line
    assert "2 critical" in line and "5 warnings" in line


@pytest.mark.parametrize("kind,level,expected", [("warn", panel.WARNING, panel.WARN),
                                               ("warn", panel.CRITICAL, panel.CRIT),
                                               ("dot0", panel.CRITICAL, panel.OK),
                                               ("dot2", panel.FINE, panel.CRIT)])
def test_state_icon_dots_use_the_explicit_kind_or_warning_level(kind, level, expected):
    assert view._state_icon(kind, level) == expected


def test_aggregate_tile_status_does_not_colour_explicitly_fine_metric_rows():
    part = panel.Part([(panel.FINE, ("Disk", "Temp", "Used")), (panel.CRITICAL, ("disk1", "31C", "20%"))],
                      heads=True, marks=[(), (panel.FINE, panel.FINE, panel.FINE)])
    field = view.rich(panel.Section("disks", "Disks", [part])).fields[0]
    assert field.name == panel.CRIT + " disk1"
    assert panel.CRIT not in field.value and panel.WARN not in field.value
    assert "31C" in field.value and "20%" in field.value
