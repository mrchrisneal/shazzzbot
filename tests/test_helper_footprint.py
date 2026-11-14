import errno
import hashlib
import json
import os
from pathlib import Path

import pytest

from shazzzbot.helper import files, footprint
from tests.test_helper_support import directory_mount_id


def put(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    path.chmod(mode)


def add_pool(name):
    root = footprint.MNT / name
    (root / "appdata").mkdir(parents=True)
    device = root.stat().st_dev
    line = f"{directory_mount_id(root)} 0 {os.major(device)}:{os.minor(device)} / {root} rw - xfs /dev/test rw\n"
    with footprint.MOUNTINFO.open("a") as stream:
        stream.write(line)
    return root


def test_home_stays_private_below_untrusted_appdata_ancestors(setup):
    cfg, _, _, config = setup
    root = config.parent
    root.parent.chmod(0o777)
    os.chown(root.parent, 99, 100)
    root.parent.parent.chmod(0o777)
    os.chown(root.parent.parent, 99, 100)
    with footprint.open_home(cfg) as fd:
        assert os.fstat(fd).st_ino == root.stat().st_ino
        assert footprint.read_at(fd, "api.key") == b"a" * 32
    footprint.guard(cfg)
    assert footprint.pool_ancestor(root.parent) and footprint.pool_ancestor(root.parent.parent)


def test_removed_or_replaced_home_is_never_recreated(setup):
    cfg, _, _, config = setup
    root = config.parent
    root.rename(root.with_name("retained"))
    with pytest.raises(FileNotFoundError):
        footprint.guard(cfg)
    assert not root.exists()
    root.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="identity changed"):
        footprint.guard(cfg)
    assert list(root.iterdir()) == []
    os.chown(root, 99, 100)
    with pytest.raises(ValueError, match="root-owned"):
        footprint.guard(cfg)


@pytest.mark.parametrize("name", footprint.CORE_FILES)
def test_missing_core_files_stop_the_guard_without_recreation(setup, name):
    cfg, _, _, config = setup
    path = config.parent / name
    path.rename(path.with_suffix(".retained"))
    with pytest.raises(FileNotFoundError):
        footprint.guard(cfg)
    assert not path.exists()


@pytest.mark.parametrize("name", footprint.BUNDLE_FILES)
@pytest.mark.parametrize("problem", ["symlink", "owner", "writable", "directory", "oversize"])
def test_bundle_files_must_be_private_root_owned_regular_no_follow_files(setup, name, problem):
    cfg, _, _, config = setup
    path = config.parent / name
    if problem in {"symlink", "directory"}:
        path.rename(path.with_suffix(".retained"))
        if problem == "symlink":
            path.symlink_to(path.with_suffix(".retained"))
        else:
            path.mkdir()
    elif problem == "owner":
        os.chown(path, 99, 100)
    elif problem == "writable":
        path.chmod(0o620)
    else:
        path.write_bytes(b"x" * 65537)
    with pytest.raises((OSError, ValueError)):
        footprint.guard(cfg)
    assert path.exists()


@pytest.mark.parametrize("problem", ["object", "fields", "format-type", "format", "platform", "revision-type",
    "revision", "version-type", "version", "version-prefix", "files-type", "files", "record-type", "record-fields",
    "hash-type", "hash", "size-type", "size-zero", "entry-cap", "archive-cap", "entry-size", "entry-hash",
    "entry-content"])
