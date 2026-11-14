import asyncio
import json
import os
import stat

import pytest

from shazzzbot import helperclient
from shazzzbot.helper import config
from shazzzbot.helper.platforms import unraid, ups_shutdown


@pytest.mark.parametrize("mode,key,value", [("batt_level", "BATTERYLEVEL", "40"),
                                          ("batt_timer", "RTVALUE", "600"), ("sec_timer", "TIMEOUT", "3600"),
                                          ("batt_level", "BATTERYLEVEL", "0"),
                                          ("batt_timer", "RTVALUE", "1.25")])
def test_only_the_selected_shutdown_threshold_can_leave_the_helper(mode, key, value):
    text = f'SHUTDOWN="{mode}"\n{key}="{value}"\nPASSWORD="must-not-leave-helper"\nUSERNAME="private"\n'
    assert ups_shutdown.parse(text) == {"mode": mode, "value": float(value)}


@pytest.mark.parametrize("text", ['', 'SHUTDOWN="other"\nBATTERYLEVEL="40"', 'SHUTDOWN="batt_level"',
                                  'SHUTDOWN="batt_level"\nBATTERYLEVEL="bad"',
                                  'SHUTDOWN="batt_level"\nBATTERYLEVEL="nan"',
                                  'SHUTDOWN="batt_level"\nBATTERYLEVEL="inf"',
                                  'SHUTDOWN="batt_level"\nBATTERYLEVEL="-1"',
                                  'SHUTDOWN="batt_level"\nSHUTDOWN="sec_timer"\nTIMEOUT="2"',
                                  'SHUTDOWN="batt_level"\nBATTERYLEVEL=40'])
def test_invalid_or_unknown_settings_return_no_threshold(text):
    assert ups_shutdown.parse(text) == {}


def test_file_reads_are_bounded_root_owned_and_never_follow_symlinks(tmp_path, monkeypatch):
    path = tmp_path / "nut-dw.cfg"
    assert ups_shutdown.read(path) == {}
    path.write_text('SHUTDOWN="batt_level"\nBATTERYLEVEL="40"\nPASSWORD="private"')
    assert ups_shutdown.read(path) == {"mode": "batt_level", "value": 40}
    assert ups_shutdown.read(path, cap=1) == {}
    original = os.fstat
    def small(fd):
        info = original(fd)
        values = list(info)
        if stat.S_ISREG(info.st_mode):
            values[6] = 0
        return os.stat_result(values)
    with monkeypatch.context() as patch:
        patch.setattr(os, "fstat", small)
        assert ups_shutdown.read(path, cap=1) == {}
    path.chmod(0o666)
    assert ups_shutdown.read(path) == {}
    path.chmod(0o600)
    actual = tmp_path / "actual"
    path.rename(actual)
    path.symlink_to(actual)
    assert ups_shutdown.read(path) == {}
    path.unlink()
    path.write_bytes(b"\xff")
    assert ups_shutdown.read(path) == {}


def test_ups_shutdown_is_explicitly_enabled_and_round_trips_through_real_auth(helper, setup, tmp_path, monkeypatch):
    path = tmp_path / "nut-dw.cfg"
    path.write_text('SHUTDOWN="batt_timer"\nRTVALUE="600"\nPASSWORD="must-not-leave-helper"')
    monkeypatch.setattr(ups_shutdown, "CONFIG", path)
    _, _, _, config_path = setup
    settings = {"format": 1, "enabled": ["ups_shutdown"]}
    config_path.write_text(json.dumps(settings))
    assert config.load_config(config_path, unraid.Unraid())[0]["enabled"] == ["ups_shutdown"]
    helper.cfg.update(enabled=["ups_shutdown"], peer_uids=[os.getuid()])
    monkeypatch.setenv("TEST_HELPER_KEY", helper.key.decode())
    settings = helperclient.Settings(str(tmp_path / "helper.sock"), "TEST_HELPER_KEY")
    async def go():
        server = await asyncio.start_unix_server(helper.client, path=settings.socket)
        async with server:
            assert await helperclient.request(settings, "ups_shutdown") == {"mode": "batt_timer", "value": 600}
            helper.cfg["enabled"] = []
            with pytest.raises(helperclient.Error, match="refused"):
                await helperclient.request(settings, "ups_shutdown")
    asyncio.run(go())
    assert "must-not-leave-helper" not in b"".join(helper.audit.lines).decode()


@pytest.mark.parametrize("value", [[], None, {"mode": "bad", "value": 1}, {"mode": 1, "value": 1},
                                   {"mode": "batt_level"},
                                   {"mode": "batt_level", "value": True}, {"mode": "batt_level", "value": "1"},
                                   {"mode": "batt_level", "value": -1}, {"mode": "batt_level", "value": float("inf")},
                                   {"mode": "batt_level", "value": 10**400}])
def test_client_validates_the_optional_setting_without_echoing_remote_fields(value):
    with pytest.raises(helperclient.Error) as error:
        helperclient.shutdown_data(value)
    assert "private" not in str(error.value)
    assert helperclient.shutdown_data({}) == {}


def test_client_discards_added_ups_fields_instead_of_echoing_them():
    result = helperclient.shutdown_data({"mode": "batt_level", "value": 1, "PASSWORD": "private"})
    assert result == {"mode": "batt_level", "value": 1}
    assert "PASSWORD" not in result and "private" not in str(result)


def test_client_rejects_the_wrong_payload_shape_and_accepts_an_empty_setting(monkeypatch):
    async def go():
        for payload in ({"wrong": {}}, {"ups_shutdown": {}}):
            reader = asyncio.StreamReader()
            reader.feed_data(json.dumps(payload).encode() + b'\n{"done":0,"secs":0}\n')
            reader.feed_eof()
            if "wrong" in payload:
                with pytest.raises(helperclient.Error):
                    await helperclient._receive(reader, "ups_shutdown")
            else:
                assert await helperclient._receive(reader, "ups_shutdown") == {}
    asyncio.run(go())
