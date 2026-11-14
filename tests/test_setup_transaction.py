import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from shazzzbot.helper import footprint
from tools import setup_files
from tools import setup_transaction as transaction


def initialized_helper(helper, identifier="a" * 32):
    data = {"format": 1, "install_id": identifier, "hook": str(footprint.HOOK), "hook_sha256": "", "active": False}
    setup_files.write_safe(helper / ".identity.json", json.dumps(data).encode())


@pytest.fixture(autouse=True)
def helper_pool_paths(monkeypatch):
    monkeypatch.setattr(footprint, "open_home", lambda path: setup_files.opened(path, directory=True))


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAZZZBOT_HELPER_INSTALLING", "1")
    app, helper = (tmp_path / name for name in ("app", "helper"))
    for root in (app, helper):
        root.mkdir(mode=0o700)
    monkeypatch.setenv("SHAZZZBOT_INSTALL_ID", "a" * 32)
    initialized_helper(helper)
    folder = transaction.snapshot(app, helper, tmp_path / "backups", 2)
    return folder, app, helper, None


def test_only_the_write_set_is_restored_and_untouched_helper_updates_survive(journal):
    folder, app, helper, _ = journal
    (app / "compose.yaml").write_bytes(b"old")
    (helper / "helper.pyz").write_bytes(b"old helper")
    transaction.replace(folder, "app", "compose.yaml", b"new")
    transaction.replace(folder, "app", "compose.yaml", b"newer")
    assert (folder / "app/compose.yaml").read_bytes() == b"old"
    (helper / "helper.pyz").write_bytes(b"independent helper update")
    transaction.restore_snapshot(folder, app, helper)
    assert (app / "compose.yaml").read_bytes() == b"old"
    assert (helper / "helper.pyz").read_bytes() == b"independent helper update"






def test_concurrent_edits_are_not_overwritten(journal):
    folder, app, helper, _ = journal
    transaction.replace(folder, "app", "compose.yaml", b"installer")
    (app / "compose.yaml").write_bytes(b"operator")
    with pytest.raises(ValueError, match="outside this transaction"):
        transaction.restore_snapshot(folder, app, helper)
    assert (app / "compose.yaml").read_bytes() == b"operator"


def test_regular_replacement_saves_the_same_durable_intent_before_publication(journal, monkeypatch):
    folder, app, _, _ = journal
    original, candidate = b"old configuration", b"new configuration"
    target = app / "compose.yaml"
    target.write_bytes(original)
    actual_write = transaction.write_safe

    def publish(path, data, **kwargs):
        if path == target:
            recorded = transaction.manifest(folder)["files"]["app/compose.yaml"]
            assert recorded["before"]["sha256"] == transaction.digest(original)
            assert recorded["after"] == transaction.digest(candidate)
            assert target.read_bytes() == original
        actual_write(path, data, **kwargs)

    monkeypatch.setattr(transaction, "write_safe", publish)
    transaction.replace(folder, "app", "compose.yaml", candidate)
    assert target.read_bytes() == candidate


def test_seal_records_a_published_file_so_rollback_can_remove_it(journal):
    folder, app, helper, _ = journal
    transaction.watch(folder, "app", "compose.yaml")
    (app / "compose.yaml").write_bytes(b"published configuration")
    transaction.seal(folder, "app", "compose.yaml")
    recorded = transaction.manifest(folder)["files"]["app/compose.yaml"]
    assert recorded == {"before": None, "after": transaction.digest(b"published configuration")}
    transaction.restore_snapshot(folder, app, helper)
    assert not (app / "compose.yaml").exists()


def test_private_helper_files_restore_only_after_a_helper_mutation(journal):
    folder, app, helper, _ = journal
    transaction.replace(folder, "helper", "policy.json", b"new policy")
    transaction.restore_snapshot(folder, app, helper)
    assert (helper / "policy.json").read_bytes() == b"new policy"
    transaction.restore_snapshot(folder, app, helper, helper_changed=True)
    assert not (helper / "policy.json").exists()


