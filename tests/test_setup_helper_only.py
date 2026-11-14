import hashlib
import json
import runpy
import sys

import pytest

from shazzzbot.helper import footprint
from tools import setup_files
from tools import setup_helper_only as helper


@pytest.fixture
def homes(tmp_path, monkeypatch):
    home, stage = (tmp_path / name for name in ("home", "stage"))
    home.mkdir(mode=0o700)
    stage.mkdir(mode=0o700)
    monkeypatch.setattr(footprint, "open_home", lambda path: setup_files.opened(path, directory=True))
    monkeypatch.setattr(footprint, "_hook", lambda _: None)
    monkeypatch.setattr(helper, "active", lambda _: True)
    return home, stage


def installed(home, stage):
    for name in ("helper.pyz", *footprint.BUNDLE_FILES):
        data = ("verified " + name).encode()
        setup_files.write_safe(home / name, data)
        setup_files.write_safe(stage / name, data)
    setup_files.write_safe(home / "config.json", b'{"format":1}')
    setup_files.write_safe(home / "api.key", b"a" * 32)
    policy = {"format": 1, "artifact": str(home / "helper.pyz"), "config": str(home / "config.json"),
              "sha256": hashlib.sha256((home / "helper.pyz").read_bytes()).hexdigest()}
    setup_files.write_safe(home / "policy.json", json.dumps(policy).encode())
    setup_files.write_safe(home / ".identity.json", json.dumps({
        "format": 1, "install_id": "a" * 32, "hook": str(footprint.HOOK),
        "hook_sha256": "a" * 64, "active": True}).encode())


def test_missing_and_empty_helper_preflight_do_not_write(homes):
    home, _ = homes
    missing = home / "absent"
    value = helper.preflight(missing)
    assert value["fresh"] and value["inode"] is None and value["fingerprint"] == "missing"
    assert len(value["install_id"]) == 32 and not missing.exists()
    value = helper.preflight(home)
    assert value["fresh"] and value["inode"] == [home.stat().st_dev, home.stat().st_ino]
    assert list(home.iterdir()) == []


def test_unidentified_helper_files_are_never_adopted(homes):
    home, _ = homes
    setup_files.write_safe(home / "helper.pyz", b"unknown")
    with pytest.raises(ValueError, match="unidentified"):
        helper.preflight(home)


def test_preflight_preserves_identity_and_detects_content_drift(homes):
    home, stage = homes
    installed(home, stage)
    first = helper.preflight(home)
    assert first["install_id"] == "a" * 32 and not first["fresh"]
    setup_files.write_safe(home / "config.json", b'{"changed": true}')
    second = helper.preflight(home)
    assert first["inode"] == second["inode"]
    assert first["fingerprint"] != second["fingerprint"]


def test_current_matches_verified_artifacts_and_required_private_files(homes):
    home, stage = homes
    installed(home, stage)
    assert helper.current(home, stage)
    (home / "api.key").unlink()
    assert not helper.current(home, stage)
    setup_files.write_safe(home / "api.key", b"a" * 32)
    setup_files.write_safe(stage / "helper.pyz", b"new bundle")
    assert not helper.current(home, stage)


@pytest.mark.parametrize("change", [[], {"sha256": "0" * 64}, {"format": 1, "artifact": "/outside"},
                                     {"config": "/outside"}])
def test_current_rejects_invalid_policy_instead_of_claiming_noop(homes, change):
    home, stage = homes
    installed(home, stage)
    value = json.loads((home / "policy.json").read_bytes())
    value = {**value, **change} if isinstance(change, dict) else change
    setup_files.write_safe(home / "policy.json", json.dumps(value).encode())
    with pytest.raises(ValueError):
        helper.current(home, stage)


def test_current_requires_active_helper_and_enabled_owned_hook(homes, monkeypatch):
    home, stage = homes
    installed(home, stage)
    value = json.loads((home / ".identity.json").read_bytes())
    value["active"] = False
    setup_files.write_safe(home / ".identity.json", json.dumps(value).encode())
    assert not helper.current(home, stage)
    value["active"] = True
    setup_files.write_safe(home / ".identity.json", json.dumps(value).encode())
    monkeypatch.setattr(helper, "active", lambda _: False)
    assert not helper.current(home, stage)
    monkeypatch.setattr(helper, "active", lambda _: True)

    def bad_hook(_):
        raise ValueError("hook disabled or not owned")

    monkeypatch.setattr(footprint, "_hook", bad_hook)
    with pytest.raises(ValueError, match="hook disabled"):
        helper.current(home, stage)


def test_inspection_cli_known_actions_and_unknown_refusal(homes, monkeypatch, capsys):
    home, stage = homes
    helper.main(["preflight", str(home)])
    assert json.loads(capsys.readouterr().out)["fresh"]
    installed(home, stage)
    helper.main(["current", str(home), str(stage)])
    assert capsys.readouterr().out == "1\n"
    from tools import setup_helper_env

    monkeypatch.setattr(setup_helper_env, "managed_env", lambda _: False)
    helper.main(["managed", str(home)])
    assert capsys.readouterr().out == "\n"
    monkeypatch.setattr(setup_helper_env, "managed_env", lambda _: True)
    monkeypatch.setattr(setup_files, "host_identity", lambda _: "b" * 32)
    monkeypatch.setattr(setup_files, "install_identity", lambda *_: "c" * 32)
    helper.main(["managed", str(home)])
    assert capsys.readouterr().out == "c" * 32 + "\n"
    monkeypatch.setattr(setup_helper_env, "needs_sync", lambda *_: True)
    helper.main(["env-needed", str(home), str(home)])
    assert capsys.readouterr().out == "1\n"
    with pytest.raises(ValueError, match="unknown helper-only"):
        helper.main(["invalid", str(home)])


def test_inspection_module_entry_runs_real_preflight(homes, monkeypatch, capsys):
    home, _ = homes
    monkeypatch.setattr(sys, "argv", ["setup_helper_only", "preflight", str(home)])
    runpy.run_path(helper.__file__, run_name="__main__")
    assert json.loads(capsys.readouterr().out)["fresh"]
