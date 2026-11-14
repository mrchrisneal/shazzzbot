import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("setup_config", ROOT / "tools" / "setup_config.py")
assert spec and spec.loader
wizard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wizard)

TOKEN = "MTAxMjM0NTY3ODkwMTIzNDU2Nzg5.GaBcDe.abcdefghijklmnopqrstuvwxyz0123456789"
GUILD, A, B, C = "123456789012345678", "223456789012345678", "323456789012345678", "423456789012345678"
CHANNELS = [{"id": A, "name": "status", "category": "Ops"}, {"id": B, "name": "audit", "category": ""},
            {"id": C, "name": "bot", "category": ""}]


@pytest.fixture
def scripted(monkeypatch, tmp_path):
    monkeypatch.setenv("SHAZZZBOT_APP", str(ROOT))
    monkeypatch.setenv("SHAZZZBOT_DISCORD_TOKEN", TOKEN)
    for name in ("SHAZZZBOT_APP_ID", "SHAZZZBOT_GUILD_ID", "SHAZZZBOT_STATUS_CHANNEL_ID", "DISCORD_TOKEN",
                 "SHAZZZBOT_AUDIT_CHANNEL_ID", "SHAZZZBOT_COMMANDS_CHANNEL_ID", "SHAZZZBOT_SET_ICON"):
        monkeypatch.delenv(name, raising=False)
    calls = []

    def setup(answers, guilds):
        replies = iter(answers)
        monkeypatch.setattr(wizard, "input_line", lambda label: next(replies) + "\n")

        def lookup(token, argv):
            calls.append(argv)
            if argv[0] == "guilds":
                return guilds.pop(0)
            if argv[0] == "icon":
                return [{"target": "application", "result": "ok"}, {"target": "avatar", "result": "HTTP 429"}]
            return CHANNELS
        monkeypatch.setattr(wizard, "lookup", lookup)
        return wizard.main(["--examples", str(ROOT), "--out", str(tmp_path), "--create", ".env",
                            "--create", "config.toml"])
    return setup, calls, tmp_path


def test_discovery_menus_fill_the_config(scripted, capsys):
    setup, calls, out = scripted
    # app id, relay URL, icon, recheck, guild, status x2, commands, audit, time zone, UPS, service, forge
    answers = ["523456789012345678", "", "", "r", "1", "1,3", "", "2", "", "", "", ""]
    assert setup(answers, [[], [{"id": GUILD, "name": "Home"}]]) == 0
    config = (out / "config.toml").read_text()
    assert f"guild_id = {GUILD}\n" in config and f"\nchannel = {A}\n" in config
    assert f"audit_channel_id = {B}\n" in config and "commands_channel_id = 0\n" in config
    assert f"[[outputs]]\nchannel = {C}\n" in config
    assert calls == [["guilds"], ["guilds"], ["channels", GUILD]]
    err = capsys.readouterr().err
    assert "The bot is in no server yet. Invite it: https://discord.com/oauth2/authorize?client_id=5234" in err
    assert f"https://discord.com/channels/{GUILD}/{C}" in err and "Ops / status" in err
    assert "Upload it by hand" not in err and TOKEN not in err
    assert f"DISCORD_TOKEN={TOKEN}\n" in (out / ".env").read_text()


def test_manual_entry_and_cancel(scripted, capsys):
    setup, _calls, out = scripted
    answers = ["", "", "n", "m", GUILD, "m", A, "", "", "", "", "", ""]
    assert setup(answers, [[]]) == 0
    assert "Developer Mode" in capsys.readouterr().err
    assert f"\nchannel = {A}\n" in (out / "config.toml").read_text()
    (out / ".env").unlink()
    (out / "config.toml").unlink()
    with pytest.raises(SystemExit, match="cancelled"):
        setup(["", "", "n", "c"], [[]])


def test_menu_rejects_bad_choices(monkeypatch):
    replies = iter(["9", "x", "0"])
    monkeypatch.setattr(wizard, "input_line", lambda label: next(replies))
    with pytest.raises(SystemExit, match="no valid choice"):
        wizard.menu("t", CHANNELS)


