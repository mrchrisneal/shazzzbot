import hashlib
import json
import os
import select
import signal
import stat
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
from tests.test_setup_launcher import launcher as helper_launch
from tools import setup_files as files
from tools import setup_helper
from tools import setup_manage as management
from tools import setup_transaction as transaction


@pytest.fixture
def rollback_home(tree, tmp_path, monkeypatch):
    from shazzzbot.helper import footprint

    _, home, _ = tree
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    monkeypatch.setattr(footprint, "WORK", work)
    old = b"verified previous archive"
    files.write_safe(home / "helper.pyz", old)
    policy = {"format": 1, "artifact": str(home / "helper.pyz"), "config": str(home / "config.json"),
              "sha256": hashlib.sha256(old).hexdigest()}
    files.write_safe(home / "policy.json", json.dumps(policy).encode())
    info = home.stat()
    return home, work / "rollback.pyz", [info.st_dev, info.st_ino], old


@pytest.mark.parametrize("point", ["before-write", "after-write", "after-ack", "missing-current"])
def test_rollback_selection_stages_only_a_pinned_archive_before_releasing_home(rollback_home, point):
    home, stage, expected, old = rollback_home
    if point != "before-write":
        files.write_safe(home / "helper.pyz.prev", old)
        files.write_safe(home / "helper.pyz", b"new archive not committed to policy")
        if point == "missing-current":
            (home / "helper.pyz").unlink()
    before = {p.name: p.read_bytes() for p in home.iterdir()}
    assert files.select_pinned_helper(home, stage, expected) == hashlib.sha256(old).hexdigest()
    assert stage.read_bytes() == old and stat.S_IMODE(stage.stat().st_mode) == 0o700
    assert before == {p.name: p.read_bytes() for p in home.iterdir()}


@pytest.mark.parametrize("bad", ["unpinned", "missing", "symlink", "owner", "mode", "oversize", "empty"])
def test_no_valid_rollback_archive_never_creates_a_stage_or_changes_previous(rollback_home, tmp_path, bad):
    home, stage, expected, old = rollback_home
    current = home / "helper.pyz"
    if bad == "unpinned":
        files.write_safe(current, b"untrusted archive")
    elif bad == "missing":
        current.unlink()
    elif bad == "symlink":
        current.unlink()
        outside = tmp_path / "outside"
        files.write_safe(outside, old)
        current.symlink_to(outside)
    elif bad == "owner":
        os.chown(current, 10001, 10001)
    elif bad == "mode":
        current.chmod(0o644)
    elif bad == "oversize":
        current.write_bytes(b"x" * (1024**2 + 1))
    else:
        current.write_bytes(b"")
    files.write_safe(home / "helper.pyz.prev", b"unpinned previous archive")
    with pytest.raises(ValueError, match="no pinned helper archive"):
        files.select_pinned_helper(home, stage, expected)
    assert not stage.exists()
    assert (home / "helper.pyz.prev").read_bytes() == b"unpinned previous archive"


@pytest.mark.parametrize("bad", [[], {}, {"extra": True}, {"sha256": "bad"}, {"previous_sha256": 1},
                                {"format": 1, "artifact": "/outside/helper.pyz"}, {"config": "/outside/config.json"}])
def test_rollback_selection_refuses_untrusted_or_foreign_policies(rollback_home, bad):
    home, stage, expected, _ = rollback_home
    policy = json.loads((home / "policy.json").read_bytes())
    if isinstance(bad, dict) and bad:
        policy.update(bad)
    else:
        policy = {"format": 1, **bad} if isinstance(bad, dict) else bad
    files.write_safe(home / "policy.json", json.dumps(policy).encode())
    with pytest.raises(ValueError, match="must be an object with a format" if bad == [] else "rollback policy"):
        files.select_pinned_helper(home, stage, expected)
    assert not stage.exists()


def test_rollback_selection_honours_the_previous_policy_pin(rollback_home):
    home, stage, expected, old = rollback_home
    policy = json.loads((home / "policy.json").read_bytes())
    policy["previous_sha256"], policy["sha256"] = policy["sha256"], "f" * 64
    files.write_safe(home / "policy.json", json.dumps(policy).encode())
    assert files.select_pinned_helper(home, stage, expected) == hashlib.sha256(old).hexdigest()


