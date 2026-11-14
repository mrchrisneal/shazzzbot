import gc
import os
import stat
from importlib.resources import files

import pytest

from shazzzbot.helper import files as trusted
from shazzzbot.helper.platforms import tasks, unraid


@pytest.fixture
def host_layout(tmp_path):
    root = tmp_path / "host"
    paths = {name: root / path.relative_to("/") for name, path in unraid.TASK_PATHS.items()}
    for source in ("cron.d", "cron.daily", "crontabs", "user.scripts.state", "parity"):
        paths[source].mkdir(parents=True)
    (root / "tmp").chmod(0o1777)
    paths["user.scripts.state"].chmod(0o755)
    paths["crontabs"].chmod(0o750)
    paths["parity"].chmod(0o700)
    (paths["cron.d"] / "root").write_text("0 4 * * * run-parts /etc/cron.daily\n")
    (paths["crontabs"] / "root").write_text("0 5 * * * /usr/bin/fstrim\n")
    target = root / "usr/local/php/unraid-tailscale-utils/daily.sh"
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o755)
    (paths["cron.daily"] / "tailscale-daily").symlink_to(target)
    zone = root / "usr/share/zoneinfo/Etc/UTC"
    zone.parent.mkdir(parents=True)
    zone.write_bytes(files("tzdata.zoneinfo").joinpath("Etc", "UTC").read_bytes())
    paths["localtime"].symlink_to(zone)
    return root, paths, target


def test_real_unraid_layout_accepts_trusted_absolute_cron_links_localtime_and_sticky_ancestors(host_layout):
    root, paths, _ = host_layout
    running = paths["user.scripts.state"] / "running"
    running.mkdir()
    (running / "nightly").touch()
    log = paths["user.scripts.state"] / "tmpScripts/nightly/log.txt"
    log.parent.mkdir(parents=True)
    log.write_text("Script Starting Nov 09, 2026  12:00.00\nScript Finished Nov 09, 2026  12:00.45\n")
    assert stat.S_IMODE((root / "tmp").stat().st_mode) == 0o1777
    collector = tasks.Collector(paths, strict=True)
    result = collector.collect()
    assert {"name": "tailscale-daily", "cron": "0 4 * * *"} in result["jobs"]
    assert {"name": "fstrim", "cron": "0 5 * * *"} in result["jobs"]
    assert result["durations"]["nightly"]["seconds"] == 45
    assert result["running"]["nightly"] == (running / "nightly").stat().st_mtime
    assert not collector.failed


@pytest.mark.parametrize("problem", ["file-owner", "file-write", "directory-owner", "directory-write", "link-owner",
                                     "ancestor-owner", "ancestor-write"])
def test_bad_periodic_targets_are_logged_and_skipped_without_losing_other_entries(host_layout, problem, caplog):
    root, paths, _ = host_layout
    folder = root / "usr/local/unsafe"
    folder.mkdir()
    bad = folder / "bad.sh"
    bad.write_text("private-content-must-not-be-logged")
    bad.chmod(0o755)
    link = paths["cron.daily"] / "aaa-bad"
    link.symlink_to(bad)
    node = bad if problem.startswith("file") else link if problem == "link-owner" else folder
    if problem.startswith("ancestor"):
        node = root / "usr/local"
    if problem.endswith("owner"):
        os.chown(node, 10001, -1, follow_symlinks=False)
    else:
        node.chmod(node.stat().st_mode | 0o002)
    good = paths["cron.daily"] / "zzz-good"
    good.touch(mode=0o755)
    collector = tasks.Collector(paths, strict=True)
    result = collector.collect()
    names = {job["name"] for job in result["jobs"]}
    assert "aaa-bad" not in names and {"zzz-good", "fstrim"} <= names
    assert "entry skipped" in caplog.text and "aaa-bad" in caplog.text
    assert "private-content-must-not-be-logged" not in caplog.text
    assert not collector.failed


