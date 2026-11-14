import re
import stat
from pathlib import Path

import pytest

from tests import test_setup_sh as installer
from tests.test_setup_sh import calls, run

env = installer.env


def managed_files(values):
    found = {}
    for name in ("SHAZZZBOT_APP_DIR", "SHAZZZBOT_HELPER_DIR", "SHAZZZBOT_BACKUP_DIR", "SHAZZZBOT_LOCK_FILE"):
        root = Path(values[name])
        paths = [root, *root.rglob("*")] if root.is_dir() else [root]
        for path in paths:
            if path.exists():
                info = path.lstat()
                found[str(path)] = (stat.S_IMODE(info.st_mode), path.read_bytes() if path.is_file() else None)
    return found


def assert_read_only(values, before):
    assert managed_files(values) == before
    assert not re.search(r"^(?:run|pull|build|rm|network create|compose .*\b(?:up|pull|rm)\b)",
                         calls(values), re.MULTILINE)


def test_declining_the_initial_action_leaves_all_managed_paths_unchanged(env):
    before = managed_files(env)
    result = run(env, "--role", "bot", "--method", "compose-image", "--no-helper", stdin="n\n")
    assert result.returncode == 0, result.stderr
    assert "not installed" in result.stdout.lower()
    assert_read_only(env, before)


def test_eof_at_the_initial_confirmation_does_not_create_a_lock_or_install_markers(env):
    before = managed_files(env)
    result = run(env, "--role", "bot", "--method", "compose-image", "--no-helper")
    assert result.returncode != 0
    assert "input" in result.stderr.lower()
    assert_read_only(env, before)


@pytest.mark.parametrize("role", ["bot", "relay"])
def test_detection_dry_run_does_not_change_files_or_start_containers(env, role):
    before = managed_files(env)
    result = run(env, "--role", role, "--method", "compose-image", "--no-helper", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "not installed" in result.stdout.lower() and "[dry-run]" in result.stdout
    assert_read_only(env, before)


def test_unsupported_bot_host_stops_before_mutation_but_relay_remains_supported(env):
    Path(env["SHAZZZBOT_UNRAID_MARKER"]).unlink()
    before = managed_files(env)
    bot = run(env, "--role", "bot", "--method", "compose-image", "--no-helper", stdin="y\n")
    assert bot.returncode != 0
    assert "not supported yet" in (bot.stdout + bot.stderr).lower()
    assert_read_only(env, before)
    relay = run(env, "--role", "relay", "--method", "compose-image", "--no-helper", "--dry-run")
    assert relay.returncode == 0, relay.stderr
    assert_read_only(env, before)


@pytest.mark.parametrize("problem", ["pending", "platform"])
def test_current_install_does_not_bypass_recovery_or_platform_checks(env, problem):
    first = installer.bot(env, "compose-image")
    assert first.returncode == 0, first.stderr
    if problem == "pending":
        Path(env["SHAZZZBOT_BACKUP_DIR"], "pending").write_text("interrupted-install\n")
    else:
        Path(env["SHAZZZBOT_UNRAID_MARKER"]).unlink()
    before = managed_files(env)
    previous = calls(env)
    result = installer.bot(env, "compose-image", stdin="")
    assert result.returncode != 0
    expected = "interrupted install needs recovery" if problem == "pending" else "not supported yet"
    assert expected in result.stderr
    assert "nothing to change" not in result.stdout
    assert managed_files(env) == before
    assert not re.search(r"^(?:run|pull|build|rm|network create|compose .*\b(?:up|pull|rm)\b)",
                         calls(env)[len(previous):], re.MULTILINE)


def test_interactively_selected_bot_is_refused_on_an_unsupported_platform(env):
    Path(env["SHAZZZBOT_UNRAID_MARKER"]).unlink()
    before = managed_files(env)
    result = run(env, "--method", "compose-image", "--no-helper", stdin="1\n")
    assert result.returncode != 0 and "not supported yet" in result.stderr
    assert_read_only(env, before)


def test_installation_change_while_taking_the_lock_stops_before_managed_writes(env):
    import shutil

    actual_flock = shutil.which("flock")
    assert actual_flock
    wrapper = Path(env["STUB_ROOT"], "bin", "flock")
    wrapper.write_text('#!/bin/bash\n'
                       'mkdir -m 700 -- "$SHAZZZBOT_APP_DIR"\n'
                       'printf "compose-image bot v9.0.0\\n" > "$SHAZZZBOT_APP_DIR/.setup-state"\n'
                       'chmod 600 -- "$SHAZZZBOT_APP_DIR/.setup-state"\n'
                       'exec ' + actual_flock + ' "$@"\n')
    wrapper.chmod(0o755)
    result = run(env, "--role", "bot", "--method", "compose-image", "--no-helper", "--yes")
    assert result.returncode != 0
    assert "install changed during setup; rerun" in result.stderr
    app = Path(env["SHAZZZBOT_APP_DIR"])
    assert list(app.iterdir()) == [app / ".setup-state"]
    assert not Path(env["SHAZZZBOT_BACKUP_DIR"]).exists()
    assert not re.search(r"^(?:run|pull|build|rm|network create|compose .*\b(?:up|pull|rm)\b)",
                         calls(env), re.MULTILINE)