@pytest.mark.parametrize("bad", ["outside", "name", "mode", "inode", "symlink"])
def test_rollback_selection_checks_private_work_and_recorded_home_identity(rollback_home, tmp_path, bad):
    home, stage, expected, _ = rollback_home
    if bad == "outside":
        stage = tmp_path / "rollback.pyz"
    elif bad == "name":
        stage = stage.with_name("other.pyz")
    elif bad == "mode":
        stage.parent.chmod(0o755)
    elif bad == "inode":
        expected = [0, 0]
    else:
        original = home.with_name("original")
        home.rename(original)
        home.symlink_to(original, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        files.select_pinned_helper(home, stage, expected)
    assert not stage.exists()


def test_rollback_selection_keeps_the_validated_home_fd_during_parent_swap(rollback_home, tmp_path, monkeypatch):
    home, stage, expected, old = rollback_home
    moved = tmp_path / "moved"
    actual_read = files.read_at

    def swap(fd, name, *args, **kwargs):
        data = actual_read(fd, name, *args, **kwargs)
        if name == "policy.json":
            home.rename(moved)
            home.mkdir(mode=0o700)
            files.write_safe(home / "helper.pyz", b"foreign")
        return data

    monkeypatch.setattr(files, "read_at", swap)
    assert files.select_pinned_helper(home, stage, expected) == hashlib.sha256(old).hexdigest()
    assert stage.read_bytes() == old and (home / "helper.pyz").read_bytes() == b"foreign"


@pytest.fixture
def tree(tmp_path, monkeypatch):
    app, helper, backups = (tmp_path / name for name in ("app", "helper", "backups"))
    app.mkdir()
    helper.mkdir(mode=0o700)
    (app / ".env").write_text("DISCORD_TOKEN=never-print-me\n")
    (app / ".env").chmod(0o600)
    machine = tmp_path / "machine-id"
    machine.write_text("1" * 32)
    monkeypatch.setenv("SHAZZZBOT_MACHINE_ID", str(machine))
    monkeypatch.setenv("SHAZZZBOT_INSTALL_ID", "a" * 32)
    monkeypatch.setenv("SHAZZZBOT_HELPER_INSTALLING", "1")
    from shazzzbot.helper import footprint

    identity = {"format": 1, "install_id": "a" * 32, "hook": str(footprint.HOOK),
                "hook_sha256": "", "active": False}
    (helper / ".identity.json").write_text(json.dumps(identity))
    (helper / ".identity.json").chmod(0o600)

    @contextmanager
    def home(path):
        with files.opened(path, directory=True) as fd:
            if stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
                raise ValueError("helper home must have mode 0700")
            yield fd

    monkeypatch.setattr(footprint, "open_home", home)
    return app, helper, backups


def test_backup_is_complete_verified_and_restores_absence_and_secrets(tree):
    app, helper, backups = tree
    (helper / "api.key").write_text("k" * 32)
    (helper / "api.key").chmod(0o600)
    (app / "compose.yaml").write_text("services: {}")
    folder = transaction.snapshot(app, helper, backups, 2)
    for area, name in (("app", ".env"), ("app", "compose.yaml"), ("app", "config.toml"), ("helper", "api.key")):
        transaction.watch(folder, area, name)
    manifest = transaction.verify_snapshot(folder, app, helper)
    assert set(manifest["files"]) == {"app/.env", "app/compose.yaml", "app/config.toml", "helper/api.key"}
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in folder.rglob("*") if path.is_file())
    transaction.replace(folder, "app", ".env", b"changed")
    transaction.replace(folder, "app", "config.toml", b"created after snapshot")
    transaction.restore_snapshot(folder, app, helper, helper_changed=True)
    assert (app / ".env").read_text() == "DISCORD_TOKEN=never-print-me\n"
    assert not (app / "config.toml").exists()
    assert (helper / "api.key").read_text() == "k" * 32


def test_backup_rotation_ignores_unmarked_newline_names_and_other_installations(tree, tmp_path):
    app, helper, backups = tree
    backups.mkdir()
    foreign = backups / "20000101-000000.bad\n.git"
    foreign.mkdir()
    sentinel = foreign / "keep"
    sentinel.write_text("keep")
    other = tmp_path / "other"
    other.mkdir()
    previous = transaction.snapshot(other, helper, backups, 1)
    first = transaction.snapshot(app, helper, backups, 1)
    second = transaction.snapshot(app, helper, backups, 1)
    assert not first.exists() and second.exists()
    assert previous.exists() and sentinel.read_text() == "keep"


