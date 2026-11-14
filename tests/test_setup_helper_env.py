import json
import runpy
import sys

import pytest

from shazzzbot.helper import footprint
from tools import setup_files
from tools import setup_helper_env as envsync
from tools import setup_transaction as transaction


@pytest.fixture
def journal(tmp_path, monkeypatch):
    app, home = (tmp_path / name for name in ("app", "helper"))
    app.mkdir(mode=0o700)
    home.mkdir(mode=0o700)
    machine = tmp_path / "machine-id"
    machine.write_text("1" * 32)
    monkeypatch.setenv("SHAZZZBOT_MACHINE_ID", str(machine))
    monkeypatch.setenv("SHAZZZBOT_INSTALL_ID", "a" * 32)
    monkeypatch.setenv("SHAZZZBOT_BOT_INSTALL_ID", "b" * 32)
    monkeypatch.setenv("SHAZZZBOT_HELPER_INSTALLING", "1")
    monkeypatch.setenv("SHAZZZBOT_HELPER_INODE", json.dumps([home.stat().st_dev, home.stat().st_ino]))
    monkeypatch.setattr(footprint, "open_home", lambda path: setup_files.opened(path, directory=True))
    setup_files.write_safe(home / ".identity.json", json.dumps({
        "format": 1, "install_id": "a" * 32, "hook": str(footprint.HOOK),
        "hook_sha256": "", "active": False}).encode())
    setup_files.write_safe(app / ".setup-identity", json.dumps({
        "format": 2, "host": "1" * 32, "app": str(app), "role": "bot", "install_id": "b" * 32}).encode())
    setup_files.write_safe(app / ".env", b"OTHER=keep\n")
    setup_files.write_safe(home / "api.key", b"c" * 32)
    folder = transaction.snapshot(app, home, tmp_path / "backups", 2)
    return app, home, folder


def test_added_key_preserves_other_bytes_and_rolls_back(journal):
    app, home, folder = journal
    original = b"# keep comments\r\nOTHER='private value'\r\n\r\nLAST=no-newline"
    setup_files.write_safe(app / ".env", original)
    identity = (app / ".setup-identity").read_bytes()
    assert envsync.sync_env(app, home, folder)
    candidate = (app / ".env").read_bytes()
    assert candidate.startswith(original + b"\r\n")
    assert b'SHAZZZBOT_HELPER_KEY="' + b"c" * 32 + b'"\r\n' in candidate
    assert b'HELPER_SOCKET_DIR="/tmp/shazzzbot-helper/socket"\r\n' in candidate
    assert (app / ".setup-identity").read_bytes() == identity
    transaction.restore_snapshot(folder, app, home)
    assert (app / ".env").read_bytes() == original


def test_replaces_only_target_assignments_and_noop_keeps_mtime(journal):
    app, home, folder = journal
    setup_files.write_safe(app / ".env", b"OTHER=keep\nexport SHAZZZBOT_HELPER_KEY='old'\nHELPER_SOCKET_DIR=old")
    assert envsync.sync_env(app, home, folder)
    expected = (b'OTHER=keep\nSHAZZZBOT_HELPER_KEY="' + b"c" * 32
                + b'"\nHELPER_SOCKET_DIR="/tmp/shazzzbot-helper/socket"')
    assert (app / ".env").read_bytes() == expected
    before = (app / ".env").stat().st_mtime_ns, (folder / "manifest.json").read_bytes()
    assert not envsync.sync_env(app, home, folder)
    assert ((app / ".env").stat().st_mtime_ns, (folder / "manifest.json").read_bytes()) == before


def test_effectively_equal_quoted_values_do_not_change_bytes_or_journal(journal):
    app, home, folder = journal
    original = (b"export SHAZZZBOT_HELPER_KEY='" + b"c" * 32
                + b"' # comment\nHELPER_SOCKET_DIR=/tmp/shazzzbot-helper/socket\n")
    setup_files.write_safe(app / ".env", original)
    before = transaction.manifest(folder)
    assert not envsync.sync_env(app, home, folder)
    assert (app / ".env").read_bytes() == original
    assert transaction.manifest(folder) == before


@pytest.mark.parametrize("original", [b"A=1\nA=2\n", b"not an assignment\n", b"A='unfinished\n"])
def test_invalid_env_refuses_without_mutation(journal, original):
    app, home, folder = journal
    setup_files.write_safe(app / ".env", original)
    before = transaction.manifest(folder)
    with pytest.raises(ValueError):
        envsync.sync_env(app, home, folder)
    assert (app / ".env").read_bytes() == original
    assert transaction.manifest(folder) == before


