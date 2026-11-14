import io
import json
import os
import runpy
import sys

import pytest

from tools import setup_manage as manage


@pytest.fixture
def tree(tmp_path, monkeypatch):
    app, helper = tmp_path / "app", tmp_path / "helper"
    app.mkdir(mode=0o700)
    helper.mkdir(mode=0o700)
    machine = tmp_path / "machine-id"
    machine.write_text("1" * 32)
    monkeypatch.setenv("SHAZZZBOT_MACHINE_ID", str(machine))
    monkeypatch.setenv("SHAZZZBOT_INSTALL_ID", "a" * 32)
    return app, helper, tmp_path / "backups", machine


def stdin(monkeypatch, value):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(value)))


def test_file_cli_dispatch_validates_reads_and_writes(tree, monkeypatch, capsys):
    app, _, _, machine = tree
    target = app / "file"
    stdin(monkeypatch, b"safe bytes")
    manage.manage(["write", str(target)])
    manage.manage(["trust", str(target)])
    manage.manage(["private", str(target)])
    manage.manage(["private", str(target), str(os.geteuid())])
    manage.manage(["trust", str(app)])
    manage.manage(["mkdir", str(app / "created")])
    manage.manage(["empty", str(app / "created")])
    manage.manage(["data-directory", str(app / "created"), str(os.geteuid())])
    (app / "linked").symlink_to(app / "created")
    with pytest.raises(OSError):
        manage.manage(["data-directory", str(app / "linked"), str(os.geteuid())])
    with pytest.raises(ValueError, match="not owned"):
        manage.manage(["empty", str(app)])
    manage.manage(["read", str(target)])
    assert capsys.readouterr().out == "safe bytes"
    manage.manage(["code", str(app)])
    manage.manage(["host", str(machine), str(app)])
    identifier = capsys.readouterr().out.strip()
    assert len(identifier) == 32 and int(identifier, 16) >= 0


def test_env_dispatch_uses_the_same_parser_and_never_logs_the_value(tree, monkeypatch, capsys):
    app, _, _, _ = tree
    env = app / ".env"
    env.write_text("A='some value' # comment\n")
    env.chmod(0o600)
    manage.manage(["env", str(env), "A"])
    assert capsys.readouterr().out == "some value\n"
    normalized = app / "normalized"
    manage.manage(["env-file", str(env), str(normalized)])
    assert normalized.read_text() == "A=some value\n"
    manage.manage(["env", str(app / "missing"), "A"])
    assert capsys.readouterr().out == "\n"


@pytest.mark.parametrize("bad", ["none", "path", "inode"])
def test_rollback_selection_dispatch_requires_its_recorded_helper(tree, monkeypatch, capsys, bad):
    app, helper, backups, _ = tree
    backups.mkdir(mode=0o700)
    journal = {"format": 2, "helper": str(helper), "helper_inode": [1, 2]}
    if bad == "path":
        journal["helper"] = str(app)
    elif bad == "inode":
        journal["helper_inode"] = None
    path = backups / "manifest.json"
    path.write_text(json.dumps(journal))
    path.chmod(0o600)
    stage = app / "rollback.pyz"
    seen = []

    def select(*args):
        seen.append(args)
        return "f" * 64

    monkeypatch.setattr(manage, "select_pinned_helper", select)
    if bad == "none":
        manage.manage(["select-helper", str(helper), str(stage), str(backups)])
        assert seen == [(helper, stage, [1, 2])]
        assert capsys.readouterr().out == "f" * 64 + "\n"
    else:
        with pytest.raises(ValueError, match="recorded installation"):
            manage.manage(["select-helper", str(helper), str(stage), str(backups)])
        assert seen == []


def test_detection_dispatch_is_read_only_and_checks_its_arguments(tree, monkeypatch, capsys):
    from tools import setup_detect

    app, helper, _, _ = tree
    args = [str(app), str(helper), str(helper / "hook"), "ghcr.io/example/shazzzbot", "v1.0.0", "bot", "/marker"]
    before = {str(path): path.read_bytes() for path in app.iterdir() if path.is_file()}
    seen = []

    def detect(*received):
        seen.append(received)
        return {"test": "report"}

    monkeypatch.setattr(setup_detect, "detect", detect)
    monkeypatch.setattr(setup_detect, "emit", lambda report: "line\t" + report["test"])
    manage.manage(["detect", *args])
    assert seen == [tuple(args)] and capsys.readouterr().out == "line\treport\n"
    assert {str(path): path.read_bytes() for path in app.iterdir() if path.is_file()} == before
    with pytest.raises(ValueError, match="seven arguments"):
        manage.manage(["detect", str(app)])