def test_corrupt_backup_is_rejected_before_restore(tree):
    app, helper, backups = tree
    folder = transaction.snapshot(app, helper, backups, 2)
    transaction.watch(folder, "app", ".env")
    (folder / "app" / ".env").write_text("corrupt")
    with pytest.raises(ValueError, match="verification failed"):
        transaction.restore_snapshot(folder, app, helper)
    assert (app / ".env").read_text() == "DISCORD_TOKEN=never-print-me\n"
    with pytest.raises(ValueError, match="match this installation"):
        transaction.verify_snapshot(folder, app.parent, helper)


@pytest.mark.parametrize("kind", ["symlink", "writable", "owner", "directory", "fifo", "parent", "control"])
def test_trusted_reads_fail_closed(tree, kind):
    app, _, _ = tree
    path = app / ".env"
    if kind == "symlink":
        path.unlink()
        path.symlink_to(app / "missing")
    elif kind == "writable":
        path.chmod(0o620)
    elif kind == "owner":
        if os.geteuid() != 0:
            pytest.skip("requires chown")
        os.chown(path, 10001, -1)
    elif kind == "directory":
        path.unlink()
        path.mkdir()
    elif kind == "fifo":
        path.unlink()
        os.mkfifo(path)
    elif kind == "parent":
        path = app / ".." / "escape"
    else:
        path = app / "line\nbreak"
    with pytest.raises((OSError, ValueError)):
        files.read_safe(path, private=True)


def test_atomic_write_does_not_follow_replaced_leaf_and_rechecks_parent(tree):
    app, _, _ = tree
    outside = app / "other"
    outside.write_text("keep")
    link = app / "link"
    link.symlink_to(outside)
    files.write_safe(link, b"new")
    assert outside.read_text() == "keep" and not link.is_symlink() and link.read_bytes() == b"new"
    app.chmod(0o777)
    with pytest.raises(ValueError, match="untrusted"):
        files.write_safe(link, b"no")
    assert link.read_bytes() == b"new"


def test_private_permission_drift_and_identity_are_checked(tree):
    app, _, _ = tree
    before = management.identity(app, "bot", "docker-run", "v1.2.3")
    files.write_safe(app / ".setup-identity", before)
    management.manage(["drift", str(app), "bot", "docker-run", "v1.2.3"])
    (app / ".env").chmod(0o644)
    with pytest.raises(ValueError, match="secret file"):
        management.identity(app, "bot", "docker-run", "v1.2.3")
    (app / ".env").chmod(0o600)
    (app / ".env").write_text("changed")
    with pytest.raises(ValueError, match="changed"):
        management.manage(["drift", str(app), "bot", "docker-run", "v1.2.3"])


def process_start(pid):
    return Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[19]


def test_helper_pid_identity_and_drain_timeout_are_verified(tree, tmp_path, capsys):
    if os.geteuid() != 0:
        pytest.skip("the helper runs as root")
    _, helper, _ = tree
    binary = helper / "helper.pyz"
    binary.write_text("import time\nprint('ready', flush=True)\ntime.sleep(60)\n")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    config = helper / "config.json"
    config.write_text(json.dumps({"format": 1, "socket": str(runtime / "helper.sock"), "action_timeout": 900}))
    config.chmod(0o600)
    child = subprocess.Popen([sys.executable, str(binary)], stdout=subprocess.PIPE)
    try:
        assert child.stdout is not None and select.select([child.stdout], [], [], 5)[0]
        assert child.stdout.readline() == b"ready\n"
        pidfile = runtime / "helper.pid"
        item = {"pid": child.pid, "start": process_start(child.pid), "path": str(binary), "action_timeout": 7200}
        pidfile.write_text(json.dumps(item))
        pidfile.chmod(0o600)
        setup_helper.process(config, binary, "helper")
        assert capsys.readouterr().out == "7230\n"
        item["start"] = "wrong"
        pidfile.write_text(json.dumps(item))
        with pytest.raises(ValueError, match="identity mismatch"):
            setup_helper.process(config, binary, "signal")
        assert child.poll() is None
        item["start"] = process_start(child.pid)
        pidfile.write_text(json.dumps(item))
        setup_helper.process(config, binary, "signal")
        child.wait(timeout=5)
        assert child.returncode == -signal.SIGTERM
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
        child.stdout.close()


