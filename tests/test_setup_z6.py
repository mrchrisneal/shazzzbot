import hashlib
import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.test_setup_launcher import launcher as helper_launch
from tests.test_setup_launcher import platform_entry
from tools import setup_answers, setup_files, setup_manage, setup_schedule
from tools import setup_transaction as transaction


@pytest.mark.parametrize("point", ["before-write", "after-write", "after-ack", "untrusted"])
def test_helper_install_recovers_a_pinned_archive_before_stopping_or_replacing(tmp_path, point):
    root = Path(__file__).resolve().parents[1]
    home, work, backup = (tmp_path / name for name in ("home", "work", "backup"))
    for folder in (home, work, backup):
        folder.mkdir(mode=0o700)
    old, current, released = b"old verified archive", b"uncommitted runtime", b"next released archive"
    selected = hashlib.sha256(old).hexdigest()
    setup_files.write_safe(home / "helper.pyz", old if point == "before-write" else current)
    if point != "before-write":
        setup_files.write_safe(home / "helper.pyz.prev", old if point != "untrusted" else b"not pinned")
    setup_files.write_safe(home / "policy.json", json.dumps({"format": 1, "artifact": str(home / "helper.pyz"),
        "config": str(home / "config.json"), "sha256": selected}).encode())
    setup_files.write_safe(home / "config.json", b'{"format":1}')
    setup_files.write_safe(home / "api.key", b"k" * 32)
    setup_files.write_safe(home / ".identity.json", b'{"format":1,"active": true}')
    info = home.stat()
    setup_files.write_safe(backup / "manifest.json", json.dumps({"format": 2, "helper": str(home),
        "helper_inode": [info.st_dev, info.st_ino]}).encode())
    setup_files.write_safe(tmp_path / "release.json", json.dumps({
        "helper_sha256": hashlib.sha256(released).hexdigest()}).encode())
    before = {p.name: p.read_bytes() for p in home.iterdir()}
    if point != "untrusted":
        handle = helper_launch.directory(home, home=True)
        try:
            archive = helper_launch.archive(handle, home)
            try:
                assert os.read(archive, 1024) == old
            finally:
                os.close(archive)
        finally:
            os.close(handle)
    script = r'''
set -euo pipefail
source "$ROOT/tools/setup_install_helper.sh"
HELPER=yes HELPER_DONE=0 HELPER_DIR="$TARGET/home" WORK_ROOT="$TARGET/work" HOOK_DIR="$TARGET/hook"
SRC="$ROOT" SETUP_HOME="$ROOT" APP_DIR="$TARGET/app" ACCEPT_FLASH=0 INSTALL_ID=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
BACKUP_PATH="$TARGET/backup" RELEASE_MANIFEST="$TARGET/release.json" HELPER_RELEASE_TAG=v1.0.0
HELPER_ONLY=0 HELPER_BUNDLE="" HELPER_STAGE="" HELPER_EXPECTED="" HELPER_PREFLIGHT_DONE=0
events="$TARGET/events"
dry() { return 1; }
say() { printf '%s\n' "$*"; }
die() { printf '%s\n' "$*" >&2; exit 1; }
helper_flash_policy() { :; }
helper_release_manifest() { :; }
helper_private_dir() { :; }
check_helper_launcher() { :; }
backup_now() { echo backup >> "$events"; }
mark_mutation() { echo mutate >> "$events"; }
stop_helper() { echo stop >> "$events"; }
start_helper() { echo start >> "$events"; }
helper_running() { return 0; }
helper_inspect() { echo 0; }
confirm() { return 0; }
temp_in() { TMPF="$WORK_ROOT/stage"; mkdir -m 700 "$TMPF"; }
bounded() { if [ "$1" = env ]; then return 0; fi; "$@"; }
helper_platform_bundle() {
  printf 'next released archive' > "$1/helper.pyz"
  printf 'verified entry' > "$1/shazzzbot-helper-unraid.py"
  printf '{}' > "$1/helper-unraid.json"
}
replace_file() { echo replace >> "$events"; mkdir -p "${1%/*}"; cat > "$1"; chmod 600 "$1"; }
install_file() { printf '%s' "$1" | replace_file "$2"; }
fs() {
  local action="$1"; shift
  case "$action" in
    select-helper)
      echo select >> "$events"
      python3 -I -B - "$ROOT" "$WORK_ROOT" "$@" <<'PYTHON'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from shazzzbot.helper import footprint
from tools import setup_files, setup_manage
footprint.WORK = Path(sys.argv[2])
footprint.open_home = lambda path: setup_files.opened(path, directory=True)
setup_manage.manage(["select-helper", *sys.argv[3:]])
PYTHON
      ;;
    read) cat "$1" ;;
    hook-active) echo 1 ;;
    helper-identity) printf '{"format":1,"active":true}' ;;
    launcher-text) printf '#!/bin/bash\nexit 0\n' ;;
    *) : ;;
  esac
}
install_helper
'''
    result = subprocess.run(["bash", "-c", script], env={**os.environ, "ROOT": str(root), "TARGET": str(tmp_path)},
                            capture_output=True, text=True, check=False, timeout=15)
    events = (tmp_path / "events").read_text().splitlines()
    if point == "untrusted":
        assert result.returncode != 0 and "no pinned helper archive" in result.stderr
        assert events == ["backup", "select"]
        assert {p.name: p.read_bytes() for p in home.iterdir()} == before
        assert "helper running" not in result.stdout
    else:
        assert result.returncode == 0, result.stderr
        assert events.index("select") < events.index("mutate") < events.index("stop") < events.index("replace")
        assert (home / "helper.pyz").read_bytes() == released
        assert (home / "helper.pyz.prev").read_bytes() == old
        assert json.loads((home / "policy.json").read_bytes())["previous_sha256"] == selected
        assert events[-1] == "start" and "helper running" in result.stdout


