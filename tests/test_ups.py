import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from shazzzbot import ups


@pytest.mark.parametrize("value,expected", [("unit@nut", ("unit", "nut", 3493)),
                                          ("unit-1@127.0.0.1:1234", ("unit-1", "127.0.0.1", 1234)),
                                          ("unit@[::1]", ("unit", "::1", 3493)),
                                          ("unit@[2001:db8::1]:3494", ("unit", "2001:db8::1", 3494)),
                                          ("unit@ups-1.", ("unit", "ups-1.", 3493))])
def test_target_accepts_names_hosts_and_optional_ports(value, expected):
    assert ups.target(value) == expected


@pytest.mark.parametrize("value", [None, True, 1, [], "", "x" * 385, "unit", "@host", "unit@", "a b@host",
                                   "unit@host\nLOGIN", "unit@host?", "unit@host#", "unit@host/path",
                                   "unit:password@host", "unit@host:0", "unit@host:65536", "unit@host:nope",
                                   "unit@host:", "unit@::1", "unit@[bad]", "unit@ups..1", "unit@-host",
                                   "unit@999.1.1.1", "unit@" + "a" * 254, "unit@host..", "unit@host?key=x",
                                   "unit@host#x", "a@b@host", "unit@[fe80::1%eth0]", "unit@[v1.abc]"])
def test_target_rejects_malformed_values_and_command_injection(value):
    with pytest.raises(ValueError):
        ups.target(value)


@pytest.mark.parametrize("state,words", [("OL CHRG", "Online, Charging"), ("OB", "On battery"),
                                        ("OB LB", "On battery, Low battery"), ("OL RB", "Online, Replace battery"),
                                        ("OL FUTURE OL", "Online, FUTURE")])
def test_parse_maps_status_words_and_keeps_units_as_numbers(state, words):
    reading = ups.parse({"ups.status": state, "battery.charge": "97.5", "battery.runtime": "2520", "ups.load": "23"})
    assert reading.words == words and reading.charge == 97.5 and reading.runtime == 2520 and reading.load == 23


@pytest.mark.parametrize("status", [None, "", "garbage", "OL\x1b", "OL " + "A" * 257, "@everyone"])
def test_garbage_status_is_no_data(status):
    assert ups.parse({"ups.status": status}) == ups.Reading()


@pytest.mark.parametrize("value", [None, "", "bad", "nan", "inf", "-1", "101"])
def test_bad_numeric_data_stays_unknown_without_hiding_low_battery(value):
    reading = ups.parse({"ups.status": "OB LB", "battery.charge": value})
    assert reading.charge is None and reading.runtime is None and reading.load is None
    assert reading.words == "On battery, Low battery"


def connection(monkeypatch, responses):
    reader = AsyncMock()
    reader.readline.side_effect = responses
    writer = Mock()
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()
    connect = AsyncMock(return_value=(reader, writer))
    monkeypatch.setattr(ups.asyncio, "open_connection", connect)
    return reader, writer, connect


def replies(state="OL CHRG"):
    return [f'VAR unit ups.status "{state}"\n'.encode(), b'VAR unit battery.charge "97"\r\n',
            b'VAR unit battery.runtime "2520"\n', b'VAR unit ups.load "23"\n']


def test_client_sends_only_fixed_read_only_get_requests_and_closes(monkeypatch):
    reader, writer, connect = connection(monkeypatch, replies())
    result = asyncio.run(ups.read("unit@nut"))
    assert result == ups.Reading(("OL", "CHRG"), 97, 2520, 23)
    connect.assert_awaited_once_with("nut", 3493, limit=ups.MAX_BYTES)
    assert [call.args[0] for call in writer.write.call_args_list] == [
        f"GET VAR unit {variable}\n".encode() for variable in ups.VARIABLES]
    assert reader.readline.await_count == writer.drain.await_count == 4
    writer.close.assert_called_once_with()
    writer.wait_closed.assert_awaited_once_with()


def test_unsupported_values_are_unknown_and_cleanup_errors_do_not_discard_data(monkeypatch):
    _, writer, _ = connection(monkeypatch, [replies()[0], *[b"ERR VAR-NOT-SUPPORTED\n"] * 3])
    writer.wait_closed.side_effect = OSError("closed")
    result = asyncio.run(ups.read("unit@nut"))
    assert result == ups.Reading(("OL", "CHRG"))
    assert ups._reply(b"ERR VAR-NOT-SUPPORTED\r\n", "unit", "ups.load") is None


@pytest.mark.parametrize("response", [b"", b"garbage\n", b"ERR UNKNOWN-UPS\n", b"ERR ACCESS-DENIED\n",
                                      b'VAR other ups.status "OL"\n', b'VAR unit ups.load "23"\n',
                                      b'VAR unit ups.status "OL"', b'VAR unit ups.status "\xff"\n'])
def test_invalid_or_mismatched_replies_stop_without_retry(monkeypatch, response):
    _, writer, _ = connection(monkeypatch, [response])
    assert asyncio.run(ups.read("unit@nut")) == ups.Reading()
    assert writer.write.call_count == 1
    writer.close.assert_called_once_with()


def test_total_response_size_is_capped(monkeypatch):
    _, writer, _ = connection(monkeypatch, replies())
    monkeypatch.setattr(ups, "MAX_BYTES", len(replies()[0]) + 1)
    assert asyncio.run(ups.read("unit@nut")) == ups.Reading()
    assert writer.write.call_count == 2


def test_single_unterminated_reply_cannot_grow_without_bound(monkeypatch):
    async def go():
        reader = asyncio.StreamReader(limit=ups.MAX_BYTES)
        reader.feed_data(b"x" * (ups.MAX_BYTES + 1))
        reader.feed_eof()
        _, writer, connect = connection(monkeypatch, [])
        connect.return_value = reader, writer
        assert await ups.read("unit@nut") == ups.Reading()
        writer.close.assert_called_once_with()
    asyncio.run(go())


def test_connection_failure_and_bad_target_are_no_data(monkeypatch):
    connect = AsyncMock(side_effect=ConnectionRefusedError("refused"))
    monkeypatch.setattr(ups.asyncio, "open_connection", connect)
    assert asyncio.run(ups.read("unit@nut")) == ups.Reading()
    assert asyncio.run(ups.read("bad target")) == ups.Reading()
    assert connect.await_count == 1


def test_deadline_bounds_a_server_that_never_answers(monkeypatch):
    async def wait():
        await asyncio.Future()
    _, writer, _ = connection(monkeypatch, [])
    monkeypatch.setattr(ups, "TIMEOUT", 0)
    writer.drain.side_effect = wait
    assert asyncio.run(ups.read("unit@nut")) == ups.Reading()
    writer.close.assert_called_once_with()


def test_cancellation_closes_the_connection_and_propagates(monkeypatch):
    _, writer, _ = connection(monkeypatch, [asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ups.read("unit@nut"))
    writer.close.assert_called_once_with()