def test_hook_executes_only_a_pinned_home_descriptor_with_a_clean_environment(tmp_path, monkeypatch):
    from tests.test_setup_launcher import make_home

    monkeypatch.setattr(helper_launch, "WORK", tmp_path / "work")
    nested = tmp_path / "home with spaces ' and $()"
    nested.mkdir(mode=0o700)
    home, hook = make_home(nested)
    calls = []
    monkeypatch.setattr(helper_launch, "mounted", lambda _: True)
    monkeypatch.setattr(os, "chdir", lambda _: None)
    monkeypatch.setattr(os, "execve", lambda binary, args, env:
                        calls.append((binary, args, env, os.read(int(env["SHAZZZBOT_HELPER_ARCHIVE_FD"]), 1024))))
    monkeypatch.setenv("SHAZZZBOT_DISCORD_TOKEN", "never-inherit")
    assert helper_launch.launch(home, hook)
    assert calls[0][1][:3] == ["/usr/bin/python3", "-I", "-B"]
    assert calls[0][1][3].startswith("/proc/self/fd/")
    assert calls[0][1][4:] == ["--config", str(home / "config.json")]
    assert set(calls[0][2]) == {"PATH", "LANG", "HOME", "TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
                               "XDG_DATA_HOME", "SHAZZZBOT_HELPER_ARCHIVE_FD"}
    assert calls[0][3] == b"verified archive"
    (home / "helper.pyz").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="no verified helper archive"):
        helper_launch.launch(home, hook)
    assert len(calls) == 1


def test_snapshot_records_immutable_previous_images(tree):
    app, helper, backups = tree
    bot_image, proxy_image = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    folder = transaction.snapshot(app, helper, backups, 1, bot_image, proxy_image)
    assert transaction.verify_snapshot(folder, app, helper)["images"] == {"bot": bot_image, "proxy": proxy_image}


def test_rollback_does_not_stop_an_untouched_helper(tmp_path):
    command = f'''source "{ROOT / 'tools/setup_prereqs.sh'}"
ROLLING_BACK=0 BACKUP_PATH=verified HELPER_CHANGED=0 SERVICE_CHANGED=0 ST_REF="" METHOD=docker-run
APP_DIR="$TARGET" HELPER_DIR="$TARGET" CODE_CHANGED=0 PREV_READY=0 HELPER_WAS_RUNNING=0
stop_helper() {{ touch "$TARGET/stopped"; }}
fs() {{ return 0; }}
say() {{ :; }}
rollback
'''
    result = subprocess.run(["bash", "-c", command], env={**os.environ, "TARGET": str(tmp_path)},
                            capture_output=True, text=True, check=False, timeout=5)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "stopped").exists()


def test_trusted_code_rejects_writable_git_metadata_and_links(tree):
    app, _, _ = tree
    git = app / ".git"
    git.mkdir()
    config = git / "config"
    config.write_text("[core]\n")
    config.chmod(0o644)
    files.trusted_code(app)
    config.chmod(0o666)
    with pytest.raises(ValueError, match="untrusted"):
        files.trusted_code(app)
    config.unlink()
    config.symlink_to(app / ".env")
    with pytest.raises(OSError):
        files.trusted_code(app)


def test_large_files_and_stream_growth_are_bounded(tree, monkeypatch):
    app, _, _ = tree
    path = app / "large"
    path.write_bytes(b"12345")
    monkeypatch.setattr(files, "MAX_FILE", 4)
    with pytest.raises(ValueError, match="size limit"):
        files.read_safe(path)
    original = os.fstat
    def small(fd):
        values = list(original(fd))
        values[6] = min(values[6], 4)
        return os.stat_result(values)
    monkeypatch.setattr(files.os, "fstat", small)
    with pytest.raises(ValueError, match="size limit"):
        files.read_safe(path)


