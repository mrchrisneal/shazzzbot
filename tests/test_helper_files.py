import os
import socket
from pathlib import Path

import pytest

from shazzzbot.helper import files, footprint


def test_runtime_rejects_paths_and_directory_permission_drift(setup):
    cfg, _, _, _config = setup
    cfg["socket"] = str(_config.parent / "wrong.sock")
    with pytest.raises(ValueError, match="runtime path"):
        files.runtime(cfg)
    cfg["socket"] = str(footprint.WORK / "socket/helper.sock")
    (footprint.WORK / "socket").chmod(0o700)
    with pytest.raises(ValueError, match="runtime directory"):
        files.runtime(cfg)
    assert (footprint.WORK / "socket").stat().st_mode & 0o777 == 0o700


def test_runtime_refuses_a_non_private_lock_file(setup):
    cfg, _, _, _config = setup
    lock = footprint.WORK / "socket/helper.lock"
    lock.write_text("retain")
    lock.chmod(0o644)
    with pytest.raises(ValueError, match="lock file"):
        files.runtime(cfg)
    assert lock.read_text() == "retain"


def test_runtime_never_removes_a_regular_file_at_the_socket_name(setup):
    cfg, _, _, _config = setup
    path = footprint.WORK / "socket/helper.sock"
    path.write_text("retain")
    with pytest.raises(ValueError, match="socket path"):
        files.runtime(cfg)
    assert path.read_text() == "retain"


def test_runtime_only_unlinks_an_owned_stale_socket(setup):
    cfg, _, _, _config = setup
    with files.trusted_open(footprint.WORK, directory=True) as directory:
        child = os.open("socket", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
    try:
        with socket.socket(socket.AF_UNIX) as stale:
            stale.bind(str(footprint.fd_path(child) / "helper.sock"))
    finally:
        os.close(child)
    path, lock = files.runtime(cfg)
    try:
        assert not path.exists()
        assert path.parent == footprint.WORK / "socket"
    finally:
        os.close(lock)


def test_process_identity_reads_only_the_current_proc_record():
    assert files.process_start(os.getpid()).isdigit()
    for pid in (None, True, 0, 1):
        with pytest.raises(ValueError, match="process id"):
            files.process_start(pid)


def test_update_event_cleanup_uses_pinned_socket_directory_fds(setup):
    _cfg, _, _, _config = setup
    root = footprint.WORK
    for name in ("update.json", "update.result"):
        files.write_private(root / "socket" / name, b"event")
    with files.trusted_open(root, directory=True) as directory:
        pinned = footprint.fd_path(directory)
        files.clear_update(pinned / "socket", root=pinned)
        files.clear_update(pinned / "socket", result=True, root=pinned)
    assert list((root / "socket").iterdir()) == []
    files.write_private(root / "socket/update.json", b"event")
    files.clear_update(root / "socket")
    assert not (root / "socket/update.json").exists()
    with pytest.raises(ValueError, match="unsafe update directory"):
        files.clear_update(root / "outside", root=root)
    with pytest.raises(ValueError, match="unsafe update directory"):
        files.clear_update(Path("/outside/socket"), root=Path("/outside"))


def test_failed_private_write_removes_only_its_temporary_file(setup, monkeypatch):
    cfg, _, _, _config = setup
    root = _config.parent
    target = root / "retain"
    files.write_private(target, b"old")

    def failed(*_args):
        raise OSError("replace failed")

    monkeypatch.setattr(files.os, "replace", failed)
    with footprint.open_home(cfg) as directory, pytest.raises(OSError, match="replace failed"):
        files.write_private(footprint.fd_path(directory) / target.name, b"new")
    assert target.read_bytes() == b"old"
    assert not list(root.glob(".helper-*"))


def test_proc_fd_parent_requires_a_trusted_directory(setup, tmp_path):
    path = tmp_path / "file"
    path.write_text("not a directory")
    fd = os.open(path, os.O_RDONLY)
    try:
        with pytest.raises(ValueError, match="untrusted"):
            files.fsync_parent(Path(f"/proc/self/fd/{fd}") / "child")
    finally:
        os.close(fd)