@pytest.fixture
def mounted_helper_home(tmp_path, monkeypatch):
    from shazzzbot.helper import footprint

    monkeypatch.setenv("SHAZZZBOT_HELPER_INSTALLING", "1")
    monkeypatch.setenv("SHAZZZBOT_INSTALL_ID", "a" * 32)
    hook = tmp_path / "user.scripts/scripts/shazzzbot-helper/script"
    monkeypatch.setattr(footprint, "HOOK", hook)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    setup_files.write_safe(home / ".identity.json", json.dumps({"format": 1, "install_id": "a" * 32,
        "hook": str(hook), "hook_sha256": "", "active": False}).encode())

    @contextmanager
    def opened_home(path):
        with setup_files.opened(path, directory=True) as fd:
            info = os.fstat(fd)
            if info.st_uid != 0 or info.st_mode & 0o077:
                raise ValueError("helper home must be root-owned with mode 0700")
            yield fd

    monkeypatch.setattr(footprint, "open_home", opened_home)
    return home


@pytest.mark.parametrize("bad", ["writable", "owner", "symlink"])
def test_private_leaf_cannot_make_an_untrusted_ancestor_safe(tmp_path, bad):
    parent = tmp_path / "ancestor"
    app = parent / "app"
    app.mkdir(parents=True, mode=0o700)
    setup_files.write_safe(app / "file", b"original")
    actual = app / "file"
    if bad == "writable":
        parent.chmod(0o777)
    elif bad == "owner":
        os.chown(parent, 1000, 1000)
    else:
        moved = tmp_path / "moved"
        parent.rename(moved)
        parent.symlink_to(moved, target_is_directory=True)
        actual = moved / "app/file"
    with pytest.raises((OSError, ValueError)):
        setup_files.read_safe(app / "file")
    with pytest.raises((OSError, ValueError)):
        setup_files.write_safe(app / "file", b"replacement")
    assert actual.read_bytes() == b"original"


def test_root_sticky_ancestor_allows_a_private_child_only(tmp_path):
    parent = tmp_path / "temporary"
    parent.mkdir()
    parent.chmod(0o1777)
    app = parent / "private"
    app.mkdir(mode=0o700)
    setup_files.write_safe(app / "file", b"safe")
    assert setup_files.read_safe(app / "file") == b"safe"
    with pytest.raises(ValueError, match="untrusted"), setup_files.opened(parent, directory=True):
        pytest.fail("a writable sticky directory was accepted as the private leaf")


