import re
from unittest.mock import patch

import pytest

from shazzzbot import jobs, panel, sysimage, taskstyle, view
from shazzzbot.helper.platforms import task_kinds
from shazzzbot.unraid import Array, Parity
from tests.test_config import BASE, load
from tests.test_jobs import NOW, TZ, Frozen


@pytest.mark.parametrize("trigger", ["elapsed", "last", "parity", "file_integrity", "appdata_backup_plugin",
                                     "appdata_backup_script", "short", "boundary", "disabled"])
def test_running_task_colour_is_shared_by_every_view_and_does_not_create_alerts(monkeypatch, trigger):
    monkeypatch.setattr(jobs, "datetime", Frozen)
    ts = NOW.timestamp()
    data = {"generated": ts, "running": {"light": ts - 60}, "jobs": [], "kinds": {}}
    array, procs, last, style = None, frozenset(), {}, taskstyle.Settings()
    if trigger == "elapsed":
        data["running"]["light"] = ts - 3601
    elif trigger == "last":
        last = {"light": 3601}
    elif trigger == "parity":
        data["running"] = {}
        array = Array("host", "7", "STARTED", False, Parity("check", True, False, 5, 0, int(ts) - 60, 0, 0, 0))
        last = {"Parity check": 7200}
    elif trigger == "file_integrity":
        data["running"] = {}
        procs = frozenset({"bunker"})
    elif trigger.startswith("appdata"):
        data["kinds"] = {"light": trigger}
    elif trigger == "boundary":
        data["running"]["light"] = ts - 3600
        last = {"light": 3600}
    elif trigger == "disabled":
        data["running"]["light"] = ts - 7200
        data["kinds"] = {"light": "appdata_backup_script"}
        style = taskstyle.Settings(0, ())
    hot = trigger not in {"short", "boundary", "disabled"}
    sec = jobs.tasks_section(data, array, TZ, [], {}, procs, last=last, style=style)
    part = next(p for p in sec.parts if p.heading == "Running (elapsed)")
    name = part.rows[0][1][1]
    assert part.rows[0][0] == panel.FINE
    assert part.colours[0] == (panel.WARNING if hot else panel.FINE,) * 2
    assert sec.issues == []
    running = jobs.active(data, array, procs, ts, [], {})
    colours = jobs.colours(data, array, procs, ts, [], {}, last, style)
    system = sysimage.status_rows([], running, [], None, False, colours)
    assert system[0].text == "none" and system[0].level == panel.FINE
    assert system[1].dot == panel.FINE and not system[1].issues
    system_sec = sysimage.section("System", "", "", system, None)
    assert system_sec.issues == []
    for section in (sec, system_sec):
        text = view.text(section).description
        rich = str(view.rich(section).to_dict())
        assert (panel.ANSI[panel.WARNING] in text) == hot
        assert "\\x1b" not in rich and "`" not in rich
        assert name in text and name in rich
        assert (panel.WARN in text) == hot and (panel.WARN in rich) == hot
    if last:
        assert "Last took" in view.text(sec).description
        assert "*Last took" in str(view.rich(sec).to_dict())
        assert all(not any(colour) for colour, (lv, _) in zip(part.colours, part.rows) if lv == panel.NOTE)
    with patch.object(view, "ImageDraw") as draw:
        draw.Draw.return_value = actual = view.ImageDraw.Draw.return_value
        actual.textlength.side_effect = lambda text, font: font.getlength(text)
        view.image(sec)
        name_calls = [call for call in actual.text.call_args_list if call.args[1] == name]
        assert name_calls and name_calls[0].kwargs["fill"] == (view.YELLOW if hot else view.TEXT)
    with patch.object(sysimage, "_text", wraps=sysimage._text) as spans:
        sysimage.render(system, now=ts)
    if hot:
        assert any(call.args[6] == sysimage.YELLOW for call in spans.call_args_list if len(call.args) > 6)


@pytest.mark.parametrize("text,minutes,kinds", [('', 60, task_kinds.HEAVY_KINDS),
                                               ('long_running_minutes = false', 0, task_kinds.HEAVY_KINDS),
                                               ('long_running_minutes = 0\nheavy_kinds = []', 0, ()),
                                               ('long_running_minutes = 15.5\nheavy_kinds = ["parity"]',
                                                15.5, ("parity",))])
def test_task_colour_settings_load_with_defaults_and_explicit_disabling(tmp_path, monkeypatch, text, minutes, kinds):
    cfg = load(tmp_path, monkeypatch, BASE + "[tasks]\n" + text)
    assert cfg.tasks == taskstyle.Settings(minutes, kinds)


@pytest.mark.parametrize("value", [[], {"unknown": 1}, {"long_running_minutes": True},
                                   {"long_running_minutes": "60"}, {"long_running_minutes": -1},
                                   {"long_running_minutes": float("nan")}, {"long_running_minutes": float("inf")},
                                   {"long_running_minutes": 10**400},
                                   {"heavy_kinds": "parity"}, {"heavy_kinds": [1]},
                                   {"heavy_kinds": ["parity", "parity"]}, {"heavy_kinds": ["guess"]}])