def test_bundle_manifest_and_entry_integrity_fail_closed(setup, problem):
    cfg, _, _, config = setup
    manifest = config.parent / "helper-unraid.json"
    value = json.loads(manifest.read_bytes())
    record = value["files"][footprint.BUNDLE_FILES[0]]
    if problem == "object":
        value = []
    elif problem == "fields":
        value["extra"] = True
    elif problem == "format-type":
        value["format"] = True
    elif problem == "format":
        value["format"] = 2
    elif problem == "platform":
        value["platform"] = "unsupported"
    elif problem == "revision-type":
        value["revision"] = 1
    elif problem == "revision":
        value["revision"] = "not-a-revision"
    elif problem == "version-type":
        value["version"] = 1
    elif problem == "version":
        value["version"] = "invalid"
    elif problem == "version-prefix":
        value["version"] = "0.1.0"
    elif problem == "files-type":
        value["files"] = []
    elif problem == "files":
        value["files"] = {}
    elif problem == "record-type":
        value["files"][footprint.BUNDLE_FILES[0]] = []
    elif problem == "record-fields":
        record["extra"] = 1
    elif problem == "hash-type":
        record["sha256"] = 1
    elif problem == "hash":
        record["sha256"] = "invalid"
    elif problem == "size-type":
        record["size"] = True
    elif problem == "size-zero":
        record["size"] = 0
    elif problem == "entry-cap":
        record["size"] = 65537
    elif problem == "archive-cap":
        value["files"]["helper.pyz"]["size"] = footprint.MAX_BYTES + 1
    elif problem == "entry-size":
        record["size"] += 1
    elif problem == "entry-hash":
        record["sha256"] = "0" * 64
    else:
        entry = config.parent / footprint.BUNDLE_FILES[0]
        source = entry.read_bytes()
        entry.write_bytes(b"!" + source[1:])
    put(manifest, json.dumps(value))
    with pytest.raises(ValueError):
        footprint.guard(cfg)


def test_runtime_only_archive_update_does_not_change_the_trusted_bundle(setup):
    cfg, _, program, config = setup
    manifest = (config.parent / "helper-unraid.json").read_bytes()
    program.write_bytes(b"a different valid runtime-only update")
    policy_path = config.parent / "policy.json"
    policy = json.loads(policy_path.read_bytes())
    policy["sha256"] = hashlib.sha256(program.read_bytes()).hexdigest()
    put(policy_path, json.dumps(policy))
    footprint.guard(cfg)
    with footprint.open_home(cfg) as fd:
        assert footprint.bundle(fd)["version"] == json.loads(manifest)["version"]
    assert (config.parent / "helper-unraid.json").read_bytes() == manifest


def test_bundle_validation_keeps_one_validated_home_descriptor_during_parent_replacement(setup, monkeypatch):
    cfg, _, _, config = setup
    root = config.parent
    inode = root.stat().st_ino
    original = footprint.read_at
    reads = []

    def read(fd, name, limit=footprint.MAX_BYTES):
        assert os.fstat(fd).st_ino == inode
        reads.append((fd, name))
        if name == "helper-unraid.json":
            root.rename(root.with_name("retained"))
            root.mkdir(mode=0o700)
        return original(fd, name, limit)

    monkeypatch.setattr(footprint, "read_at", read)
    footprint.guard(cfg)
    assert len({fd for fd, _ in reads}) == 1
    assert {name for _, name in reads} >= set(footprint.CORE_FILES)
    assert list(root.iterdir()) == []
    with pytest.raises(ValueError, match="identity changed"):
        footprint.guard(cfg)


@pytest.mark.parametrize("key", ["key_file", "socket", "log_file", "installation_policy"])
def test_configured_paths_cannot_escape_the_one_home(setup, key):
    cfg, _, _, _ = setup
    cfg[key] = "/outside/file"
    with pytest.raises(ValueError, match="must stay"):
        footprint.guard(cfg)


@pytest.mark.parametrize("change", ["hook", "schedule", "inactive", "install-id", "socket-mode"])
def test_guard_requires_the_owned_enabled_hook_and_complete_home(setup, change):
    cfg, _, _, config = setup
    root = config.parent
    if change == "hook":
        footprint.HOOK.write_text("changed")
    elif change == "schedule":
        (footprint.HOOK.parent.parent.parent / "schedule.json").write_text("{}")
    elif change == "inactive":
        footprint.record_identity(root, cfg["_install_id"], active=False)
        footprint.guard(cfg, active=False)
    elif change == "install-id":
        cfg["_install_id"] = "c" * 32
    else:
        (footprint.WORK / "socket").chmod(0o755)
    with pytest.raises(ValueError):
        footprint.guard(cfg)