def test_config_uid_exception_does_not_allow_a_non_root_ancestor(tmp_path):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    config = parent / "config.toml"
    setup_files.write_safe(config, b"guild_id = 1\n", uid=10001)
    assert setup_files.read_safe(config, private=True) == b"guild_id = 1\n"
    os.chown(parent, 10001, 10001)
    with pytest.raises(ValueError, match="untrusted"):
        setup_files.read_safe(config, private=True)






def test_saved_answers_survive_rollback_without_overwriting_existing_config(tmp_path):
    app, stage, backup = (tmp_path / name for name in ("app", "stage", "backups"))
    app.mkdir()
    stage.mkdir()
    setup_files.write_safe(stage / ".env", b"DISCORD_TOKEN=secret")
    setup_files.write_safe(stage / "config.toml", b"version=1")
    setup_answers.answers(backup, stage, app, "bot", "compose-image", save=True)
    (stage / ".env").unlink()
    (stage / "config.toml").unlink()
    (app / "config.toml").write_text("keep")
    setup_answers.answers(backup, stage, app, "bot", "compose-image", save=False)
    assert (stage / ".env").read_bytes() == b"DISCORD_TOKEN=secret"
    assert not (stage / "config.toml").exists() and (app / "config.toml").read_text() == "keep"
    with pytest.raises(ValueError, match="belong"):
        setup_answers.answers(backup, stage, app, "relay", "compose-image", save=False)


def test_schedule_is_explicit_preserves_other_scripts_and_rolls_back(tmp_path, mounted_helper_home):
    helper = tmp_path / "user.scripts/scripts/shazzzbot-helper"
    helper.mkdir(parents=True)
    app, backups = tmp_path / "app", tmp_path / "backups"
    app.mkdir()
    home = mounted_helper_home
    schedule = helper.parent.parent / "schedule.json"
    schedule.write_text('{"other":{"frequency":"daily"}}')
    snapshot = transaction.snapshot(app, home, backups, 5)
    setup_manage.manage(["schedule", str(helper), str(snapshot)])
    value = json.loads(schedule.read_text())
    assert value["other"] == {"frequency": "daily"}
    assert value["shazzzbot-helper"] == {"frequency": "boot", "script": str(helper / "script")}
    transaction.restore_snapshot(snapshot, app, home, helper_changed=True)
    assert json.loads(schedule.read_text()) == {"other": {"frequency": "daily"}}


@pytest.mark.parametrize("case", ["path", "array", "occupied"])
def test_schedule_refuses_unknown_locations_or_unowned_entries(tmp_path, case):
    helper = tmp_path / "user.scripts/scripts/shazzzbot-helper"
    helper.mkdir(parents=True)
    if case == "path":
        helper = tmp_path
    else:
        value = [] if case == "array" else {"shazzzbot-helper": {"script": "other"}}
        (helper.parent.parent / "schedule.json").write_text(json.dumps(value))
    with pytest.raises((ValueError, TypeError)):
        setup_schedule.schedule(helper, tmp_path / "unused")


