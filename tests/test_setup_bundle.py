import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from tools import build_helper, setup_artifacts, setup_bundle

TAG = "v1.2.3"
REVISION = "a" * 40


@pytest.fixture
def local(tmp_path):
    root = tmp_path / "bundle"
    root.mkdir(mode=0o700)
    build_helper.bundle(root, TAG, REVISION)
    return root


def contents(root):
    return {path.name: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in root.iterdir()}


def test_local_bundle_reads_verified_bytes_without_writes_or_a_release_anchor(local):
    before = contents(local)
    result = setup_bundle.local_bundle(local)
    assert result.version == TAG and result.revision == REVISION
    assert set(result.files) == {setup_bundle.MANIFEST, setup_bundle.ENTRY, "helper.pyz"}
    assert result.manifest == json.loads(before[setup_bundle.MANIFEST][0])
    assert result.files == {name: data for name, (data, _) in before.items()}
    assert contents(local) == before


def test_staging_uses_the_already_verified_bytes_after_the_source_is_gone(local, tmp_path, monkeypatch):
    result = setup_bundle.local_bundle(local, TAG)
    local.rename(tmp_path / "retained")
    work = tmp_path / "work"
    stage = work / "bundle"
    stage.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(setup_bundle.footprint, "WORK", work)
    setup_bundle.stage_bundle(result, stage)
    assert {path.name: path.read_bytes() for path in stage.iterdir()} == result.files
    assert (stage / setup_bundle.MANIFEST).stat().st_mode & 0o777 == 0o600
    for name in (setup_bundle.ENTRY, "helper.pyz"):
        assert (stage / name).stat().st_mode & 0o777 == 0o700
    assert not local.exists()


def test_explicit_version_must_match_without_changing_the_bundle(local):
    before = contents(local)
    with pytest.raises(ValueError, match="--version"):
        setup_bundle.local_bundle(local, "v1.2.4")
    assert contents(local) == before


@pytest.mark.parametrize("problem", ["extra", "missing", "directory", "symlink", "owner", "writable",
                                     "short-size", "tampered", "oversize", "empty"])
def test_untrusted_or_changed_bundle_files_are_refused(local, problem):
    path = local / "helper.pyz"
    if problem == "extra":
        (local / "unexpected").write_text("unrelated file")
    elif problem == "missing":
        path.unlink()
    elif problem == "directory":
        path.unlink()
        path.mkdir()
    elif problem == "symlink":
        kept = local.parent / "archive"
        path.rename(kept)
        path.symlink_to(kept)
    elif problem == "owner":
        os.chown(path, 1000, -1)
    elif problem == "writable":
        path.chmod(0o720)
    elif problem == "short-size":
        path.write_bytes(path.read_bytes()[:10])
    elif problem == "tampered":
        data = path.read_bytes()
        path.write_bytes(b"!" + data[1:])
    elif problem == "oversize":
        path.write_bytes(b"x" * (setup_artifacts.MAX_UPDATE + 1))
    else:
        path.write_bytes(b"")
    names = set(local.iterdir())
    with pytest.raises((ValueError, OSError)):
        setup_bundle.local_bundle(local)
    assert set(local.iterdir()) == names


@pytest.mark.parametrize("problem", ["root-owner", "root-writable", "root-symlink", "ancestor-symlink",
                                     "ancestor-writable"])
def test_local_bundle_directory_and_ancestors_are_trusted_without_following_links(local, tmp_path, problem):
    target = local
    if problem == "root-owner":
        os.chown(local, 1000, -1)
    elif problem == "root-writable":
        local.chmod(0o720)
    elif problem == "root-symlink":
        target = tmp_path / "link"
        target.symlink_to(local, target_is_directory=True)
    elif problem == "ancestor-symlink":
        parent = tmp_path / "parent-link"
        parent.symlink_to(local.parent, target_is_directory=True)
        target = parent / local.name
    else:
        local.parent.chmod(0o777)
    with pytest.raises((ValueError, OSError)):
        setup_bundle.local_bundle(target)