def test_private_file_errors_include_the_required_remedy(tree):
    app, _, _ = tree
    path = app / ".env"
    path.chmod(0o644)
    with pytest.raises(ValueError, match="0600.*0700 on vfat"):
        files.inspect_file(path, private=True, uid=os.geteuid())
    path.chmod(0o600)
    files.inspect_file(path, private=True, uid=os.geteuid())


def test_machine_identity_is_validated_without_generating_or_changing_it(tree):
    app, _, _ = tree
    path = app / "machine-id"
    for content in ("invalid", "0" * 32):
        path.write_text(content)
        with pytest.raises(ValueError, match="machine-id"):
            files.host_identity(path)
        assert path.read_text() == content
    first = files.install_identity("1" * 32, app)
    assert len(first) == 32 and int(first, 16) >= 0
    files.write_safe(app / ".setup-identity", json.dumps({"format": 2, "host": "1" * 32, "app": str(app),
                                                        "install_id": first}).encode())
    assert files.install_identity("1" * 32, app) == first
    with pytest.raises(ValueError, match="machine id"):
        files.install_identity("invalid", app)


def test_config_cannot_be_owned_by_an_unrelated_user(tree):
    if os.geteuid() != 0:
        pytest.skip("requires chown")
    app, _, _ = tree
    path = app / "config.toml"
    path.write_bytes(b"private")
    os.chown(path, 12345, -1)
    with pytest.raises(ValueError, match="owned by root or uid 10001"):
        files.read_safe(path)


def test_trusted_code_walks_nested_directories_and_top_level_files(tree):
    app, _, _ = tree
    (app / "assets/icons").mkdir(parents=True)
    (app / "assets/icons/file").write_bytes(b"safe")
    (app / "Dockerfile").write_text("FROM scratch\n")
    files.trusted_code(app)


def test_helper_home_keeps_the_validated_inode_for_writes_and_removal(setup):
    root = setup[3].parent
    info = root.stat()
    expected = [info.st_dev, info.st_ino]
    with files.helper_home(root, expected) as fd:
        retained = root.with_name("retained-home")
        root.rename(retained)
        root.mkdir(mode=0o700)
        (root / "keep").write_bytes(b"replacement")
        files.write_at(fd, "backups/private", b"original")
        data, saved = files.read_at(fd, "backups/private", private=True)
        assert data == b"original" and saved.st_ino == (retained / "backups/private").stat().st_ino
        files.unlink_at(fd, "backups/private")
        assert not (retained / "backups/private").exists()
        assert (root / "keep").read_bytes() == b"replacement"
        assert not (root / "backups").exists()
    with pytest.raises(ValueError, match="identity changed"), files.helper_home(root, expected):
        pass
    with files.helper_home(root) as fd:
        assert os.fstat(fd).st_ino == root.stat().st_ino


@pytest.mark.parametrize("expected", [[], [1], [1, 2, 3], [True, 2], [-1, 2], ["1", 2], (1, 2)])
def test_helper_home_refuses_malformed_inode_records(setup, expected):
    with pytest.raises(ValueError, match="identity changed"), files.helper_home(setup[3].parent, expected):
        pass


@pytest.mark.parametrize("relative", ["", ".", "..", "../escape", "/absolute", "a/../escape", "a//b", "a/./b",
                                      "bad\nname", "bad\rname", "bad\0name", None])
@pytest.mark.parametrize("operation", ["read", "write", "unlink"])
def test_fd_relative_operations_refuse_invalid_names(tree, relative, operation):
    app = tree[0]
    with files.opened(app, directory=True) as fd, pytest.raises(ValueError, match="relative"):
        if operation == "write":
            files.write_at(fd, relative, b"never")
        elif operation == "read":
            files.read_at(fd, relative)
        else:
            files.unlink_at(fd, relative)
    assert sorted(path.name for path in app.iterdir()) == [".env"]


@pytest.mark.parametrize("change", ["symlink", "owner", "writable"])
@pytest.mark.parametrize("operation", ["read", "write", "unlink"])
def test_fd_relative_parent_walk_refuses_untrusted_directories(tree, change, operation):
    app = tree[0]
    parent = app / "nested"
    parent.mkdir(mode=0o700)
    (parent / "keep").write_bytes(b"retain")
    if change == "symlink":
        parent.rename(app / "original")
        parent.symlink_to(app / "original", target_is_directory=True)
    elif change == "owner":
        os.chown(parent, 10001, -1)
    else:
        parent.chmod(0o777)
    with files.opened(app, directory=True) as fd, pytest.raises((OSError, ValueError)):
        if operation == "read":
            files.read_at(fd, "nested/keep")
        elif operation == "write":
            files.write_at(fd, "nested/keep", b"never")
        else:
            files.unlink_at(fd, "nested/keep")
    assert (parent / "keep").read_bytes() == b"retain"


