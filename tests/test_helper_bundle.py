import hashlib
import json
import runpy
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from tools import build_helper, setup_artifacts

ROOT = Path(__file__).resolve().parent.parent
TAG = "v1.2.3"
REVISION = "a" * 40
ENTRY = "shazzzbot-helper-unraid.py"


@pytest.fixture
def sources(tmp_path):
    root = tmp_path / "source"
    helper = root / "shazzzbot/helper"
    helper.mkdir(parents=True)
    (helper / "__init__.py").write_text("VERSION = '1.2.3'\n")
    (root / ENTRY).write_text("print('Unraid launcher')\n")
    return root, helper


def anchored(path):
    data = json.loads(path.read_bytes())
    return {"version": TAG, "revision": REVISION, "helper_sha256": data["files"]["helper.pyz"]["sha256"],
            "platforms": {"unraid": hashlib.sha256(path.read_bytes()).hexdigest()}}


def test_bundle_is_reproducible_and_uses_fixed_files_without_privileged_reads(sources, tmp_path, monkeypatch):
    root, helper = sources
    monkeypatch.setattr(setup_artifacts, "read_safe", lambda *args: pytest.fail("build must not require root"))
    first = build_helper.bundle(tmp_path / "first", TAG, REVISION, helper)
    second = build_helper.bundle(tmp_path / "second", TAG, REVISION, helper)
    assert first.name == second.name == "helper-unraid.json"
    assert first.read_bytes() == second.read_bytes()
    value = json.loads(first.read_bytes())
    assert set(value) == {"format", "platform", "version", "revision", "files"}
    assert value["format"] == 1 and value["platform"] == "unraid"
    assert value["version"] == TAG and value["revision"] == REVISION
    assert set(value["files"]) == {"helper.pyz", ENTRY}
    assert {p.name for p in first.parent.iterdir()} == {"helper.pyz", ENTRY, "helper-unraid.json"}
    assert (first.parent / ENTRY).read_bytes() == (root / ENTRY).read_bytes()
    with zipfile.ZipFile(first.parent / "helper.pyz") as archive:
        assert archive.read("VERSION") == (ROOT / "VERSION").read_bytes()
    for name, record in value["files"].items():
        path = first.parent / name
        assert record == {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size": path.stat().st_size}
        assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_root_bundle_never_overwrites_or_chmods_its_source_entry(sources, monkeypatch):
    root, helper = sources
    entry = root / ENTRY
    before = (entry.read_bytes(), entry.stat().st_mtime_ns, stat.S_IMODE(entry.stat().st_mode))
    actual_write = Path.write_bytes

    def write(path, data):
        assert path != entry
        return actual_write(path, data)

    monkeypatch.setattr(Path, "write_bytes", write)
    build_helper.bundle(root, TAG, REVISION, helper)
    assert before == (entry.read_bytes(), entry.stat().st_mtime_ns, stat.S_IMODE(entry.stat().st_mode))


@pytest.mark.parametrize("tag,revision", [("../bad", REVISION), ("1.2.3", REVISION), (TAG, "short"),
                                        (TAG, "A" * 40)])
def test_bundle_rejects_unpinned_release_identity_before_writing(sources, tmp_path, tag, revision):
    _, helper = sources
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="release tag and revision"):
        build_helper.bundle(output, tag, revision, helper)
    assert not output.exists()


@pytest.mark.parametrize("size", [0, 65537])
def test_bundle_rejects_empty_or_oversize_entries(sources, tmp_path, size):
    root, helper = sources
    (root / ENTRY).write_bytes(b"x" * size)
    with pytest.raises(ValueError, match="entry"):
        build_helper.bundle(tmp_path / "output", TAG, REVISION, helper)


def test_bundle_rejects_oversize_archive(sources, tmp_path, monkeypatch):
    _, helper = sources
    monkeypatch.setattr(setup_artifacts, "MAX_UPDATE", 1)
    with pytest.raises(ValueError, match="archive"):
        build_helper.bundle(tmp_path / "output", TAG, REVISION, helper)


def test_bundle_cli_supports_source_override(sources, tmp_path, monkeypatch, capsys):
    _, helper = sources
    output = tmp_path / "output"
    monkeypatch.setattr(sys, "argv", ["build_helper.py", "--source", str(helper), "--bundle-dir", str(output),
                                     "--version", TAG, "--revision", REVISION])
    build_helper.main()
    assert capsys.readouterr().out.strip() == str(output / "helper-unraid.json")


@pytest.mark.parametrize("extra", [[], ["--version", TAG]])
def test_bundle_cli_requires_revision(tmp_path, monkeypatch, extra):
    monkeypatch.setattr(sys, "argv", ["build_helper.py", "--bundle-dir", str(tmp_path), *extra])
    with pytest.raises(SystemExit) as error:
        build_helper.main()
    assert error.value.code == 2