def test_flash_and_relative_input_paths_are_refused_without_creating_anything(tmp_path):
    for path in (Path("/boot/bundle"), Path("relative"), tmp_path / "../bundle"):
        with pytest.raises(ValueError):
            setup_bundle.local_bundle(path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("field,value", [("format", True), ("format", 2), ("platform", "other"),
    ("version", 1), ("version", "1.2.3"), ("version", "v01.2.3"), ("version", "v1.2.3-rc.1"),
    ("revision", 1), ("revision", "b" * 39), ("extra", True)])
def test_platform_document_strictly_validates_local_identity(local, field, value):
    manifest = local / setup_bundle.MANIFEST
    body = json.loads(manifest.read_bytes())
    body[field] = value
    manifest.write_text(json.dumps(body))
    before = contents(local)
    with pytest.raises(ValueError):
        setup_bundle.local_bundle(local)
    assert contents(local) == before


@pytest.mark.parametrize("data", [b"[]", b"not JSON", b"x" * 65537])
def test_platform_document_rejects_non_objects_invalid_json_and_oversize_metadata(data):
    with pytest.raises(ValueError):
        setup_artifacts.platform_document(data, "unraid")


def test_pure_platform_validator_refuses_an_unsupported_platform():
    with pytest.raises(ValueError, match="unsupported"):
        setup_artifacts.platform_document(b"{}", "other")


@pytest.mark.parametrize("growth", [0, 65537])
def test_file_size_is_rechecked_after_fstat_without_an_unbounded_read(local, monkeypatch, growth):
    path = local / setup_bundle.MANIFEST
    inode = path.stat().st_ino
    original = setup_bundle.os.fstat

    def fstat(fd):
        info = original(fd)
        if info.st_ino == inode:
            path.write_bytes(b"x" * growth)
        return info

    monkeypatch.setattr(setup_bundle.os, "fstat", fstat)
    with pytest.raises(ValueError, match="size limit"):
        setup_bundle.local_bundle(local)


def test_parent_replacement_cannot_redirect_verified_bundle_reads(local, tmp_path, monkeypatch):
    expected = contents(local)
    original = setup_bundle._read
    retained = tmp_path / "retained"
    descriptors = []

    def read(fd, name, limit):
        descriptors.append(os.fstat(fd).st_ino)
        if name == setup_bundle.MANIFEST:
            local.rename(retained)
            local.mkdir(mode=0o700)
            (local / "operator-file").write_text("unrelated")
        return original(fd, name, limit)

    monkeypatch.setattr(setup_bundle, "_read", read)
    result = setup_bundle.local_bundle(local)
    assert len(set(descriptors)) == 1
    assert result.files == {name: data for name, (data, _) in expected.items()}
    assert {path.name for path in local.iterdir()} == {"operator-file"}


@pytest.mark.parametrize("problem", ["outside", "work-root", "public", "symlink"])
def test_staging_refuses_untrusted_destinations_without_writing(local, tmp_path, monkeypatch, problem):
    bundle = setup_bundle.local_bundle(local)
    work = tmp_path / "work"
    stage = work / "bundle"
    stage.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(setup_bundle.footprint, "WORK", work)
    target = stage
    if problem == "outside":
        target = tmp_path / "outside"
    elif problem == "work-root":
        target = work
    elif problem == "public":
        stage.chmod(0o755)
    else:
        target = work / "link"
        target.symlink_to(stage, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        setup_bundle.stage_bundle(bundle, target)
    assert list(stage.iterdir()) == []


def test_bundle_manifest_records_match_the_actual_archive_and_entry(local):
    result = setup_bundle.local_bundle(local)
    for name, record in result.manifest["files"].items():
        assert record["size"] == len(result.files[name])
        assert record["sha256"] == hashlib.sha256(result.files[name]).hexdigest()