@pytest.mark.parametrize("point", ["before-pin", "after-pin", "after-artifact"])
def test_launcher_handles_each_publication_crash_point(tmp_path, monkeypatch, point, mounted_helper_home):
    monkeypatch.setattr(helper_launch, "WORK", tmp_path / "work")
    home = mounted_helper_home
    platform_entry(home)
    hook = tmp_path / "user.scripts/scripts/shazzzbot-helper/script"
    hook.parent.mkdir(parents=True)
    old, new = b"old verified archive", b"new verified archive"
    artifact, config, policy = (home / name for name in ("helper.pyz", "config.json", "policy.json"))
    setup_files.write_safe(hook, b"verified hook")
    setup_files.write_safe(hook.parent.parent.parent / "schedule.json", json.dumps({
        "shazzzbot-helper": {"frequency": "boot", "script": str(hook)}}).encode())
    setup_files.write_safe(home / ".identity.json", json.dumps({"format": 1, "install_id": "a" * 32,
        "hook": str(hook), "hook_sha256": hashlib.sha256(hook.read_bytes()).hexdigest(), "active": True}).encode())
    setup_files.write_safe(home / "api.key", b"a" * 32)
    setup_files.write_safe(config, json.dumps({"format": 1, "key_file": str(home / "api.key"),
        "socket": str(helper_launch.WORK / "socket/helper.sock"), "installation_policy": str(policy)}).encode())
    app = tmp_path / "app"
    app.mkdir(mode=0o700)
    snapshot = transaction.snapshot(app, home, tmp_path / "backups", 5)
    transaction.replace(snapshot, "helper", "helper.pyz", new if point == "after-artifact" else old)
    transaction.replace(snapshot, "helper", "helper.pyz.prev", old)
    value = {"format": 1, "artifact": str(artifact),
             "sha256": hashlib.sha256(old if point == "before-pin" else new).hexdigest(),
             "config": str(config)}
    if point != "before-pin":
        value["previous_sha256"] = hashlib.sha256(old).hexdigest()
    transaction.replace(snapshot, "helper", "policy.json", json.dumps(value).encode())
    assert artifact.stat().st_mode & 0o777 == 0o700
    assert (home / "helper.pyz.prev").stat().st_mode & 0o777 == 0o700
    assert config.stat().st_mode & 0o777 == policy.stat().st_mode & 0o777 == 0o600
    installer = Path("tools/setup_install_helper.sh").read_text()
    assert 'chmod 700 -- "$HELPER_DIR/helper.pyz"' not in installer
    calls = []
    monkeypatch.setattr(helper_launch, "mounted", lambda _: True)
    monkeypatch.setattr(os, "chdir", lambda _: None)
    monkeypatch.setattr(os, "execve", lambda _binary, _args, env:
                        calls.append(os.read(int(env["SHAZZZBOT_HELPER_ARCHIVE_FD"]), 1024)))
    assert helper_launch.launch(home, hook)
    assert calls == [new if point == "after-artifact" else old]
    artifact.write_bytes(b"interrupted write")
    assert helper_launch.launch(home, hook)
    assert calls[-1] == old
    hook.write_bytes(b"unrelated user command")
    with pytest.raises(ValueError, match="hook no longer matches"):
        helper_launch.launch(home, hook)


def test_random_identity_is_pinned_by_the_current_install_identity(tmp_path):
    identifier = setup_files.install_identity("1" * 32, tmp_path)
    assert len(identifier) == 32 and all(char in "0123456789abcdef" for char in identifier)
    setup_files.write_safe(tmp_path / ".setup-identity", json.dumps({"format": 2, "host": "1" * 32,
                                                                  "app": str(tmp_path),
                                                                  "install_id": identifier}).encode())
    assert setup_files.install_identity("1" * 32, tmp_path) == identifier
    setup_files.write_safe(tmp_path / ".setup-identity", b'{"format":2}')
    with pytest.raises(ValueError, match="mismatch"):
        setup_files.install_identity("1" * 32, tmp_path)
    setup_files.write_safe(tmp_path / ".setup-identity",
                           json.dumps({"format": 2, "host": "1" * 32, "app": str(tmp_path),
                                       "install_id": "bad"}).encode())
    with pytest.raises(ValueError, match="invalid install id"):
        setup_files.install_identity("1" * 32, tmp_path)


@pytest.mark.parametrize("identifier", [None, 3, "a" * 31, "invalid:identity"])
def test_install_identity_requires_a_current_32_character_id(tmp_path, identifier):
    setup_files.write_safe(tmp_path / ".setup-identity",
                           json.dumps({"format": 2, "host": "1" * 32, "app": str(tmp_path),
                                       "install_id": identifier}).encode())
    with pytest.raises(ValueError, match="invalid install id"):
        setup_files.install_identity("1" * 32, tmp_path)


