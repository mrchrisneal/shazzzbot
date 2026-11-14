from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from shazzzbot import jobs as tasks
from shazzzbot import view
from shazzzbot.docker import Container
from shazzzbot.glyphs import WARN
from shazzzbot.helper.platforms import task_parsing
from shazzzbot.unraid import Array, Parity

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 11, 9, 20, 0, tzinfo=TZ)  # a Monday
CRON = """# Generated parity check schedule:
30 6 * * 3 /usr/local/sbin/mdcmd check NOCORRECT &> /dev/null
# Appdata.Backup cron settings
30 6 * * 1 php /usr/local/emhttp/plugins/appdata.backup/scripts/backup.php > /dev/null 2>&1
# Generated cron schedule for user.scripts
*/5 * * * * /usr/local/emhttp/plugins/user.scripts/startCustom.php /boot/config/plugins/user.scripts/scripts/hc/script
# Job for VM Backup plugin default:
30 6 * * 5 /usr/local/emhttp/plugins/vmbackup/runscript.php run_backup default > /dev/null 2>&1
* * * * * bash /etc/hetrixtools/hetrixtools_agent.sh >> /etc/hetrixtools/hetrixtools_cron.log 2>&1
10 6 * * 0 /usr/bin/run-parts /etc/cron.weekly 1> /dev/null
"""


def test_parse_cron_names_jobs_and_keeps_no_commands():
    jobs, runparts = task_parsing.parse_cron(CRON)
    assert [j["name"] for j in jobs] == ["Parity check", "Appdata.Backup", "hc", "VM Backup plugin default",
                                         "hetrixtools_agent.sh"]
    assert runparts == {"weekly": "10 6 * * 0"} and "mdcmd" not in str(jobs)


def test_next_run():
    assert tasks.next_run("30 6 * * 3", NOW) == datetime(2026, 11, 11, 6, 30, tzinfo=TZ)
    assert tasks.next_run("*/5 * * * *", NOW) == NOW + timedelta(minutes=5)
    assert tasks.next_run("0 9 1 * 1", NOW) == datetime(2026, 11, 16, 9, 0, tzinfo=TZ)  # dom OR dow
    assert tasks.next_run("10 0 1 * *", NOW) == datetime(2026, 12, 1, 0, 10, tzinfo=TZ)
    assert tasks.next_run("0 0 * * 7", NOW) == datetime(2026, 11, 15, 0, 0, tzinfo=TZ)


def test_upcoming_drops_frequent_jobs_and_sorts():
    jobs, _ = task_parsing.parse_cron(CRON)
    assert [n for _, n in tasks.upcoming(jobs, NOW)] == ["Parity check", "VM Backup plugin default", "Appdata.Backup"]


class Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz)


def test_embed_rows_fit_and_flag_stale_data(monkeypatch):
    monkeypatch.setattr(tasks, "datetime", Frozen)
    jobs, _ = task_parsing.parse_cron(CRON)
    p = Parity("check P", True, True, 25.0, 0.0, int(NOW.timestamp()) - 7200, 0, 0, 0)
    a = Array("t", "7.3.4", "STARTED", False, p)
    sec = tasks.tasks_section({"generated": 0, "jobs": jobs, "running": {"hc": NOW.timestamp()}}, a, TZ, [], {})
    assert f"{WARN} needs the shazzzbot helper" in view.rich(sec).description
    rows = [r for r in view.text(sec).description.splitlines() if not r.startswith(("**", "`"))]
    assert any("Parity - 25% paused" in r for r in rows) and all(len(r) <= 53 for r in rows), rows
    hidden = view.rich(tasks.tasks_section(
        {"generated": NOW.timestamp(), "jobs": jobs, "running": {"hc": 1.0}}, None, TZ, ["Parity*", "hc"], {}))
    assert "Parity" not in hidden.fields[1].value and "hc" not in hidden.fields[0].value
    assert tasks._rows([(1800, "SSD trim", 0), (None, "Mover", 0)]) == [(0, ("30m", "SSD trim")), (0, ("", "Mover"))]
    assert tasks.dur(59) == "0m" and tasks.dur(3 * 3600 + 1200) == "3h20m" and tasks.dur(2 * 86400 + 3600) == "2d01h"


