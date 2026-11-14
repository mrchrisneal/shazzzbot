import asyncio
import copy
import hashlib
import json
import os
import time
from unittest.mock import AsyncMock, Mock

import pytest

from shazzzbot import helperclient as client
from shazzzbot.helper import VERSION
from tests.test_helper_support import writer

KEY = "c" * 32


def snapshot():
    return {"generated": time.time(), "jobs": [{"name": "Backup", "cron": "0 2 * * *"}], "smart": ["cache"],
            "os_update": "", "running": {}, "finished": {}, "durations": {"Backup": {"at": 1, "seconds": None}}}


def identity():
    return {"protocol": client.PROTOCOL, "version": VERSION, "sha256": "a" * 64,
            "bundle_version": VERSION, "entry_sha256": "b" * 64}


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_HELPER_KEY", KEY)
    return client.Settings(str(tmp_path / "helper.sock"), "TEST_HELPER_KEY")


def test_helper_settings_use_schema_required_keys_without_exposing_key_values(settings):
    assert set(client.schema.names("helper")) == {"socket", "key_env"}
    assert client.validate({"socket": settings.socket, "key_env": settings.key_env}) == settings
    with pytest.raises(SystemExit) as error:
        client.validate({"socket": settings.socket, "key_en": settings.key_env})
    assert "key_en" in str(error.value) and "key_env" in str(error.value) and KEY not in str(error.value)


def test_signed_requests_round_trip_through_real_helper_verifier(helper, settings):
    helper.key = KEY.encode()
    helper.cfg.update(enabled=["tasks"], peer_uids=[os.getuid()])
    data = snapshot()
    helper.platform.read = AsyncMock(return_value=data)
    verify = Mock(wraps=helper.verify)
    helper.verify = verify
    async def go():
        server = await asyncio.start_unix_server(helper.client, path=settings.socket)
        async with server:
            identity = await client.request(settings, "hello")
            entry = helper.path.parent / "shazzzbot-helper-unraid.py"
            assert identity == {"protocol": client.PROTOCOL, "version": VERSION, "sha256": helper.sha256,
                                "bundle_version": VERSION,
                                "entry_sha256": hashlib.sha256(entry.read_bytes()).hexdigest()}
            assert await client.request(settings, "tasks") == data
    asyncio.run(go())
    requests = [json.loads(call.args[0]) for call in verify.call_args_list]
    assert [r["action"] for r in requests] == ["hello", "tasks"]
    assert len({r["nonce"] for r in requests}) == 2
    assert all(r["protocol"] == client.PROTOCOL for r in requests)
    assert all(r["args"] == {} and abs(time.time() - r["ts"]) < 5 for r in requests)
    assert b"action tasks" in b"".join(helper.audit.lines)


@pytest.mark.parametrize("case", ["refused", "timeout", "oversize", "total-size", "broken-json", "duplicate",
                                 "constant", "list", "done-type", "done-fields", "done-secs", "failed", "missing",
                                 "trailing", "multiple", "task-fields", "task-data", "identity-fields", "version-type",
                                 "version", "sha-type", "sha", "eof", "deep"])