@pytest.mark.parametrize("source", ["cron.daily", "user.scripts.state"])
def test_untrusted_source_roots_disable_only_that_source_and_are_logged_once(host_layout, source, caplog):
    _, paths, _ = host_layout
    paths[source].chmod(0o1777)
    collector = tasks.Collector(paths, strict=True)
    result = collector.collect()
    assert source in collector.failed
    assert {"name": "fstrim", "cron": "0 5 * * *"} in result["jobs"]
    assert caplog.text.count("source skipped") == 1
    if source == "cron.daily":
        assert all(job["name"] != "tailscale-daily" for job in result["jobs"])
    else:
        assert {"name": "tailscale-daily", "cron": "0 4 * * *"} in result["jobs"]
        assert result["running"] == {}


def test_unreadable_cron_and_state_entries_do_not_discard_readable_siblings(host_layout, monkeypatch, caplog):
    from contextlib import contextmanager

    _, paths, _ = host_layout
    (paths["cron.daily"] / "aaa-denied").touch(mode=0o755)
    (paths["cron.d"] / "aaa-denied").touch()
    running = paths["user.scripts.state"] / "running"
    running.mkdir()
    for name in ("aaa-denied", "zzz-readable"):
        (running / name).touch()
    original = tasks.Collector._open
    @contextmanager
    def denied(self, parts, **kwargs):
        if parts[-1] == "aaa-denied":
            raise PermissionError
        with original(self, parts, **kwargs) as fd:
            yield fd
    monkeypatch.setattr(tasks.Collector, "_open", denied)
    result = tasks.Collector(paths, strict=True).collect()
    assert "zzz-readable" in result["running"] and "aaa-denied" not in result["running"]
    assert {"name": "tailscale-daily", "cron": "0 4 * * *"} in result["jobs"]
    assert caplog.text.count("entry skipped") >= 3


def test_bad_entry_encodings_are_logged_without_discarding_other_cron_entries(host_layout, caplog):
    _, paths, _ = host_layout
    (paths["cron.d"] / "aaa-garbage").write_bytes(b"\xff")
    result = tasks.Collector(paths, strict=True).collect()
    assert {"name": "tailscale-daily", "cron": "0 4 * * *"} in result["jobs"]
    assert "aaa-garbage" in caplog.text and "UnicodeDecodeError" in caplog.text


def test_trusted_relative_symlink_chains_are_walked_from_the_original_directory(host_layout):
    _, paths, target = host_layout
    link = paths["cron.daily"] / "relative"
    link.symlink_to("tailscale-daily")
    other = paths["cron.daily"] / "parent-relative"
    other.symlink_to("../../usr/local/php/unraid-tailscale-utils/daily.sh")
    for name in ("relative", "parent-relative"):
        with trusted.trusted_open(paths["cron.daily"], (name,), follow=True) as fd:
            assert os.read(fd, 100) == target.read_bytes()


@pytest.mark.parametrize("target", ["loop", ".", "missing", "file/child"])
def test_symlink_loops_nonfiles_and_broken_targets_are_skipped_without_fd_leaks(host_layout, target, caplog):
    _, paths, _ = host_layout
    folder = paths["cron.daily"]
    (folder / "file").touch()
    (folder / "loop").symlink_to(target)
    gc.collect()
    before = len(os.listdir("/proc/self/fd"))
    result = tasks.Collector(paths, strict=True).collect()
    assert {"name": "tailscale-daily", "cron": "0 4 * * *"} in result["jobs"]
    assert all(job["name"] != "loop" for job in result["jobs"])
    assert len(os.listdir("/proc/self/fd")) == before
    if target != "missing":
        assert "entry skipped" in caplog.text


def test_sticky_ancestors_must_still_be_root_owned_and_cannot_be_a_source_root(host_layout, caplog):
    root, paths, _ = host_layout
    os.chown(root / "tmp", 10001, -1)
    result = tasks.Collector(paths, strict=True).collect()
    assert result["running"] == {} and "source skipped" in caplog.text
    assert {"name": "tailscale-daily", "cron": "0 4 * * *"} in result["jobs"]


def test_a_symlink_to_the_filesystem_root_is_not_accepted_as_a_regular_file(host_layout, caplog):
    _, paths, _ = host_layout
    (paths["cron.daily"] / "directory").symlink_to("/")
    result = tasks.Collector(paths, strict=True).collect()
    assert all(job["name"] != "directory" for job in result["jobs"])
    assert "directory" in caplog.text