def test_fd_reads_pair_data_and_metadata_from_one_open_leaf(tree, monkeypatch):
    app = tree[0]
    path = app / ".env"
    original = files._read_fd
    previous = path.stat()

    def swap(fd, **kwargs):
        path.rename(app / "retained")
        path.write_bytes(b"replacement")
        path.chmod(0o600)
        return original(fd, **kwargs)

    monkeypatch.setattr(files, "_read_fd", swap)
    with files.opened(app, directory=True) as fd:
        data, info = files.read_at(fd, ".env", private=True)
    assert data == b"DISCORD_TOKEN=never-print-me\n" and info.st_ino == previous.st_ino
    assert path.read_bytes() == b"replacement"


def test_fd_writes_apply_metadata_before_publication_and_keep_the_pinned_parent(tree, monkeypatch):
    app = tree[0]
    retained = app.with_name("retained")
    replace = os.replace
    seen = []

    def publish(source, destination, **kwargs):
        info = os.stat(source, dir_fd=kwargs["src_dir_fd"], follow_symlinks=False)
        seen.append((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)))
        app.rename(retained)
        app.mkdir(mode=0o700)
        (app / "code").write_bytes(b"replacement")
        replace(source, destination, **kwargs)

    monkeypatch.setattr(files.os, "replace", publish)
    with files.opened(app, directory=True) as fd:
        files.write_at(fd, "code", b"verified", uid=10001, gid=10002, mode=0o700)
    assert seen == [(10001, 10002, 0o700)]
    assert (retained / "code").read_bytes() == b"verified"
    assert (app / "code").read_bytes() == b"replacement"


@pytest.mark.parametrize("kwargs", [{"uid": True}, {"uid": -1}, {"gid": "root"}, {"gid": 2**32 - 1},
                                    {"mode": True}, {"mode": -1}, {"mode": 0o4755}, {"mode": 0o666}])
def test_fd_writes_refuse_invalid_metadata_before_creating_paths(tree, kwargs):
    app = tree[0]
    with files.opened(app, directory=True) as fd, pytest.raises(ValueError, match="ownership or mode"):
        files.write_at(fd, "new/path", b"never", **kwargs)
    assert not (app / "new").exists()


def test_failed_fd_write_removes_only_its_temporary_file(tree, monkeypatch):
    app = tree[0]

    def failure(*_args, **_kwargs):
        raise OSError("publish failed")

    monkeypatch.setattr(files.os, "replace", failure)
    with files.opened(app, directory=True) as fd:
        with pytest.raises(OSError, match="publish failed"):
            files.write_at(fd, ".env", b"never", gid=10002)
        assert files.read_at(fd, ".env", private=True)[0] == b"DISCORD_TOKEN=never-print-me\n"
    assert sorted(path.name for path in app.iterdir()) == [".env"]


def test_fd_operations_do_not_create_missing_read_or_unlink_parents(tree):
    app = tree[0]
    with files.opened(app, directory=True) as fd:
        with pytest.raises(FileNotFoundError):
            files.read_at(fd, "missing/file")
        with pytest.raises(FileNotFoundError):
            files.unlink_at(fd, "missing/file")
        with pytest.raises(OSError):
            files.read_at(fd, ".env/child")
    assert not (app / "missing").exists()


def test_fd_operations_recheck_the_root_fd_and_refuse_leaf_links(tree):
    app = tree[0]
    with files.opened(app, directory=True) as fd:
        (app / "link").symlink_to(app / ".env")
        with pytest.raises(OSError):
            files.read_at(fd, "link")
        files.unlink_at(fd, "link")
        assert (app / ".env").exists() and not (app / "link").is_symlink()
        app.chmod(0o777)
        with pytest.raises(ValueError, match="untrusted"):
            files.write_at(fd, "never", b"never")
