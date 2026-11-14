import json
import os
import runpy
import sys
import tomllib
from pathlib import Path

import pytest

from tools import setup_config as wizard

ROOT = Path(__file__).resolve().parent.parent
GUILD, CHANNEL = "123456789012345678", "223456789012345678"
TOKEN = "MTAxMjM0NTY3ODkwMTIzNDU2Nzg5.GaBcDe.abcdefghijklmnopqrstuvwxyz0123456789"


class Answers:
    def __init__(self, values=(), yes=()):
        self.values, self.decisions = iter(values), iter(yes)

    def ask(self, *args, **kwargs):
        return next(self.values)

    def yes(self, *args, **kwargs):
        return next(self.decisions)


def test_ask_validates_env_defaults_and_secret_prompts(monkeypatch, capsys):
    check = wizard.pattern(wizard.SNOWFLAKE, "invalid id")
    monkeypatch.setenv("TEST_WIZARD_VALUE", "bad")
    with pytest.raises(SystemExit, match="invalid id"):
        wizard.Wizard(False).ask("TEST_WIZARD_VALUE", "Value", check)
    assert wizard.Wizard(False).ask("", "Value", check, default=CHANNEL) == CHANNEL
    assert wizard.Wizard(False).ask("", "Value", check, required=False) == ""
    with pytest.raises(wizard.Missing):
        wizard.Wizard(False).ask("", "Value", check)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(wizard.getpass, "getpass", lambda label: "hidden")
    assert wizard.Wizard(True).ask("", "Secret", lambda value: None, secret=True) == "hidden"
    replies = iter(["invalid", CHANNEL])
    monkeypatch.setattr(wizard, "input_line", lambda label: next(replies))
    assert wizard.Wizard(True).ask("", "Value", check) == CHANNEL
    assert "invalid id" in capsys.readouterr().err


def test_yes_and_input_read_exactly_one_line(monkeypatch, capsys):
    assert wizard.Wizard(False).yes("Continue") is False
    replies = iter(["yes", "", "n"])
    monkeypatch.setattr(wizard, "input_line", lambda label: next(replies))
    assert wizard.Wizard(True).yes("Continue")
    assert wizard.Wizard(True).yes("Continue", True)
    assert wizard.Wizard(True).yes("Continue", True) is False
    monkeypatch.undo()
    data = iter([b"a", b"\n", b"b"])
    monkeypatch.setattr(os, "read", lambda fd, length: next(data))
    assert wizard.input_line("Prompt: ") == "a\n" and next(data) == b"b"
    monkeypatch.setattr(os, "read", lambda fd, length: b"")
    with pytest.raises(SystemExit, match="no input"):
        wizard.input_line("Prompt: ")
    assert "Prompt:" in capsys.readouterr().err


def test_validators_report_bad_values_without_a_traceback():
    def invalid():
        raise TypeError
    assert wizard.validator(invalid) == "TypeError"
    with pytest.raises(SystemExit, match="example file"):
        wizard.set_line([], "missing", "new")
    assert json.loads(wizard.toml_block("table", {"key": "value"}).split(" = ", 1)[1]) == "value"


def test_service_wizard_skips_invalid_entries_and_accepts_url_and_tcp(capsys):
    answers = Answers(["invalid", "", "bad-url", "Local", "container", "http://example.com",
                       "Port", "", "", "example.com:443", "Empty", "", "", ""], [True, True, True, True, False])
    rows = wizard.services(answers)
    assert rows == [{"name": "Local", "container": "container", "url": "http://example.com"},
                    {"name": "Port", "tcp": "example.com:443"}]
    assert "skipped" in capsys.readouterr().err


def test_forge_wizard_validates_before_persisting_tokens(monkeypatch, capsys):
    monkeypatch.setenv("FORGEJO_TOKEN", "")
    monkeypatch.setenv("GITHUB_TOKEN", "")
    answers = Answers(["forgejo", "https://example.com", "invalid-fixture-token", "invalid-repo",
                       "github", "accepted-fixture-token", "owner/repo"], [True, True, False])
    secrets = {}
    rows = wizard.forges(answers, secrets)
    assert len(rows) == 1 and rows[0]["type"] == "github"
    assert list(secrets.values()) == ["accepted-fixture-token"]
    assert "invalid-fixture-token" not in capsys.readouterr().err


