import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from shazzzbot import config, preview, previewcfg, previewdata, previewvary, schema
from shazzzbot.helper import formats
from tools import setup_config, setup_schema


@pytest.mark.parametrize("header", ["", "config_format = true\n", 'config_format = "1"\n',
                                   "config_format = 0\n", "config_format = -1\n", "config_format = 2\n"])
def test_runtime_and_schema_refuse_unknown_config_formats_without_rewriting(tmp_path, monkeypatch, header):
    original = (header + "guild_id = 1\noutputs = [{channel = 2}]\n").encode()
    path = tmp_path / "config.toml"
    path.write_bytes(original)
    path.chmod(0o600)
    monkeypatch.setenv("DISCORD_TOKEN", "test-placeholder")
    with pytest.raises(formats.FormatError):
        config.load(str(path))
    with pytest.raises(formats.FormatError):
        setup_schema.audit(original)
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_current_config_format_is_required_and_the_schema_is_its_only_default(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(previewcfg.BASE)
    monkeypatch.setenv("DISCORD_TOKEN", "test-placeholder")
    cfg = config.load(str(path))
    assert cfg.config_format == schema.default("config", "config_format") == 1
    assert schema.SCHEMAS["config"]["config_format"].required
    assert not schema.SCHEMAS["config"]["config_format"].editable


@pytest.mark.parametrize("raw", [{"user": "operator", "hash": "private-placeholder"},
                                  {"format": 2, "user": "operator", "hash": "private-placeholder"},
                                  {"format": True, "user": "operator", "hash": "private-placeholder"}])
def test_unsupported_login_header_never_rotates_credentials(tmp_path, raw):
    path = tmp_path / "preview-login.json"
    original = json.dumps(raw).encode()
    path.write_bytes(original)
    cfg = SimpleNamespace(preview=None, state_file=str(tmp_path / "state.json"))
    with pytest.raises(formats.FormatError):
        preview.login(cfg)
    assert path.read_bytes() == original and list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("raw", [{}, {"format": 0}, {"format": True}, {"format": 2}])
def test_preview_snapshot_entrypoints_refuse_unsupported_headers_before_using_values(tmp_path, raw):
    diagnostics, cfg = previewcfg.check("", previewcfg.BASE)
    assert not diagnostics and cfg is not None
    with pytest.raises(formats.FormatError):
        previewdata.load(raw, cfg, str(tmp_path))
    text = json.dumps(raw)
    for step in (0, 1):
        with pytest.raises(formats.FormatError):
            previewvary.vary(text, step)
    with pytest.raises(formats.FormatError):
        asyncio.run(previewdata.render(text, ""))
    assert not list(tmp_path.iterdir())


def test_editor_exposes_an_invalid_base_format_as_a_diagnostic():
    base = previewcfg.BASE.replace("config_format = 1", "config_format = 2")
    diagnostics, cfg = previewcfg.check('[system]\ntitle = "unchanged"\n', base)
    assert cfg is None and "newer Shazzzbot" in diagnostics[0]["message"]


def test_config_audit_and_dry_migration_do_not_stamp_unversioned_input(tmp_path):
    path = tmp_path / "config.toml"
    original = b"guild_id=1\noutputs=[{channel=2}]\n"
    path.write_bytes(original)
    path.chmod(0o600)
    for action in ("--audit-config", "--migrate-config"):
        assert setup_config.schema_command([action, str(path), "--dry-run"]) == 1
    assert path.read_bytes() == original and list(tmp_path.iterdir()) == [path]


def test_toml_format_steps_use_real_headers_before_adding_commented_defaults(monkeypatch):
    spec = schema.SCHEMAS["config"]["config_format"]
    monkeypatch.setitem(schema.SCHEMAS["config"], "config_format", replace(spec, default=2, minimum=2, maximum=2))
    original = b"config_format = 1\r\n# Keep this comment.\r\nguild_id=1\r\noutputs=[{channel=2}]\r\n"
    calls = []

    def advance(data):
        calls.append(data)
        return data.replace(b"config_format = 1", b"config_format = 2", 1)

    monkeypatch.setattr(setup_schema, "TOML_STEPS", {1: advance})
    report = setup_schema.audit(original)
    assert not report.errors and calls == [original]
    assert report.summary().splitlines()[0] == "~ config_format = 1 -> 2"
    assert report.candidate.startswith(original.replace(b"config_format = 1", b"config_format = 2", 1))
    assert b"# default interval = 10\r\n" in report.candidate
    assert setup_schema.audit(report.candidate).candidate == report.candidate
    assert calls == [original]
    old_catalogue = report.candidate.replace(b"config_format = 2", b"config_format = 1", 1)
    upgraded = setup_schema.audit(old_catalogue)
    assert upgraded.candidate == report.candidate and not upgraded.additions
    assert upgraded.summary() == "~ config_format = 1 -> 2"


@pytest.mark.parametrize("candidate", [b"config_format = 1\n", b"config_format = 3\n", b"guild_id=1\n",
                                       b"config_format=true\n", b"not TOML", b"\xff", "config_format=2\n"])
def test_toml_steps_cannot_claim_a_different_format_than_their_bytes(monkeypatch, candidate):
    spec = schema.SCHEMAS["config"]["config_format"]
    monkeypatch.setitem(schema.SCHEMAS["config"], "config_format", replace(spec, default=2, minimum=2, maximum=2))
    monkeypatch.setattr(setup_schema, "TOML_STEPS", {1: lambda _data: candidate})
    with pytest.raises(formats.FormatError):
        setup_schema.audit(b"config_format=1\nguild_id=1\noutputs=[{channel=2}]\n")


def test_toml_step_tables_are_empty_in_production_and_use_shared_validation(monkeypatch):
    assert setup_schema.TOML_STEPS == {}
    monkeypatch.setattr(setup_schema, "TOML_STEPS", {1: None})
    with pytest.raises(formats.FormatError, match="steps"):
        setup_schema.audit(b"config_format=1\nguild_id=1\noutputs=[{channel=2}]\n")


def test_runtime_older_config_format_names_the_manual_migration_path(tmp_path, monkeypatch):
    spec = schema.SCHEMAS["config"]["config_format"]
    monkeypatch.setitem(schema.SCHEMAS["config"], "config_format", replace(spec, default=2, minimum=2, maximum=2))
    path = tmp_path / "config.toml"
    original = b"config_format=1\nguild_id=1\noutputs=[{channel=2}]\n"
    path.write_bytes(original)
    monkeypatch.setenv("DISCORD_TOKEN", "test-placeholder")
    with pytest.raises(formats.FormatError, match="run setup.sh --action migrate"):
        config.load(str(path))
    assert path.read_bytes() == original
    assert setup_schema.TOML_STEPS == {}