def test_identity_payload_is_pure_and_records_prepared_hook_bytes(setup):
    cfg, _, _, config = setup
    root = config.parent
    before = (root / ".identity.json").read_bytes()
    payload = footprint.identity_payload(root, cfg["_install_id"], active=False)
    assert (root / ".identity.json").read_bytes() == before
    value = json.loads(payload)
    assert value["active"] is False
    assert value["hook_sha256"] == hashlib.sha256(footprint.HOOK.read_bytes()).hexdigest()
    footprint.record_identity(root, cfg["_install_id"], active=False)
    with pytest.raises(ValueError, match="not active"):
        footprint.guard(cfg)
    footprint.record_identity(root, cfg["_install_id"], footprint.HOOK)
    footprint.guard(cfg)
    with pytest.raises(ValueError, match="mismatch"):
        footprint.identity_payload(root, "c" * 32)
    with pytest.raises(ValueError, match="startup hook"):
        footprint.identity_payload(root, cfg["_install_id"], Path("/other"))
    with pytest.raises(ValueError, match="startup hook"):
        footprint.identity_payload(root, cfg["_install_id"], active=1)


@pytest.mark.parametrize("field,value", [("format", True), ("format", 2), ("active", 1),
    ("install_id", 1), ("install_id", "bad"), ("hook", "/other"), ("hook_sha256", 3),
    ("hook_sha256", "bad"), ("hook_sha256", ""), ("extra", True)])
def test_invalid_identity_is_not_used(setup, field, value):
    cfg, _, _, config = setup
    path = config.parent / ".identity.json"
    body = json.loads(path.read_text())
    body[field] = value
    put(path, json.dumps(body))
    with pytest.raises(ValueError, match="identity"):
        footprint.guard(cfg)


def test_identity_must_be_an_object(setup):
    cfg, _, _, config = setup
    put(config.parent / ".identity.json", "[]")
    with pytest.raises(ValueError, match="identity"):
        footprint.guard(cfg)


def test_initialize_only_creates_an_identified_home_on_a_mounted_pool(setup):
    cfg, _, _, config = setup
    root = config.parent.with_name("new-helper")
    previous = os.umask(0o077)
    try:
        footprint.initialize(root, "c" * 32)
    finally:
        os.umask(previous)
    assert root.stat().st_mode & 0o777 == 0o700
    assert not (root / "socket").exists()
    with footprint.open_home(root) as fd:
        assert footprint.identity(fd)["active"] is False
    footprint.initialize(root, "c" * 32)
    with pytest.raises(ValueError, match="another installation"):
        footprint.initialize(root, "d" * 32)
    with pytest.raises(ValueError, match="install id"):
        footprint.initialize(root, "invalid")
    unknown = root.with_name("unknown")
    put(unknown / "operator-file", "retained")
    unknown.chmod(0o700)
    with pytest.raises(ValueError, match="unidentified"):
        footprint.initialize(unknown, "c" * 32)
    assert (unknown / "operator-file").read_text() == "retained"
    footprint.MOUNTINFO.write_text("")
    unmounted = root.with_name("never-created")
    with pytest.raises(ValueError, match="not mounted"):
        footprint.initialize(unmounted, cfg["_install_id"])
    assert not unmounted.exists()


@pytest.mark.parametrize("kind", ["symlink", "mode", "owner"])
def test_home_and_socket_refuse_untrusted_replacements(setup, kind):
    cfg, _, _, config = setup
    root = config.parent
    socket = footprint.WORK / "socket"
    socket.rmdir()
    if kind == "symlink":
        socket.symlink_to(root, target_is_directory=True)
    else:
        socket.mkdir(mode=0o750)
        if kind == "mode":
            socket.chmod(0o777)
        if kind == "owner":
            os.chown(socket, 99, 100)
    with pytest.raises((ValueError, OSError)):
        footprint.ensure_socket(cfg["socket_gid"])
    with pytest.raises((ValueError, OSError)):
        footprint.guard(cfg)


@pytest.mark.parametrize("requested", ["relative", "/boot/helper", "/mnt/user0/appdata/helper",
                                       "/mnt/disk1/appdata/helper", "/mnt/cache/other/helper"])
