import re

import pytest

from shazzzbot import panel, unraid, view
from shazzzbot.config import DEFAULT_THRESHOLDS as TH
from shazzzbot.docker import Container
from shazzzbot.glyphs import CRIT, OK, WARN
from shazzzbot.host import Snapshot
from tests.test_unraid import DISKS, VAR


def test_level():
    assert [panel.level(v, (80, 90)) for v in (10, 85, 95)] == [panel.FINE, panel.WARNING, panel.CRITICAL]


def test_table_every_row_has_a_slot_glyph():
    lines = panel.table([(panel.FINE, "a"), (panel.WARNING, "b"), (panel.CRITICAL, "c")]).splitlines()
    assert lines[1:4] == [f"{OK} a", f"{WARN} b", f"{CRIT} c"]


def test_alerts_and_embeds():
    a = unraid.build(unraid.parse_ini(VAR)[""], unraid.parse_ini(DISKS))
    s = Snapshot(cpu_pct=95, ram_used_mb=1, ram_total_mb=10, top_cpu=[("plex", 3.0)], top_ram=[("plex", 2048.0)])
    cs = [Container("1", "web", "running", "Up 2 hours (unhealthy)"), Container("2", "db", "exited", "Exited")]
    issues = panel.alerts(s, a, cs, TH)
    texts = [t for _, t in issues]
    assert issues[0][0] == panel.CRITICAL and "CPU 95%" in texts and not any("full" in t for t in texts)
    assert "web unhealthy" in texts
    apps = view.rich(panel.apps_section(s))
    assert apps.fields[0].name == "By CPU (95%)" and apps.fields[1].name == "By RAM (0.0GB/0.0GB)"
    assert "**plex:**\u00a0 3.0%" in apps.fields[0].value
    assert "plex" in apps.fields[1].value and "2.0GB" in apps.fields[1].value
    arr = view.text(panel.array_section(a, frozenset({"disk1"}), {"sdb": (12.5, 0.4, 37.0)}))
    lines = arr.description.splitlines()
    assert lines[:3] == ["**Array Status**", "```", f"{CRIT} Array started (1 issue)"]
    plain = [re.sub("\x1b\\[[0-9;]*m", "", line) for line in lines]
    assert any(line.startswith(f"{CRIT} disk1      -   98%  20.0K") for line in plain), lines
    assert "12MB/s" in arr.description and "410KB/s" in arr.description and "37%" in arr.description
    svc = view.text(panel.docker_section(cs, None))
    assert f"{WARN} Running   1 of 2" in svc.description and f"{WARN} web unhealthy" in svc.description
    for e in (apps, arr, svc):
        assert len(e) < 6000


def test_flow_scales_whole_units():
    assert [panel.flow(v, 1024, "B/s") for v in (0, 0.5, 12.4, 999.4, 1500)] == [
        "0KB/s", "512KB/s", "12MB/s", "999MB/s", "1GB/s"]


def test_rows_fit_a_phone():
    a = unraid.build(unraid.parse_ini(VAR)[""], unraid.parse_ini(DISKS))
    s = Snapshot(cpu_pct=99.9, iowait_pct=12.5, ram_used_mb=48000, ram_total_mb=64000, temp_c=89, load=(12, 12, 12),
                 top_cpu=[("averyveryverylongname", 100.0)] * 5, top_ram=[("averyveryverylongname", 99999.0)] * 5)
    # A 375px phone shows about 31 cells; the slot emoji is a bit over 2 cells on iOS.
    apps = view.text(panel.apps_section(s)).description.splitlines()
    assert all(len(line) <= panel.WIDTH + 3 for line in apps if not line.startswith("**")), apps
    lines = view.text(panel.array_section(a)).description.splitlines()[1:-1]
    disk_rows = [re.sub("\x1b\\[[0-9;]*m", "", x) for x in lines]
    assert all(len(line) <= panel.WIDTH + 3 for line in disk_rows), disk_rows


