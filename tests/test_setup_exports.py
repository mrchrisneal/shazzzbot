import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import setup_exports as exports


@pytest.fixture
def paths(tmp_path, monkeypatch):
    for name in ("TESTPARM", "SMB_CONF", "UNRAID", "NFS_EXPORTS", "NFS_FOLDER"):
        monkeypatch.setattr(exports, name, tmp_path / name.lower())
    return tmp_path


def samba(monkeypatch, text="[global]\n", *, rc=0):
    exports.TESTPARM.write_text("testparm placeholder")
    calls = []

    def invoke(command, **options):
        calls.append((command, options))
        return SimpleNamespace(returncode=rc, stdout=text.encode())

    monkeypatch.setattr(exports.subprocess, "run", invoke)
    return calls


@pytest.mark.parametrize("path", ["relative", "/a/../b", "/a\nb", "/a\rb", "/a\0b", "/a%u", "/a*", "/a?", "/a\\b"])
def test_location_rejects_uncertain_paths(path):
    with pytest.raises(ValueError, match="unavailable or dynamic"):
        exports.location(path)


def test_location_canonicalizes_symlinks_and_overlap_is_bidirectional(paths):
    target = paths / "private"
    target.mkdir()
    (paths / "alias").symlink_to(target)
    assert exports.location(str(paths / "alias")) == target
    assert exports.overlaps(target, target)
    assert exports.overlaps(target, paths)
    assert exports.overlaps(paths, target)
    assert not exports.overlaps(target, paths / "separate")


def test_absent_samba_and_nfs_do_not_block_linux(paths):
    assert exports.smb_exports() == []
    assert exports.nfs_exports() == []
    assert exports.private_runtime(paths / "private") is None


def test_samba_presence_requires_working_trusted_testparm(paths):
    exports.UNRAID.write_text("unraid")
    with pytest.raises(ValueError, match="SMB export configuration"):
        exports.smb_exports()
    exports.TESTPARM.write_text("testparm placeholder")
    exports.TESTPARM.chmod(0o777)
    with pytest.raises(ValueError, match="untrusted"):
        exports.smb_exports()


@pytest.mark.parametrize("text,rc", [("[global]\n", 1), ("x" * (1024**2 + 1), 0)])
def test_samba_failed_or_oversize_result_is_refused(paths, monkeypatch, text, rc):
    samba(monkeypatch, text, rc=rc)
    with pytest.raises(ValueError, match="unavailable"):
        exports.smb_exports()


def test_samba_timeout_is_refused(paths, monkeypatch):
    samba(monkeypatch)
    monkeypatch.setattr(exports.subprocess, "run", lambda *_args, **_options:
                        (_ for _ in ()).throw(subprocess.TimeoutExpired("testparm", 5)))
    with pytest.raises(ValueError, match="unavailable"):
        exports.smb_exports()


def test_samba_collects_active_paths_and_inherits_defaults(paths, monkeypatch):
    calls = samba(monkeypatch, "[global]\npath = /exports/default\n[One]\npath = /exports/one\n"
                  "[Two]\n[Off]\navailable = no\npath = /private\n")
    assert exports.smb_exports() == [Path("/exports/one"), Path("/exports/default")]
    assert calls[0][0] == [str(exports.TESTPARM), "-s"]
    assert calls[0][1]["timeout"] == 5


@pytest.mark.parametrize("text", ["invalid", "[share]\npath=/path\n", "[global]\n[share]\n",
                                       "[global]\n[share]\npath=/path\navailable=maybe\n"])
def test_samba_unknown_configuration_is_refused(paths, monkeypatch, text):
    samba(monkeypatch, text)
    with pytest.raises(ValueError):
        exports.smb_exports()


@pytest.mark.parametrize("permission", ["yes", "no"])
@pytest.mark.parametrize("relationship", ["same", "ancestor", "descendant"])
def test_runtime_refuses_all_active_smb_exports_even_read_only(paths, monkeypatch, permission, relationship):
    target = paths / "private"
    share = {"same": target, "ancestor": paths, "descendant": target / "child"}[relationship]
    samba(monkeypatch, f"[global]\n[share]\npath={share}\nread only={permission}\n")
    with pytest.raises(ValueError, match="outside every SMB and NFS export"):
        exports.private_runtime(target)


