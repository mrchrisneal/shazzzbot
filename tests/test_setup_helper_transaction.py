import json
import runpy
import sys
from pathlib import Path

import pytest

from shazzzbot.helper import footprint
from tools import setup_files
from tools import setup_helper_transaction as backend
from tools import setup_transaction as transaction


@pytest.fixture
def home(tmp_path, monkeypatch):
    machine = tmp_path / "machine-id"
    machine.write_text("c" * 32)
    monkeypatch.setenv("SHAZZZBOT_MACHINE_ID", str(machine))
    root = tmp_path / "helper"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(footprint, "open_home", lambda path: setup_files.opened(path, directory=True))
    monkeypatch.setenv("SHAZZZBOT_HELPER_HOME", str(root))
    info = root.stat()
    monkeypatch.setenv("SHAZZZBOT_HELPER_INODE", json.dumps([info.st_dev, info.st_ino]))
    monkeypatch.setenv("SHAZZZBOT_HELPER_APP", "")
    monkeypatch.setenv("SHAZZZBOT_INSTALL_ID", "a" * 32)
    monkeypatch.setenv("SHAZZZBOT_HELPER_INSTALLING", "1")
    hook = tmp_path / "scripts/shazzzbot-helper"
    hook.mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("SHAZZZBOT_HELPER_HOOK_DIR", str(hook))
    identity = {"format": 1, "install_id": "a" * 32, "hook": str(footprint.HOOK),
                "hook_sha256": "", "active": False}
    setup_files.write_safe(root / ".identity.json", json.dumps(identity).encode())
    return root


def snapshot(home, keep=2):
    return backend.home_snapshot(home, None, keep, backend.binding().inode)


def test_private_snapshot_payload_restore_and_pending_lifecycle(home):
    assert backend.pending(home) is None
    assert backend.pending(home, commit=True) is None
    assert not (home / "transactions").exists()
    folder = snapshot(home)
    assert folder.parent == home / "transactions"
    assert transaction.manifest(folder)["app"] is None
    assert folder.stat().st_mode & 0o077 == 0
    setup_files.write_safe(home / "api.key", b"original")
    transaction.replace(folder, "helper", "api.key", b"replacement")
    assert (home / "backups" / folder.name / "helper/api.key").read_bytes() == b"original"
    assert not (folder / "helper").exists()
    assert backend.pending(home, folder) == folder
    assert backend.pending(home, folder) == folder
    assert backend.pending(home) == folder
    transaction.restore_snapshot(folder, None, home, helper_changed=True)
    assert (home / "api.key").read_bytes() == b"original"
    assert backend.pending(home, commit=True) == folder
    assert backend.pending(home) is None
    assert backend.pending(home, commit=True) is None
    assert (folder / "manifest.json").exists()


def test_helper_only_transaction_forbids_app_writes_and_foreign_metadata(home, tmp_path):
    folder = snapshot(home)
    with pytest.raises(ValueError, match="write set"):
        transaction.replace(folder, "app", ".env", b"secret")
    with pytest.raises(ValueError, match="private home"):
        transaction.manifest(tmp_path / folder.name)
    value = transaction.manifest(folder)
    for field, other in (("app", str(tmp_path)), ("helper_inode", [0, 0]), ("hook", "/wrong")):
        changed = dict(value, **{field: other})
        with pytest.raises(ValueError):
            transaction.save(folder, changed)
    with pytest.raises(TypeError, match="files"):
        transaction.save(folder, dict(value, files=[]))
    for key in ("wrong", "outside/file", "app/compose.yaml"):
        with pytest.raises(ValueError, match="write set"):
            transaction.save(folder, dict(value, files={key: {}}))


def test_pending_refuses_conflicts_corruption_and_symlinks(home, tmp_path):
    folder, other = snapshot(home), snapshot(home)
    backend.pending(home, folder)
    with pytest.raises(ValueError, match="another"):
        backend.pending(home, other)
    with pytest.raises(ValueError, match="outside"):
        backend.pending(tmp_path)
    marker = home / "transactions/pending"
    setup_files.write_safe(marker, b"x" * 4097)
    with pytest.raises(ValueError, match="too large"):
        backend.pending(home)
    marker.unlink()
    marker.symlink_to(folder / "manifest.json")
    with pytest.raises(OSError):
        backend.pending(home)
    marker.unlink()
    setup_files.write_safe(marker, (str(folder) + "\n").encode())
    (folder / "manifest.json").unlink()
    with pytest.raises(FileNotFoundError):
        backend.pending(home, commit=True)
    assert marker.exists()
    with pytest.raises(FileNotFoundError):
        backend.pending(home, folder)


def test_helper_journal_cannot_follow_replaced_home_or_metadata(home, monkeypatch):
    folder = snapshot(home)
    original = home.with_name("original")
    home.rename(original)
    home.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="identity"):
        transaction.manifest(folder)
    home.rmdir()
    original.rename(home)
    metadata = home / "transactions"
    metadata.rename(home / "saved")
    metadata.symlink_to(home / "saved", target_is_directory=True)
    with pytest.raises(OSError):
        snapshot(home)
    metadata.unlink()
    (home / "saved").rename(metadata)
    metadata.chmod(0o755)
    with pytest.raises(ValueError, match="private"):
        snapshot(home)
    with pytest.raises(ValueError, match="private home"), backend.roots(home):
        pass