def test_protocol_errors_are_clean_and_never_return_partial_data(settings, monkeypatch, case):
    hello = identity()
    done = {"done": 0, "secs": 0}
    messages = [hello, done]
    action = "hello"
    if case == "refused":
        messages = [{"error": "secret details"}]
    elif case == "oversize":
        monkeypatch.setattr(client, "MAX_RESPONSE", 128)
        messages = [{"padding": "x" * 256}]
    elif case == "total-size":
        monkeypatch.setattr(client, "MAX_RESPONSE", len(json.dumps(hello).encode()) + 1)
    elif case.startswith("done-"):
        messages[-1] = {"done": True, "secs": 0} if case == "done-type" else (
            {"done": 0} if case == "done-fields" else {"done": 0, "secs": "bad"})
    elif case == "failed":
        messages[-1]["done"] = 1
    elif case == "missing":
        messages = [done]
    elif case == "trailing":
        messages += [hello]
    elif case == "multiple":
        messages.insert(1, hello)
    elif case == "task-fields":
        action, messages = "tasks", [{"wrong": {}}, done]
    elif case == "task-data":
        action, messages = "tasks", [{"tasks": {}}, done]
    elif case == "identity-fields":
        messages[0] = {}
    elif case in {"version-type", "version", "sha-type", "sha"}:
        hello["version" if case.startswith("version") else "sha256"] = 1 if case.endswith("type") else "!"
    elif case == "list":
        messages = [[]]
    raw = b"".join(json.dumps(m).encode() + b"\n" for m in messages)
    if case == "broken-json":
        raw = b"broken\n"
    elif case == "duplicate":
        raw = b'{"version":"a","version":"b"}\n'
    elif case == "constant":
        raw = b'{"secs":NaN}\n'
    elif case == "deep":
        raw = b"[" * 2000 + b"0" + b"]" * 2000 + b"\n"
    elif case == "eof":
        raw = b""
    async def go():
        release = asyncio.Event()
        finished = asyncio.Event()
        async def respond(reader, output):
            try:
                await reader.readline()
                if case == "timeout":
                    await release.wait()
                else:
                    output.write(raw)
                    await output.drain()
            finally:
                output.close()
                await output.wait_closed()
                finished.set()
        server = await asyncio.start_unix_server(respond, path=settings.socket)
        async with server:
            with pytest.raises(client.Error) as caught:
                await client.request(settings, action)
            assert "secret" not in str(caught.value)
            release.set()
            await finished.wait()
    if case == "timeout":
        monkeypatch.setattr(client, "TIMEOUT", 0.03)
    asyncio.run(go())


def test_missing_socket_key_and_privileged_action_fail_closed(settings, monkeypatch):
    with pytest.raises(client.Error, match="unreachable"):
        asyncio.run(client.request(settings, "tasks"))
    with pytest.raises(client.Error, match="read-only"):
        asyncio.run(client.request(settings, "vm.start"))
    monkeypatch.delenv(settings.key_env)
    with pytest.raises(client.Error, match="key"):
        asyncio.run(client.request(settings, "tasks"))


@pytest.mark.parametrize("close_error", [None, OSError(), TimeoutError()])
def test_non_root_peer_is_refused_and_connection_is_closed(settings, monkeypatch, close_error):
    output = writer(uid=10001)
    output.wait_closed.side_effect = close_error
    monkeypatch.setattr(asyncio, "open_unix_connection", AsyncMock(return_value=(None, output)))
    with pytest.raises(client.Error, match="root"):
        asyncio.run(client.request(settings, "tasks"))
    output.write.assert_not_called()
    output.close.assert_called_once()
    output.wait_closed.assert_awaited_once()


@pytest.mark.parametrize("field,value", [("generated", True), ("generated", 0), ("generated", float("inf")),
                                       ("jobs", {}), ("jobs", [{}]), ("jobs", [{"name": "", "cron": "0 2 * * *"}]),
                                       ("jobs", [{"name": "a", "cron": 1}]),
                                       ("jobs", [{"name": "a", "cron": "x" * 257}]),
                                       ("jobs", [{"name": "a", "cron": "0 2 * *"}]),
                                       ("jobs", [{"name": "a", "cron": "99 2 * * *"}]),
                                       ("smart", {}), ("smart", [""]), ("os_update", 1), ("os_update", "x" * 257),
                                       ("os_update", "line\nbreak"), ("running", []), ("running", {"": 1}),
                                       ("running", {"a": -1}), ("finished", {"a": "bad"}), ("durations", []),
                                       ("durations", {"": {}}), ("durations", {"a": 1}),
                                       ("durations", {"a": {"seconds": 1}}),
                                       ("durations", {"a": {"at": True, "seconds": 1}}),
                                       ("durations", {"a": {"at": 1, "seconds": -1}})])