def test_journal_rejects_unknown_paths_versions_and_retention(journal):
    folder, app, helper, _ = journal
    for area, name in (("app", "../../outside"), ("other", "config.json"), ("helper", "foreign")):
        with pytest.raises(ValueError, match="write set"):
            transaction.watch(folder, area, name)
    with pytest.raises(ValueError, match="retention"):
        transaction.snapshot(app, helper, folder.parent, 0)
    value = transaction.manifest(folder)
    value["format"] = 999
    (folder / "manifest.json").write_text(json.dumps(value))
    with pytest.raises(ValueError, match="written by a newer Shazzzbot"):
        transaction.verify_snapshot(folder, app, helper)
    before = (folder / "manifest.json").read_bytes()
    with pytest.raises(ValueError, match="written by a newer Shazzzbot"):
        transaction.snapshot(app, helper, folder.parent, 1)
    assert (folder / "manifest.json").read_bytes() == before


@pytest.mark.parametrize("branch", ["resource-guard", "compose-stop", "helper-stop", "file-restore"])
def test_recovery_errors_keep_files_until_active_resources_are_stopped(tmp_path, branch):
    root = Path(__file__).resolve().parent.parent
    command = f'''source "{root / 'tools/setup_prereqs.sh'}"
APP_DIR="$TARGET" HELPER_DIR="$TARGET" BACKUP_PATH=verified METHOD=compose-image ROLE=bot ST_REF=""
HELPER_CHANGED=0 HELPER_WAS_RUNNING=0 SERVICE_CHANGED=1 PREV_READY=0 CODE_CHANGED=0
export ROLLING_BACK=0
say() {{ :; }}
warn() {{ :; }}
die() {{ exit 1; }}
assert_resources() {{ [ "$BRANCH" != resource-guard ] || die; }}
compose() {{ [ "$BRANCH" != compose-stop ]; }}
service() {{ echo shazzzbot; }}
compose_file() {{ echo compose.yaml; }}
fs() {{ printf '%s\\n' "$*" >> "$TARGET/restores"; [ "$BRANCH" != file-restore ]; }}
stop_helper() {{ return 1; }}
if [ "$BRANCH" = helper-stop ]; then HELPER_CHANGED=1; fi
rollback
'''
    (tmp_path / "compose.yaml").write_text("owned")
    result = subprocess.run(["bash", "-c", command], env={**os.environ, "TARGET": str(tmp_path), "BRANCH": branch},
                            capture_output=True, text=True, check=False, timeout=5)
    assert result.returncode != 0
    if branch == "file-restore":
        assert "restore verified" in (tmp_path / "restores").read_text()
    else:
        assert not (tmp_path / "restores").exists()


def test_transaction_manifest_is_private_and_contains_only_the_touched_files(journal):
    folder, _, _, _ = journal
    transaction.replace(folder, "app", ".env", b"secret")
    value = json.loads((folder / "manifest.json").read_text())
    assert set(value["files"]) == {"app/.env"}
    assert "secret" not in (folder / "manifest.json").read_text()
    assert (folder / "manifest.json").stat().st_mode & 0o077 == 0


def test_watched_but_unmodified_absence_needs_no_cleanup(journal):
    folder, app, helper, _ = journal
    transaction.watch(folder, "app", "config.toml")
    transaction.restore_snapshot(folder, app, helper)
    assert not (app / "config.toml").exists()








def test_helper_backups_stay_inside_its_pinned_home(tmp_path, monkeypatch):
    app, helper = tmp_path / "app", tmp_path / "helper"
    app.mkdir(mode=0o700)
    helper.mkdir(mode=0o700)
    monkeypatch.setenv("SHAZZZBOT_HELPER_INSTALLING", "1")
    monkeypatch.setenv("SHAZZZBOT_INSTALL_ID", "a" * 32)
    initialized_helper(helper)
    folder = transaction.snapshot(app, helper, app / "backups", 2)
    (helper / "config.json").write_bytes(b"old")
    (helper / "config.json").chmod(0o600)
    transaction.replace(folder, "helper", "config.json", b"new")
    assert (helper / "backups" / folder.name / "helper/config.json").read_bytes() == b"old"
    assert not (folder / "helper").exists()
    transaction.restore_snapshot(folder, app, helper, helper_changed=True)
    assert (helper / "config.json").read_bytes() == b"old"
    helper.rename(tmp_path / "replaced")
    helper.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="identity changed"):
        transaction.restore_snapshot(folder, app, helper, helper_changed=True)