def test_next_run_ranges_and_impossible_dates():
    assert tasks.next_run("0 9-17/4 * * *", NOW) == datetime(2026, 11, 10, 9, 0, tzinfo=TZ)
    assert tasks.next_run("0 0 31 2 *", NOW) is None


def test_upcoming_skips_bad_input():
    assert tasks.upcoming([{"name": "x", "cron": "61 * * * *"}, {"name": "y"}], NOW) == []


def test_embed_shows_mover_and_an_unavailable_helper():
    a = Array("t", "7.3.4", "STARTED", True, Parity("", False, False, 0.0, 0.0, 0, 0, 0, 0))
    e = view.rich(tasks.tasks_section(None, a, TZ, [], {}))
    assert f"{WARN} needs the shazzzbot helper" in e.description and "Mover" in e.fields[0].value
    assert view.rich(tasks.tasks_section({"generated": datetime.now().timestamp()}, a, TZ, [], {})).description is None


def test_task_duration_column_in_text_rich_and_image_with_unknown_values_blank(monkeypatch):
    from unittest.mock import patch

    monkeypatch.setattr(tasks, "datetime", Frozen)
    data = {"generated": NOW.timestamp(), "jobs": [{"name": n, "cron": "0 0 * * *"}
                                                  for n in ("backup", "unknown", "long")],
            "running": {"backup": NOW.timestamp() - 120}}
    section = tasks.tasks_section(data, None, TZ, [], {"backup": "Backup"}, last={"backup": 45, "long": 3900})
    running = section.parts[1].rows
    soon = {cells[1]: cells[-1] for _, cells in section.parts[2].rows}
    assert running == [(0, ("2m", "Backup")), (tasks.NOTE, ("", "Last took 45s"))]
    assert soon == {"Backup": "(last took 45s)", "Long": "(last took 1h05m)", "Unknown": ""}
    text = view.text(section).description
    rich = str(view.rich(section).to_dict())
    for value in ("(last took 45s)", "(last took 1h05m)"):
        assert value in text and value in rich
    assert all(len(line) <= 53 for line in text.splitlines())
    assert not any("last took" in line for line in text.splitlines() if "Unknown" in line)
    with patch.object(view, "_rows", wraps=view._rows) as draw:
        assert view.image(section).startswith(b"\x89PNG")
    assert any(call.args[1].rows == section.parts[2].rows for call in draw.call_args_list)
    unknown = tasks.tasks_section(data, None, TZ, [], {}, last={})
    assert "last took" not in view.text(unknown).description
    assert "last took" not in str(view.rich(unknown).to_dict())
    assert view.image(unknown).startswith(b"\x89PNG")


def test_last_duration_follows_renames_and_avoids_ambiguous_names():
    assert tasks._last_names({"a": 12, "b": 45, "c": 60}, {"a": "same", "b": "same"}) == {"C": 60}


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("rename", [{}, {"Parity check": "Array check"}])
def test_running_parity_shows_its_last_completed_duration(monkeypatch, paused, rename):
    monkeypatch.setattr(tasks, "datetime", Frozen)
    a = Array("t", "7.3.4", "STARTED", False, Parity("check P", True, paused, 25, 0, int(NOW.timestamp()), 0, 0, 0))
    section = tasks.tasks_section(None, a, TZ, [], rename, last={"Parity check": 720})
    assert section.parts[1].rows[1] == (tasks.NOTE, ("", "Last took 12m"))


@pytest.mark.parametrize("command,cron", [
    ('/watchtower --schedule "0 0 22 * * *" --cleanup', "0 22 * * *"),
    ("/watchtower --schedule '0 22 * * *' app", "0 22 * * *"),
    ("/watchtower --schedule 0 0 22 * * * --cleanup", "0 22 * * *"),
    ("/watchtower --schedule 0 22 * * * --cleanup", "0 22 * * *"),
    ("/watchtower --schedule 0 0 22 * * * app", "0 22 * * *"),
    ('/watchtower --schedule="30 0 22 * * *"', "0 22 * * *"),
    ("/watchtower --schedule=0 0 22 * * * -c", "0 22 * * *"),
    ('/watchtower --schedule "0 */15 1-3 * JAN,MAR MON-FRI"', "*/15 1-3 * 1,3 1-5"),
    ("/watchtower --schedule 0 0 22 * * sun", "0 22 * * 0"),
    ("/watchtower --schedule '0 22 * * 7'", "0 22 * * 7")])