@pytest.mark.parametrize("missing", [".setup-identity", ".env"])
def test_unmanaged_or_missing_env_is_not_read_or_changed(journal, missing):
    app, home, folder = journal
    (app / missing).unlink()
    (home / "api.key").unlink()
    assert not envsync.sync_env(app, home, folder)


@pytest.mark.parametrize("change", [{"format": 1}, {"host": "2" * 32}, {"app": "/elsewhere"},
                                    {"install_id": "d" * 32}, {"install_id": "bad"}, {"role": "relay"}])
def test_managed_identity_must_match_host_app_and_bot_id(journal, change):
    app, home, folder = journal
    value = json.loads((app / ".setup-identity").read_bytes())
    value.update(change)
    setup_files.write_safe(app / ".setup-identity", json.dumps(value).encode())
    with pytest.raises(ValueError):
        envsync.sync_env(app, home, folder)


@pytest.mark.parametrize("change", [{"app": "/wrong"}, {"helper": "/wrong"}, {"helper_inode": None}])
def test_transaction_must_bind_both_directories_and_inode(journal, change):
    app, home, folder = journal
    value = transaction.manifest(folder)
    value.update(change)
    transaction.save(folder, value)
    with pytest.raises(ValueError):
        envsync.sync_env(app, home, folder)


def test_bad_key_and_public_env_refuse_without_publication(journal):
    app, home, folder = journal
    setup_files.write_safe(home / "api.key", b"too short")
    with pytest.raises(ValueError, match="invalid helper API key"):
        envsync.sync_env(app, home, folder)
    (app / ".env").chmod(0o644)
    with pytest.raises(ValueError, match="secret file"):
        envsync.sync_env(app, home, folder)


@pytest.mark.parametrize("change", ["before", "during"])
def test_operator_env_change_refuses_before_publication(journal, monkeypatch, change):
    app, home, folder = journal
    if change == "before":
        transaction.watch(folder, "app", ".env")
        setup_files.write_safe(app / ".env", b"OTHER=operator\n")
    else:
        watch = transaction.watch

        def changed(*args):
            watch(*args)
            setup_files.write_safe(app / ".env", b"OTHER=operator\n")

        monkeypatch.setattr(transaction, "watch", changed)
    with pytest.raises(ValueError, match="changed during setup"):
        envsync.sync_env(app, home, folder)
    assert (app / ".env").read_bytes() == b"OTHER=operator\n"


def test_default_bot_id_validation_and_cli_output(journal, monkeypatch, capsys):
    app, home, folder = journal
    monkeypatch.delenv("SHAZZZBOT_BOT_INSTALL_ID")
    monkeypatch.setattr(sys, "argv", ["setup_helper_env", str(app), str(home), str(folder)])
    envsync.main()
    assert capsys.readouterr().out == "1\n"
    (app / ".env").chmod(0o644)
    with pytest.raises(SystemExit, match="secret file"):
        envsync.main()


def test_needs_sync_does_not_create_a_journal_or_write_env(journal):
    app, home, folder = journal
    original = (app / ".env").read_bytes()
    before = transaction.manifest(folder)
    assert envsync.needs_sync(app, home)
    assert (app / ".env").read_bytes() == original
    assert transaction.manifest(folder) == before
    assert envsync.sync_env(app, home, folder)
    assert not envsync.needs_sync(app, home)
    (app / ".setup-identity").unlink()
    assert not envsync.needs_sync(app, home)


def test_needs_sync_requires_independent_helper_inode(journal, monkeypatch):
    app, home, _ = journal
    monkeypatch.delenv("SHAZZZBOT_HELPER_INODE")
    with pytest.raises(ValueError, match="recorded helper directory identity"):
        envsync.needs_sync(app, home)
    monkeypatch.setenv("SHAZZZBOT_HELPER_INODE", "[0, 1]")
    with pytest.raises(ValueError, match="identity changed"):
        envsync.needs_sync(app, home)


def test_environment_module_entry_runs_real_transaction(journal, monkeypatch, capsys):
    app, home, folder = journal
    monkeypatch.setattr(sys, "argv", ["setup_helper_env", str(app), str(home), str(folder)])
    runpy.run_path(envsync.__file__, run_name="__main__")
    assert capsys.readouterr().out == "1\n"
    assert b"SHAZZZBOT_HELPER_KEY=" in (app / ".env").read_bytes()