def test_snapshot_retention_keeps_unrecognised_or_unverifiable_directories(journal):
    folder, app, helper, _ = journal
    unknown = folder.parent / "operator-files"
    unknown.mkdir(mode=0o700)
    malformed = folder.parent / "20000101-000000.123456abcdef"
    malformed.mkdir(mode=0o700)
    newest = transaction.snapshot(app, helper, folder.parent, 1)
    assert newest.is_dir() and unknown.is_dir() and malformed.is_dir()
    assert not folder.exists()


def test_snapshot_rejects_wrong_installation_and_changed_backup(journal):
    folder, app, helper, _ = journal
    with pytest.raises(ValueError, match="does not match"):
        transaction.verify_snapshot(folder, helper, app)
    (app / "compose.yaml").write_bytes(b"original")
    transaction.replace(folder, "app", "compose.yaml", b"replacement")
    (folder / "app/compose.yaml").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="verification failed"):
        transaction.restore_snapshot(folder, app, helper)
    assert (app / "compose.yaml").read_bytes() == b"replacement"


def test_retention_prunes_helper_payloads_before_their_journals(journal):
    folder, app, helper, _ = journal
    (helper / "policy.json").write_bytes(b"old pin")
    transaction.replace(folder, "helper", "policy.json", b"new pin")
    unrelated = helper / "backups/operator-files"
    unrelated.mkdir(mode=0o700)
    newest = transaction.snapshot(app, helper, folder.parent, 1)
    assert newest.is_dir() and unrelated.is_dir()
    assert not folder.exists() and not (helper / "backups" / folder.name).exists()


def test_retention_handles_absent_helper_payloads(journal):
    folder, _, helper, _ = journal
    transaction.replace(folder, "helper", "policy.json", b"new pin")
    value = transaction.manifest(folder)
    transaction.prune_payload(folder, value)
    (helper / "backups").mkdir(mode=0o700)
    transaction.prune_payload(folder, value)
    value["helper_inode"] = [-1, -1]
    with pytest.raises(ValueError, match="identity changed"):
        transaction.prune_payload(folder, value)
    assert folder.is_dir() and (helper / "policy.json").read_bytes() == b"new pin"


def test_retention_keeps_the_journal_if_helper_cleanup_fails(journal, monkeypatch):
    folder, app, helper, _ = journal
    (helper / "policy.json").write_bytes(b"old pin")
    transaction.replace(folder, "helper", "policy.json", b"new pin")
    def busy(*args, **kwargs):
        raise OSError("busy")
    monkeypatch.setattr(transaction.shutil, "rmtree", busy)
    with pytest.raises(OSError, match="busy"):
        transaction.snapshot(app, helper, folder.parent, 1)
    assert folder.is_dir()
    assert (helper / "backups" / folder.name / "helper/policy.json").read_bytes() == b"old pin"


@pytest.mark.parametrize("unsafe", ["symlink", "parent-mode", "entry-mode"])
def test_retention_never_follows_untrusted_payload_paths(journal, unsafe, tmp_path):
    folder, _, helper, _ = journal
    (helper / "policy.json").write_bytes(b"old pin")
    transaction.replace(folder, "helper", "policy.json", b"new pin")
    parent, entry = helper / "backups", helper / "backups" / folder.name
    if unsafe == "symlink":
        entry.rename(tmp_path / "saved")
        entry.symlink_to(tmp_path / "saved", target_is_directory=True)
    else:
        (parent if unsafe == "parent-mode" else entry).chmod(0o777)
    with pytest.raises((OSError, ValueError)):
        transaction.prune_payload(folder, transaction.manifest(folder))
    assert folder.is_dir()
    assert (entry / "helper/policy.json").read_bytes() == b"old pin"






