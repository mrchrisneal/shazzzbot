import tomllib

import pytest

from shazzzbot import config, schema
from tests.test_setup_config import GUILD, ROOT, TOKEN, A, B, C, wizard
from tools import schema_export


def test_documentation_examples_do_not_change_runtime_defaults():
    assert schema.SCHEMAS["config"]["outputs"].required
    with pytest.raises(KeyError, match="has no default"):
        schema.default("config", "outputs")
    assert schema.default("host", "docker_proxy") is None
    assert schema.default("ups", "target") == ""
    assert schema.default("config", "node_updates") == {}
    assert schema.default("service_check", "container") == ""
    assert schema.default("preview", "enabled") is False
    assert "example_active" not in schema.names()


def test_generated_example_preserves_every_wizard_anchor(tmp_path):
    schema_export.write(tmp_path)
    text = (tmp_path / "config.example.toml").read_text()
    assert text == schema_export.example()
    assert ('# [helper]\n# Unix socket path inside the bot container.\n'
            '# socket = "/run/shazzzbot-helper/helper.sock"') in text
    for prefix in ("guild_id = ", "audit_channel_id = ", "commands_channel_id = ", "channel = ", "timezone = ",
                   "# [ups]", "# target = ", "docker_proxy = ", "# [helper]", "# socket = ", "# key_env = "):
        assert sum(line.startswith(prefix) for line in text.splitlines()) == 1, prefix
    values = tomllib.loads(text)
    assert values["outputs"] and values["outputs"][0]["channel"] == 0
    assert values["host"]["docker_proxy"] == "http://docker-proxy:2375"
    assert "ups" not in values and "helper" not in values and "forges" not in values
    assert schema_export.check(tmp_path) == []


@pytest.mark.parametrize(("bot_only", "nut", "helper"), [
    (False, "", False), (True, "unit@nut:3493", False), (False, "unit@nut:3493", True),
])
def test_real_wizard_uses_generated_example_without_network(tmp_path, monkeypatch, bot_only, nut, helper):
    examples = tmp_path / "examples"
    schema_export.write(examples)
    (examples / ".env.example").write_text((ROOT / ".env.example").read_text())
    for name in ("SHAZZZBOT_APP_ID", "SHAZZZBOT_RELAY_URL", "SHAZZZBOT_RELAY_KEY", "SHAZZZBOT_RELAY_FINGERPRINT",
                 "SHAZZZBOT_HELPER_KEY", "RELAY_URL", "RELAY_KEY", "RELAY_FINGERPRINT"):
        monkeypatch.delenv(name, raising=False)
    for name, value in {"SHAZZZBOT_DISCORD_TOKEN": TOKEN, "DISCORD_TOKEN": TOKEN,
                        "SHAZZZBOT_GUILD_ID": GUILD, "SHAZZZBOT_STATUS_CHANNEL_ID": A,
                        "SHAZZZBOT_COMMANDS_CHANNEL_ID": C, "SHAZZZBOT_AUDIT_CHANNEL_ID": B,
                        "SHAZZZBOT_TIMEZONE": "UTC", "SHAZZZBOT_UPS": nut, "SHAZZZBOT_SET_ICON": "0"}.items():
        monkeypatch.setenv(name, value)
    if helper:
        monkeypatch.setenv("SHAZZZBOT_HELPER_KEY", "a" * 32)

    def forbidden_lookup(*args):
        pytest.fail("the fully supplied wizard must not contact Discord")

    monkeypatch.setattr(wizard, "lookup", forbidden_lookup)
    result = wizard.bot(wizard.Wizard(False), examples, {"config.toml", ".env"}, bot_only)
    text = result["config.toml"]
    values = tomllib.loads(text)
    assert values["guild_id"] == int(GUILD)
    assert values["audit_channel_id"] == int(B) and values["commands_channel_id"] == int(C)
    assert values["outputs"][0]["channel"] == int(A)
    assert ("docker_proxy" not in values["host"]) == bot_only
    assert values.get("ups", {}).get("target", "") == nut
    assert ("helper" in values) == helper
    if helper:
        assert values["helper"] == {"socket": "/run/shazzzbot-helper/helper.sock", "key_env": "SHAZZZBOT_HELPER_KEY"}
    candidate = tmp_path / "candidate.toml"
    candidate.write_text(text)
    loaded = config.load(str(candidate))
    assert loaded.outputs[0].channel == int(A) and bool(loaded.helper) == helper