def test_cli_emits_versioned_deployment_state_and_reads_release_tag(tree, capsys):
    app, _, _, _ = tree
    manage.manage(["layout-state-data", str(app), "compose-image", "bot", "v1.2.3"])
    assert json.loads(capsys.readouterr().out) == {
        "format": 1, "method": "compose-image", "role": "bot", "ref": "v1.2.3"}
    release = app / "release.json"
    release.write_text(json.dumps({"format": 1, "version": "v1.2.3"}))
    manage.manage(["release-tag", str(release)])
    assert capsys.readouterr().out == "v1.2.3\n"


def test_state_fingerprint_tracks_metadata_and_the_directory_without_writing(tree, capsys):
    app, _, backups, _ = tree

    def fingerprint(root):
        manage.manage(["state-fingerprint", str(root), str(backups)])
        value = capsys.readouterr().out.strip()
        assert len(value) == 64 and int(value, 16) >= 0
        return value

    missing = app / "missing"
    assert fingerprint(missing) == fingerprint(missing) and not missing.exists()
    before = fingerprint(app)
    assert before == fingerprint(app) and not list(app.iterdir()) and not backups.exists()
    state = app / ".setup-state"
    state.write_text("compose-image bot v1.0.0\n")
    state.chmod(0o600)
    changed = fingerprint(app)
    assert changed != before
    backups.mkdir(mode=0o700)
    pending = backups / "pending"
    pending.write_text("interrupted\n")
    pending.chmod(0o600)
    assert fingerprint(app) != changed
    pending.unlink()
    saved = state.read_bytes()
    app.rename(app.with_name("previous"))
    app.mkdir(mode=0o700)
    state.write_bytes(saved)
    state.chmod(0o600)
    assert fingerprint(app) != changed


def test_identity_is_pinned_to_machine_and_install_path(tree, capsys):
    app, _, _, _ = tree
    manage.manage(["identity", str(app), "bot", "docker-run", "v1.2.3"])
    manage.manage(["owner", str(app)])
    manage.manage(["drift", str(app), "bot", "docker-run", "v1.2.3"])
    manage.manage(["identity-data", str(app), "bot", "docker-run", "v1.2.3"])
    assert json.loads(capsys.readouterr().out)["host"] == "1" * 32
    value = json.loads((app / ".setup-identity").read_bytes())
    value["host"] = "2" * 32
    (app / ".setup-identity").write_text(json.dumps(value))
    with pytest.raises(ValueError, match="identity mismatch"):
        manage.manage(["owner", str(app)])


def test_journal_cli_dispatch_restores_only_watched_targets(tree, monkeypatch, capsys):
    app, helper, backups, _ = tree
    manage.manage(["backup", str(app), str(helper), str(backups), "2"])
    folder = capsys.readouterr().out.strip()
    manage.manage(["watch", folder, "app", ".env"])
    stdin(monkeypatch, b"safe")
    manage.manage(["replace", folder, "app", ".env", str(os.geteuid())])
    stdin(monkeypatch, b"safe compose")
    manage.manage(["replace", folder, "app", "compose.yaml"])
    manage.manage(["seal", folder, "app", ".env"])
    manage.manage(["restore", folder, str(app), str(helper), "0"])
    assert not (app / ".env").exists() and not (app / "compose.yaml").exists()