def test_relay_wizard_writes_only_validated_connection_settings(monkeypatch):
    for name in ("RELAY_URL", "RELAY_KEY", "RELAY_FINGERPRINT"):
        monkeypatch.setenv(name, "")
    lines = ["# RELAY_URL=", "# RELAY_KEY=", "# RELAY_FINGERPRINT="]
    wizard.relay(Answers(["https://example.com:8443", "k" * 32, "a" * 64]), lines)
    assert lines == ["RELAY_URL=https://example.com:8443", "RELAY_KEY=" + "k" * 32, "RELAY_FINGERPRINT=" + "a" * 64]
    with pytest.raises(SystemExit):
        wizard.relay(Answers(["bad-url", "k" * 32, "a" * 64]), lines)


def test_lookup_handles_success_and_clean_failures(monkeypatch, capsys):
    from shazzzbot import discover
    async def good(argv, token):
        return [{"id": CHANNEL}]
    monkeypatch.setattr(discover, "run", good)
    assert wizard.lookup("private-fixture-token", ["guilds"]) == [{"id": CHANNEL}]
    async def bad(argv, token):
        raise OSError
    monkeypatch.setattr(discover, "run", bad)
    assert wizard.lookup("private-fixture-token", ["guilds"]) is None
    assert "private-fixture-token" not in capsys.readouterr().err


def test_menus_allow_manual_and_optional_input_and_deduplicate_choices(monkeypatch):
    rows = [{"id": GUILD, "name": "A"}, {"id": CHANNEL, "name": "B"}]
    replies = iter(["", "m", "1,1,2"])
    monkeypatch.setattr(wizard, "input_line", lambda label: next(replies))
    assert wizard.menu("optional", rows, optional=True) == []
    assert wizard.menu("manual", rows) is None
    assert wizard.menu("many", rows, many=True) == [GUILD, CHANNEL]


def test_guild_falls_back_when_discovery_is_unavailable_or_manual(monkeypatch):
    monkeypatch.delenv("SHAZZZBOT_GUILD_ID", raising=False)
    w = wizard.Wizard(True)
    monkeypatch.setattr(w, "ask", lambda *a, **k: GUILD)
    monkeypatch.setattr(wizard, "lookup", lambda *a: None)
    assert wizard.pick_guild(w, "token", "") == GUILD
    monkeypatch.setattr(wizard, "lookup", lambda *a: [{"id": GUILD, "name": "A"}])
    monkeypatch.setattr(wizard, "menu", lambda *a, **k: None)
    assert wizard.pick_guild(w, "token", "") == GUILD


@pytest.mark.parametrize("choices", [[None], [[CHANNEL], None], [[CHANNEL], [], None]])
def test_channel_menus_can_return_to_manual_entry(monkeypatch, choices):
    monkeypatch.delenv("SHAZZZBOT_STATUS_CHANNEL_ID", raising=False)
    w = wizard.Wizard(True)
    replies = iter([CHANNEL, "", ""])
    monkeypatch.setattr(w, "ask", lambda *a, **k: next(replies))
    monkeypatch.setattr(wizard, "lookup", lambda *a: [{"id": CHANNEL, "name": "A"}])
    picked = iter(choices)
    monkeypatch.setattr(wizard, "menu", lambda *a, **k: next(picked))
    assert wizard.pick_channels(w, "token", GUILD) == ([CHANNEL], "", "")


def test_successful_icon_update_has_no_manual_upload_warning(monkeypatch, capsys):
    monkeypatch.setenv("SHAZZZBOT_SET_ICON", "1")
    monkeypatch.setattr(wizard, "lookup", lambda *a: [{"target": "avatar", "result": "ok"}])
    wizard.set_icon(wizard.Wizard(False), "token", "")
    assert "Upload it by hand" not in capsys.readouterr().err


@pytest.fixture
def supplied(monkeypatch):
    for name, value in {"SHAZZZBOT_DISCORD_TOKEN": TOKEN, "SHAZZZBOT_GUILD_ID": GUILD,
                        "SHAZZZBOT_STATUS_CHANNEL_ID": CHANNEL, "SHAZZZBOT_APP_ID": "",
                        "SHAZZZBOT_AUDIT_CHANNEL_ID": "", "SHAZZZBOT_COMMANDS_CHANNEL_ID": "",
                        "SHAZZZBOT_HELPER_KEY": "k" * 32, "SHAZZZBOT_HELPER_SOCKET_DIR": "/data/helper",
                        "SHAZZZBOT_UPS": "ups@example.com", "SHAZZZBOT_RELAY_URL": "",
                        "DISCORD_TOKEN": TOKEN}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("SHAZZZBOT_SET_ICON", raising=False)


