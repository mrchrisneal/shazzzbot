import asyncio
import json
import logging
from unittest.mock import AsyncMock

import pytest
from PIL import ImageDraw

from shazzzbot import bot as runtime
from shazzzbot import helperclient, panel, previewcfg, previewdata, sysimage, view
from shazzzbot.helper.auth import Verifier
from shazzzbot.helper.constants import PROTOCOL
from tests.test_bot import make_bot


def test_different_protocol_logs_once_and_keeps_tasks_visible(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("TEST_HELPER_KEY", "k" * 32)
    settings = helperclient.Settings(str(tmp_path / "helper.sock"), "TEST_HELPER_KEY")
    bot = make_bot(tmp_path, helper=settings)
    requests = []
    verifier = Verifier(b"k" * 32)

    async def helper(reader, writer):
        raw = await reader.readline()
        verifier.verify(raw)
        request = json.loads(raw)
        requests.append(request)
        identity = {"protocol": PROTOCOL + 1, "version": "0.1.0", "sha256": "a" * 64,
                    "bundle_version": "0.1.0", "entry_sha256": "b" * 64}
        writer.write(json.dumps(identity).encode() + b'\n{"done":0,"secs":0}\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def run():
        server = await asyncio.start_unix_server(helper, path=settings.socket)
        async with server:
            for _ in range(2):
                sections = await bot.collect()
                tasks = next(section for section in sections if section.key == "tasks")
                assert (panel.WARNING, "needs the shazzzbot helper") in tasks.issues
                assert "needs the shazzzbot helper" in str(tasks.parts)
                assert bot.helper_node.level == panel.WARNING

    with caplog.at_level(logging.WARNING, "shazzzbot"):
        asyncio.run(run())
    assert [request["protocol"] for request in requests] == [PROTOCOL, PROTOCOL]
    assert [request["action"] for request in requests] == ["hello", "hello"]
    assert caplog.text.count("protocol mismatch") == 1
    assert f"expected {PROTOCOL}" in caplog.text and f"received {PROTOCOL + 1}" in caplog.text
    assert "helper too old" not in caplog.text
    assert "rerun setup.sh --helper" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    assert "k" * 32 not in caplog.text


@pytest.mark.parametrize("missing", ["version", "bundle_version", "entry_sha256"])
def test_partial_helper_identity_is_invalid(missing):
    values = {"version": "0.2.0", "bundle_version": "0.1.0", "entry_sha256": "a" * 64}
    values[missing] = None
    with pytest.raises(ValueError, match="identity is incomplete"):
        runtime.Node("helper", "host.example.com", **values)


@pytest.mark.parametrize("version,bundle,needed", [
    ("0.2.0", "0.1.0", True), ("0.1.0", "0.2.0", True),
    ("0.2.0", "0.2.0", False), ("0.3.0", "0.3.0", False)])
def test_helper_update_flag_uses_both_components_in_all_views(tmp_path, monkeypatch, version, bundle, needed):
    monkeypatch.setattr(runtime, "__version__", "0.2.0")
    settings = helperclient.Settings("/run/helper.sock", "TEST_HELPER_KEY")
    bot = make_bot(tmp_path, helper=settings)
    bot.latest_release = "v0.2.0"
    bot._check_release = AsyncMock()
    identity = {"protocol": PROTOCOL, "version": version, "sha256": "a" * 64,
                "bundle_version": bundle, "entry_sha256": "b" * 64}
    data = {"generated": runtime.time.time(), "jobs": [], "smart": [], "os_update": "",
            "running": {}, "finished": {}, "durations": {}}
    monkeypatch.setattr(helperclient, "request", AsyncMock(side_effect=[identity, data]))
    section = next(section for section in asyncio.run(bot.collect()) if section.key == "system")
    node = bot.helper_node
    assert (node.version, node.bundle_version, node.entry_sha256) == (version, bundle, "b" * 64)
    assert node.update_needed(bot.latest_release) is needed
    text = view.text(section).description
    rich = str(view.rich(section).to_dict())
    assert "2 nodes ok" in text and "2 nodes ok" in rich
    assert ("update available!" in text) is needed
    assert ("update available!" in rich) is needed
    drawn = []
    original = ImageDraw.ImageDraw.text

    def draw(self, xy, text, *args, **kwargs):
        drawn.append(str(text))
        return original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", draw)
    assert sysimage.render(bot._system, status=bot._status).startswith(b"\x89PNG")
    assert "2 nodes ok" in "".join(drawn)
    assert ("(update available!)" in drawn) is needed
    report = bot.update_report(node)
    assert ("Installed bundle version:" in report) == (version != bundle)
    assert ("Update available." in report) is needed
    assert ("Full helper bundle update required." in report) == (bundle == "0.1.0")
    assert "--upgrade --helper" in report


def test_invalid_component_version_never_claims_current():
    node = runtime.Node("helper", "host.example.com", "not-a-version",
                        bundle_version="0.2.0", entry_sha256="a" * 64)
    assert node.update_needed("v0.2.0") is None
    assert runtime.Node("helper", "host.example.com", None).update_needed("v0.2.0") is None


def test_helper_report_keeps_trusted_commands_and_fits_the_discord_limit(tmp_path):
    bot = make_bot(tmp_path)
    bot.latest_release = "v999999999.999999999.999999999"
    bot._release_checked = 1700000000.0
    host = "a" * 249 + ".com"
    run_on = "b" * 249 + ".com"
    command = "python3 /trusted/helper --update " + "a" * 64
    command += " " + "x" * (1000 - len(command) - 1)
    bot.cfg.node_updates = {"helper": {"hostname": host, "run_on": run_on, "command": command}}
    node = runtime.Node("helper", "untrusted host", "999999998.999999999.999999999",
                        bundle_version="999999997.999999999.999999999", entry_sha256="a" * 64)
    report = bot.update_report(node)
    assert len(report) <= 2000
    assert host in report and run_on in report and command in report
    assert "(runtime only)" in report and "--upgrade --helper" in report
    assert "untrusted host" not in report and "a" * 64 in report
    bot.cfg.node_updates = {"helper": {"hostname": host, "command": "./setup.sh --upgrade --helper"}}
    assert "(runtime only)" not in bot.update_report(node)


def test_helper_component_identity_survives_preview_snapshot_round_trip(tmp_path):
    bot = make_bot(tmp_path, helper=helperclient.Settings("/run/helper.sock", "TEST_HELPER_KEY"))
    bot.helper_node = runtime.Node("helper", "host.example.com", "0.2.0",
                                   bundle_version="0.1.0", entry_sha256="a" * 64)
    bot._read_tasks = AsyncMock(return_value=None)
    snapshot = asyncio.run(previewdata.dump(bot))
    assert snapshot["helper_node"]["bundle_version"] == "0.1.0"
    assert snapshot["helper_node"]["entry_sha256"] == "a" * 64
    _, cfg = previewcfg.check("", previewcfg.BASE)
    copy = previewdata.load(snapshot, cfg, str(tmp_path / "copy"))
    asyncio.run(copy._read_tasks())
    assert copy.helper_node == bot.helper_node