def test_home_shape_refuses_flash_fuse_array_and_other_directories(setup, requested):
    with pytest.raises(ValueError):
        footprint.resolve_home(Path(requested))


def test_pool_ancestor_only_relaxes_the_two_mounted_ancestors(setup):
    _, _, _, config = setup
    root = config.parent
    assert footprint.pool_ancestor(root.parent.parent)
    assert footprint.pool_ancestor(root.parent)
    assert not footprint.pool_ancestor(root)
    assert not footprint.pool_ancestor(footprint.MNT)
    assert not footprint.pool_ancestor(Path("/outside"))
    assert not footprint.pool_ancestor(footprint.MNT / "user/appdata")
    assert not footprint.pool_ancestor(root.parent.parent / "other")


@pytest.mark.parametrize("text", ["invalid", "1 0 bad / {pool} rw - xfs /dev/test rw\n",
    "1 0 0:bad / {pool} rw - xfs /dev/test rw\n"])
def test_invalid_mount_information_fails_closed(setup, text):
    _, _, _, config = setup
    footprint.MOUNTINFO.write_text(text.format(pool=config.parent.parent.parent))
    with pytest.raises(ValueError, match="mount|device"):
        footprint.mounted_pools()


def test_pool_requires_a_single_real_rw_unix_mount(setup):
    cfg, _, _, config = setup
    root = config.parent
    line = footprint.MOUNTINFO.read_text()
    footprint.MOUNTINFO.write_text(line + line)
    with pytest.raises(ValueError, match="ambiguous"):
        footprint.mounted_pools()
    for changed in (line.replace(" - xfs ", " - fuse.shfs "), line.replace(" rw -", " ro -")):
        footprint.MOUNTINFO.write_text(changed)
        with pytest.raises(ValueError, match="not mounted"):
            footprint.guard(cfg)
    footprint.MOUNTINFO.write_text(line.replace(line.split()[0], str(directory_mount_id(root) + 1), 1))
    with pytest.raises(ValueError, match="mount changed"):
        footprint.resolve_home(root)


def test_mount_input_is_bounded(setup):
    footprint.MOUNTINFO.write_bytes(b"x" * (footprint.MAX_BYTES + 1))
    with pytest.raises(ValueError, match="size limit"):
        footprint.mounted_pools()


@pytest.mark.parametrize("mount_id", ["0", "-1", "1x", "1" * 21])
def test_mount_id_is_a_bounded_positive_integer(setup, mount_id):
    fields = footprint.MOUNTINFO.read_text().split(" ", 1)
    footprint.MOUNTINFO.write_text(mount_id + " " + fields[1])
    with pytest.raises(ValueError, match="mount id"):
        footprint.mounted_pools()


def test_btrfs_subvolume_device_can_differ_from_its_mount_device(setup):
    cfg, _, _, config = setup
    root = config.parent.parent.parent
    device = root.stat().st_dev
    fields = footprint.MOUNTINFO.read_text().split()
    fields[2] = f"{os.major(device)}:{os.minor(device) + 1}"
    fields[fields.index("-") + 1] = "btrfs"
    footprint.MOUNTINFO.write_text(" ".join(fields) + "\n")
    assert footprint.mounted_pools()[root.name] == directory_mount_id(root)
    footprint.guard(cfg)


@pytest.mark.parametrize("data", [b"", b"pos:\t0\n", b"mnt_id:\t1\nmnt_id:\t1\n",
    b"mnt_id:\t0\n", b"mnt_id:\t-1\n", b"mnt_id:\t1junk\n", b"mnt_id:\t1",
    b"mnt_id:\t" + b"1" * 21 + b"\n", b"mnt_id:\t\xff\n", b"x" * 4097, None, "unreadable"])