def test_memory_capacity_has_units_and_error_counts_stay_unitless_in_every_view():
    from unittest.mock import patch

    assert panel.mem(512) == "512MB" and panel.mem(2048) == "2.0GB"
    assert [panel.size(value) for value in (512, 1024, 1024**2, 1024**3)] == ["512.0KB", "1.0MB", "1.0GB", "1.0TB"]
    assert panel.capacity(1024, 2048, 1024, "GB") == "1.0GB/2.0GB"
    array = unraid.build(unraid.parse_ini(VAR)[""], unraid.parse_ini(DISKS))
    section = panel.array_section(array)
    table = section.parts[1]
    assert all((cells[3] == "-" or cells[3].endswith("B")) and cells[-2].isdigit()
               for _, cells in table.rows[1:])
    assert "KB" in view.text(section).description
    values = str(view.rich(section).to_dict())
    assert "KB" in values and "0 errors" not in values
    with patch.object(view, "_rows", wraps=view._rows) as draw:
        assert view.image(section).startswith(b"\x89PNG")
    assert any(any(cells[-2].isdigit() for _, cells in call.args[1].rows[1:])
               for call in draw.call_args_list if call.args[1].heads)


def test_docker_counts_and_app_ranks_stay_unitless():
    from shazzzbot.docker import Engine
    from tests.test_docker import ct

    section = panel.docker_section([ct("app", project="stack")], Engine("27", "overlay2", 2))
    summary = dict(cells for _, cells in section.parts[0].rows)
    assert summary == {"Engine": "27 overlay2", "Images": "2", "Running": "1 of 1",
                       "Stacks": "1 of 1 stack up", "Networks": "1 network in use"}
    assert section.parts[1].rows[1][1][1] == "1/1"
    assert panel.apps_section(Snapshot(top_cpu=[("app", 5)], top_ram=[("app", 512)])).parts[0].rows[0][1][0] == "1"


def test_apps_trends_use_the_selected_window_and_keep_new_or_expired_samples_blank():
    from shazzzbot.apphistory import AppHistory
    from shazzzbot.glyphs import NO_DATA
    from shazzzbot.spark import TREND_WIDTH, spark

    history = AppHistory(60)
    for t, value in ((0, 100), (240, 25), (300, 50)):
        history.add(Snapshot(top_cpu=[("app", value)], top_ram=[("app", value * 2)]), 600, t)
    snapshot = Snapshot(top_cpu=[("app", 50), ("new", 5)], top_ram=[("app", 100), ("new", 50)])
    history.add(snapshot, 600, 300)
    section = panel.apps_section(snapshot, history, 300, now=300)
    for part, metric in zip(section.parts, ("cpu", "ram")):
        values = history.values(metric, "app", 0, 300)
        assert part.rows[0][1][-1] == spark(values, TREND_WIDTH, start=0, end=300)
        assert part.trends[0].samples == tuple(values)
        assert part.rows[1][1][-1] == NO_DATA * (TREND_WIDTH - 1) + chr(0x28c0)
        assert len(part.trends[1].samples) == 1
        assert part.align == "llrl"
    assert section.note == "Trends, last 5m (partial data)"
    short = panel.apps_section(snapshot, history, 50, now=300)
    assert all(part.rows[0][1][-1] == NO_DATA * (TREND_WIDTH - 1) + chr(0x28c0) for part in short.parts)
    empty = panel.apps_section(snapshot, history, 300, now=1000)
    assert all(part.trends == [None, None] for part in empty.parts)
    assert not section.issues


def test_app_trends_fit_phone_width_with_long_names():
    from shazzzbot.apphistory import AppHistory

    history = AppHistory(60)
    snapshot = Snapshot(top_cpu=[("very_long_app_name_" * 4, 100.0)],
                        top_ram=[("very_long_app_name_" * 4, 99999.0)])
    history.add(snapshot, 300, 0)
    history.add(snapshot, 300, 300)
    text = view.text(panel.apps_section(snapshot, history, 300, now=300)).description
    assert all(len(line) <= panel.WIDTH + 3 for line in text.splitlines())
    assert chr(0x2026) in text and "100.0%" in text and "97.7GB" in text


