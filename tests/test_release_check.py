import json
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from shazzzbot import __version__
from shazzzbot.helper.constants import VERSION
from tools import build_helper, release_check

SHA = "a" * 40


def successful(**changes):
    return {"head_sha": SHA, "event": "push", "status": "completed", "conclusion": "success", **changes}


@pytest.mark.parametrize("tag", [None, 1, "", "1.2.3", "v1.2", "v1.2.3.4", "v01.2.3", "v1.02.3", "v1.2.03",
                                  "v1.2.3-rc1", "v1.2.3+build", "v1.2.3\n", "v-1.2.3"])
def test_versions_reject_noncanonical_tags(tag):
    with pytest.raises(ValueError, match="canonical"):
        release_check.check_versions(tag, "1.2.3", "1.2.3", "1.2.3")


@pytest.mark.parametrize("versions", [("1.2.2", "1.2.3", "1.2.3"), ("1.2.3", "1.2.2", "1.2.3"),
                                       ("1.2.3", "1.2.3", "1.2.2"), (None, "1.2.3", "1.2.3")])
def test_versions_reject_each_mismatched_source(versions):
    with pytest.raises(ValueError, match="must agree"):
        release_check.check_versions("v1.2.3", *versions)


@pytest.mark.parametrize("version", ["0.0.0", "1.0.0", "10.20.30"])
def test_versions_admit_only_matching_release_artifacts(version):
    assert release_check.check_versions("v" + version, version, version, version) is None


@pytest.mark.parametrize("sha", [None, 1, "", "a" * 39, "a" * 41, "A" * 40, "g" * 40, SHA + "\n"])
def test_ci_rejects_noncanonical_commit_identity(sha):
    with pytest.raises(ValueError, match="full lowercase"):
        release_check.check_ci({"workflow_runs": [successful()]}, sha)


@pytest.mark.parametrize("data", [None, [], "", {}, {"workflow_runs": None}, {"workflow_runs": {}},
                                   {"workflow_runs": "success"}])
def test_ci_rejects_malformed_response_shape(data):
    with pytest.raises(ValueError, match="workflow_runs"):
        release_check.check_ci(data, SHA)


@pytest.mark.parametrize("runs", [[], [None], [1], ["success"], [{}],
                                   [successful(head_sha="b" * 40)], [successful(head_sha=True)],
                                   [successful(event="pull_request")], [successful(event=None)],
                                   [successful(status="in_progress")], [successful(status=True)],
                                   [successful(conclusion="failure")], [successful(conclusion="cancelled")],
                                   [successful(conclusion=None)]])
def test_ci_rejects_each_non_admitting_run(runs):
    with pytest.raises(ValueError, match="no completed successful push"):
        release_check.check_ci({"workflow_runs": runs}, SHA)


def test_ci_accepts_a_matching_push_even_when_other_runs_do_not_qualify():
    data = {"workflow_runs": [successful(conclusion="failure"), successful(head_sha="b" * 40), successful()]}
    assert release_check.check_ci(data, SHA) is None


def test_cli_versions_and_ci_accept_valid_local_inputs(tmp_path):
    release_check.main(["versions", "v1.2.3", "1.2.3", "1.2.3", "1.2.3"])
    path = tmp_path / "ci.json"
    path.write_text(json.dumps({"workflow_runs": [successful()]}))
    release_check.main(["ci", SHA, str(path)])


@pytest.mark.parametrize("kind", ["versions", "json", "missing", "ci"])
def test_cli_refusals_exit_nonzero_with_a_clear_reason(tmp_path, capsys, kind):
    path = tmp_path / "ci.json"
    path.write_text("not json" if kind == "json" else '{"workflow_runs": []}')
    args = ["versions", "v1.2.3", "1.2.3", "1.2.2", "1.2.3"] if kind == "versions" else \
        ["ci", SHA, str(path.with_name("missing") if kind == "missing" else path)]
    with pytest.raises(SystemExit) as error:
        release_check.main(args)
    assert error.value.code == 1
    assert "release check failed:" in capsys.readouterr().err