@pytest.mark.parametrize("area,name", [("helper", "config.json"), ("hook", "script"), ("schedule", "schedule.json")])
def test_watch_keeps_source_and_backup_payload_in_the_validated_helper(journal, tmp_path, monkeypatch, area, name):
    folder, _, helper, _ = journal
    source = helper if area == "helper" else tmp_path / area
    source.mkdir(mode=0o700, exist_ok=True)
    value = transaction.manifest(folder)
    value[area] = str(source)
    transaction.save(folder, value)
    (source / name).write_bytes(b"original")
    (source / name).chmod(0o600)
    foreign, saved = tmp_path / "foreign", tmp_path / "validated"
    foreign.mkdir(mode=0o700)
    (foreign / name).write_bytes(b"foreign")
    original = transaction.helper_home
    @contextmanager
    def swapped(path, expected=None):
        with original(path, expected) as fd:
            helper.rename(saved)
            foreign.rename(helper)
            yield fd
    monkeypatch.setattr(transaction, "helper_home", swapped)
    transaction.watch(folder, area, name)
    assert (saved / "backups" / folder.name / area / name).read_bytes() == b"original"
    assert (helper / name).read_bytes() == b"foreign"
    before = transaction.manifest(folder)["files"][area + "/" + name]["before"]
    assert before["sha256"] == transaction.digest(b"original")


@pytest.mark.parametrize("operation", ["seal", "replace", "verify", "restore", "unlink"])
def test_helper_operations_never_reopen_a_swapped_helper(journal, tmp_path, monkeypatch, operation):
    folder, app, helper, _ = journal
    name = "policy.json"
    if operation != "unlink":
        (helper / name).write_bytes(b"original")
        (helper / name).chmod(0o600)
    transaction.replace(folder, "helper", name, b"installed")
    foreign, saved = tmp_path / "foreign", tmp_path / "validated"
    foreign.mkdir(mode=0o700)
    (foreign / name).write_bytes(b"foreign")
    original = transaction.helper_home
    @contextmanager
    def swapped(path, expected=None):
        with original(path, expected) as fd:
            helper.rename(saved)
            foreign.rename(helper)
            yield fd
    monkeypatch.setattr(transaction, "helper_home", swapped)
    if operation == "seal":
        transaction.seal(folder, "helper", name)
        assert transaction.manifest(folder)["files"]["helper/" + name]["after"] == transaction.digest(b"installed")
    elif operation == "replace":
        transaction.replace(folder, "helper", name, b"replacement")
        assert (saved / name).read_bytes() == b"replacement"
    elif operation == "verify":
        transaction.verify_snapshot(folder, app, helper)
    else:
        transaction.restore_snapshot(folder, app, helper, helper_changed=True)
        if operation == "unlink":
            assert not (saved / name).exists()
        else:
            assert (saved / name).read_bytes() == b"original"
    assert (helper / name).read_bytes() == b"foreign"


def test_snapshot_records_the_validated_helper_inode_even_if_the_name_is_swapped(journal, tmp_path, monkeypatch):
    folder, app, helper, _ = journal
    foreign, saved = tmp_path / "foreign", tmp_path / "validated"
    foreign.mkdir(mode=0o700)
    expected = [helper.stat().st_dev, helper.stat().st_ino]
    original = transaction.helper_home
    @contextmanager
    def swapped(path, expected=None):
        with original(path, expected) as fd:
            helper.rename(saved)
            foreign.rename(helper)
            yield fd
    monkeypatch.setattr(transaction, "helper_home", swapped)
    newest = transaction.snapshot(app, helper, folder.parent, 2)
    assert transaction.manifest(newest)["helper_inode"] == expected
    assert list(helper.iterdir()) == []


