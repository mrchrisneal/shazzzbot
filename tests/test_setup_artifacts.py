import hashlib
import io
import json
import tarfile

import pytest

from tools import setup_artifacts as artifacts


@pytest.fixture
def release(tmp_path):
    path = tmp_path / "release.json"
    value = {"format": 1, "version": "v1.2.3", "revision": "a" * 40, "source_sha256": "b" * 64,
             "helper_sha256": "c" * 64, "image": "ghcr.io/owner/app:v1.2.3@sha256:" + "d" * 64,
             "platforms": {"unraid": "e" * 64}}
    path.write_text(json.dumps(value))
    return path, value


def test_release_digests_and_version_are_validated(release):
    path, value = release
    assert artifacts.release(path, "v1.2.3") == value
    for tag in ("v1.2.4", "1.2.3"):
        with pytest.raises(ValueError, match="version mismatch"):
            artifacts.release(path, tag)


@pytest.mark.parametrize("field,bad", [("revision", "bad"), ("revision", 123), ("source_sha256", "G" * 64),
                                       ("helper_sha256", "a" * 63), ("image", "image:latest"), ("image", None)])
def test_release_rejects_invalid_hashes_and_unpinned_images(release, field, bad):
    path, value = release
    value[field] = bad
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="invalid release"):
        artifacts.release(path, "v1.2.3")


@pytest.mark.parametrize("value", [None, [], {}, {"other": "a" * 64}, {"unraid": "short"}, {"unraid": 1},
                                  {"unraid": "A" * 64}, {"unraid": "a" * 64, "other": "b" * 64}])
def test_release_platform_map_is_closed(release, value):
    path, data = release
    data["platforms"] = value
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="platform"):
        artifacts.release(path, "v1.2.3")


@pytest.mark.parametrize("value", [[], {"format": 1}, {"format": 1, "version": "v1.2.3", "secret": "never print"}])
def test_release_manifest_schema_is_closed(release, value):
    path, _ = release
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="manifest"):
        artifacts.release(path, "v1.2.3")


def archive(tmp_path, members):
    path = tmp_path / "source.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        root = tarfile.TarInfo("source")
        root.type = tarfile.DIRTYPE
        tar.addfile(root)
        for name, content, kind in members:
            item = tarfile.TarInfo(name)
            item.type = kind
            item.size = len(content)
            if kind == tarfile.SYMTYPE:
                item.linkname = "/etc/passwd"
            tar.addfile(item, io.BytesIO(content))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


REQUIRED = ("compose.yaml", "compose.relay.yaml", "config.example.toml", ".env.example", "docker-proxy/haproxy.cfg",
            "helper.example.json", "shazzzbot/helper/cli.py")


def test_verified_release_extracts_only_regular_members(tmp_path):
    members = [("source/extra", b"", tarfile.DIRTYPE)]
    members += [("source/" + name, b"validated source", tarfile.REGTYPE) for name in REQUIRED]
    path, digest = archive(tmp_path, members)
    destination = tmp_path / "stage"
    artifacts.extract(path, destination, digest)
    assert (destination / "docker-proxy/haproxy.cfg").read_bytes() == b"validated source"
    assert (destination / "extra").is_dir()
    assert not list(destination.rglob("*.pyz"))
    for bad in ("short", "a" * 64):
        with pytest.raises(ValueError, match="digest|SHA-256"):
            artifacts.verify(path, bad)


@pytest.mark.parametrize("member,kind", [("/absolute", tarfile.REGTYPE), ("source/../escape", tarfile.REGTYPE),
                                         ("source/a\\b", tarfile.REGTYPE), ("single", tarfile.REGTYPE),
                                         ("source/link", tarfile.SYMTYPE), ("source/fifo", tarfile.FIFOTYPE)])
def test_archive_paths_and_links_fail_before_writing(tmp_path, member, kind):
    path, digest = archive(tmp_path, [(member, b"", kind)])
    destination = tmp_path / "stage"
    with pytest.raises(ValueError, match="member"):
        artifacts.extract(path, destination, digest)
    assert not destination.exists()


def test_duplicate_and_excessive_archives_are_rejected(tmp_path, monkeypatch):
    members = [("source/same", b"data", tarfile.REGTYPE)] * 2
    path, digest = archive(tmp_path, members)
    with pytest.raises(ValueError, match="duplicate"):
        artifacts.extract(path, tmp_path / "stage", digest)
    path, digest = archive(tmp_path, [("source/large", b"12345", tarfile.REGTYPE)])
    monkeypatch.setattr(artifacts, "MAX_SOURCE", 4)
    with pytest.raises(ValueError, match="size limit"):
        artifacts.extract(path, tmp_path / "stage", digest)
    entries = [(f"source/x{i}", b"", tarfile.REGTYPE) for i in range(10001)]
    path, digest = archive(tmp_path, entries)
    with pytest.raises(ValueError, match="too many"):
        artifacts.extract(path, tmp_path / "stage", digest)


def test_incomplete_release_never_becomes_a_candidate(tmp_path):
    path, digest = archive(tmp_path, [("source/compose.yaml", b"", tarfile.REGTYPE)])
    with pytest.raises(ValueError, match="required content"):
        artifacts.extract(path, tmp_path / "stage", digest)


def test_an_unreadable_archive_member_is_not_installed(tmp_path, monkeypatch):
    path, digest = archive(tmp_path, [("source/compose.yaml", b"content", tarfile.REGTYPE)])
    monkeypatch.setattr(tarfile.TarFile, "extractfile", lambda *args: None)
    with pytest.raises(ValueError, match="member is missing"):
        artifacts.extract(path, tmp_path / "stage", digest)