def test_bad_timezone_is_a_validation_error():
    assert wizard.timezone("NoSuch/Zone") == 'use a name like "America/Los_Angeles"'
    assert wizard.timezone("../bad") is not None
    assert wizard.timezone("UTC") is None


def test_relay_extra_keys_use_a_secret_prompt(monkeypatch):
    answers = iter(["example.com", "a" * 32])
    seen = []

    def ask(self, env, question, check, **kwargs):
        value = next(answers)
        seen.append((env, kwargs))
        assert check(value) is None
        return value
    monkeypatch.setattr(wizard.Wizard, "ask", ask)
    result = wizard.relay_node(wizard.Wizard(True))
    assert "RELAY_KEYS=" + "a" * 32 in result[".env"]
    assert seen[-1] == ("SHAZZZBOT_RELAY_KEYS", {"required": False, "secret": True})


def test_config_only_repair_does_not_request_unpersistable_forge_secrets(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SHAZZZBOT_GUILD_ID", GUILD)
    monkeypatch.setenv("SHAZZZBOT_STATUS_CHANNEL_ID", A)
    monkeypatch.setenv("DISCORD_TOKEN", TOKEN)
    monkeypatch.setattr(wizard, "set_icon", lambda *a: None)
    monkeypatch.setattr(wizard, "services", lambda *a: [])
    monkeypatch.setattr(wizard, "forges", lambda *a: pytest.fail("cannot save new forge secrets in an existing .env"))
    monkeypatch.setattr(wizard, "input_line", lambda *a: "")
    files = wizard.bot(wizard.Wizard(True), ROOT, {"config.toml"}, False)
    assert set(files) == {"config.toml"}
    assert "put their token environment variables in the existing .env first" in capsys.readouterr().err


def test_blank_required_answers_retry_without_an_unbound_error(monkeypatch, capsys):
    monkeypatch.setattr(wizard, "input_line", lambda label: "")
    with pytest.raises(SystemExit, match="no valid answer"):
        wizard.Wizard(True).ask("", "Required", lambda value: None)
    assert capsys.readouterr().err.count("a value is needed") == 3


def test_icon_changes_require_an_explicit_yes_or_flag(monkeypatch, capsys):
    calls = []
    monkeypatch.delenv("SHAZZZBOT_SET_ICON", raising=False)
    monkeypatch.setattr(wizard, "input_line", lambda label: "")
    monkeypatch.setattr(wizard, "lookup", lambda *args: calls.append(args) or [])
    wizard.set_icon(wizard.Wizard(True), "private-token", "123")
    assert calls == []
    monkeypatch.setenv("SHAZZZBOT_SET_ICON", "1")
    wizard.set_icon(wizard.Wizard(False), "private-token", "123")
    assert len(calls) == 1 and calls[0][1][0] == "icon"
    text = capsys.readouterr().err
    assert "Upload it by hand" in text and "private-token" not in text


def test_dotenv_quotes_inline_comments_and_export_have_one_meaning():
    assert wizard.dotenv(' # comment\nA=plain # comment\nB="double value" # comment\n'
                         "C='single # quoted'\nexport D=hello#literal\nEMPTY=\nTAB=\"x\\ty\"\n") == {
                             "A": "plain", "B": "double value", "C": "single # quoted", "D": "hello#literal",
                             "EMPTY": "", "TAB": "x\ty"}


@pytest.mark.parametrize("text", ["NO_EQUALS", "BAD NAME=value", "A=x\nA=y", "A='unterminated", 'A="bad\\q"',
                                  "A=$OTHER", 'A="line\\nbreak"', 'A="return\\rbreak"', "A=\0"])
def test_dotenv_rejects_ambiguous_or_multiline_values_without_echoing_them(text):
    with pytest.raises(ValueError) as error:
        wizard.dotenv(text)
    assert text not in str(error.value)


def test_candidate_role_cannot_be_overridden_by_the_env_file(tmp_path):
    (tmp_path / ".env").write_text("SHAZZZBOT_ROLE=relay\n")
    with pytest.raises(ValueError, match="match the selected role"):
        wizard.validate_candidate(tmp_path, "bot")