def test_bad_fdinfo_refuses_and_closes_the_pool_descriptor(setup, monkeypatch, data):
    _, _, _, config = setup
    source = config.parent / "fdinfo-test"
    if isinstance(data, bytes):
        put(source, data)
    read = footprint._read
    opened = []

    def read_fdinfo(path, limit=footprint.MAX_BYTES):
        if path.parent.name != "fdinfo":
            return read(path, limit)
        assert path.parent.parent == Path(f"/proc/{os.getpid()}")
        assert limit == 4096
        opened.append(int(path.name))
        if data == "unreadable":
            raise PermissionError("fdinfo unreadable")
        return read(source, limit)

    monkeypatch.setattr(footprint, "_read", read_fdinfo)
    with pytest.raises((ValueError, OSError)), footprint.pool_open(config.parent.parent.parent.name):
        pytest.fail("untrusted fdinfo accepted")
    assert len(opened) == 1
    with pytest.raises(OSError) as error:
        os.fstat(opened[0])
    assert error.value.errno == errno.EBADF


@pytest.mark.parametrize("location", ["cache", "cache/appdata/shazzzbot-helper", "absolute"])
def test_fuse_location_is_resolved_to_one_verified_pool(setup, monkeypatch, location):
    _, _, _, config = setup
    expected = config.parent
    data = str(expected) if location == "absolute" else location
    monkeypatch.setattr(os, "getxattr", lambda *_args: data.encode())
    requested = footprint.MNT / "user/appdata/shazzzbot-helper"
    assert footprint.resolve_home(requested) == expected
    assert footprint.resolve_home(expected) == expected


def no_location(*_args):
    raise OSError(errno.ENODATA, "no location")


def test_fuse_resolution_without_xattr_uses_unique_existing_home_or_explicit_share_pool(setup, monkeypatch):
    _, _, _, config = setup
    root = config.parent
    monkeypatch.setattr(os, "getxattr", no_location)
    requested = footprint.MNT / "user/appdata/shazzzbot-helper"
    assert footprint.resolve_home(requested) == root
    monkeypatch.setattr(footprint, "SHARE_CONFIG", root.parent / "share.cfg")
    put(footprint.SHARE_CONFIG, 'shareCachePool="cache"\n')
    new = footprint.MNT / "user/appdata/new-helper"
    assert footprint.resolve_home(new) == root.with_name("new-helper")
    assert not root.with_name("new-helper").exists()
    footprint.SHARE_CONFIG.unlink()
    with pytest.raises(ValueError, match="ambiguous"):
        footprint.resolve_home(new)


def test_fuse_resolution_never_picks_the_first_of_several_pools(setup, monkeypatch):
    _, _, _, config = setup
    other = add_pool("other")
    (other / "appdata/shazzzbot-helper").mkdir(mode=0o700)
    monkeypatch.setattr(os, "getxattr", no_location)
    requested = footprint.MNT / "user/appdata/shazzzbot-helper"
    with pytest.raises(ValueError, match="ambiguous"):
        footprint.resolve_home(requested)
    monkeypatch.setattr(os, "getxattr", lambda *_args: b"cache,other")
    with pytest.raises(ValueError, match="ambiguous"):
        footprint.resolve_home(requested)
    monkeypatch.setattr(os, "getxattr", lambda *_args: b"not-mounted")
    with pytest.raises(ValueError, match="mounted real pool"):
        footprint.resolve_home(requested)
    assert config.parent.exists()


def test_location_permission_errors_are_not_hidden(setup, monkeypatch):
    def denied(*_args):
        raise PermissionError(errno.EACCES, "denied")
    monkeypatch.setattr(os, "getxattr", denied)
    with pytest.raises(PermissionError):
        footprint.resolve_home(footprint.MNT / "user/appdata/helper")