def test_task_snapshot_is_validated_as_a_whole(field, value):
    data = snapshot()
    data[field] = value
    with pytest.raises(client.Error):
        client.tasks_data(data)


def test_snapshot_requires_all_fields_freshness_and_valid_completed_durations():
    for value in ([], {}, {**snapshot(), "generated": time.time() + 31}):
        with pytest.raises(client.Error):
            client.tasks_data(value)
    data = snapshot()
    data["durations"]["Backup"]["seconds"] = 45
    data["running"] = {"Backup": time.time()}
    data["finished"] = {"Old": 1}
    projected = client.tasks_data(data)
    assert projected == data and projected is not data


def receive(action, messages):
    async def go():
        reader = asyncio.StreamReader(limit=client.MAX_RESPONSE)
        reader.feed_data(b"".join(json.dumps(message).encode() + b"\n" for message in messages))
        reader.feed_eof()
        return await client._receive(reader, action)
    return asyncio.run(go())


@pytest.mark.parametrize("action", ["hello", "tasks", "ups_shutdown", "empty-ups"])
def test_added_response_fields_are_accepted_but_projected_out(action):
    done = {"done": 0, "secs": 0, "future": {"not": "returned"}}
    if action == "hello":
        expected = identity()
        payload = {**expected, "future": {"not": "returned"}}
    elif action == "tasks":
        expected = snapshot()
        expected["running"] = {"Parity": time.time()}
        expected["kinds"] = {"Parity": "parity"}
        data = copy.deepcopy(expected)
        data["future"] = "not returned"
        data["jobs"][0]["future"] = "not returned"
        data["durations"]["Backup"]["future"] = "not returned"
        payload = {"tasks": data, "future": "not returned"}
    else:
        expected = {} if action == "empty-ups" else {"mode": "batt_level", "value": 40}
        data = {**expected, "future": "not returned"}
        payload = {"ups_shutdown": data, "future": "not returned"}
        action = "ups_shutdown"
    assert receive(action, [payload, done]) == expected


@pytest.mark.parametrize("protocol", [None, True, False, "1", -1, 65536])
def test_hello_protocol_must_be_a_known_integer(protocol):
    payload = identity()
    payload.pop("protocol")
    if protocol is not None:
        payload["protocol"] = protocol
    with pytest.raises(client.Error, match="identity") as caught:
        receive("hello", [payload, {"done": 0, "secs": 0}])
    assert not isinstance(caught.value, client.ProtocolMismatch)


@pytest.mark.parametrize("protocol", [0, 2])
def test_known_different_hello_protocol_is_an_explicit_mismatch(protocol):
    payload = {**identity(), "protocol": protocol}
    with pytest.raises(client.ProtocolMismatch) as caught:
        receive("hello", [payload, {"done": 0, "secs": 0}])
    assert caught.value.expected == client.PROTOCOL
    assert caught.value.received == protocol
    assert f"expected {client.PROTOCOL}" in str(caught.value)
    assert "rerun setup.sh --helper" in str(caught.value)


@pytest.mark.parametrize("action", ["hello", "tasks", "ups_shutdown"])
def test_authenticated_protocol_rejections_report_the_helpers_expected_version(action):
    with pytest.raises(client.ProtocolMismatch) as caught:
        receive(action, [{"error": "protocol mismatch", "expected": 2, "received": client.PROTOCOL,
                          "future": "secret details"}])
    assert caught.value.received == 2
    assert f"expected {client.PROTOCOL}, received 2" in str(caught.value)
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("action", ["hello", "tasks"])
def test_refusal_errors_keep_their_type_without_exposing_details(action):
    with pytest.raises(client.Error, match="refused") as caught:
        receive(action, [{"error": "secret details"}])
    assert not isinstance(caught.value, client.ProtocolMismatch)
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("expected", [None, True, "2", -1, 65536, client.PROTOCOL])
def test_protocol_rejections_need_a_known_different_integer(expected):
    with pytest.raises(client.Error, match="invalid protocol rejection") as caught:
        receive("hello", [{"error": "protocol mismatch", "expected": expected, "received": client.PROTOCOL}])
    assert not isinstance(caught.value, client.ProtocolMismatch)