def test_script_entry_point_uses_the_same_admission_gate(monkeypatch):
    monkeypatch.setattr(sys, "argv", [release_check.__file__, "versions", "v1.2.3", "1.2.3", "1.2.3", "1.2.3"])
    runpy.run_path(release_check.__file__, run_name="__main__")


def test_bot_and_standalone_archive_use_one_lightweight_version_source(tmp_path):
    archive = tmp_path / "helper.pyz"
    build_helper.build(archive)
    root = Path(__file__).resolve().parent.parent
    for path, module, name in ((root, "shazzzbot", "__version__"), (archive, "helper", "VERSION")):
        probe = ("import sys; sys.path.insert(0,sys.argv[1]); "
                 f"from {module} import {name}; "
                 f"print({name}); "
                 "assert not any(n.endswith(('helper.server','helper.files','helper.process')) for n in sys.modules)")
        result = subprocess.run([sys.executable, "-I", "-B", "-c", probe, str(path)], cwd=tmp_path,
                                capture_output=True, text=True, timeout=10, check=True)
        assert result.stdout.strip() == __version__ == VERSION


def test_changing_only_the_canonical_file_changes_source_and_archive_versions(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    project = tmp_path / "project"
    helper = project / "shazzzbot/helper"
    helper.mkdir(parents=True)
    for name in ("__init__.py", "constants.py"):
        shutil.copyfile(root / "shazzzbot/helper" / name, helper / name)
    shutil.copyfile(root / "shazzzbot/__init__.py", project / "shazzzbot/__init__.py")
    monkeypatch.setattr(build_helper, "ROOT", project)
    for version in ("7.8.9", "7.8.10"):
        (project / "VERSION").write_text(version + "\n")
        archive = tmp_path / "helper.pyz"
        build_helper.build(archive, helper)
        for source, module, name in ((project, "shazzzbot", "__version__"), (archive, "helper", "VERSION")):
            command = ("import sys; sys.path.insert(0,sys.argv[1]); "
                       f"from {module} import {name}; print({name})")
            result = subprocess.run([sys.executable, "-I", "-B", "-c", command, str(source)], cwd=tmp_path,
                                    capture_output=True, text=True, timeout=10, check=True)
            assert result.stdout.strip() == version


@pytest.mark.parametrize("value", [b"1.0.0", b"1.0.0\n", b"0.0.0\n", b"10.20.30\n"])
def test_canonical_version_file_is_the_release_authority(tmp_path, value):
    path = tmp_path / "VERSION"
    path.write_bytes(value)
    assert release_check.version_file(path) == value.decode().rstrip("\n")


@pytest.mark.parametrize("value", [b"", b"v1.0.0\n", b"01.0.0\n", b"1.0.0 \n", b"1.0.0\r\n",
                                  b"1.0.0\n\n", b"1.0\n", b"1.0.0-rc1\n", b"1.0.0\xff"])
def test_version_file_rejects_noncanonical_contents(tmp_path, value):
    path = tmp_path / "VERSION"
    path.write_bytes(value)
    with pytest.raises(ValueError):
        release_check.version_file(path)


def test_changelog_notes_include_only_the_exact_plain_version_section():
    text = "# Changes\n\n## Unreleased\n\nFuture.\n\n## 1.0.0\n\nReleased.\n\n### Details\n- Safe.\n\n## 0.9.0\nOld.\n"
    assert release_check.notes(text, "v1.0.0") == "Released.\n\n### Details\n- Safe.\n"
    assert release_check.notes("## 1.0.0\nFinal without newline", "v1.0.0") == "Final without newline\n"


@pytest.mark.parametrize("text", ["## [1.0.0]\nWrong heading.\n", "## 0.1.0\nOther.\n",
                                 "## 1.0.0\n\n## 0.9.0\nOther.\n",
                                 "## 1.0.0\nFirst.\n## 1.0.0\nSecond.\n"])
def test_missing_duplicate_and_empty_release_notes_refuse(text):
    with pytest.raises(ValueError, match="CHANGELOG"):
        release_check.notes(text, "v1.0.0")


@pytest.fixture
def assets(tmp_path):
    root, downloaded = tmp_path / "local", tmp_path / "downloaded"
    root.mkdir()
    downloaded.mkdir()
    records = []
    for name in release_check.ASSETS:
        data = ("verified content for " + name).encode()
        (root / name).write_bytes(data)
        (downloaded / name).write_bytes(data)
        records.append({"name": name, "size": len(data), "state": "uploaded"})
    return root, downloaded, {"draft": True, "assets": records}


def test_draft_assets_and_downloaded_bytes_must_all_match(assets):
    root, downloaded, value = assets
    release_check.check_draft(value)
    release_check.check_assets(value, root)
    release_check.check_downloads(root, downloaded)


@pytest.mark.parametrize("value", [None, [], {}, {"draft": False}, {"draft": 1}, {"draft": "true"}])
def test_published_or_malformed_release_is_never_modified(value):
    with pytest.raises(ValueError, match="remain a draft"):
        release_check.check_draft(value)


@pytest.mark.parametrize("records", [None, {}, [], [None], [1] * 6, [{}] * 6,
                                    [{"name": "extra"}] * 6])
def test_remote_assets_require_exact_names_and_count(assets, records):
    root, _, value = assets
    value["assets"] = records
    with pytest.raises(ValueError, match="assets|asset"):
        release_check.check_assets(value, root)


def test_remote_duplicate_asset_is_refused(assets):
    root, _, value = assets
    value["assets"][-1] = value["assets"][0]
    with pytest.raises(ValueError, match="duplicate"):
        release_check.check_assets(value, root)


@pytest.mark.parametrize("field,bad", [("size", True), ("size", "10"), ("size", 0), ("size", 1),
                                      ("state", "new"), ("state", None)])
def test_remote_assets_require_uploaded_state_and_matching_size(assets, field, bad):
    root, _, value = assets
    value["assets"][0][field] = bad
    with pytest.raises(ValueError, match="size or upload state"):
        release_check.check_assets(value, root)


def test_empty_local_artifact_cannot_be_published(assets):
    root, _, value = assets
    (root / release_check.ASSETS[0]).write_bytes(b"")
    value["assets"][0]["size"] = 0
    with pytest.raises(ValueError, match="size or upload state"):
        release_check.check_assets(value, root)


@pytest.mark.parametrize("problem", ["missing", "extra", "tampered"])
def test_remote_download_is_rejected_before_publish_when_incomplete_or_changed(assets, problem):
    root, downloaded, _ = assets
    first = downloaded / release_check.ASSETS[0]
    if problem == "missing":
        first.unlink()
    elif problem == "extra":
        (downloaded / "unexpected").write_bytes(b"extra")
    else:
        first.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="names|SHA-256"):
        release_check.check_downloads(root, downloaded)


def test_new_cli_commands_validate_local_release_before_publication(assets, tmp_path, capsys):
    root, downloaded, value = assets
    info = tmp_path / "release-info.json"
    info.write_text(json.dumps(value))
    version = tmp_path / "VERSION"
    version.write_text("1.0.0\n")
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("## 1.0.0\n\nExact notes.\n")
    release_check.main(["version", str(version)])
    release_check.main(["notes", "v1.0.0", str(changelog)])
    assert capsys.readouterr().out == "1.0.0\nExact notes.\n"
    release_check.main(["draft", str(info)])
    release_check.main(["assets", str(info), str(root)])
    release_check.main(["downloads", str(root), str(downloaded)])
    shutil.rmtree(downloaded)
    with pytest.raises(SystemExit) as error:
        release_check.main(["downloads", str(root), str(downloaded)])
    assert error.value.code == 1