def test_work_directory_is_the_only_private_transient_root(setup):
    root = footprint.ensure_workdir()
    assert root == footprint.WORK and root.stat().st_mode & 0o777 == 0o700
    assert footprint.ensure_workdir() == root
    inode = root.stat().st_ino
    root.chmod(0o755)
    assert footprint.ensure_workdir().stat().st_ino == inode
    assert root.stat().st_mode & 0o777 == 0o700
    root.chmod(0o777)
    with pytest.raises(ValueError, match="untrusted"):
        footprint.ensure_workdir()
    root.chmod(0o700)
    root.rename(root.with_name("retained-work"))
    root.symlink_to(root.parent, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        footprint.ensure_workdir()


def test_home_reads_are_bounded_private_and_final_nofollow(setup):
    cfg, _, _, config = setup
    root = config.parent
    with footprint.open_home(cfg) as fd:
        for name in ("", ".", "..", "a/b"):
            with pytest.raises(ValueError, match="name"):
                footprint.read_at(fd, name)
        with pytest.raises(ValueError, match="size limit"):
            footprint.read_at(fd, "api.key", 1)
        (root / "api.key").chmod(0o644)
        with pytest.raises(ValueError, match="private"):
            footprint.read_at(fd, "api.key")
        (root / "api.key").unlink()
        (root / "api.key").symlink_to(root / "config.json")
        with pytest.raises(OSError):
            footprint.read_at(fd, "api.key")


def test_proc_fd_writes_stay_on_the_original_home_after_parent_rename(setup):
    cfg, _, _, config = setup
    root = config.parent
    with footprint.open_home(cfg) as fd:
        retained = root.with_name("retained")
        root.rename(retained)
        root.mkdir(mode=0o700)
        files.write_private(footprint.fd_path(fd) / "owned", b"pinned")
        assert footprint.read_at(fd, "owned") == b"pinned"
        assert not (root / "owned").exists()
    assert (retained / "owned").read_bytes() == b"pinned"


@pytest.mark.parametrize("tail", ["cache/appdata", "cache/other/H", "cache/appdata/H/nested",
                                  "user0/appdata/H", "disk1/appdata/H"])
def test_pool_home_shape_is_exactly_one_appdata_directory(setup, tail):
    with pytest.raises(ValueError, match="appdata"):
        footprint.resolve_home(footprint.MNT / tail)


def test_config_name_is_fixed_in_the_home(setup):
    from shazzzbot.helper import config as configuration
    from shazzzbot.helper.platforms import unraid

    cfg, _, _, config = setup
    wrong = config.parent / "other.json"
    with pytest.raises(ValueError, match="config.json"):
        configuration.load_config(wrong, unraid.Unraid())
    cfg["_config"] = str(wrong)
    with pytest.raises(ValueError, match="config.json"):
        footprint.home(cfg)


@pytest.mark.parametrize("setting", [{"action_timeout": 86401}, {"release_url": "http://unsafe"}, {"release_url": 1}])
def test_config_bounds_and_release_scheme_fail_closed(setup, setting):
    from shazzzbot.helper import config as configuration
    from shazzzbot.helper.platforms import unraid

    path = setup[3]
    files.write_private(path, json.dumps({"format": 1, **setting}).encode())
    with pytest.raises(ValueError):
        configuration.load_config(path, unraid.Unraid())


def test_work_directory_refuses_a_foreign_owner(setup):
    root = footprint.ensure_workdir()
    os.chown(root, 99, 100)
    with pytest.raises(ValueError, match="untrusted"):
        footprint.ensure_workdir()


def test_docker_created_runtime_directories_are_normalized_without_replacement(setup):
    work = footprint.WORK
    socket = work / "socket"
    before = [path.stat().st_ino for path in (work, socket)]
    work.chmod(0o755)
    socket.chmod(0o755)
    assert footprint.ensure_socket(10002) == socket
    assert [path.stat().st_ino for path in (work, socket)] == before
    assert work.stat().st_mode & 0o777 == 0o700
    assert socket.stat().st_mode & 0o777 == 0o750 and socket.stat().st_gid == 10002


@pytest.mark.parametrize("gid", [True, -1, 2**32 - 1, "10001"])
def test_socket_group_is_bounded_and_exact_integer(setup, gid):
    with pytest.raises(ValueError, match="group"):
        footprint.ensure_socket(gid)


def test_guard_rejects_work_mode_drift_without_changing_it(setup):
    cfg = setup[0]
    footprint.WORK.chmod(0o755)
    with pytest.raises(ValueError, match="work directory"):
        footprint.guard(cfg)
    assert footprint.WORK.stat().st_mode & 0o777 == 0o755