def test_array_flags_disk_status_errors_and_last_parity():
    p = unraid.Parity("", False, False, 0.0, 0.0, 0, 0, -4, 3)
    bad = unraid.Disk("disk2", "Data", "sdc", "DISK_DSBL", 50, 4 * 1024**3, 2 * 1024**3, 7)
    rows = [t for _, t in panel.array_checks(unraid.Array("t", "7", "STOPPED", False, p, [bad]))]
    assert rows == ["Array STOPPED", "Last parity check: exit -4, 3 errors", "disk2 DISK_DSBL",
                    "disk2 7 read/write errors", "disk2 running warm (50C)"]
    e = view.text(panel.array_section(unraid.Array("t", "7", "STOPPED", False, p, [bad])))
    assert f"{CRIT} Array stopped" in e.description and "2.0TB" in e.description
    hot = Snapshot(temp_c=96.0, load=(0.0, 0.0, 0.0))
    assert (panel.CRITICAL, "CPU temp 96C") in panel.system_checks(hot, TH)


@pytest.mark.parametrize("rotational,temp,expected", [
    (True, None, panel.FINE),
    (True, 44, panel.FINE),
    (True, 45, panel.WARNING),
    (True, 54, panel.WARNING),
    (True, 55, panel.CRITICAL),
    (False, None, panel.FINE),
    (False, 59, panel.FINE),
    (False, 60, panel.WARNING),
    (False, 69, panel.WARNING),
    (False, 70, panel.CRITICAL),
])
def test_disk_temperature_levels(rotational, temp, expected):
    disk = unraid.Disk("disk1", "Data", "sdb", "DISK_OK", temp, 100, 50, 0, rotational)
    checks = panel.disk_checks(disk)
    word = "hot" if expected == panel.CRITICAL else "warm"
    assert checks == ([(expected, f"disk1 running {word} ({temp}C)")] if expected else [])
    section = panel.array_section(unraid.Array("tower", "7", "STARTED", False,
                                                unraid.Parity("", False, False, 0, 0, 0, 0, 0, 0), [disk]))
    assert section.parts[1].rows[1][0] == expected
    assert section.parts[1].marks[:2] == [
        (), (panel.FINE, expected, panel.FINE, panel.FINE, panel.FINE, panel.FINE, panel.FINE, panel.FINE, panel.FINE)]
    assert section.issues == checks


def test_table_drops_glyphs_without_status_and_fits_the_limit():
    assert panel.table([(panel.FINE, "a  "), (panel.FINE, "b")]) == "```\na\nb\n```"
    assert panel.table([(panel.FINE, "a"), (panel.WARNING, "b")]).count(" a") == 1
    cut = panel.table([(panel.FINE, "x" * 10)] * 20, 100)
    assert len(cut) <= 100 and cut.endswith("...and 13 more\n```")
    assert cut.splitlines()[1:-2] == ["x" * 10] * 7


def test_text_tables_fit_the_width_by_clipping_names():
    from shazzzbot.docker import Container
    cs = [Container(str(i), f"c{i}", "running", "Up",
                    project="authelia_foundation_stack_for_the_whole_household") for i in range(3)]
    cs.append(Container("x", "a_really_long_container_name_that_will_not_fit_on_one_line",
                        "exited", "Exited (1) 2 hours ago",
                        project="authelia_foundation_stack_for_the_whole_household"))
    lines = [x for x in view.text(panel.docker_section(cs, None)).description.splitlines()
             if not x.startswith(("```", "**"))]
    assert all(len(line) <= panel.WIDTH + 3 for line in lines), lines
    assert any(line.startswith(CRIT + " ") and line.endswith(chr(0x2026) + " 3/4") for line in lines), lines
    assert any("a_really_long_container" in x for x in lines) and lines[-1].endswith("exited (1)")  # wrapped
    assert panel.kv(0, "Engine", "x" * 80, 8)[1] == "Engine   " + "x" * (panel.WIDTH - 10) + chr(0x2026)
    assert panel.grid([(0, ("x" * 40, "1"))], "lr", width=10) == [(0, "x" * 7 + chr(0x2026) + " 1")]
    assert panel.grid([(0, ("ab", "1"))], "lr", width=10) == [(0, "ab" + " " * 6 + "1")]


