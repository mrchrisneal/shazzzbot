import json
import os
from datetime import UTC, datetime
from importlib.resources import files
from types import SimpleNamespace

import pytest

from shazzzbot.helper.platforms import task_parsing, tasks
from tests.test_helper_support import CRON, MONITOR, source_paths, tree


def test_smart_warnings_skip_acknowledged():
    assert task_parsing.smart_warnings(MONITOR) == ["cache2", "disk2", "disk2"]
    assert task_parsing.smart_warnings("") == []


def test_parse_cron_names_and_runparts():
    jobs, runparts = task_parsing.parse_cron(CRON)
    assert jobs == [{"name": "Mover", "cron": "0 3 * * *"}, {"name": "gp-mirrors", "cron": "*/5 * * * *"},
                    {"name": "fstrim", "cron": "15 2 * * *"}]
    assert runparts == {"hourly": "0 * * * *", "weekly": "30 4 * * 0"}


def test_collect_reads_every_source(tmp_path, monkeypatch):
    tree(tmp_path, {"isNewer": True, "version": "7.3.4"})
    data = tasks.Collector(source_paths(tmp_path)).collect()
    names = [j["name"] for j in data["jobs"]]
    assert names == ["Mover", "gp-mirrors", "fstrim", "xfs_defrag", "nightly"]
    assert data["smart"] == ["cache2", "disk2", "disk2"] and data["os_update"] == "7.3.4"
    assert list(data["running"]) == ["gp-mirrors"] and list(data["finished"]) == ["old"]


def test_collect_tolerates_missing_sources(tmp_path, monkeypatch):
    data = tasks.Collector(source_paths(tmp_path)).collect()
    assert data["jobs"] == [] and data["os_update"] == ""
    tree(tmp_path, {"isNewer": "yes", "version": "7.3.4"})
    assert tasks.Collector(source_paths(tmp_path)).collect()["os_update"] == ""


def test_collect_exports_only_completed_durations_from_logs_and_history(tmp_path, monkeypatch):
    tree(tmp_path, {})
    (tmp_path / "localtime").write_bytes(files("tzdata.zoneinfo").joinpath("UTC").read_bytes())
    script = tmp_path / "user.scripts.state" / "tmpScripts" / "old"
    script.mkdir(parents=True)
    (script / "log.txt").write_text("Script Starting Nov 09, 2026  12:00.00\nsecret output\n"
                                    "Script Finished Nov 09, 2026  12:00.45\n")
    ended = datetime(2026, 11, 9, 12, 0, 45, tzinfo=UTC).timestamp()
    os.utime(tmp_path / "user.scripts.state" / "finished" / "old", (ended, ended))
    backup = tmp_path / "appdata.backup"
    backup.mkdir()
    (backup / "ab.log").write_text("[09.11.2026 12:00:00][info][Main] WELCOME TO APPDATA.BACKUP!! :D\n"
                                   "[09.11.2026 12:12:00][info][Main] "
                                   "DONE! Thanks for using this plugin and have a safe day ;)\n")
    parity = tmp_path / "parity"
    parity.mkdir()
    (parity / "parity-checks.log").write_text("Nov 09 12:00:00|3900|100 MB/s|0|0|check P\n")
    data = tasks.Collector(source_paths(tmp_path)).collect()
    assert {n: r["seconds"] for n, r in data["durations"].items()} == {
        "old": 45.0, "Appdata.Backup": 720.0, "Parity check": 3900.0}
    assert "secret" not in json.dumps(data)
    os.utime(tmp_path / "user.scripts.state" / "finished" / "old", (ended + 60, ended + 60))
    assert tasks.Collector(source_paths(tmp_path)).completed()["old"]["seconds"] is None
    os.utime(tmp_path / "user.scripts.state" / "finished" / "old", (ended, ended))
    (backup / "running").touch()
    assert "Appdata.Backup" not in tasks.Collector(source_paths(tmp_path)).completed()
    (script / "log.txt").write_text("Script Starting Nov 09, 2026  12:00.00\n")
    assert tasks.Collector(source_paths(tmp_path)).completed()["old"]["seconds"] is None
    (backup / "running").unlink()
    (backup / "ab.log").write_text("[09.11.2026 12:00:00][info][Main] WELCOME TO APPDATA.BACKUP!! :D\n")
    assert "Appdata.Backup" not in tasks.Collector(source_paths(tmp_path)).completed()
    (tmp_path / "localtime").write_bytes(b"not timezone data")
    assert tasks.Collector(source_paths(tmp_path)).completed()["Parity check"]["seconds"] == 3900
    assert tasks.Collector(source_paths(tmp_path)).completed()["old"]["seconds"] is None