def test_process_and_release_dispatch_pass_only_the_named_arguments(tree, monkeypatch, capsys):
    app, _, _, _ = tree
    seen = []
    monkeypatch.setattr(manage.setup_helper, "process", lambda *args: seen.append(args))
    monkeypatch.setattr(manage.setup_helper, "start", lambda *args: seen.append(args))
    for action in ("helper", "signal"):
        manage.manage([action, str(app / "config.json"), str(app / "helper.pyz")])
    manage.manage(["start-helper", str(app / "launcher.py"), str(app / "config.json"), str(app / "helper.pyz")])
    assert seen == [(app / "config.json", app / "helper.pyz", "helper"),
                    (app / "config.json", app / "helper.pyz", "signal"),
                    (app / "launcher.py", app / "config.json", app / "helper.pyz")]
    seen.clear()
    monkeypatch.setattr(manage.artifacts, "verify", lambda *args: seen.append(args))
    monkeypatch.setattr(manage.artifacts, "extract", lambda *args: seen.append(args))
    monkeypatch.setattr(manage.artifacts, "release", lambda *args: {"image": "pinned"})
    manage.manage(["verify", str(app / "file"), "hash"])
    manage.manage(["extract", str(app / "file"), str(app / "stage"), "hash"])
    manage.manage(["release", str(app / "release.json"), "v1.2.3", "image"])
    assert seen == [(app / "file", "hash"), (app / "file", app / "stage", "hash")]
    assert capsys.readouterr().out == "pinned\n"


def test_cli_errors_exit_nonzero_with_a_reason(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["setup_manage.py", "unknown", "/not-a-target"])
    with pytest.raises(SystemExit, match="unknown installer operation"):
        manage.main()
    with pytest.raises(SystemExit, match="unknown installer operation"):
        runpy.run_path(manage.__file__, run_name="__main__")
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("role,method,expected", [
    ("bot", "compose-build", ("compose.yaml", "docker-proxy/haproxy.cfg")),
    ("relay", "compose-build", ("compose.relay.yaml",)),
    ("bot", "compose-image", ("compose.yaml", "docker-proxy/haproxy.cfg")),
    ("relay", "compose-image", ("compose.yaml",)),
    ("bot", "docker-run", ("docker-proxy/haproxy.cfg",)),
    ("relay", "docker-run", ()), ("bot", "bot-only", ())])
def test_identity_tracks_only_the_files_managed_by_this_method(role, method, expected):
    assert manage.managed_names(role, method) == expected


def test_unneeded_files_are_not_adopted_by_an_install_identity(tree):
    app, _, _, _ = tree
    (app / "compose.yaml").write_text("unrelated application")
    value = json.loads(manage.identity(app, "bot", "bot-only", "v1.2.3"))
    assert "compose.yaml" not in value["files"]
    for role, method in (("unknown", "docker-run"), ("bot", "unknown"), ("relay", "bot-only")):
        with pytest.raises(ValueError, match="unknown install"):
            manage.managed_names(role, method)


def test_changed_managed_files_are_refused_but_user_config_remains_editable(tree):
    app, _, _, _ = tree
    compose = app / "compose.yaml"
    compose.write_text("owned")
    manage.manage(["identity", str(app), "bot", "compose-image", "v1.2.3"])
    manage.manage(["managed", str(app), "bot", "compose-image"])
    (app / "config.toml").write_text("operator config edit")
    (app / "config.toml").chmod(0o600)
    manage.managed_files(app, "bot", "compose-image")
    compose.write_text("unrelated application")
    with pytest.raises(ValueError, match="reconcile it"):
        manage.managed_files(app, "bot", "compose-image")
    assert compose.read_text() == "unrelated application"
    compose.unlink()
    manage.managed_files(app, "bot", "compose-image")
    compose.write_text("unregistered")
    manage.manage(["identity", str(app), "bot", "bot-only", "v1.2.3"])
    with pytest.raises(ValueError, match="reconcile it"):
        manage.managed_files(app, "bot", "compose-image")


@pytest.mark.parametrize(("value", "error", "reason"), [
    ([], ValueError, "must be an object"), ({"format": 99}, ValueError, "written by a newer Shazzzbot"),
    ({"format": 2, "files": []}, TypeError, "unsupported install identity")])
def test_unknown_identity_schemas_fail_closed(tree, value, error, reason):
    app, _, _, _ = tree
    path = app / ".setup-identity"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    with pytest.raises(error, match=reason):
        manage.managed_files(app, "bot", "compose-image")
    with pytest.raises(ValueError, match="identity mismatch" if error is TypeError else reason):
        manage.manage(["owner", str(app)])