def test_bundle_cli_defaults_to_the_checked_release_version(sources, tmp_path, monkeypatch, capsys):
    _, helper = sources
    output = tmp_path / "output"
    monkeypatch.setattr(sys, "argv", ["build_helper.py", "--source", str(helper), "--bundle-dir", str(output),
                                     "--revision", REVISION])
    build_helper.main()
    path = output / "helper-unraid.json"
    assert capsys.readouterr().out.strip() == str(path)
    assert json.loads(path.read_bytes())["version"] == "v" + build_helper.version()


def test_bundle_script_entry_uses_default_project_sources(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["build_helper.py", "--bundle-dir", str(tmp_path), "--version", TAG,
                                     "--revision", REVISION])
    runpy.run_path(str(ROOT / "tools/build_helper.py"), run_name="__main__")
    assert capsys.readouterr().out.strip() == str(tmp_path / "helper-unraid.json")
    assert (tmp_path / ENTRY).read_bytes() == (ROOT / ENTRY).read_bytes()


def test_bundle_direct_script_works_with_isolated_python(sources, tmp_path):
    _, helper = sources
    output = tmp_path / "output"
    result = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "tools/build_helper.py"),
                             "--bundle-dir", str(output), "--source", str(helper), "--version", TAG,
                             "--revision", REVISION], capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(output / "helper-unraid.json")


def test_platform_manifest_is_anchored_to_release(sources, tmp_path):
    _, helper = sources
    path = build_helper.bundle(tmp_path / "output", TAG, REVISION, helper)
    expected = json.loads(path.read_bytes())
    assert setup_artifacts.platform_manifest(path, anchored(path), "unraid") == expected


@pytest.mark.parametrize("case", ["unsupported", "missing-pin", "bad-pin", "wrong-hash", "oversize", "bad-json"])
def test_platform_manifest_checks_pin_before_parsing(sources, tmp_path, case):
    _, helper = sources
    path = build_helper.bundle(tmp_path / "output", TAG, REVISION, helper)
    release = anchored(path)
    platform = "unraid"
    if case == "unsupported":
        platform = "other"
    elif case == "missing-pin":
        release["platforms"] = {}
    elif case == "bad-pin":
        release["platforms"]["unraid"] = "short"
    else:
        path.write_bytes(b"x" * 65537 if case == "oversize" else b"not JSON")
        if case == "bad-json":
            release["platforms"]["unraid"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        setup_artifacts.platform_manifest(path, release, platform)


@pytest.mark.parametrize("field,value", [
    ("format", True), ("format", 2), ("platform", "other"), ("version", "v1.2.4"),
    ("revision", "b" * 40), ("extra", "not allowed"), ("files", []),
    ("files", {"../outside": {"sha256": "b" * 64, "size": 1}}),
])
def test_platform_manifest_rejects_wrong_identity_or_file_set(sources, tmp_path, field, value):
    _, helper = sources
    path = build_helper.bundle(tmp_path / "output", TAG, REVISION, helper)
    release = anchored(path)
    body = json.loads(path.read_bytes())
    body[field] = value
    path.write_text(json.dumps(body))
    release["platforms"]["unraid"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="platform"):
        setup_artifacts.platform_manifest(path, release, "unraid")


@pytest.mark.parametrize("record", [None, {}, {"sha256": "bad", "size": 1}, {"sha256": 1, "size": 1},
                                   {"sha256": "b" * 64, "size": True}, {"sha256": "b" * 64, "size": 0},
                                   {"sha256": "b" * 64, "size": 65537},
                                   {"sha256": "b" * 64, "size": 1, "url": "https://example.com"}])
def test_platform_manifest_rejects_bad_entry_record(sources, tmp_path, record):
    _, helper = sources
    path = build_helper.bundle(tmp_path / "output", TAG, REVISION, helper)
    release = anchored(path)
    body = json.loads(path.read_bytes())
    body["files"][ENTRY] = record
    path.write_text(json.dumps(body))
    release["platforms"]["unraid"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="file digest or size"):
        setup_artifacts.platform_manifest(path, release, "unraid")


def test_platform_manifest_rejects_archive_size_and_digest_disagreement(sources, tmp_path):
    _, helper = sources
    path = build_helper.bundle(tmp_path / "output", TAG, REVISION, helper)
    release = anchored(path)
    body = json.loads(path.read_bytes())
    body["files"]["helper.pyz"]["size"] = setup_artifacts.MAX_UPDATE + 1
    path.write_text(json.dumps(body))
    release["platforms"]["unraid"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="size"):
        setup_artifacts.platform_manifest(path, release, "unraid")
    body["files"]["helper.pyz"]["size"] = 1
    path.write_text(json.dumps(body))
    release["platforms"]["unraid"] = hashlib.sha256(path.read_bytes()).hexdigest()
    release["helper_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="disagrees"):
        setup_artifacts.platform_manifest(path, release, "unraid")