def test_text_disk_row_drops_to_single_spaces_to_fit():
    head = ("Disk", "Temp", "Used", "Free", "Read", "Write", "Busy")
    row = ("cache2", "37C", "100%", "115.7GB", "294KB/s", "690KB/s", "100%")
    lines = panel.grid([(0, head), (0, row)], "lrrrrrr", width=panel.WIDTH - panel.ICON_TEXT_WIDTH)
    assert all(len(text) <= panel.WIDTH - panel.ICON_TEXT_WIDTH for _, text in lines)
    assert lines[1][1] == "cache2  37C 100% 115.7GB 294KB/s 690KB/s 100%"


def test_stack_rows_have_four_states_and_off_stacks_have_no_issues():
    from tests.test_docker import stack_cases

    containers = stack_cases()
    section = panel.docker_section(containers, None)
    stacks = next(p for p in section.parts if p.heading == "Stacks")
    states = {cells[0]: (lv, icon) for (lv, cells), icon in zip(stacks.rows[1:], stacks.icons[1:])}
    assert states == {"healthy": (panel.FINE, "run"), "warning": (panel.WARNING, "dot1"),
                      "critical": (panel.CRITICAL, "dot2"), "off": (panel.FINE, "stop")}
    assert any(lv == panel.CRITICAL and text.startswith("critical degraded:") for lv, text in section.issues)
    assert not any(text.startswith("off") for _, text in section.issues)
    assert panel.docker_section([c for c in containers if c.project == "off"], None).issues == []


def test_disk_tables_render_as_inline_fields_with_values_beneath_labels():
    a = unraid.build(unraid.parse_ini(VAR)[""], unraid.parse_ini(DISKS))
    for section in (panel.array_section(a), panel.array_section(a, io={"sdb": (12.5, 0.4, 37.0)})):
        fields = [f for f in view.rich(section).fields if f.inline]
        assert fields and fields[0].name == a.disks[0].name
        assert all(f.value.endswith(("\n\u00a0", "```")) for f in fields)
        assert [f.name for f in fields if panel.WARN in f.name or panel.CRIT in f.name] == [f"{panel.CRIT} disk1",
                                                                                     f"{panel.CRIT} Total"]
        assert "\n" in fields[0].value
        assert len(view.rich(section)) <= 6000


def test_docker_platform_uses_inline_values_and_spacers():
    section = panel.docker_section([], None)
    fields = view.rich(section).fields
    assert [f.name for f in fields] == ["Running", "Stacks", "Networks"]
    assert all(f.inline and f.value.endswith("\n\u00a0") for f in fields)
    assert fields[0].value == "0 of 0\n\u00a0"
    assert fields[0].value == "0 of 0\n\u00a0"


