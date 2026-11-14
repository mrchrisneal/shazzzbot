import os
import runpy
import sys
import tomllib
from pathlib import Path

import pytest

from shazzzbot import __version__
from tools import setup_config
from tools import setup_transaction as transaction
from tools.setup_schema import audit

BASE = b'config_format = 1\r\n# operator note\r\nguild_id = 1\r\n[[outputs]]\r\nchannel = 2\r\n'
ENV = b'DISCORD_TOKEN="example-placeholder" # keep this comment\n'


@pytest.fixture
def installation(tmp_path):
    tmp_path.chmod(0o700)
    config = tmp_path / "config.toml"
    config.write_bytes(BASE)
    config.chmod(0o600)
    env = tmp_path / ".env"
    env.write_bytes(ENV)
    env.chmod(0o600)
    return config


@pytest.fixture
def journal(installation, tmp_path_factory, monkeypatch):
    monkeypatch.delenv("SHAZZZBOT_HELPER_INSTALLING", raising=False)
    root = installation.parent
    folder = transaction.snapshot(root, root / "helper", tmp_path_factory.mktemp("journals"), 2)
    transaction.watch(folder, "app", "config.toml")
    return folder


@pytest.mark.parametrize("action", ["--audit-config", "--migrate-config"])
def test_review_and_dry_run_write_nothing(installation, action, capsys):
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in installation.parent.iterdir()}
    args = [action, str(installation)]
    if action == "--migrate-config":
        args += ["--dry-run", "--confirm"]
    assert setup_config.main(args) == 0
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in installation.parent.iterdir()} == before
    out = capsys.readouterr()
    assert "+ interval = 10 (commented)" in out.out
    assert "example-placeholder" not in out.out + out.err


def test_migration_requires_confirmation_and_keeps_comments_and_secrets(installation, journal, capsys):
    args = ["--migrate-config", str(installation), "--transaction", str(journal)]
    assert setup_config.main(args) == 1
    assert "explicit confirmation" in capsys.readouterr().err
    assert sorted(p.name for p in installation.parent.iterdir()) == [".env", "config.toml"]
    assert setup_config.main([*args, "--confirm"]) == 0
    migrated = installation.read_bytes()
    assert migrated.startswith(BASE)
    assert tomllib.loads(migrated.decode()) == tomllib.loads(BASE.decode())
    backup = installation.with_name(f"config.toml.bak-{__version__}")
    assert backup.read_bytes() == BASE
    assert (installation.parent / ".env").read_bytes() == ENV
    before = {p.name: p.stat().st_mtime_ns for p in installation.parent.iterdir()}
    assert setup_config.main([*args, "--confirm"]) == 0
    assert {p.name: p.stat().st_mtime_ns for p in installation.parent.iterdir()} == before
    assert capsys.readouterr().out.endswith("Config is current.\n")


@pytest.mark.parametrize("data, message", [
    (b'config_format = 1\nguild_id = 1\nintervl = 10\n', "did you mean"),
    (b'config_format = 1\nguild_id = 1\n[[outputs]]\nchannel = 2\n[[outputs]]\nchannel = 2\n', "duplicate channel"),
    (b'config_format = 1\nguild_id = 1\n', "missing required"),
])
def test_invalid_config_never_creates_a_backup(installation, journal, data, message, capsys):
    installation.write_bytes(data)
    assert setup_config.main(["--migrate-config", str(installation), "--confirm", "--transaction", str(journal)]) == 1
    out = capsys.readouterr()
    assert message in out.out + out.err
    assert installation.read_bytes() == data
    assert not list(installation.parent.glob("*.bak-*"))


def test_config_read_failures_and_noncanonical_filename_are_reported(installation, capsys):
    assert setup_config.main(["--audit-config", str(installation.with_name("other.toml"))]) == 1
    assert "config.toml" in capsys.readouterr().err
    installation.unlink()
    assert setup_config.main(["--audit-config", str(installation)]) == 1
    assert "No such file" in capsys.readouterr().err


def test_byte_validation_restores_environment_after_success_and_failure(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "previous-placeholder")
    monkeypatch.delenv("EXAMPLE_TEMPORARY", raising=False)
    env = ENV + b"EXAMPLE_TEMPORARY=present\n"
    before = dict(os.environ)
    setup_config.validate_config_bytes(audit(BASE).candidate, env)
    assert dict(os.environ) == before
    with pytest.raises(SystemExit, match="missing required"):
        setup_config.validate_config_bytes(b"config_format = 1\n", env)
    assert dict(os.environ) == before
    with pytest.raises(ValueError, match="must be bot"):
        setup_config.validate_config_bytes(BASE, ENV + b"SHAZZZBOT_ROLE=relay\n")
    assert dict(os.environ) == before


def test_missing_credential_refuses_before_backup(installation, journal, monkeypatch, capsys):
    monkeypatch.setenv("DISCORD_TOKEN", "inherited-placeholder")
    (installation.parent / ".env").write_bytes(b"")
    assert setup_config.main(["--migrate-config", str(installation), "--confirm", "--transaction", str(journal)]) == 1
    assert "DISCORD_TOKEN is not set" in capsys.readouterr().err
    assert not list(installation.parent.glob("*.bak-*"))
    assert os.environ["DISCORD_TOKEN"] == "inherited-placeholder"


def test_audit_entry_point_uses_the_image_source_root(installation, monkeypatch, capsys):
    root = Path(__file__).resolve().parent.parent
    monkeypatch.setenv("SHAZZZBOT_APP", str(root))
    monkeypatch.setattr(sys, "argv", [str(root / "tools/setup_config.py"), "--audit-config", str(installation)])
    with pytest.raises(SystemExit) as result:
        runpy.run_path(str(root / "tools/setup_config.py"), run_name="__main__")
    assert result.value.code == 0
    assert "+ interval" in capsys.readouterr().out


def test_missing_anonymous_memory_support_fails_closed_without_disk_fallback(
        installation, journal, monkeypatch, capsys):
    monkeypatch.delattr(os, "memfd_create")
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in installation.parent.iterdir()}
    assert setup_config.main(["--migrate-config", str(installation), "--confirm", "--transaction", str(journal)]) == 1
    assert "requires os.memfd_create; no files were changed" in capsys.readouterr().err
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in installation.parent.iterdir()} == before


def test_real_migration_requires_a_transaction_argument_without_an_optional_old_path(installation, capsys):
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in installation.parent.iterdir()}
    with pytest.raises(SystemExit) as result:
        setup_config.main(["--migrate-config", str(installation), "--confirm"])
    assert result.value.code == 2
    assert "needs --transaction" in capsys.readouterr().err
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in installation.parent.iterdir()} == before
