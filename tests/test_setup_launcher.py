import hashlib
import importlib.util
import json
import os
import runpy
import stat
import sys
from pathlib import Path

import pytest

from tools import build_helper, setup_schedule

spec = importlib.util.spec_from_file_location("shazzzbot-helper-unraid", Path(__file__).resolve().parents[1]
                                             / "shazzzbot-helper-unraid.py")
launcher = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = launcher
spec.loader.exec_module(launcher)


def private(path, data):
    path.write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode())
    path.chmod(0o600)
    return path


def platform_entry(root):
    source = Path(launcher.__file__).read_bytes()
    private(root / launcher.ENTRY, source)
    private(root / "helper-unraid.json", {"format": 1, "platform": "unraid", "version": "v1.0.0",
        "revision": "a" * 40, "files": {launcher.ENTRY: {"sha256": hashlib.sha256(source).hexdigest(),
                                                        "size": len(source)}}})


def make_home(tmp_path):
    root = tmp_path / "home"
    root.mkdir(mode=0o700)
    hook = tmp_path / "user.scripts/scripts/shazzzbot-helper/script"
    hook.parent.mkdir(parents=True)
    private(hook, b"verified hook")
    private(hook.parent.parent.parent / "schedule.json",
            {"shazzzbot-helper": {"frequency": "boot", "script": str(hook)}})
    private(root / ".identity.json", {"format": 1, "install_id": "a" * 32, "hook": str(hook),
                                      "hook_sha256": hashlib.sha256(hook.read_bytes()).hexdigest(), "active": True})
    private(root / "config.json", {"format": 1, "key_file": str(root / "api.key"),
                                   "socket": str(launcher.WORK / "socket/helper.sock"),
                                   "installation_policy": str(root / "policy.json")})
    private(root / "api.key", b"a" * 32)
    private(root / "helper.pyz", b"verified archive")
    platform_entry(root)
    private(root / "policy.json", {"format": 1, "artifact": str(root / "helper.pyz"),
                                   "config": str(root / "config.json"),
                                   "sha256": hashlib.sha256((root / "helper.pyz").read_bytes()).hexdigest()})
    return root, hook


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "WORK", tmp_path / "work")
    return make_home(tmp_path)


def test_mount_check_only_accepts_a_mounted_writable_real_pool(tmp_path, monkeypatch):
    path = tmp_path / "mountinfo"
    monkeypatch.setattr(launcher, "MOUNTINFO", path)
    path.write_text("bad\n1 0 0:1 / /mnt/cache rw - tmpfs source rw\n"
                    "2 0 0:2 / /mnt/cache ro - xfs source rw\n"
                    "3 0 0:3 / /mnt/cache rw - xfs source ro\n")
    assert not launcher.mounted(Path("/mnt/cache/appdata/helper"))
    path.write_text("1 0 0:1 / /mnt/cache rw - xfs /dev/disk rw\n")
    assert launcher.mounted(Path("/mnt/cache/appdata/helper"))
    path.write_text("x" * (launcher.MAX_BYTES + 1))
    with pytest.raises(ValueError, match="size limit"):
        launcher.mounted(Path("/mnt/cache/appdata/helper"))


@pytest.mark.parametrize("path", ["relative", "/mnt/user/appdata/helper", "/mnt/user0/appdata/helper",
                                      "/mnt/disk1/appdata/helper", "/mnt/cache/other/helper",
                                      "/mnt/cache/appdata/helper/extra", "/mnt/cache/appdata/../helper"])
def test_mount_check_refuses_uncertain_locations(path):
    with pytest.raises(ValueError, match="real pool"):
        launcher.mounted(Path(path))


def test_directory_walk_rejects_symlinks_wrong_owner_and_permissions(home, tmp_path):
    root, _ = home
    with pytest.raises(ValueError, match="invalid helper path"):
        launcher.directory(Path("relative"))
    with pytest.raises(ValueError, match="invalid helper path"):
        launcher.directory(Path("/a/../b"))
    alias = tmp_path / "link"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(OSError):
        launcher.directory(alias, home=True)
    root.chmod(0o750)
    with pytest.raises(ValueError, match="permissions"):
        launcher.directory(root, home=True)
    root.chmod(0o700)
    os.chown(root, 1000, -1)
    with pytest.raises(ValueError, match="ownership"):
        launcher.directory(root, home=True)


def test_directory_ignores_only_pool_and_appdata_ancestor_permissions(monkeypatch, tmp_path):
    root = tmp_path / "a/b/c"
    root.mkdir(parents=True, mode=0o700)
    fd = launcher.directory(root, home=True)
    assert stat.S_IMODE(os.fstat(fd).st_mode) == 0o700
    os.close(fd)
    first = tmp_path / "unsafe"
    first.mkdir(mode=0o777)
    first.chmod(0o777)
    with pytest.raises(ValueError):
        launcher.directory(first)