def test_disks_include_live_io_and_total_sums_with_busiest_disk():
    a = unraid.build({"mdState": "STARTED"}, {
        "disk1": {"status": "DISK_OK", "device": "sda", "temp": "31", "fsSize": "100", "fsFree": "50"},
        "disk2": {"status": "DISK_OK", "device": "sdb", "temp": "32", "fsSize": "100", "fsFree": "25"},
        "cache": {"status": "DISK_OK", "device": "nvme0n1"},
    })
    io = {"sda": (16.0, 0.5, 45.0), "sdb": (4.0, 1.5, 75.0), "unmounted": (999.0, 999.0, 100.0)}
    section = panel.array_section(a, ["disk2", "disk2", "dev3"], io)
    table = section.parts[1]
    assert table.rows[0] == (panel.FINE, ("Disk", "Temp", "Used", "Free", "Read", "Write", "Busy", "Errors", "SMART"))
    assert table.align == "lrrrrrrrr" and table.heads
    assert table.rows[1][1] == ("disk1", "31C", "50%", "50.0KB", "16MB/s", "512KB/s", "45%", "0", "ok")
    assert table.rows[2][1][-5:] == ("4MB/s", "2MB/s", "75%", "0", "2 warnings")
    assert table.marks[2][-1] == panel.WARNING
    assert table.rows[3][1][-5:] == ("0KB/s", "0KB/s", "0%", "0", "ok")
    assert table.marks[:4] == [(), (panel.FINE,) * 9, (*(panel.FINE,) * 8, panel.WARNING), (panel.FINE,) * 9]
    assert table.rows[4][1] == ("Total", "~32C", "62%", "75.0KB", "20MB/s", "2MB/s", "~40%", "0", "2 warnings")
    assert table.marks[4] == (*(panel.FINE,) * 8, panel.WARNING)
    hot = panel.array_section(a, ["disk2"], io, {**TH, "hdd_temp": (30.0, 31.0), "disk_full": (60.0, 95.0)})
    assert hot.parts[1].marks[-1][:3] == (panel.FINE, panel.CRITICAL, panel.WARNING)
    assert section.parts[0].rows[1:] == [(panel.FINE, ("Total read: 20MB/s",)), (panel.FINE, ("Total write: 2MB/s",)),
                                     (panel.FINE, ("Busiest disk: 75%",))]
    assert section.issues == [(panel.WARNING, "disk2 SMART warning")]
    fields = view.rich(section).fields
    disk = next(f for f in fields if f.name == "disk1")
    assert "Read:   16MB/s\nWrite:  512KB/s\nBusy:   45%" in disk.value


def test_empty_disks_and_busy_io_keep_fine_totals():
    empty = unraid.build({"mdState": "STARTED"}, {})
    section = panel.array_section(empty, io={})
    assert section.parts[0].rows[3] == (panel.FINE, ("Busiest disk: 0%",))
    assert len(section.parts[1].rows) == 1 and not section.issues
    empty.disks.append(unraid.Disk("disk1", "Data", "sda", "DISK_OK", None, 0, None, 0))
    section = panel.array_section(empty, io={"sda": (1.0, 2.0, 100.0)})
    assert all(lv == panel.FINE for lv, _ in section.parts[0].rows[1:])
    assert "1 issue" in section.parts[0].rows[0][1][0]
    assert section.issues == [(panel.WARNING, "disk1 100% busy")]


def test_load_rows_list_only_values_at_each_level(monkeypatch):
    monkeypatch.setattr(panel, "CORES", 1)
    rows = [r for r in panel.system_checks(Snapshot(load=(1.5, 0.8, 0.9)), TH) if r[1].startswith("Load")]
    assert rows == [(panel.CRITICAL, "Load 1.50 (1m)"), (panel.WARNING, "Load 0.80 (5m), 0.90 (15m)")]
    assert not [r for r in panel.system_checks(Snapshot(load=(0.1, 0.1, 0.1)), TH) if r[1].startswith("Load")]


def test_group_usage_folds_disks_per_level():
    rows = [(panel.CRITICAL, "disk1 98% used"), (panel.CRITICAL, "Array 22.1/22.7 TB (97%)"),
            (panel.CRITICAL, "disk2 97% used"), (panel.WARNING, "disk7 91% used")]
    assert panel.group_usage(rows, TH) == [(panel.CRITICAL, "2 disks 95%+ used"),
                                           (panel.CRITICAL, "Array 22.1/22.7 TB (97%)"),
                                           (panel.WARNING, "disk7 91% used")]


