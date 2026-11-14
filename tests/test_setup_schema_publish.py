import os
import stat

import pytest

from tools import setup_transaction as transaction


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    home = tmp_path / "app"
    home.mkdir(mode=0o700)
    config = home / "config.toml"
    config.write_bytes(b"# private comment\r\nname = 'private-value'\r\n")
    config.chmod(0o600)
    monkeypatch.delenv("SHAZZZBOT_HELPER_INSTALLING", raising=False)
    folder = transaction.snapshot(home, tmp_path / "helper", tmp_path / "backups", 2)
    transaction.watch(folder, "app", "config.toml")
    return home, config, config.read_bytes(), folder


def test_migration_backs_up_exact_bytes_before_publish_and_repeat_is_noop(config_home, monkeypatch):
    home, config, original, folder = config_home
    candidate = original + b"# interval = 60\r\n"
    actual_write = transaction.write_at
    calls = []

    def publish(parent, name, data, **kwargs):
        backup = home / "config.toml.bak-1.0.0"
        assert backup.read_bytes() == original
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600
        assert config.read_bytes() == original
        assert transaction.manifest(folder)["files"]["app/config.toml"]["after"] == transaction.digest(data)
        calls.append((name, kwargs))
        actual_write(parent, name, data, **kwargs)

    monkeypatch.setattr(transaction, "write_at", publish)
    assert transaction.migrate_config(home, original, candidate, "1.0.0", folder) is True
    assert config.read_bytes() == candidate
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert calls == [("config.toml", {"uid": 0})]
    before = {path.name: path.stat().st_mtime_ns for path in home.iterdir()}
    journal_before = (folder / "manifest.json").read_bytes(), (folder / "manifest.json").stat().st_mtime_ns
    assert transaction.migrate_config(home, candidate, candidate, "1.0.0", folder) is False
    assert before == {path.name: path.stat().st_mtime_ns for path in home.iterdir()}
    assert journal_before == ((folder / "manifest.json").read_bytes(), (folder / "manifest.json").stat().st_mtime_ns)


def test_matching_existing_backup_is_reused(config_home):
    home, config, original, folder = config_home
    backup = home / "config.toml.bak-1.0.0-rc.1"
    backup.write_bytes(original)
    backup.chmod(0o600)
    stamp = backup.stat().st_mtime_ns
    assert transaction.migrate_config(home, original, original + b"# new\n", "1.0.0-rc.1", folder)
    assert backup.stat().st_mtime_ns == stamp
    assert config.read_bytes() == original + b"# new\n"


@pytest.mark.parametrize("version", ["../outside", "v1.0.0", "1.0", "1.0.0/extra", "1.0.0\n"])
def test_backup_version_cannot_change_the_path(config_home, version):
    home, config, original, folder = config_home
    with pytest.raises(ValueError, match="invalid config backup version"):
        transaction.migrate_config(home, original, original + b"# new\n", version, folder)
    assert config.read_bytes() == original
    assert list(home.iterdir()) == [config]


def test_existing_different_backup_is_never_overwritten(config_home):
    home, config, original, folder = config_home
    backup = home / "config.toml.bak-1.0.0"
    backup.write_bytes(b"other original")
    backup.chmod(0o600)
    with pytest.raises(ValueError, match="different contents"):
        transaction.migrate_config(home, original, original + b"# new\n", "1.0.0", folder)
    assert config.read_bytes() == original
    assert backup.read_bytes() == b"other original"


@pytest.mark.parametrize("target", ["config.toml", "config.toml.bak-1.0.0"])
def test_symlinked_config_or_backup_is_refused(config_home, tmp_path, target):
    home, _config, original, folder = config_home
    outside = tmp_path / "outside"
    outside.write_bytes(original)
    outside.chmod(0o600)
    path = home / target
    path.unlink(missing_ok=True)
    path.symlink_to(outside)
    with pytest.raises(OSError):
        transaction.migrate_config(home, original, original + b"# new\n", "1.0.0", folder)
    assert outside.read_bytes() == original


def test_changed_config_is_refused_without_a_backup(config_home):
    home, config, original, folder = config_home
    config.write_bytes(b"operator edit")
    with pytest.raises(ValueError, match="changed during setup"):
        transaction.migrate_config(home, original, original + b"# new\n", "1.0.0", folder)
    assert config.read_bytes() == b"operator edit"
    assert list(home.iterdir()) == [config]


def test_change_while_backing_up_is_not_replaced(config_home, monkeypatch):
    home, config, original, folder = config_home
    actual_fsync = os.fsync

    def fsync(fd):
        actual_fsync(fd)
        config.write_bytes(b"operator edit")

    monkeypatch.setattr(transaction.os, "fsync", fsync)
    with pytest.raises(ValueError, match="changed during setup"):
        transaction.migrate_config(home, original, original + b"# new\n", "1.0.0", folder)
    assert config.read_bytes() == b"operator edit"
    assert (home / "config.toml.bak-1.0.0").read_bytes() == original


