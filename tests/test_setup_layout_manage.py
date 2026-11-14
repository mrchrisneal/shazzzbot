import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from shazzzbot.helper import footprint
from tools import setup_layout as layout
from tools import setup_manage as manage
from tools import setup_pack as pack
from tools import setup_schedule


@pytest.mark.parametrize("path", ["relative", "/", "//double-root", "/a/../b", "/bad\nname", "/bad\rname",
                                  "/bad\0name", "/bad:name", "/bad,name"])
def test_installation_paths_are_validated_without_mutation(path):
    with pytest.raises(ValueError, match="path"):
        manage.manage(["path", path])


def test_installation_path_is_lexically_normalized(capsys):
    manage.manage(["path", "/private//./app"])
    assert capsys.readouterr().out == "/private/app\n"


def test_helper_resolution_initialization_and_work_dispatch(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(footprint, "resolve_home",
                        lambda path: calls.append(("resolve", path)) or Path("/mnt/pool/appdata/H"))
    monkeypatch.setattr(footprint, "initialize", lambda *args: calls.append(("initialize", args)))
    monkeypatch.setattr(footprint, "ensure_workdir", lambda: calls.append(("work", ())))
    manage.manage(["resolve-helper", "/mnt/user/appdata/H"])
    assert calls[-1] == ("resolve", Path("/mnt/user/appdata/H"))
    assert capsys.readouterr().out == "/mnt/pool/appdata/H\n"
    manage.manage(["init-helper", "/mnt/pool/appdata/H", "a" * 32])
    assert calls[-1] == ("initialize", (Path("/mnt/pool/appdata/H"), "a" * 32))
    manage.manage(["work-root", "/tmp/shazzzbot-helper"])
    assert calls[-1] == ("work", ())


def test_layout_state_context_and_environment_dispatch(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(layout, "state", lambda path: calls.append(("state", path)) or "compose-build bot revision")
    monkeypatch.setattr(layout, "context", lambda: {"role": "bot"})
    monkeypatch.setattr(layout, "normalize_environment", lambda *args: calls.append(("environment", args)))
    manage.manage(["layout-state", "/private/app"])
    assert calls[-1] == ("state", Path("/private/app"))
    assert capsys.readouterr().out == "compose-build bot revision\n"
    manage.manage(["layout-info", "/private/app", "role"])
    assert capsys.readouterr().out == "bot\n"
    manage.manage(["layout-env", "/private/app", "/private/candidate"])
    assert calls[-1] == ("environment", (Path("/private/app"), Path("/private/candidate"), "bot"))


def test_source_pack_and_rollback_dispatch_convert_only_paths(monkeypatch):
    calls = []
    monkeypatch.setattr(pack, "apply", lambda *args: calls.append(("pack", args)))
    monkeypatch.setattr(layout, "rollback_compose", lambda *args: calls.append(("rollback", args)))
    manage.manage(["source-pack", "/private/source", "/tmp/shazzzbot-helper/source.pack", "a" * 40, "origin"])
    assert calls[-1] == ("pack", (Path("/private/source"),
                                   Path("/tmp/shazzzbot-helper/source.pack"), "a" * 40, "origin"))
    manage.manage(["layout-rollback-compose", "/private", "/private/compose.yaml", "verified-image"])
    assert calls[-1] == ("rollback", (Path("/private"), Path("/private/compose.yaml"), "verified-image"))


def test_compose_dispatch_reads_complete_template_and_bounds_stdin(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(manage, "read_safe", lambda path: calls.append(("read", path)) or b"services:\n")
    monkeypatch.setattr(layout, "context", lambda: {"role": "bot"})
    monkeypatch.setattr(layout, "compose", lambda *args: calls.append(("compose", args)) or "compiled\n")
    manage.manage(["layout-compose", "/private", "/private/template.yaml"])
    assert capsys.readouterr().out == "compiled\n"
    assert calls == [("read", Path("/private/template.yaml")), ("compose", ("services:\n", {"role": "bot"}))]
    monkeypatch.setattr(sys, "stdin", io.StringIO("x" * (manage.artifacts.MAX_SOURCE + 2)))
    manage.manage(["layout-compose-input", "/private"])
    assert capsys.readouterr().out == "compiled\n"
    assert calls[-1] == ("compose", ("x" * (manage.artifacts.MAX_SOURCE + 1), {"role": "bot"}))


@pytest.mark.parametrize("active", ["0", "1"])
def test_helper_identity_dispatch_is_a_pure_payload_from_verified_paths(monkeypatch, capsys, active):
    calls = []
    monkeypatch.setattr(footprint, "identity_payload",
                        lambda *args, **kwargs: calls.append((args, kwargs)) or b"identity")
    manage.manage(["helper-identity", "/mnt/pool/appdata/H", "a" * 32, "/boot/user.scripts/script", active])
    assert capsys.readouterr().out == "identity"
    assert calls == [((Path("/mnt/pool/appdata/H"), "a" * 32),
                     {"hook": Path("/boot/user.scripts/script"), "active": active == "1"})]


def test_hook_text_and_active_dispatch(tmp_path, monkeypatch, capsys):
    calls = []
    source = tmp_path / "staged-entry.py"
    source.write_bytes(b"verified platform entry")
    source.chmod(0o600)
    monkeypatch.setattr(setup_schedule, "hook_text", lambda *args: calls.append(args) or b"hook")
    monkeypatch.setattr(setup_schedule, "active", lambda path: calls.append((path,)) or True)
    manage.manage(["launcher-text", "/mnt/pool/appdata/H", "/boot/user.scripts/script", str(source)])
    assert capsys.readouterr().out == "hook"
    assert calls[-1] == (Path("/mnt/pool/appdata/H"), Path("/boot/user.scripts/script"), b"verified platform entry")
    manage.manage(["hook-active", "/boot/user.scripts"])
    assert capsys.readouterr().out == "1\n"
    assert calls[-1] == (Path("/boot/user.scripts"),)


def test_private_configuration_drift_is_detected_before_deployment(tmp_path, monkeypatch):
    app = tmp_path / "private"
    app.mkdir(mode=0o700)
    monkeypatch.setattr(manage, "host_identity", lambda: "1" * 32)
    monkeypatch.setenv("SHAZZZBOT_INSTALL_ID", "a" * 32)
    env = app / ".env"
    env.write_text("DISCORD_TOKEN=original\n")
    env.chmod(0o600)
    manage.manage(["identity", str(app), "bot", "bot-only", "v1.2.3"])
    env.write_text("DISCORD_TOKEN=replacement\n")
    with pytest.raises(ValueError, match="configuration changed"):
        manage.manage(["drift", str(app), "bot", "bot-only", "v1.2.3"])
    assert env.read_text() == "DISCORD_TOKEN=replacement\n"


def test_cli_reports_a_subprocess_timeout_without_a_traceback(monkeypatch, capsys):
    def failure(_args):
        raise subprocess.TimeoutExpired(["git", "status"], 120)
    monkeypatch.setattr(manage, "manage", failure)
    monkeypatch.setattr(sys, "argv", ["setup_manage.py", "source-pack", "/private"])
    with pytest.raises(SystemExit, match="timed out"):
        manage.main()
    assert "Traceback" not in capsys.readouterr().err


def test_recovery_identity_cannot_be_reassigned_after_it_is_saved(tmp_path, monkeypatch):
    backup = tmp_path / "backup"
    backup.mkdir(mode=0o700)
    app = tmp_path / "private/app"
    monkeypatch.setattr(manage, "host_identity", lambda: "1" * 32)
    manage.manage(["remember-id", str(backup), str(app), "a" * 32])
    saved = (backup / "install-identity.json").read_bytes()
    manage.manage(["remember-id", str(backup), str(app), "a" * 32])
    with pytest.raises(ValueError, match="saved install identity mismatch"):
        manage.manage(["remember-id", str(backup), str(app), "b" * 32])
    assert (backup / "install-identity.json").read_bytes() == saved


@pytest.mark.parametrize("identifier", [None, "", "invalid", "A" * 32, "a" * 31, "a" * 32 + ":" + "b" * 64])
def test_install_identity_requires_an_explicit_environment_id(tmp_path, monkeypatch, identifier):
    if identifier is None:
        monkeypatch.delenv("SHAZZZBOT_INSTALL_ID", raising=False)
    else:
        monkeypatch.setenv("SHAZZZBOT_INSTALL_ID", identifier)
    app = tmp_path / "uncreated"
    with pytest.raises(ValueError, match="invalid install id"):
        manage.manage(["identity", str(app), "bot", "bot-only", "v1.2.3"])
    assert not app.exists()


def test_install_identity_records_the_explicit_environment_id(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAZZZBOT_INSTALL_ID", "a" * 32)
    monkeypatch.setattr(manage, "host_identity", lambda: "1" * 32)
    value = json.loads(manage.identity(tmp_path, "relay", "docker-run", "v1.2.3"))
    assert value["install_id"] == "a" * 32 and value["role"] == "relay"


@pytest.mark.parametrize("identifier", ["", "invalid", "A" * 32, "a" * 32 + ":" + "b" * 64])
def test_recovery_identity_rejects_invalid_ids_without_overwriting(tmp_path, monkeypatch, identifier):
    backup = tmp_path / "backup"
    backup.mkdir(mode=0o700)
    monkeypatch.setattr(manage, "host_identity", lambda: "1" * 32)
    manage.manage(["remember-id", str(backup), "/private/app", "a" * 32])
    saved = (backup / "install-identity.json").read_bytes()
    with pytest.raises(ValueError, match="invalid install id"):
        manage.manage(["remember-id", str(backup), "/private/app", identifier])
    assert (backup / "install-identity.json").read_bytes() == saved


def test_policy_pin_only_accepts_current_or_previous_verified_artifacts(tmp_path, capsys):
    current = hashlib.sha256(b"current archive").hexdigest()
    previous = hashlib.sha256(b"previous archive").hexdigest()
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"format": 1, "sha256": current, "previous_sha256": previous}))
    policy.chmod(0o600)
    manage.manage(["verify-policy", str(policy), current])
    manage.manage(["verify-policy", str(policy), previous])
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        manage.manage(["verify-policy", str(policy), hashlib.sha256(b"tampered archive").hexdigest()])
    assert capsys.readouterr().out == ""


def test_socket_directory_dispatch_uses_only_the_fixed_ram_path(monkeypatch):
    calls = []
    monkeypatch.setattr(footprint, "ensure_socket", lambda gid: calls.append(gid))
    manage.manage(["socket-directory", str(footprint.WORK / "socket"), "10002"])
    assert calls == [10002]
    with pytest.raises(ValueError):
        manage.manage(["socket-directory", "/elsewhere/socket", "10002"])
    assert calls == [10002]