def test_watchtower_schedules_are_normalized_and_keep_only_name_and_cron(command, cron):
    container = Container("w", "auto-updater", "running", "Up", image="registry/WatchTower:latest",
                          command=command + " --http-api-token private-secret")
    assert tasks.container_jobs([container]) == [{"name": "auto-updater", "cron": cron}]
    assert "private-secret" not in str(tasks.container_jobs([container]))


@pytest.mark.parametrize("command", [
    "", "/watchtower --scheduler '0 22 * * *'", "/watchtower --schedule", "/watchtower --schedule=",
    "/watchtower --schedule ''", "/watchtower --schedule 'unterminated", "x" * 16385,
    "/watchtower --schedule '0 22 * * *' --schedule '0 23 * * *'", "/watchtower --schedule '0 22 * *'",
    "/watchtower --schedule '0 0 22 * * * 2026'", "/watchtower --schedule 0 0 22 * * * 2026",
    "/watchtower --schedule 0 0 22 * * * MON", "/watchtower --schedule '60 0 22 * * *'",
    "/watchtower --schedule '*/0 22 * * *'", "/watchtower --schedule '1-0 22 * * *'",
    "/watchtower --schedule '0 22 0 * *'", "/watchtower --schedule '0 25 * * *'",
    "/watchtower --schedule '0 22 * 13 *'", "/watchtower --schedule '0 22 * * 8'",
    "/watchtower --schedule '0 22 * * BAD'", "/watchtower --schedule '0 22 * * *;reboot'",
    "/watchtower --schedule '0-999999999999999999 22 * * *'", "/watchtower --schedule -1 22 * * *"])
def test_invalid_or_ambiguous_container_schedules_fail_closed(command):
    container = Container("w", "updater", "running", "Up", image="watchtower", command=command)
    assert tasks.container_jobs([container]) == []


def test_non_watchtower_containers_are_not_guessed_from_names_or_commands():
    container = Container("w", "watchtower", "running", "Up", image="other/updater",
                          command="/watchtower --schedule '0 22 * * *'")
    assert tasks.container_jobs([container]) == []


@pytest.mark.parametrize("ignore", [[], ["auto-*"], ["Container maintenance"]])
def test_file_and_container_jobs_share_sorting_ignore_and_rename(ignore):
    data = {"jobs": [{"name": "health_check.sh", "cron": "0 21 * * *"}]}
    container = Container("w", "auto-updater", "running", "Up", image="watchtower",
                          command="/watchtower --schedule '0 0 22 * * *'")
    rename = {"health_check.sh": "Host maintenance", "auto-updater": "Container maintenance"}
    expected = [(3600, "Host maintenance")] + ([] if ignore else [(7200, "Container maintenance")])
    assert tasks.soonest(data, NOW, ignore, rename, [container]) == expected
    assert tasks.soonest(data, NOW, ["health_*", "Container maintenance"], rename, [container]) == []


def test_container_jobs_show_in_all_modes_without_masking_a_missing_helper(monkeypatch):
    monkeypatch.setattr(tasks, "datetime", Frozen)
    container = Container("w", "auto-updater", "running", "Up", image="watchtower",
                          command="/watchtower --schedule '0 0 22 * * *'")
    section = tasks.tasks_section(None, None, TZ, [], {"auto-updater": "Container maintenance"}, containers=[container])
    assert section.issues == [(1, "needs the shazzzbot helper")]
    assert "Container maintenance" in view.text(section).description
    assert "Container maintenance" in str(view.rich(section).to_dict())
    assert view.image(section).startswith(b"\x89PNG")


def test_missing_helper_banner_in_every_view_and_stale_schedules_are_not_reused(monkeypatch):
    from unittest.mock import patch

    monkeypatch.setattr(tasks, "datetime", Frozen)
    old = {"generated": NOW.timestamp() - tasks.STALE - 1,
           "jobs": [{"name": "stale scheduled job", "cron": "0 2 * * *"}], "running": {"stale running job": 1}}
    for data, required in ((None, False), (None, True), (old, True)):
        section = tasks.tasks_section(data, None, TZ, [], {}, required=required)
        assert section.issues == ([(1, "needs the shazzzbot helper")] if required else [])
        for rendered in (view.text(section).description, str(view.rich(section).to_dict())):
            assert "needs the shazzzbot helper" in rendered and "stale" not in rendered
        with patch.object(view, "_rows", wraps=view._rows) as draw:
            assert view.image(section).startswith(b"\x89PNG")
        assert any("needs the shazzzbot helper" in str(call.args[1].rows) for call in draw.call_args_list)