@pytest.mark.parametrize("name,mode", [("helper.pyz", 0o700), ("helper.pyz.prev", 0o700), ("api.key", 0o600),
                                      ("shazzzbot-helper-unraid.py", 0o700), ("helper-unraid.json", 0o600)])
def test_helper_publication_applies_final_mode_before_replacement(journal, name, mode):
    folder, _, helper, _ = journal
    transaction.replace(folder, "helper", name, b"published")
    assert (helper / name).stat().st_mode & 0o777 == mode


def test_snapshot_without_helper_installation_does_not_open_or_pin_a_helper(journal, monkeypatch):
    folder, app, helper, _ = journal
    monkeypatch.delenv("SHAZZZBOT_HELPER_INSTALLING")
    def forbidden(*args, **kwargs):
        pytest.fail("an app-only snapshot must not open the helper")
    monkeypatch.setattr(transaction, "helper_home", forbidden)
    newest = transaction.snapshot(app, helper, folder.parent, 2)
    assert transaction.manifest(newest)["helper_inode"] is None


def test_missing_helper_lease_fails_closed():
    with pytest.raises(ValueError, match="validated directory lease"):
        transaction.require_home(None)


@pytest.mark.parametrize("mode", [0o400, 0o700])
def test_helper_restore_preserves_recorded_private_mode_and_ownership(journal, mode):
    folder, app, helper, _ = journal
    path = helper / "config.json"
    path.write_bytes(b"original")
    path.chmod(mode)
    original = path.stat()
    transaction.replace(folder, "helper", "config.json", b"replacement")
    assert path.stat().st_mode & 0o777 == 0o600
    transaction.restore_snapshot(folder, app, helper, helper_changed=True)
    restored = path.stat()
    assert path.read_bytes() == b"original"
    assert (restored.st_mode & 0o777, restored.st_uid, restored.st_gid) == (mode, original.st_uid, original.st_gid)


def test_snapshot_refuses_a_foreign_helper_swapped_in_before_open(journal, tmp_path):
    folder, app, helper, _ = journal
    foreign = tmp_path / "foreign"
    foreign.mkdir(mode=0o700)
    initialized_helper(foreign, "b" * 32)
    (foreign / "config.json").write_bytes(b"foreign")
    helper.rename(tmp_path / "original")
    foreign.rename(helper)
    with pytest.raises(ValueError, match="another installation"):
        transaction.snapshot(app, helper, folder.parent, 2)
    assert (helper / "config.json").read_bytes() == b"foreign"
    assert json.loads((helper / ".identity.json").read_bytes())["install_id"] == "b" * 32


def test_unbound_journal_cannot_write_verify_restore_or_remove_helper_data(journal, monkeypatch):
    folder, app, helper, _ = journal
    monkeypatch.delenv("SHAZZZBOT_HELPER_INSTALLING")
    unbound = transaction.snapshot(app, helper, folder.parent, 2)
    path = helper / "policy.json"
    path.write_bytes(b"untouched")
    transaction.replace(unbound, "app", "compose.yaml", b"app-only")
    assert (app / "compose.yaml").read_bytes() == b"app-only"
    with pytest.raises(ValueError, match="recorded directory identity"):
        transaction.replace(unbound, "helper", "policy.json", b"foreign replacement")
    value = transaction.manifest(unbound)
    value["files"]["helper/policy.json"] = {"before": None, "after": transaction.digest(b"untouched")}
    transaction.save(unbound, value)
    payload = helper / "backups" / unbound.name
    payload.mkdir(parents=True, mode=0o700)
    (payload / "keep").write_bytes(b"retained")
    for operation in (lambda: transaction.verify_snapshot(unbound, app, helper),
                      lambda: transaction.restore_snapshot(unbound, app, helper, helper_changed=True),
                      lambda: transaction.prune_payload(unbound, value)):
        with pytest.raises(ValueError, match="recorded directory identity"):
            operation()
    assert path.read_bytes() == b"untouched"
    assert (payload / "keep").read_bytes() == b"retained"
    assert unbound.is_dir()