def test_runtime_refuses_wide_links_root_write_on_unrelated_share(paths, monkeypatch):
    samba(monkeypatch, "[global]\nwide links=yes\n[share]\npath=/elsewhere\nread only=no\nforce user=root\n")
    with pytest.raises(ValueError, match="wide links"):
        exports.private_runtime(paths / "private")


def test_runtime_accepts_unrelated_shares_and_ignores_disabled_share(paths, monkeypatch):
    samba(monkeypatch, f"[global]\n[share]\npath=/elsewhere\n[off]\npath={paths}\navailable=no\n")
    exports.NFS_EXPORTS.write_text('/exports/other *(ro)\n')
    assert exports.private_runtime(paths / "private") is None


def test_nfs_paths_support_comments_quotes_octal_spaces_and_continuations():
    assert exports.nfs_paths('# comment\n"/share/a b" host(ro)\n/share/c\\040d \\\n host(rw)\n') == [
        Path("/share/a b"), Path("/share/c d")]


@pytest.mark.parametrize("text", ["/path", "/path host(ro", "/path host)(rw)", '/path "unterminated',
                                       "relative *(rw)", "/path/../other *(ro)"])
def test_nfs_paths_reject_malformed_exports(text):
    with pytest.raises(ValueError):
        exports.nfs_paths(text)


@pytest.mark.parametrize("options", ["ro", "rw"])
@pytest.mark.parametrize("relationship", ["same", "ancestor", "descendant"])
def test_runtime_refuses_nfs_overlap_regardless_options(paths, options, relationship):
    target = paths / "private"
    share = {"same": target, "ancestor": paths, "descendant": target / "child"}[relationship]
    exports.NFS_EXPORTS.write_text(f'"{share}" *(sync,{options})\n')
    with pytest.raises(ValueError, match="outside every SMB and NFS export"):
        exports.private_runtime(target)


def test_nfs_reads_exports_directory_and_skips_other_files(paths):
    exports.NFS_EXPORTS.write_text("/share/one host\n")
    exports.NFS_FOLDER.mkdir()
    (exports.NFS_FOLDER / "one.exports").write_text("/share/two *(ro)\n")
    (exports.NFS_FOLDER / "ignore").write_text("invalid")
    assert exports.nfs_exports() == [Path("/share/one"), Path("/share/two")]


def test_nfs_rejects_aggregate_size_and_excess_file_count(paths, monkeypatch):
    exports.NFS_FOLDER.mkdir()
    exports.NFS_EXPORTS.write_text("/share/one host\n")
    (exports.NFS_FOLDER / "one.exports").write_text("/share/two host\n")
    monkeypatch.setattr(exports, "MAX_BYTES", 20)
    with pytest.raises(ValueError, match="size limit"):
        exports.nfs_exports()
    monkeypatch.setattr(exports.os, "listdir", lambda _: ["file"] * 1025)
    with pytest.raises(ValueError, match="too many"):
        exports.nfs_exports()


@pytest.mark.parametrize("kind", ["file-link", "directory-link", "file-owner", "directory-owner", "file-write",
                                  "dir-write"])
def test_nfs_rejects_untrusted_paths(paths, kind):
    if kind.startswith("file"):
        exports.NFS_EXPORTS.write_text("/share *(ro)\n")
        if kind == "file-link":
            target = paths / "real"
            exports.NFS_EXPORTS.rename(target)
            exports.NFS_EXPORTS.symlink_to(target)
        elif kind == "file-owner":
            os.chown(exports.NFS_EXPORTS, 1000, -1)
        else:
            exports.NFS_EXPORTS.chmod(0o666)
    else:
        exports.NFS_FOLDER.mkdir()
        if kind == "directory-link":
            target = paths / "real"
            exports.NFS_FOLDER.rename(target)
            exports.NFS_FOLDER.symlink_to(target, target_is_directory=True)
        elif kind == "directory-owner":
            os.chown(exports.NFS_FOLDER, 1000, -1)
        else:
            exports.NFS_FOLDER.chmod(0o777)
    with pytest.raises((OSError, ValueError)):
        exports.nfs_exports()