def test_retention_prunes_payload_and_metadata_once(home):
    folder = snapshot(home)
    setup_files.write_safe(home / "api.key", b"old")
    transaction.watch(folder, "helper", "api.key")
    payload = home / "backups" / folder.name
    ignored = home / "transactions/unrelated"
    ignored.mkdir(mode=0o700)
    second = snapshot(home)
    assert folder.exists()
    third = snapshot(home)
    assert not folder.exists() and not payload.exists()
    assert second.exists() and third.exists() and ignored.exists()


def test_managed_env_uses_separate_identity_and_payload_home(home, tmp_path, monkeypatch):
    app = tmp_path / "bot"
    app.mkdir(mode=0o700)
    monkeypatch.setenv("SHAZZZBOT_HELPER_APP", str(app))
    monkeypatch.setenv("SHAZZZBOT_BOT_INSTALL_ID", "b" * 32)
    identity = {"format": 2, "app": str(app), "host": setup_files.host_identity(),
                "role": "bot", "install_id": "b" * 32}
    setup_files.write_safe(app / ".setup-identity", json.dumps(identity).encode())
    setup_files.write_safe(app / ".env", b"OTHER=value\n")
    folder = backend.home_snapshot(home, app, 2, backend.binding().inode, managed=True)
    transaction.replace(folder, "app", ".env", b"OTHER=value\nHELPER_KEY=private\n")
    assert (home / "backups" / folder.name / "app/.env").read_bytes() == b"OTHER=value\n"
    backend.main(["restore", str(folder), str(app), str(home), "0"])
    assert (app / ".env").read_bytes() == b"OTHER=value\n"
    assert json.loads((app / ".setup-identity").read_bytes()) == identity
    with pytest.raises(ValueError, match="write set"):
        transaction.replace(folder, "app", ".setup-identity", b"changed")
    monkeypatch.setenv("SHAZZZBOT_BOT_INSTALL_ID", "a" * 32)
    with pytest.raises(ValueError, match="managed bot"):
        backend.binding()


@pytest.mark.parametrize("value", [None, {}, [1], [-1, 2], [True, 2], [1, "2"]])
def test_independent_inode_validation(home, monkeypatch, value):
    monkeypatch.setenv("SHAZZZBOT_HELPER_INODE", json.dumps(value))
    with pytest.raises(ValueError, match="independently recorded"):
        backend.binding()


def test_snapshot_scope_checks_and_cli(home, monkeypatch, capsys):
    with pytest.raises(ValueError, match="scope"):
        backend.home_snapshot(home, None, 2, [0, 0])
    monkeypatch.setenv("SHAZZZBOT_HELPER_INSTALLING", "0")
    with pytest.raises(ValueError, match="installation identity"):
        snapshot(home)
    monkeypatch.setenv("SHAZZZBOT_HELPER_INSTALLING", "1")
    backend.main(["snapshot", str(home), "", "2"])
    folder = Path(capsys.readouterr().out.strip())
    backend.main(["check-pending", str(home)])
    backend.main(["pending", str(home), str(folder)])
    with pytest.raises(ValueError, match="interrupted"):
        backend.main(["check-pending", str(home)])
    backend.main(["restore", str(folder), "", str(home), "1"])
    with pytest.raises(ValueError, match="recovery"):
        backend.main(["restore", str(folder), "wrong", str(home), "1"])
    backend.main(["commit", str(home)])
    with pytest.raises(ValueError, match="unknown"):
        backend.main(["wrong"])


def test_directory_identity_is_checked_for_every_operation(home, monkeypatch):
    folder = snapshot(home)
    monkeypatch.setenv("SHAZZZBOT_INSTALL_ID", "b" * 32)
    with pytest.raises(ValueError, match="another installation"):
        transaction.manifest(folder)


def test_backend_module_entry(home, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["setup_helper_transaction", "check-pending", str(home)])
    runpy.run_module("tools.setup_helper_transaction", run_name="__main__")


def test_manifest_reads_the_validated_inode_when_parent_path_is_swapped(home, monkeypatch):
    folder = snapshot(home)
    actual = backend.read_at
    original = home.with_name("original-helper")

    def swap(parent, name, **kwargs):
        home.rename(original)
        home.mkdir(mode=0o700)
        (home / "foreign").write_bytes(b"untouched")
        return actual(parent, name, **kwargs)

    monkeypatch.setattr(backend, "read_at", swap)
    value = transaction.manifest(folder)
    assert value["helper_inode"] == backend.binding().inode
    assert (home / "foreign").read_bytes() == b"untouched"
    with pytest.raises(ValueError, match="identity"):
        transaction.save(folder, value)