def test_net_io_is_checked_against_link_speed():
    s = Snapshot(net_speed_mbps=1000, net_rx_mbps=960, net_tx_mbps=10)
    assert (panel.CRITICAL, "Net I/O 96% of link") in panel.system_checks(s, TH)


def test_top_app_names_cap_at_32_chars():
    from shazzzbot.host import Snapshot
    long = "FORGEJO-ACTIONS-TASK-6217_WORKFLOW-68d8af6cd8ef6d73500257b198b928a761"
    rows = panel.apps_section(Snapshot(top_cpu=[(long, 80.8), ("x" * 32, 1.0)])).parts[0].rows
    assert rows[0][1][1] == long[:29] + "..." and len(rows[0][1][1]) == 32 and rows[1][1][1] == "x" * 32


def test_standalone_containers_are_never_a_degraded_stack():
    loose = [Container("job", "x", "exited", "Exited (137)"), Container("tool", "y", "created", "Created"),
             Container("web", "z", "running", "Up")]
    s = panel.docker_section(loose, None)
    assert s.issues == []
    assert "(no stack)  1/3" in view.text(s).description and "Issues" not in view.text(s).description


def test_disk_busy_is_warning_only_in_system_disks_and_grouped_issues():
    from shazzzbot import sysimage
    from shazzzbot.charts import YELLOW
    from shazzzbot.unraid import Array, Disk, Parity

    snapshot = Snapshot(disk_util_pct=100)
    checks = panel.system_checks(snapshot, TH)
    assert (panel.WARNING, "Disk I/O 100% busiest") in checks
    table = sysimage.rows(snapshot, TH)
    row = next(r for r in table if r.label == "Disk I/O")
    assert row.level == panel.WARNING and row.colour == YELLOW and row.fill == 1
    table += sysimage.status_rows([(panel.WARNING, "Disk I/O 100% busiest")], [], [], None, False)
    system = sysimage.section("System", "", "", table, None)
    assert (panel.WARNING, "Disk I/O 100% busiest") in system.issues
    disks = [Disk("disk1", "Data", "sdb", "DISK_OK", 30, 100, 50, 0),
             Disk("disk2", "Data", "sdc", "DISK_OK", 30, 100, 50, 0)]
    section = panel.array_section(Array("host", "7", "STARTED", False,
                                       Parity("", False, False, 0, 0, 0, 0, 0, 0), disks=disks),
                                  io={"sdb": (1, 2, 100), "sdc": (2, 1, 95)})
    part = section.parts[-1]
    assert [lv for lv, cells in part.rows[1:] if cells[0] != "Total"] == [panel.WARNING, panel.WARNING]
    assert all(marks[6] == panel.WARNING for marks in part.marks[1:-1])
    assert section.issues == [(panel.WARNING, "2 disks 95%+ busy")]
    mixed = [(panel.WARNING, "disk1 100% busy"), (panel.WARNING, "disk2 95% busy"),
             (panel.WARNING, "disk1 92% used"), (panel.WARNING, "disk2 93% used"),
             (panel.CRITICAL, "disk3 99% used"), (panel.WARNING, "cache SMART warning")]
    assert panel.group_usage(mixed, TH) == [(panel.WARNING, "2 disks 95%+ busy"),
        (panel.WARNING, "2 disks 90%+ used"), (panel.CRITICAL, "disk3 99% used"),
        (panel.WARNING, "cache SMART warning")]
    for card in (system, section):
        assert "100%" in view.text(card).description
        assert view.image(card).startswith(b"\x89PNG")
        assert ("busiest" if card.key == "system" else "busy") in str(view.rich(card).to_dict())