@pytest.mark.parametrize("problem", ["mode", "owner", "directory", "oversize"])
def test_private_reads_are_bounded_and_fail_closed(home, monkeypatch, problem):
    root, _ = home
    path = root / "sample"
    private(path, b"secret")
    if problem == "mode":
        path.chmod(0o644)
    elif problem == "owner":
        os.chown(path, 1000, -1)
    elif problem == "directory":
        path.unlink()
        path.mkdir()
    else:
        monkeypatch.setattr(launcher, "MAX_BYTES", 1)
    fd = launcher.directory(root, home=True)
    try:
        with pytest.raises(ValueError):
            launcher.read(fd, path.name)
    finally:
        os.close(fd)


def test_document_requires_an_object(home):
    root, _ = home
    private(root / "document.json", [])
    fd = launcher.directory(root, home=True)
    try:
        with pytest.raises(TypeError, match="object"):
            launcher.document(fd, "document.json")
    finally:
        os.close(fd)


@pytest.mark.parametrize("problem", ["identity", "hash", "schedule", "config"])
def test_active_hook_requires_matching_identity_schedule_and_config(home, problem):
    root, hook = home
    if problem == "identity":
        value = json.loads((root / ".identity.json").read_bytes())
        value["active"] = False
        private(root / ".identity.json", value)
    elif problem == "hash":
        private(hook, b"changed hook")
    elif problem == "schedule":
        private(hook.parent.parent.parent / "schedule.json", {})
    else:
        private(root / "config.json", {"format": 1})
    fd = launcher.directory(root, home=True)
    try:
        with pytest.raises(ValueError):
            launcher.active(fd, root, hook)
    finally:
        os.close(fd)


@pytest.mark.parametrize("problem", ["schema", "bad-pin", "mismatch", "missing", "unsafe"])
def test_archive_requires_a_current_or_previous_pin(home, problem):
    root, _ = home
    policy = json.loads((root / "policy.json").read_bytes())
    if problem == "schema":
        policy["unexpected"] = "value"
    elif problem == "bad-pin":
        policy["sha256"] = "bad"
    elif problem == "mismatch":
        private(root / "helper.pyz", b"changed")
    elif problem == "missing":
        (root / "helper.pyz").unlink()
    else:
        (root / "helper.pyz").chmod(0o644)
    private(root / "policy.json", policy)
    fd = launcher.directory(root, home=True)
    try:
        with pytest.raises(ValueError):
            launcher.archive(fd, root)
    finally:
        os.close(fd)


def test_verified_previous_archive_is_pinned_not_copied(home):
    root, _ = home
    private(root / "helper.pyz.prev", b"old verified")
    policy = json.loads((root / "policy.json").read_bytes())
    policy["previous_sha256"] = hashlib.sha256(b"old verified").hexdigest()
    private(root / "policy.json", policy)
    private(root / "helper.pyz", b"damaged")
    fd = launcher.directory(root, home=True)
    try:
        code = launcher.archive(fd, root)
        try:
            assert os.read(code, 20) == b"old verified"
        finally:
            os.close(code)
    finally:
        os.close(fd)
    assert (root / "helper.pyz").read_bytes() == b"damaged"


