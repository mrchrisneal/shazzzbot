import os
from pathlib import Path

import pytest

from shazzzbot.helper import footprint
from tools import setup_files as files


@pytest.fixture
def pool_tree(tmp_path, monkeypatch):
    root = tmp_path / "root"
    home = root / "mnt/cache/appdata/H"
    home.mkdir(parents=True, mode=0o700)
    home.chmod(0o700)
    for parent in (home.parent, home.parent.parent):
        os.chown(parent, 99, 100)
        parent.chmod(0o777)
    secret = home / "file"
    secret.write_bytes(b"private")
    secret.chmod(0o600)
    actual = os.open
    seen = []

    def opened(path, flags, *args, **kwargs):
        return actual(str(root) if path == "/" else path, flags, *args, **kwargs)

    def verified(path):
        seen.append(path)
        return path in {Path("/mnt/cache"), Path("/mnt/cache/appdata")}

    monkeypatch.setattr(files.os, "open", opened)
    monkeypatch.setattr(footprint, "pool_ancestor", verified)
    return home, seen


def test_installer_fd_walk_allows_only_the_verified_pool_ancestors(pool_tree):
    home, seen = pool_tree
    assert files.read_safe(Path("/mnt/cache/appdata/H/file"), private=True) == b"private"
    assert seen == [Path("/mnt/cache"), Path("/mnt/cache/appdata")]
    assert (home / "file").read_bytes() == b"private"


def test_verified_pool_directory_cannot_be_read_as_a_regular_file(pool_tree):
    with pytest.raises(ValueError, match="ancestors must be directories"):
        files.read_safe(Path("/mnt/cache"))


def test_installer_requires_the_appdata_home_itself_to_be_private(pool_tree):
    home, _ = pool_tree
    home.chmod(0o755)
    with pytest.raises(ValueError, match="mode 0700"):
        files.write_safe(Path("/mnt/cache/appdata/H/file"), b"changed")
    assert (home / "file").read_bytes() == b"private"


def test_installer_refuses_a_foreign_owned_home_below_valid_pool_ancestors(pool_tree):
    home, _ = pool_tree
    os.chown(home, 99, 100)
    with pytest.raises(ValueError, match="untrusted"):
        files.read_safe(Path("/mnt/cache/appdata/H/file"))
    assert (home / "file").read_bytes() == b"private"


def test_an_unmounted_pool_is_refused_before_any_directory_creation(pool_tree, monkeypatch):
    home, _ = pool_tree

    def unmounted(_path):
        raise ValueError("pool is not mounted")

    monkeypatch.setattr(footprint, "pool_ancestor", unmounted)
    with pytest.raises(ValueError, match="not mounted"), files.opened(
            Path("/mnt/missing/appdata/H"), directory=True, create=True):
        pytest.fail("unmounted pool was opened")
    assert not (home.parents[2] / "missing").exists()