def test_device_alert_mapping_keeps_levels_and_ignores_unrelated_alarms():
    a = unraid.build(unraid.parse_ini(VAR)[""], unraid.parse_ini(DISKS))
    a.disks[0].device = "sdf"
    rows = [(panel.WARNING, "sdf 10min_disk_backlog 8000 ms"),
            (panel.CRITICAL, "/dev/sdf read_errors 12"), (panel.WARNING, "nvme2n1 temperature 70 C"),
            (panel.WARNING, "cpu load 95%"), (panel.WARNING, "not_sdf alarm 1")]
    assert panel.map_disk_alerts(a, rows) == {
        rows[0][1]: (panel.WARNING, f"{a.disks[0].name} 10min_disk_backlog 8000 ms"),
        rows[1][1]: (panel.CRITICAL, f"{a.disks[0].name} read_errors 12"),
        rows[2][1]: (panel.WARNING, "nvme2n1 temperature 70 C")}


@pytest.mark.parametrize("counts,expected", [([], []), ([(0, "repo")], []),
    ([(1, "repo")], [(panel.MORE, "1 additional repo not visible")]),
    ([(3, "run"), (0, "repo")], [(panel.MORE, "3 additional runs not visible")]),
    ([(2, "warning"), (1, "error")], [(panel.MORE, "2 additional warnings and 1 error not visible")])])
def test_more_uses_one_shared_phrase_and_omits_empty_summaries(counts, expected):
    assert panel.more(counts) == expected


def test_table_keeps_the_more_marker_without_adding_status_slots_to_fine_rows():
    rows = [(panel.FINE, "Repo  State"), (panel.FINE, "owner/repo  pass"),
            *panel.more([(3, "repo")])]
    assert panel.table(rows).splitlines() == ["```", "Repo  State", "owner/repo  pass",
                                              "+ 3 additional repos not visible", "```"]
    warned = [(panel.FINE, "Repo  State"), (panel.WARNING, "owner/repo  offline"), rows[-1]]
    assert panel.table(warned).splitlines()[1:4] == [f"{OK} Repo  State", f"{WARN} owner/repo  offline",
                                                    "+ 3 additional repos not visible"]


def test_long_drive_names_are_clipped_for_display_and_huge_error_counts_capped():
    a = unraid.build({"mdState": "STARTED"}, {})
    long = "nvme_appdata_mirror_pool_for_containers"
    a.disks.append(unraid.Disk(long, "Cache", "nvme0n1", "DISK_OK", None, 100, 50, 4294967295))
    a.disks.append(unraid.Disk(long[:12] + "x", "Cache", "nvme1n1", "DISK_OK", None, 100, 50, 0))
    section = panel.array_section(a, io={"nvme0n1": (0.0, 0.0, 100.0)})
    table = section.parts[1].rows
    assert table[1][1][0] == panel.drive(long) == "nvme_appda" + panel.ELL
    assert table[1][1][7] == "99,999+" and table[-1][1][7] == "99,999+"
    issues = [text for _, text in section.issues]
    assert f"{long} 99,999+ read/write errors" in issues and f"{long} 100% busy" in issues
    assert panel.drive("disk1") == "disk1" and panel.count(99_999) == "99999"


def test_degraded_stack_lists_each_down_container_on_its_own_line():
    def ct(name, state="exited", code=1):
        return Container(name, name, state, f"Exited ({code}) 1 hour ago", "media", ("bridge",))
    members = [ct("sonarr"), ct("radarr", code=137), Container("web", "web", "running", "Up", "media", ())]
    issues = panel.docker_section(members, None).issues
    assert issues == [(panel.CRITICAL, "media degraded:\nsonarr exited (1)\nradarr exited (137)")]
    rows = view.parts(panel.docker_section(members, None))[-1].rows
    assert [lv for lv, _ in rows] == [panel.CRITICAL, panel.CONT, panel.CONT]


def test_degraded_stack_lists_at_most_five_containers():
    members = [Container(f"c{i}", f"c{i}", "exited", "Exited (1) 1 hour ago", "big", ()) for i in range(8)]
    members.append(Container("up", "up", "running", "Up 1 hour", "big", ()))
    (_, text), = panel.docker_section(members, None).issues
    assert text.split("\n") == ["big degraded:", *(f"c{i} exited (1)" for i in range(5)), "...and 3 more"]