def test_duration_log_reads_are_bounded_regular_files_and_never_follow_symlinks(tmp_path, monkeypatch):
    collector = tasks.Collector({name: tmp_path / name for name in ("missing", "directory", "pipe", "link", "log")})
    assert collector._log("missing") == ("", 0.0)
    (tmp_path / "directory").mkdir()
    assert collector._log("directory") == ("", 0.0)
    os.mkfifo(tmp_path / "pipe")
    assert collector._log("pipe") == ("", 0.0)
    path = tmp_path / "log"
    path.write_bytes(b"private")
    (tmp_path / "link").symlink_to(path)
    assert collector._log("link") == ("", 0.0)
    monkeypatch.setattr(tasks, "LOG_LIMIT", 4)
    assert collector._log("log") == ("", 0.0)
    info = path.stat()
    monkeypatch.setattr(os, "fstat", lambda _: SimpleNamespace(st_mode=info.st_mode, st_size=0,
                                                                      st_mtime=info.st_mtime))
    assert collector._log("log") == ("", 0.0)


@pytest.mark.parametrize("frequency,cron", [("hourly", "15 * * * *"), ("daily", "20 3 * * *"),
                                          ("weekly", "25 4 * * 1"), ("monthly", "30 5 1 * *")])
def test_collect_expands_executable_cron_directories_without_reading_scripts(tmp_path, monkeypatch, frequency, cron):
    root = tmp_path / "cron.d"
    root.mkdir()
    (root / "root").write_text(f"{cron} root /usr/bin/run-parts /etc/cron.{frequency}\n")
    for name in ("hourly", "daily", "weekly", "monthly"):
        folder = tmp_path / f"cron.{name}"
        folder.mkdir()
        executable = folder / "maintenance.sh"
        executable.write_text("private arguments must not be exported\n")
        executable.chmod(0o755)
        (folder / "not-executable").write_text("skip")
        (folder / "not-executable").chmod(0o644)
        (folder / "directory").mkdir()
        stub = folder / "user.script.start.backup"
        stub.write_text("skip")
        stub.chmod(0o755)
    data = tasks.Collector(source_paths(tmp_path)).collect()
    assert data["jobs"] == [{"name": "maintenance.sh", "cron": cron}]
    assert "private arguments" not in json.dumps(data)


def test_runparts_stubs_do_not_duplicate_user_scripts(tmp_path, monkeypatch):
    tree(tmp_path, {})
    folder = tmp_path / "cron.weekly"
    folder.mkdir()
    stub = folder / "user.script.start.xfs_defrag"
    stub.touch(mode=0o755)
    names = [job["name"] for job in tasks.Collector(source_paths(tmp_path)).collect()["jobs"]]
    assert names.count("xfs_defrag") == 1 and stub.name not in names


def test_executable_cron_files_without_a_runparts_schedule_are_skipped(tmp_path, monkeypatch):
    folder = tmp_path / "cron.daily"
    folder.mkdir()
    (folder / "backup").touch(mode=0o755)
    assert tasks.Collector(source_paths(tmp_path)).collect()["jobs"] == []