@pytest.mark.parametrize("received", [None, True, False, "1", "secret", -1, 0, 2, 65536])
def test_protocol_rejections_must_match_the_clients_signed_protocol(received):
    with pytest.raises(client.Error, match="invalid protocol rejection") as caught:
        receive("hello", [{"error": "protocol mismatch", "expected": 2, "received": received}])
    assert not isinstance(caught.value, client.ProtocolMismatch)
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("field", ["expected", "received"])
def test_protocol_rejection_fields_are_required(field):
    response = {"error": "protocol mismatch", "expected": 2, "received": client.PROTOCOL}
    response.pop(field)
    with pytest.raises(client.Error, match="invalid protocol rejection") as caught:
        receive("hello", [response])
    assert not isinstance(caught.value, client.ProtocolMismatch)


@pytest.mark.parametrize("fields", [{"version": VERSION}, {"sha256": "a" * 64}])
def test_identity_required_fields_remain_required_with_additions(fields):
    with pytest.raises(client.Error, match="identity"):
        receive("hello", [{"protocol": client.PROTOCOL, "future": "allowed", **fields}])


@pytest.mark.parametrize("field", ["version", "bundle_version", "sha256", "entry_sha256"])
def test_each_identity_field_is_required_even_with_unknown_additions(field):
    payload = identity()
    payload.pop(field)
    payload["future"] = "allowed"
    with pytest.raises(client.Error, match="identity"):
        receive("hello", [payload])


@pytest.mark.parametrize("field", ["version", "bundle_version"])
@pytest.mark.parametrize("value", [None, True, 1, "", "v0.2.0", "01.2.3", "1.02.3", "1.2.03", "1.2",
                                  "1.2.3.4", "0.2.0-beta", "0.2.0+build", "!", "1000000000.0.0"])
def test_runtime_and_bundle_versions_must_be_canonical_plain_versions(field, value):
    payload = identity()
    payload[field] = value
    with pytest.raises(client.Error, match="identity"):
        receive("hello", [payload])


@pytest.mark.parametrize("field", ["sha256", "entry_sha256"])
@pytest.mark.parametrize("value", [None, True, 1, "", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "a" * 64 + "\n"])
def test_runtime_and_entry_hashes_must_be_lowercase_sha256(field, value):
    payload = identity()
    payload[field] = value
    with pytest.raises(client.Error, match="identity"):
        receive("hello", [payload])


def test_hello_preserves_distinct_runtime_and_installed_bundle_identities():
    expected = {**identity(), "version": "0.1.0", "bundle_version": "0.2.0"}
    payload = {**expected, "future": "discarded"}
    assert receive("hello", [payload, {"done": 0, "secs": 0}]) == expected


@pytest.mark.parametrize("kinds", [[], {"not running": "parity"}, {"Parity": 1}, {"Parity": "unknown"}])
def test_added_fields_do_not_relax_task_kind_values(kinds):
    data = snapshot()
    data.update(running={"Parity": time.time()}, kinds=kinds, future="allowed")
    with pytest.raises(client.Error, match="kinds"):
        client.tasks_data(data)


@pytest.mark.parametrize("data", [{"mode": "unknown", "value": 40}, {"mode": "batt_level", "value": True},
                                  {"mode": "batt_level", "value": -1}, {"mode": "batt_level"}])
def test_added_fields_do_not_relax_ups_required_fields_or_values(data):
    with pytest.raises(client.Error, match="UPS"):
        receive("ups_shutdown", [{"ups_shutdown": {**data, "future": "allowed"}, "future": "allowed"}])