@pytest.mark.parametrize("options", [[], ["--check"], ["--update", "b" * 64]])
def test_launch_executes_only_the_verified_descriptor_with_clean_environment(home, monkeypatch, options):
    root, hook = home
    monkeypatch.setattr(launcher, "mounted", lambda _: True)
    calls = []

    def execute(binary, args, env):
        archive = int(env["SHAZZZBOT_HELPER_ARCHIVE_FD"])
        assert os.get_inheritable(archive)
        assert os.read(archive, 100) == b"verified archive"
        assert os.fstat(archive).st_ino == (launcher.WORK / "runtime/helper.pyz").stat().st_ino
        for item in Path("/proc/self/fd").iterdir():
            try:
                target = item.readlink()
            except FileNotFoundError:
                continue
            assert target != root and root not in target.parents
        calls.append((binary, args, env))

    monkeypatch.setattr(launcher.os, "execve", execute)
    directories = []
    monkeypatch.setattr(launcher.os, "chdir", directories.append)
    assert launcher.launch(root, hook, options=options)
    binary, args, env = calls[0]
    assert binary == "/usr/bin/python3" and args[:3] == [binary, "-I", "-B"]
    assert args[3].startswith("/proc/self/fd/")
    assert args[4:] == ["--config", str(root / "config.json"), *options]
    assert set(env) == {"PATH", "LANG", "HOME", "TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                        "SHAZZZBOT_HELPER_ARCHIVE_FD"}
    assert env["HOME"] == env["TMPDIR"] == str(launcher.WORK)
    assert directories == [launcher.WORK]
    assert sorted(path.name for path in root.iterdir()) == [".identity.json", "api.key", "config.json",
        "helper-unraid.json", "helper.pyz", "policy.json", "shazzzbot-helper-unraid.py"]


def test_missing_pool_waits_briefly_then_creates_nothing(home, monkeypatch):
    root, hook = home
    times = iter([0, 0, 31])
    sleeps = []
    monkeypatch.setattr(launcher.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(launcher.time, "sleep", sleeps.append)
    monkeypatch.setattr(launcher, "mounted", lambda _: False)
    before = list(root.iterdir())
    assert not launcher.launch(root, hook)
    assert sleeps == [1] and list(root.iterdir()) == before
    assert not launcher.WORK.exists()


@pytest.mark.parametrize("kind", ["new", "permissions", "owner", "symlink"])
def test_workdir_is_the_only_root_private_temporary_directory(home, kind):
    root, _ = home
    work = launcher.WORK
    if kind != "new":
        if kind == "symlink":
            work.symlink_to(root, target_is_directory=True)
        else:
            work.mkdir(mode=0o700)
            if kind == "permissions":
                work.chmod(0o777)
            else:
                os.chown(work, 1000, -1)
        with pytest.raises((OSError, ValueError)):
            launcher.workdir()
    else:
        launcher.workdir()
        launcher.workdir()
        assert stat.S_IMODE(work.stat().st_mode) == 0o700 and work.stat().st_uid == 0


def test_docker_precreated_work_and_socket_are_normalized_without_replacing_inodes(home):
    launcher.WORK.mkdir(mode=0o755)
    socket = launcher.WORK / "socket"
    socket.mkdir(mode=0o755)
    original = launcher.WORK.stat().st_ino, socket.stat().st_ino
    launcher.workdir(1234)
    assert (launcher.WORK.stat().st_ino, socket.stat().st_ino) == original
    assert stat.S_IMODE(launcher.WORK.stat().st_mode) == 0o700
    assert stat.S_IMODE(socket.stat().st_mode) == 0o750 and socket.stat().st_gid == 1234


@pytest.mark.parametrize("kind", ["runtime-mode", "socket-owner", "runtime-link", "group"])
def test_work_subdirectories_and_group_fail_closed(home, kind):
    root, _ = home
    if kind == "group":
        with pytest.raises(ValueError, match="group"):
            launcher.workdir(True)
        return
    launcher.WORK.mkdir(mode=0o700)
    if kind == "runtime-link":
        (launcher.WORK / "runtime").symlink_to(root, target_is_directory=True)
    elif kind == "runtime-mode":
        (launcher.WORK / "runtime").mkdir(mode=0o777)
        (launcher.WORK / "runtime").chmod(0o777)
    else:
        (launcher.WORK / "socket").mkdir(mode=0o750)
        os.chown(launcher.WORK / "socket", 1000, -1)
    with pytest.raises((OSError, ValueError)):
        launcher.workdir()


def test_failed_runtime_publication_keeps_the_previous_bytes_and_removes_only_its_temp(home, monkeypatch):
    launcher.workdir()
    target = launcher.WORK / "runtime/helper.pyz"
    target.write_bytes(b"previous")
    monkeypatch.setattr(launcher.os, "replace", lambda *_args, **_kwargs:
                        (_ for _ in ()).throw(OSError("replace failed")))
    with pytest.raises(OSError, match="replace failed"):
        launcher.publish(b"new")
    assert target.read_bytes() == b"previous"
    assert list(target.parent.iterdir()) == [target]


def test_launch_requires_root_and_fixed_options(home, monkeypatch):
    root, hook = home
    monkeypatch.setattr(launcher.os, "geteuid", lambda: 1)
    with pytest.raises(ValueError, match="requires root"):
        launcher.launch(root, hook)
    monkeypatch.setattr(launcher.os, "geteuid", lambda: 0)
    with pytest.raises(ValueError, match="option"):
        launcher.launch(root, hook, options=["--config", "/untrusted"])


def test_main_handles_missing_home_and_missing_mount_without_actions(home, monkeypatch, capsys):
    with pytest.raises(SystemExit, match="hook refused"):
        launcher.main()
    with pytest.raises(SystemExit, match="hook refused"):
        runpy.run_path(launcher.__file__, run_name="__main__")
    root, hook = home
    monkeypatch.setattr(launcher, "HOME", root)
    monkeypatch.setattr(launcher, "HOOK", hook)
    monkeypatch.setattr(launcher, "launch", lambda *_args, **_kwargs: False)
    assert launcher.main() is None
    assert "not mounted" in capsys.readouterr().err
    monkeypatch.setattr(launcher, "launch", lambda *_args, **_kwargs: True)
    assert launcher.main() is None
    assert capsys.readouterr().err == ""


def test_direct_entry_uses_its_own_absolute_parent_and_the_unraid_hook(monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "launch", lambda home, hook, **kwargs: calls.append((home, hook, kwargs)) or True)
    monkeypatch.setattr(launcher.sys, "argv", [launcher.__file__, "--check"])
    launcher.main()
    assert calls == [(Path(launcher.__file__).absolute().parent,
                      Path("/boot/config/plugins/user.scripts/scripts/shazzzbot-helper/script"),
                      {"options": ["--check"]})]
    monkeypatch.setattr(launcher, "HOME", "invalid")
    with pytest.raises(SystemExit, match="hook refused"):
        launcher.main()


def test_hook_is_one_bash_entry_with_a_fixed_stdlib_loader(home):
    root, hook = home
    source = Path(launcher.__file__).read_bytes()
    body = setup_schedule.hook_text(root, hook, source)
    assert body.startswith(b"#!/bin/bash\nHOME=")
    assert body.splitlines()[1] == (
        b"HOME=/tmp/shazzzbot-helper TMPDIR=/tmp/shazzzbot-helper "
        b"XDG_CACHE_HOME=/tmp/shazzzbot-helper/cache XDG_CONFIG_HOME=/tmp/shazzzbot-helper/config "
        b'XDG_DATA_HOME=/tmp/shazzzbot-helper/data exec /usr/bin/python3 -I -B - "$@" <<\'SHAZZZBOT_HELPER_PY\'')
    assert str(root).encode() in body and str(hook).encode() in body
    assert b"sys.path" not in body and b"from setup_files" not in body
    compile(body.decode().split("\n", 2)[2].rsplit("SHAZZZBOT_HELPER_PY\n", 1)[0], "hook", "exec")
    with pytest.raises(ValueError, match="absolute"):
        setup_schedule.hook_text(Path("relative"), hook, source)


def test_hook_generator_never_reopens_the_helper_home(home, monkeypatch):
    root, hook = home
    source = Path(launcher.__file__).read_bytes()
    expected = setup_schedule.hook_text(root, hook, source)
    (root / launcher.ENTRY).unlink()
    root.rename(root.with_name("renamed"))
    monkeypatch.setattr(setup_schedule, "read_safe", lambda *_args, **_kwargs:
                        (_ for _ in ()).throw(AssertionError("helper home must not be reopened")))
    assert setup_schedule.hook_text(root, hook, source) == expected


def test_launch_still_requires_the_installed_platform_entry(home):
    root, hook = home
    (root / launcher.ENTRY).unlink()
    fd = launcher.directory(root, home=True)
    try:
        with pytest.raises(FileNotFoundError):
            launcher.active(fd, root, hook)
    finally:
        os.close(fd)


@pytest.mark.parametrize("problem", ["format", "format-version", "platform", "files", "record", "keys", "hash-type",
                                     "hash", "size-type", "size-zero", "size-cap", "entry-size", "entry-hash"])
def test_platform_manifest_rejects_invalid_metadata_or_changed_entry(home, problem):
    root, hook = home
    manifest = json.loads((root / "helper-unraid.json").read_bytes())
    record = manifest["files"][launcher.ENTRY]
    if problem == "format":
        manifest["format"] = True
    elif problem == "format-version":
        manifest["format"] = 2
    elif problem == "platform":
        manifest["platform"] = "unsupported"
    elif problem == "files":
        manifest["files"] = []
    elif problem == "record":
        manifest["files"][launcher.ENTRY] = []
    elif problem == "keys":
        record["unexpected"] = 1
    elif problem == "hash-type":
        record["sha256"] = 1
    elif problem == "hash":
        record["sha256"] = "invalid"
    elif problem == "size-type":
        record["size"] = True
    elif problem == "size-zero":
        record["size"] = 0
    elif problem == "size-cap":
        record["size"] = launcher.MAX_BYTES + 1
    elif problem == "entry-size":
        record["size"] += 1
    else:
        record["sha256"] = "0" * 64
    private(root / "helper-unraid.json", manifest)
    fd = launcher.directory(root, home=True)
    try:
        with pytest.raises(ValueError, match="manifest"):
            launcher.active(fd, root, hook)
    finally:
        os.close(fd)


def test_schedule_state_is_explicit_and_malformed_data_fails_closed(home):
    _, hook = home
    path = hook.parent.parent.parent / "schedule.json"
    assert setup_schedule.active(hook.parent)
    path.unlink()
    assert not setup_schedule.active(hook.parent)
    private(path, [])
    with pytest.raises(TypeError, match="schedule"):
        setup_schedule.active(hook.parent)


def test_builder_entry_point_can_show_help_without_writing_an_artifact(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["build_helper.py", "--help"])
    with pytest.raises(SystemExit) as caught:
        runpy.run_path(build_helper.__file__, run_name="__main__")
    assert caught.value.code == 0 and "--source" in capsys.readouterr().out