def test_task_colour_settings_fail_closed(value):
    with pytest.raises(SystemExit, match="tasks") as error:
        taskstyle.validate(value)
    if isinstance(value, dict) and isinstance(value.get("heavy_kinds"), list) and any(
            isinstance(kind, str) and kind not in task_kinds.HEAVY_KINDS for kind in value["heavy_kinds"]):
        assert all(kind in str(error.value) for kind in task_kinds.HEAVY_KINDS)


def test_long_running_threshold_only_compares_observed_elapsed_and_previous_duration():
    setting = taskstyle.Settings(1, ())
    assert not setting.yellow(None, None, "")
    assert not setting.yellow(60, 60, "")
    assert setting.yellow(61, None, "") and setting.yellow(None, 61, "")
    assert not taskstyle.Settings(0, ()).yellow(99999, 99999, "parity")


def test_task_style_defaults_and_unknown_field_suggestions_follow_schema():
    assert taskstyle.validate({}) == taskstyle.Settings()
    assert taskstyle.Settings().long_running_minutes == taskstyle.schema.default("tasks", "long_running_minutes")
    assert taskstyle.Settings().heavy_kinds == tuple(taskstyle.schema.default("tasks", "heavy_kinds"))
    with pytest.raises(SystemExit) as error:
        taskstyle.validate({"long_runing_minutes": 5})
    assert "long_runing_minutes" in str(error.value) and "long_running_minutes" in str(error.value)


def test_source_kinds_use_plugin_commands_and_never_display_names():
    for prefix in ("appdata.backup", "ca.backup2"):
        body = f'php "/usr/local/emhttp/plugins/{prefix}/scripts/backup.php"'
        assert task_kinds.script_kind(body) == "appdata_backup_script"
    assert not task_kinds.script_kind('echo "Appdata Backup"')
    assert not task_kinds.script_kind('/tmp/appdata.backup/scripts/backup.php')
    assert not task_kinds.script_kind('/usr/local/emhttp/plugins/appdata.backup/scripts/backup.php.untrusted')


def test_colour_only_running_names_obey_worst_case_width_and_keep_last_note_muted(monkeypatch):
    monkeypatch.setattr(jobs, "datetime", Frozen)
    name = "A very long running task name " * 8
    data = {"generated": NOW.timestamp(), "running": {name: NOW.timestamp() - 7200}, "jobs": []}
    section = jobs.tasks_section(data, None, TZ, [], {}, last={name: 9000})
    text = view.text(section).description
    assert "Last took 2h30m" in text
    assert all(len(re.sub(r"\x1b\[[0-9;]*m", "", line)) <= panel.WIDTH + 3
               for line in text.splitlines() if not line.startswith(("```", "**")))
    with patch.object(view, "_rows", wraps=view._rows) as rows:
        assert view.image(section).startswith(b"\x89PNG")
    assert any(panel.clip(name, 30) in str(call.args[1].rows) for call in rows.call_args_list)


def test_collector_labels_heavy_sources_without_returning_script_bodies(tmp_path):
    from shazzzbot.helper.platforms import tasks
    from tests.test_helper_support import source_paths

    sources = source_paths(tmp_path)
    running = sources["user.scripts.state"] / "running"
    running.mkdir(parents=True)
    for name, body in (("ordinary-name", 'php /usr/local/emhttp/plugins/appdata.backup/scripts/backup.php'),
                       ("Appdata backup", 'echo "ordinary task"')):
        (running / name).touch()
        folder = sources["user.scripts"] / "scripts" / name
        folder.mkdir(parents=True)
        (folder / "script").write_text(body + '\nPASSWORD="must-not-leave-helper"')
    sources["appdata.backup"].mkdir()
    (sources["appdata.backup"] / "running").touch()
    data = tasks.Collector(sources, strict=True).collect()
    assert data["kinds"] == {"ordinary-name": "appdata_backup_script", "Appdata.Backup": "appdata_backup_plugin"}
    assert "must-not-leave-helper" not in str(data)
    assert "Appdata backup" not in data["kinds"]


@pytest.mark.parametrize("kinds", [[], {"absent": "parity"}, {"light": "unknown"}, {"light": 1}])
def test_helper_client_refuses_invalid_task_kind_metadata(kinds, monkeypatch):
    from shazzzbot import helperclient

    monkeypatch.setattr(helperclient.time, "time", lambda: 100)
    data = {"generated": 100, "jobs": [], "smart": [], "os_update": "", "running": {"light": 99},
            "finished": {}, "durations": {}, "kinds": kinds}
    with pytest.raises(helperclient.Error, match="task kinds"):
        helperclient.tasks_data(data)
    data["kinds"] = {"light": "appdata_backup_script"}
    assert helperclient.tasks_data(data) == data


def test_system_text_expands_values_without_moving_colour_to_another_row():
    plain = panel.Part([(0, ("Load", "1  2  3")), (0, ("Tasks", "1 task running:  Work"))], grid=True,
                       colours=[(), (0, 1)])
    split = view._system_text(plain)
    assert split.colours == [(0,), (0,), (0,), (0,), (1,)]
    lines = view.text(panel.Section("system", "System", [plain])).description
    assert panel.ANSI[panel.WARNING] + "Work" in lines
    assert panel.ANSI[panel.WARNING] + "1  " not in lines
