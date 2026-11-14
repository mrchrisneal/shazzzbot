import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from shazzzbot import bot as B
from shazzzbot import config, panel, relay, sysimage, view
from tests.test_bot import OPER, cmd_bot, ctx, make_bot


@pytest.mark.parametrize("settings", [[], {"unknown": {}}, {"bot": "bad"}, {"bot": {"extra": "x"}},
                                     {"bot": {"hostname": False}}, {"bot": {"hostname": "bad host"}},
                                     {"bot": {"hostname": "valid", "run_on": 1}},
                                     *({"bot": {"hostname": "valid", "command": command}} for command in
                                       (None, 1, "", " ", "x" * 1001, "a\nb", "```", "\x7f", "\u00a0"))])
def test_update_commands_fail_closed(settings):
    cfg = config.Config("t", 1, [config.Output(2)], node_updates=settings)
    with pytest.raises(SystemExit, match="node_updates"):
        config._check(cfg, "config.toml")


def test_update_commands_load_for_known_nodes(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    monkeypatch.setenv("RELAY_URL", "https://relay.example.com")
    monkeypatch.setenv("RELAY_KEY", "k" * 32)
    monkeypatch.setenv("RELAY_FINGERPRINT", "0" * 64)
    path = tmp_path / "config.toml"
    path.write_text('config_format = 1\nguild_id=1\n[[outputs]]\nchannel=2\n'
                    '[node_updates.bot]\nhostname="bot.example.com"\ncommand="./deploy.sh target"\n'
                    '[node_updates.helper]\nhostname="bot.example.com"\n'
                    'command="python3 /trusted/script --update hash"\n'
                    '[node_updates."https://relay.example.com"]\nhostname="relay.example.com"\n'
                    'run_on="workstation.example.com"\ncommand="./deploy.sh target"\n')
    cfg = config.load(str(path))
    assert cfg.node_updates["bot"]["hostname"] == "bot.example.com"
    assert cfg.node_updates[cfg.relays[0].url]["run_on"] == "workstation.example.com"


def test_update_lists_every_node_and_only_trusted_commands(tmp_path, monkeypatch):
    b, cmds = cmd_bot(tmp_path)
    b.cfg.perms = {"admin": {"users": [OPER]}}
    r = relay.Relay("https://relay.example.com:8443", "k", "0" * 64)
    b.cfg.relays = [r]
    b.cfg.node_updates = {
        "bot": {"hostname": "bot.example.com", "run_on": "workstation.example.com", "command": "./deploy.sh target"},
        r.url: {"hostname": "relay.example.com", "command": "./update-relay"},
        "helper": {"hostname": "bot.example.com", "command": "python3 /trusted/script --update " + "a" * 64}}
    b.latest_release = "v99.0.0"
    b.http_session = MagicMock()
    b.helper_node = B.Node("helper", "untrusted @everyone", "0.2.0",
                           bundle_version="0.2.0", entry_sha256="a" * 64)
    monkeypatch.setattr(b, "_check_release", AsyncMock())
    ping = AsyncMock(return_value=(panel.FINE, 5.0, "", "0.0.1"))
    monkeypatch.setattr(relay, "ping", ping)
    caller = ctx(OPER)
    asyncio.run(cmds["update"].callback(caller))
    ping.assert_awaited_once_with(b.http_session, r)
    replies = [call.args[0] for call in caller.respond.await_args_list]
    assert len(replies) == 3
    assert "**NODE:** bot on `bot.example.com`" in replies[0] and f"Current version: v{B.__version__}" in replies[0]
    assert "Run on `workstation.example.com`:\n```\n./deploy.sh target\n```" in replies[0]
    assert "relay on `relay.example.com`" in replies[1] and "Current version: v0.0.1" in replies[1]
    assert "helper on `bot.example.com`" in replies[2] and "Current version: v0.2.0" in replies[2]
    assert all("Update available." in reply and "untrusted" not in reply for reply in replies)
    assert all(len(reply) <= 2000 for reply in replies)
    for call in caller.respond.await_args_list:
        mentions = call.kwargs["allowed_mentions"]
        assert call.kwargs["ephemeral"] and not mentions.everyone and not mentions.users and not mentions.roles


def test_update_unknown_nodes_hostname_and_maximum_command_length(tmp_path):
    b = make_bot(tmp_path)
    b.array = SimpleNamespace(name="host.example.com")
    assert all(n.hostname == "host.example.com" for n in b.node_inventory())
    b.latest_release = "v99.0.0"
    helper = b.node_inventory()[-1]
    assert "Version comparison unavailable." in b.update_report(helper)
    b.latest_release = None
    assert "Latest public release: unavailable." in b.update_report(helper)
    b.cfg.node_updates["bot"] = {"hostname": "a" + "_" * 252, "run_on": "b" + "_" * 252, "command": "x" * 1000}
    b.latest_release = "_" * 64
    text = b.update_report(b.node_inventory()[0])
    assert "Version comparison unavailable." in text and len(text) <= 2000
    b._release_error = "response too large"
    b._release_checked = 1700000000.0
    text = b.update_report(b.node_inventory()[0])
    assert "Latest known public release:" in text and "response too large" in text
    assert "Release check:" in text and len(text) <= 2000
    b.array = SimpleNamespace(name="")
    b.cfg.relays = [relay.Relay("https:///", "k", "0" * 64)]
    assert b.node_inventory()[1].hostname == "unknown"
    assert not B.newer("9" * 5000, B.__version__)


@pytest.mark.parametrize(("relay_version", "helper_version", "latest", "outdated", "count"), [
    ("0.0.1", None, B.__version__, True, 2), (B.__version__, None, B.__version__, False, 2),
    (None, "0.0.1", B.__version__, True, 3), (None, B.__version__, None, False, 3),
    (None, None, "v99.0.0", True, 2)])
def test_nodes_update_flag_in_text_rich_and_image(tmp_path, monkeypatch, relay_version, helper_version,
                                                latest, outdated, count):
    r = relay.Relay("https://relay.example.com", "k", "0" * 64)
    b = make_bot(tmp_path, relays=[r])
    b.nodes = [(panel.FINE, 91.0, "")]
    b.node_versions = {r.url: relay_version}
    b.latest_release = latest
    if helper_version:
        from shazzzbot import helperclient

        b.cfg.helper = helperclient.Settings("/run/helper.sock", "TEST_HELPER_KEY")
        monkeypatch.setattr(helperclient, "request", AsyncMock(side_effect=[
            {"version": helper_version, "sha256": "a" * 64, "protocol": 1,
             "bundle_version": helper_version, "entry_sha256": "b" * 64}, {"generated": B.time.time()}]))
    monkeypatch.setattr(b, "_check_release", AsyncMock())
    section = asyncio.run(b.collect())[0]
    expected = f"{count} nodes ok"
    assert expected in b._status[1]
    text, rich = view.text(section).description, str(view.rich(section).to_dict())
    assert expected in text and expected in rich
    assert ("update available!" in text) == outdated
    assert ("update available!" in rich) == outdated
    labels = []
    original = sysimage.Row
    def row(*args, **kwargs):
        result = original(*args, **kwargs)
        labels.append(result)
        return result
    monkeypatch.setattr(sysimage, "Row", row)
    assert sysimage.render(b._system, status=b._status).startswith(b"\x89PNG")
    node_row = next(r for r in labels if r.label == "Nodes")
    assert bool(node_row.note) == outdated and node_row.dot == panel.FINE


def test_unhealthy_node_retains_update_notice():
    level, text = sysimage.node_status([(panel.FINE, None), (panel.WARNING, None)], outdated=True)
    assert level == panel.WARNING and text == "1 of 2 nodes ok (see logs)  (update available!)"


@pytest.mark.parametrize(("body", "level", "version"), [
    ({"ok": True}, panel.WARNING, None),
    ({"ok": True, "protocol": relay.PROTOCOL, "version": "0.2.0"}, panel.FINE, "0.2.0"),
    ([], panel.WARNING, None), ({}, panel.WARNING, None), ({"ok": False}, panel.WARNING, None),
    *(({"ok": True, "protocol": relay.PROTOCOL, "version": value}, panel.WARNING, None)
      for value in (1, [], "0.2.0;bad", "1" * 5000))])
def test_ping_requires_the_current_protocol_and_valid_remote_version(body, level, version):
    response = MagicMock()
    response.json = AsyncMock(return_value=body)
    http = MagicMock()
    http.get.return_value.__aenter__ = AsyncMock(return_value=response)
    http.get.return_value.__aexit__ = AsyncMock(return_value=False)
    r = relay.Relay("https://relay.example.com", "k", "0" * 64)
    result = asyncio.run(relay.ping(http, r))
    assert result[0] == level and result[3] == version
    assert bool(result[2]) == (level != panel.FINE)