@pytest.mark.parametrize("value", [None, [], {}, {"host": "1" * 32, "app": 7, "install_id": "a" * 32},
                                 {"host": "1" * 32, "app": "/private/app", "install_id": None},
                                 {"host": "1" * 32, "app": "/private/app", "install_id": "invalid"}])
def test_saved_identity_rejects_malformed_content_instead_of_generating_a_new_id(tmp_path, value):
    data = {"format": 1, **value} if isinstance(value, dict) else value
    setup_files.write_safe(tmp_path / "install-identity.json", json.dumps(data).encode())
    message = ("saved install identity mismatch" if isinstance(value, dict)
               else "saved install identity must be an object")
    with pytest.raises(ValueError, match=message):
        setup_files.install_identity("1" * 32, tmp_path / "app", tmp_path)


def test_installer_file_commands_cover_the_new_safe_operations(tmp_path, monkeypatch, capsys):
    home, entry = tmp_path / "home", tmp_path / "entry"
    home.mkdir(mode=0o700)
    entry.mkdir(mode=0o700)
    platform_entry(home)
    source = Path(helper_launch.__file__).read_bytes()
    staged = tmp_path / "staged-entry.py"
    setup_files.write_safe(staged, source)
    body = setup_schedule.hook_text(home, entry / "script", source)
    setup_manage.manage(["launcher-text", str(home), str(entry / "script"), str(staged)])
    assert capsys.readouterr().out.encode() == body
    setup_files.write_safe(entry / "script", body)
    setup_files.write_safe(home / ".identity.json", json.dumps({"format": 1, "hook": str(entry / "script"),
                           "hook_sha256": hashlib.sha256(body).hexdigest()}).encode())
    setup_manage.manage(["check-launcher", str(entry), str(home)])
    setup_manage.manage(["checksum", str(entry / "script")])
    digest = capsys.readouterr().out.strip()
    setup_files.write_safe(home / "pin", json.dumps({"format": 1, "sha256": digest}).encode())
    setup_manage.manage(["verify-policy", str(home / "pin"), digest])
    setup_files.write_safe(entry / "script", b"wrong")
    with pytest.raises(ValueError, match="hook"):
        setup_manage.manage(["check-launcher", str(entry), str(home)])


def test_answers_commands_refresh_helper_credentials_without_printing_them(tmp_path, monkeypatch, capsys):
    app, out, backup = (tmp_path / name for name in ("app", "out", "backup"))
    for path in (app, out, backup):
        path.mkdir(mode=0o700)
    setup_files.write_safe(out / ".env", b"SHAZZZBOT_HELPER_KEY=old\nHELPER_SOCKET_DIR=/old\n")
    setup_manage.manage(["save-answers", str(backup), str(out), str(app), "bot", "compose-image"])
    (out / ".env").unlink()
    monkeypatch.setenv("SHAZZZBOT_HELPER_KEY", "new-private-key")
    monkeypatch.setenv("SHAZZZBOT_HELPER_SOCKET_DIR", "/private/socket")
    setup_manage.manage(["restore-answers", str(backup), str(out), str(app), "bot", "compose-image"])
    assert "SHAZZZBOT_HELPER_KEY=new-private-key" in (out / ".env").read_text()
    assert "HELPER_SOCKET_DIR=/private/socket" in (out / ".env").read_text()
    assert capsys.readouterr() == ("", "")


def test_backup_identity_preserves_retained_resources_after_a_failed_fresh_install(tmp_path, monkeypatch):
    app, backup, machine = (tmp_path / name for name in ("app", "backup", "machine"))
    backup.mkdir(mode=0o700)
    machine.write_text("1" * 32)
    monkeypatch.setenv("SHAZZZBOT_MACHINE_ID", str(machine))
    assert len(setup_files.install_identity("1" * 32, app, backup)) == 32
    identity = "a" * 32
    setup_manage.manage(["remember-id", str(backup), str(app), identity])
    setup_manage.manage(["remember-id", str(backup), str(app), identity])
    with pytest.raises(ValueError, match="invalid install id"):
        setup_manage.manage(["remember-id", str(backup), str(app), "invalid:identity"])
    assert setup_files.install_identity("1" * 32, app, backup) == identity
    with pytest.raises(ValueError, match="mismatch"):
        setup_manage.manage(["remember-id", str(backup), str(app), "b" * 32])
    with pytest.raises(ValueError, match="mismatch"):
        setup_files.install_identity("2" * 32, app, backup)
    other = setup_files.install_identity("1" * 32, tmp_path / "other-app", backup)
    assert len(other) == 32 and other != identity