def test_bot_wizard_can_create_env_only_or_optional_helper_ups_config(supplied):
    w = wizard.Wizard(False)
    assert set(wizard.bot(w, ROOT, {".env"}, True)) == {".env"}
    files = wizard.bot(w, ROOT, {".env", "config.toml"}, True)
    assert tomllib.loads(files["config.toml"])["helper"]["socket"] == "/run/shazzzbot-helper/helper.sock"
    assert tomllib.loads(files["config.toml"])["ups"] == {"target": "ups@example.com"}
    assert "HELPER_SOCKET_DIR=/data/helper" in files[".env"]


def test_duplicate_status_or_audit_channel_is_rejected(supplied, monkeypatch):
    monkeypatch.setenv("SHAZZZBOT_AUDIT_CHANNEL_ID", CHANNEL)
    with pytest.raises(SystemExit, match="must be different"):
        wizard.bot(wizard.Wizard(False), ROOT, {"config.toml"}, False)


def test_config_only_without_a_token_does_not_call_discord(supplied, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "")
    monkeypatch.setattr(wizard, "set_icon", lambda *a: pytest.fail("no token: no Discord call"))
    files = wizard.bot(wizard.Wizard(False), ROOT, {"config.toml"}, False)
    assert set(files) == {"config.toml"}


def test_relay_wizard_rejects_bad_names_and_keys(monkeypatch):
    monkeypatch.setenv("RELAY_KEYS", "")
    monkeypatch.setenv("SHAZZZBOT_RELAY_ALLOW", "not/a/host")
    with pytest.raises(SystemExit, match="host names"):
        wizard.relay_node(wizard.Wizard(False))
    monkeypatch.setenv("SHAZZZBOT_RELAY_ALLOW", "example.com")
    monkeypatch.setenv("SHAZZZBOT_RELAY_KEYS", "short")
    with pytest.raises(SystemExit):
        wizard.relay_node(wizard.Wizard(False))


def test_main_missing_values_and_empty_create_set_are_safe(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SHAZZZBOT_APP", str(ROOT))
    monkeypatch.delenv("SHAZZZBOT_RELAY_ALLOW", raising=False)
    args = ["--examples", str(ROOT), "--out", str(tmp_path), "--role", "relay", "--no-prompt"]
    assert wizard.main(args) == 1
    assert "missing required value" in capsys.readouterr().err
    monkeypatch.setenv("SHAZZZBOT_RELAY_ALLOW", "example.com")
    monkeypatch.setenv("SHAZZZBOT_RELAY_KEYS", "")
    monkeypatch.setattr(sys, "argv", ["setup_config", *args])
    assert wizard.main() == 0 and list(tmp_path.iterdir()) == []


def test_candidate_validation_uses_parsed_env_before_loading_config(tmp_path, monkeypatch):
    from shazzzbot import config
    (tmp_path / ".env").write_text('DISCORD_TOKEN="fixture" # comment\nRELAY_ALLOW=example.com\n')
    called = []
    monkeypatch.setattr(config, "load", lambda path: called.append(path))
    monkeypatch.setenv("DISCORD_TOKEN", "previous")
    monkeypatch.setenv("RELAY_ALLOW", "")
    monkeypatch.setenv("RELAY_KEYS", "")
    wizard.validate_candidate(tmp_path, "bot")
    assert called == [str(tmp_path / "config.toml")] and os.environ["DISCORD_TOKEN"] == "fixture"
    wizard.validate_candidate(tmp_path, "relay")
    monkeypatch.setenv("SHAZZZBOT_APP", str(ROOT))
    assert wizard.main(["--validate", str(tmp_path), "relay"]) == 0
    (tmp_path / ".env").write_text("RELAY_ALLOW=\n")
    with pytest.raises(SystemExit, match="required"):
        wizard.validate_candidate(tmp_path, "relay")


def test_wizard_entry_point_exposes_help_without_mutation(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["setup_config", "--help"])
    with pytest.raises(SystemExit) as caught:
        runpy.run_path(wizard.__file__, run_name="__main__")
    assert caught.value.code == 0 and "--create" in capsys.readouterr().out