def test_running_last_duration_is_grey_under_the_name_in_text_image_and_italic_in_embed(monkeypatch):
    import re
    import unicodedata

    from PIL import ImageDraw

    from shazzzbot import charts, panel

    monkeypatch.setattr(tasks, "datetime", Frozen)
    name = "A maintenance task with the longest supported display name"
    data = {"generated": NOW.timestamp(), "running": {name: NOW.timestamp() - 90 * 86400, "Unknown": NOW.timestamp()}}
    section = tasks.tasks_section(data, None, TZ, [], {}, last={name: 14 * 3600 + 12 * 60})
    text = view.text(section).description
    plain = [re.sub("\x1b\\[[0-9;]*m", "", line) for line in text.splitlines()]
    note = next(line for line in plain if "Last took" in line)
    task = next(line for line in plain if "A maintenance" in line)
    def width(text):
        return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    assert width(note[:note.index("Last took")]) == width(task[:task.index("A maintenance")])
    assert all(len(line) <= panel.WIDTH + 3 for line in plain)
    assert "\x1b[" not in next(line for line in text.splitlines() if "Last took" in line)  # ANSI grey is unreadable
    rich = view.rich(section)
    running = next(field.value for field in rich.fields if field.name == "Running (elapsed)")
    shown = panel.clip(name, 30)
    assert f"{WARN} **{shown}**" in running and "\n*Last took 14h12m*\n" in running
    assert running.count("Last took") == 1 and "Unknown" in running
    drawn = []
    original = ImageDraw.ImageDraw.text
    def capture(draw, xy, text, **kwargs):
        drawn.append((xy, text, kwargs))
        return original(draw, xy, text, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    assert view.image(section).startswith(b"\x89PNG")
    task_xy = next(xy for xy, text, _ in drawn if text == shown)
    note_xy, _, style = next(row for row in drawn if row[1] == "Last took 14h12m")
    assert note_xy[0] == task_xy[0] and note_xy[1] - task_xy[1] == view.ROW_H * view.K
    assert style["fill"] == charts.DIM and "Italic" in style["font"].getname()[1]
    assert sum(text.startswith("Last took") for _, text, _ in drawn) == 1


def test_last_duration_continuation_also_aligns_with_a_warning_status_slot(monkeypatch):
    import re
    import unicodedata

    monkeypatch.setattr(tasks, "datetime", Frozen)
    a = Array("host", "7", "STARTED", False,
              Parity("check P", True, True, 25, 0, int(NOW.timestamp()) - 7200, 0, 0, 0))
    section = tasks.tasks_section(None, a, TZ, [], {}, last={"Parity check": 720})
    lines = [re.sub("\x1b\\[[0-9;]*m", "", line) for line in view.text(section).description.splitlines()]
    main = next(line for line in lines if "Parity - 25% paused" in line)
    note = next(line for line in lines if "Last took" in line)
    def width(text):
        return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    assert width(main[:main.index("Parity")]) == width(note[:note.index("Last took")])
    value = next(f.value for f in view.rich(section).fields if f.name == "Running (elapsed)")
    assert f"{WARN} **Parity - 25% paused**" in value and "\n*Last took 12m*" in value


def test_task_names_are_clipped_to_thirty_characters_but_keep_their_last_duration(monkeypatch):
    from shazzzbot import panel

    monkeypatch.setattr(tasks, "datetime", Frozen)
    name = "Offsite copy of family photos and scanned documents"
    data = {"generated": NOW.timestamp(), "running": {name: NOW.timestamp() - 60},
            "jobs": [{"name": name, "cron": "0 22 * * *"}]}
    section = tasks.tasks_section(data, None, TZ, [], {}, last={name: 600})
    clipped = name[:29].rstrip() + panel.ELL
    running, upcoming = section.parts[1].rows, section.parts[2].rows
    assert running[0][1][1] == clipped and running[1][1][1] == "Last took 10m"
    assert upcoming[0][1][1] == clipped and upcoming[0][1][2] == "(last took 10m)"