@pytest.mark.parametrize("bad", [False, "mode", "owner"])
def test_lock_parent_requires_a_trusted_non_sticky_directory(tmp_path, bad):
    path = tmp_path / "locks"
    path.mkdir(mode=0o700)
    if bad:
        if bad == "mode":
            path.chmod(0o1777)
        else:
            os.chown(path, 99, 100)
    if bad:
        with pytest.raises(ValueError, match="untrusted"):
            setup_manage.manage(["lock-parent", str(path)])
    else:
        setup_manage.manage(["lock-parent", str(path)])


def test_descriptor_reads_keep_the_original_leaf_after_the_path_is_replaced(tmp_path):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    app = parent / "app"
    app.mkdir(mode=0o700)
    setup_files.write_safe(app / "file", b"original")
    with setup_files.opened(app, directory=True) as root:
        app.rename(parent / "moved")
        app.mkdir(mode=0o700)
        os.chown(app, 99, 100)
        (app / "file").write_bytes(b"substituted")
        fd = os.open("file", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root)
        try:
            assert os.read(fd, 1024) == b"original"
        finally:
            os.close(fd)
        assert (app / "file").read_bytes() == b"substituted"
        with pytest.raises(ValueError, match="untrusted"):
            setup_files.read_safe(app / "file")


@pytest.mark.parametrize("bad", [False, True])
def test_helper_policy_cannot_leave_its_home(tmp_path, bad):
    value = {"format": 1, "artifact": str(tmp_path / "helper.pyz"),
             "config": str(tmp_path / "config.json"), "sha256": "a" * 64}
    if bad:
        value["artifact"] = "/boot/another-helper/helper.pyz"
    setup_files.write_safe(tmp_path / "policy.json", json.dumps(value).encode())
    if bad:
        with pytest.raises(ValueError, match="identity"):
            setup_manage.manage(["helper-policy", str(tmp_path)])
    else:
        setup_manage.manage(["helper-policy", str(tmp_path)])


def test_identity_rejects_a_symlink_before_using_it(tmp_path):
    path = tmp_path / "app-link"
    path.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="untrusted"):
        setup_files.install_identity("1" * 32, path)


def test_recovery_identity_is_saved_once_per_transaction(tmp_path):
    script = '''
source tools/setup_prereqs.sh
BACKUP_ROOT="$1" APP_DIR="$1/app" INSTALL_ID=abcdef MUTATING=0
backup_now() { :; }
die() { exit 1; }
fs() { printf '%s\\n' "$1" >> "$BACKUP_ROOT/calls"; cat >/dev/null; }
mark_mutation
mark_mutation
'''
    result = subprocess.run(["bash", "-c", script, "identity", str(tmp_path)],
                            check=False, capture_output=True, text=True, input="")
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls").read_text().splitlines()
    assert calls.count("remember-id") == 1 and calls.count("write") == 2


def test_startup_schedule_can_be_created_once_without_a_previous_schedule_file(tmp_path, mounted_helper_home):
    helper = tmp_path / "user.scripts/scripts/shazzzbot-helper"
    helper.mkdir(parents=True)
    app = tmp_path / "app"
    app.mkdir()
    home = mounted_helper_home
    snapshot = transaction.snapshot(app, home, tmp_path / "backups", 5)
    setup_schedule.schedule(helper, snapshot)
    setup_schedule.schedule(helper, snapshot)
    transaction.restore_snapshot(snapshot, app, home, helper_changed=True)
    assert not (helper.parent.parent / "schedule.json").exists()