def test_atomic_publish_failure_preserves_original_and_backup(config_home, monkeypatch):
    home, config, original, folder = config_home
    actual_replace = os.replace

    def fail(*args, **kwargs):
        if args[1] == "config.toml":
            raise OSError("publish failed")
        return actual_replace(*args, **kwargs)

    monkeypatch.setattr(transaction.os, "replace", fail)
    with pytest.raises(OSError, match="publish failed"):
        transaction.migrate_config(home, original, original + b"# new\n", "1.0.0", folder)
    assert config.read_bytes() == original
    assert (home / "config.toml.bak-1.0.0").read_bytes() == original
    assert sorted(path.name for path in home.iterdir()) == ["config.toml", "config.toml.bak-1.0.0"]


def test_private_config_owner_is_preserved(config_home):
    home, config, original, folder = config_home
    os.chown(config, 10001, 10001)
    assert transaction.migrate_config(home, original, original + b"# new\n", "1.0.0", folder)
    assert config.stat().st_uid == 10001
    assert (home / "config.toml.bak-1.0.0").stat().st_uid == 0


def test_untrusted_config_owner_is_refused(config_home):
    home, config, original, folder = config_home
    os.chown(config, 10002, 10002)
    with pytest.raises(ValueError, match="root or uid 10001"):
        transaction.migrate_config(home, original, original + b"# new\n", "1.0.0", folder)
    assert config.read_bytes() == original
    assert list(home.iterdir()) == [config]


def test_app_parent_swap_cannot_redirect_config_publish(config_home, tmp_path, monkeypatch):
    home, _config, original, folder = config_home
    actual_fsync = os.fsync
    moved = tmp_path / "moved"
    replacement = tmp_path / "replacement"
    replacement.mkdir(mode=0o700)
    (replacement / "config.toml").write_bytes(b"unrelated config")

    def fsync(fd):
        actual_fsync(fd)
        if home.is_dir() and not home.is_symlink():
            home.rename(moved)
            home.symlink_to(replacement, target_is_directory=True)

    monkeypatch.setattr(transaction.os, "fsync", fsync)
    candidate = original + b"# new\n"
    assert transaction.migrate_config(home, original, candidate, "1.0.0", folder)
    assert (moved / "config.toml").read_bytes() == candidate
    assert (moved / "config.toml.bak-1.0.0").read_bytes() == original
    assert (replacement / "config.toml").read_bytes() == b"unrelated config"


def test_crash_after_publication_before_seal_can_restore_the_original(config_home, monkeypatch):
    home, config, original, folder = config_home
    candidate = original + b"# new default\n"
    actual_write = transaction.write_at

    class Crash(BaseException):
        pass

    def publish_then_crash(*args, **kwargs):
        actual_write(*args, **kwargs)
        raise Crash

    monkeypatch.setattr(transaction, "write_at", publish_then_crash)
    with pytest.raises(Crash):
        transaction.migrate_config(home, original, candidate, "1.0.0", folder)
    assert config.read_bytes() == candidate
    assert transaction.manifest(folder)["files"]["app/config.toml"]["after"] == transaction.digest(candidate)
    transaction.restore_snapshot(folder, home, home.parent / "helper")
    assert config.read_bytes() == original
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert (home / "config.toml.bak-1.0.0").read_bytes() == original


def test_intent_write_failure_never_publishes_the_candidate(config_home, monkeypatch):
    home, config, original, folder = config_home

    def fail(*args):
        raise OSError("intent write failed")

    monkeypatch.setattr(transaction, "save", fail)
    with pytest.raises(OSError, match="intent write failed"):
        transaction.migrate_config(home, original, original + b"# new\n", "1.0.0", folder)
    assert config.read_bytes() == original
    assert transaction.manifest(folder)["files"]["app/config.toml"]["after"] == transaction.digest(original)


@pytest.mark.parametrize("bad", ["app", "unwatched", "before", "digest", "backup", "list", "string", "null"])
def test_migration_refuses_the_wrong_journal_or_preimage_before_writing(config_home, bad):
    home, config, original, folder = config_home
    value = transaction.manifest(folder)
    if bad == "app":
        value["app"] = str(home / "other")
    elif bad == "unwatched":
        value["files"] = {}
    elif bad == "before":
        value["files"]["app/config.toml"]["before"] = None
    elif bad == "digest":
        value["files"]["app/config.toml"]["before"]["sha256"] = "f" * 64
    elif bad == "backup":
        (folder / "app/config.toml").write_bytes(b"corrupt preimage")
    else:
        value["files"] = {"list": [], "string": "invalid", "null": None}[bad]
    transaction.save(folder, value)
    with pytest.raises(ValueError, match="watched preimage"):
        transaction.migrate_config(home, original, original + b"# new\n", "1.0.0", folder)
    assert config.read_bytes() == original
    assert not list(home.glob("config.toml.bak-*"))


def test_operator_edit_after_intent_is_not_replaced_or_overwritten_by_recovery(config_home, monkeypatch):
    home, config, original, folder = config_home
    actual_save = transaction.save

    def intent_then_edit(*args):
        actual_save(*args)
        config.write_bytes(b"operator edit")

    monkeypatch.setattr(transaction, "save", intent_then_edit)
    with pytest.raises(ValueError, match="changed during setup"):
        transaction.migrate_config(home, original, original + b"# new\n", "1.0.0", folder)
    with pytest.raises(ValueError, match="outside this transaction"):
        transaction.restore_snapshot(folder, home, home.parent / "helper")
    assert config.read_bytes() == b"operator edit"
