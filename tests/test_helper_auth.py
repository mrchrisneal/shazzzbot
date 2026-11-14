import asyncio
import hashlib
import hmac
import json
import time
from asyncio.exceptions import TimeoutError as AsyncTimeoutError
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shazzzbot.helper import auth, constants, footprint
from shazzzbot.helper import server as service
from shazzzbot.helper.platforms import unraid
from tests.test_helper_support import signed, writer

NEXT_VERSION = f"{constants.version_number(constants.VERSION)[0] + 1}.0.0"
LATER_VERSION = f"{constants.version_number(constants.VERSION)[0] + 2}.0.0"


def test_signed_request_replay_expiry_and_nonce_capacity(helper, monkeypatch):
    assert helper.verify(signed(helper)) == ("hello", {})
    with pytest.raises(ValueError):
        helper.verify(signed(helper))
    helper.nonces = {"n" * 16: time.time() - 62}
    assert helper.verify(signed(helper))[0] == "hello"
    helper.nonces = {str(i): time.time() for i in range(4096)}
    with pytest.raises(ValueError):
        helper.verify(signed(helper))
    helper.nonces = {}
    altered = json.loads(signed(helper))
    altered["action"] = "script.run"
    with pytest.raises(ValueError):
        helper.verify(auth.canonical(altered) + b"\n")


@pytest.mark.parametrize("changes", [{"action": 1}, {"args": []}, {"ts": True}, {"ts": 0},
                                     {"nonce": "short"}, {"nonce": 1}, {"ts": 10**1000},
                                     {"ts": -10**1000}])
def test_invalid_signed_fields(helper, changes):
    error = TypeError if {"action", "args"} & changes.keys() else ValueError
    with pytest.raises(error):
        helper.verify(signed(helper, **changes))


@pytest.mark.parametrize("raw", [b"x" * 4097, b"{}", b"[]\n", b"{}\n", b"not-json\n"])
def test_malformed_requests(helper, raw):
    with pytest.raises(ValueError):
        helper.verify(raw)


@pytest.mark.parametrize("sig", [0, "short", "z" * 64])
def test_bad_signatures(helper, sig):
    obj = json.loads(signed(helper))
    obj["sig"] = sig
    with pytest.raises(ValueError):
        helper.verify(auth.canonical(obj) + b"\n")


def test_nonfinite_request_timestamp(helper):
    obj = json.loads(signed(helper))
    obj["ts"] = float("nan")
    with pytest.raises(ValueError):
        helper.verify(json.dumps(obj).encode() + b"\n")


def test_hello_identity_update_notice_and_repeat_window(helper, monkeypatch, capsys):
    helper.cfg.update(update_version=NEXT_VERSION, update_sha256="b" * 64)
    fresh = service.Helper(helper.cfg, helper.key, helper.path, unraid.Unraid())
    now = [1.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    emit, metadata = AsyncMock(), {"latest_version": NEXT_VERSION, "latest_sha256": "b" * 64}
    asyncio.run(helper.dispatch("hello", metadata, emit))
    asyncio.run(helper.dispatch("hello", metadata, emit))
    output = capsys.readouterr()
    assert f"startup version={constants.VERSION} sha256={fresh.sha256}" in output.out
    assert helper.key.decode() not in output.out
    assert output.err.count("update available") == 1 and "--update " + "b" * 64 in output.err
    assert str(footprint.HOOK) in output.err and "run: bash " in output.err
    assert "runtime only" in output.err and "full helper bundle update required" in output.err
    assert "--upgrade --helper" in output.err
    assert str(helper.path) not in output.err
    assert emit.await_args_list[0].args[0] == {"version": constants.VERSION, "sha256": helper.sha256,
        "protocol": constants.PROTOCOL, "bundle_version": constants.VERSION,
        "entry_sha256": hashlib.sha256((helper.path.parent / footprint.BUNDLE_FILES[0]).read_bytes()).hexdigest()}
    assert emit.await_args_list[1].args[0] == {"done": 0, "secs": 0}
    now[0] += 3600
    helper.audit.notice(*helper.audit.latest, helper.sha256, helper.path)
    assert "update available" in capsys.readouterr().err
    helper.audit.notice(constants.VERSION, helper.sha256, helper.sha256, helper.path)
    assert helper.audit.latest is None
    asyncio.run(helper.dispatch("hello", {}, emit))


@pytest.mark.parametrize("args", [{"latest_version": "x"}, {"latest_version": "bad\n", "latest_sha256": "b" * 64},
                                  {"latest_version": "x", "latest_sha256": "b" * 64},
                                  {"latest_version": "x", "latest_sha256": "bad"},
                                  {"latest_version": 1, "latest_sha256": "b" * 64},
                                  {"latest_version": "x", "latest_sha256": 1}])
def test_hello_rejects_untrusted_metadata(helper, args):
    with pytest.raises(ValueError):
        asyncio.run(helper.dispatch("hello", args, AsyncMock()))


@pytest.mark.parametrize("bound,pin,incoming,command", [
    (NEXT_VERSION, "b", "b", True), (LATER_VERSION, "b", "b", False), ("", "b", "b", False),
    (NEXT_VERSION, "", "b", False), (NEXT_VERSION, "c", "b", False),
    (NEXT_VERSION, "own", "own", False), (constants.VERSION, "own", "b", False)])
@pytest.mark.parametrize("version", [NEXT_VERSION, "v" + NEXT_VERSION])
def test_hello_notice_requires_a_matching_version_bound_config_pin(helper, capsys, bound, pin, incoming,
                                                                 command, version):
    pin = helper.sha256 if pin == "own" else pin * 64
    incoming = helper.sha256 if incoming == "own" else incoming * 64
    helper.cfg.update(update_version=bound, update_sha256=pin)
    asyncio.run(helper.dispatch("hello", {"latest_version": version, "latest_sha256": incoming}, AsyncMock()))
    output = capsys.readouterr()
    assert ("run: bash " in output.err) is command
    assert ("--update " + incoming in output.err) is command
    assert ("pin update_version and update_sha256" in output.err) is not command
    assert "update available" in output.out and "update available" in output.err


def test_hello_tag_prefix_alone_cannot_bind_an_empty_config_version(helper, capsys):
    helper.cfg.update(update_version="", update_sha256="b" * 64)
    with pytest.raises(ValueError, match="version"):
        asyncio.run(helper.dispatch("hello", {"latest_version": "v", "latest_sha256": "b" * 64}, AsyncMock()))
    output = capsys.readouterr()
    assert "run: bash " not in output.err and "--update" not in output.err and helper.audit.latest is None


def test_hello_uses_validated_bundle_version_and_entry_digest(helper):
    path = helper.path.parent / footprint.BUNDLE_FILES[1]
    bundle = json.loads(path.read_bytes())
    bundle["version"] = "v1.2.3"
    path.write_text(json.dumps(bundle))
    emit = AsyncMock()
    asyncio.run(helper.dispatch("hello", {}, emit))
    assert emit.await_args_list[0].args[0] == {
        "version": constants.VERSION, "sha256": helper.sha256, "protocol": constants.PROTOCOL,
        "bundle_version": "1.2.3", "entry_sha256": bundle["files"][footprint.BUNDLE_FILES[0]]["sha256"]}


@pytest.mark.parametrize("bad", ["missing-manifest", "missing-entry", "malformed-manifest", "changed-entry"])
def test_hello_never_returns_partial_or_unverified_bundle_identity(helper, bad):
    entry = helper.path.parent / footprint.BUNDLE_FILES[0]
    manifest = helper.path.parent / footprint.BUNDLE_FILES[1]
    if bad == "missing-manifest":
        manifest.unlink()
    elif bad == "missing-entry":
        entry.unlink()
    elif bad == "malformed-manifest":
        manifest.write_bytes(b"{}")
    else:
        entry.write_bytes(b"changed entry")
    emit = AsyncMock()
    with pytest.raises((OSError, ValueError)):
        asyncio.run(helper.dispatch("hello", {}, emit))
    emit.assert_not_awaited()


def test_unix_client_credentials_authentication_limits_and_timeout(helper):
    helper.cfg["peer_uids"] = [10001]
    for uid, raw, allowed in ((0, signed(helper), False), (10001, b"bad\n", False),
                              (10001, signed(helper, action=1), False), (10001, signed(helper), True)):
        w, r = writer(uid), SimpleNamespace(readline=AsyncMock(return_value=raw))
        asyncio.run(helper.client(r, w))
        reply = json.loads(w.write.call_args_list[0].args[0])
        assert ("version" in reply) is allowed
        w.close.assert_called_once()
        assert not helper.clients
    w, r = writer(), SimpleNamespace(readline=AsyncMock(side_effect=AsyncTimeoutError))
    asyncio.run(helper.client(r, w))
    assert json.loads(w.write.call_args.args[0]) == {"error": "refused"}
    helper.clients = set(range(64))
    asyncio.run(helper.client(r, w))
    assert len(helper.clients) == 64


def test_nonce_expiry_uses_wall_time_even_when_monotonic_clock_changes(helper, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    monkeypatch.setattr(time, "monotonic", lambda: -1000000)
    raw = signed(helper, ts=1030)
    assert helper.verify(raw)[0] == "hello"
    now[0] -= 5
    with pytest.raises(ValueError):
        helper.verify(signed(helper))
    now[0] = 1060
    with pytest.raises(ValueError):
        helper.verify(raw)
    now[0] = 1061
    assert helper.verify(signed(helper))[0] == "hello"


@pytest.mark.parametrize("protocol", [0, 2, 65535])
def test_authenticated_protocol_mismatch_is_typed_bounded_and_consumes_the_nonce(helper, protocol):
    raw = signed(helper, protocol=protocol)
    with pytest.raises(auth.ProtocolMismatch) as caught:
        helper.verify(raw)
    assert caught.value.expected == constants.PROTOCOL and caught.value.received == protocol
    with pytest.raises(ValueError) as replay:
        helper.verify(raw)
    assert not isinstance(replay.value, auth.ProtocolMismatch)


def test_missing_protocol_is_a_closed_shape_error(helper):
    obj = json.loads(signed(helper))
    del obj["protocol"]
    del obj["sig"]
    obj["sig"] = hmac.new(helper.key, auth.canonical(obj), hashlib.sha256).hexdigest()
    with pytest.raises(ValueError) as caught:
        helper.verify(auth.canonical(obj) + b"\n")
    assert not isinstance(caught.value, auth.ProtocolMismatch)
    assert not helper.nonces


@pytest.mark.parametrize("protocol", [True, False, "private-fixture", None, 1.0, -1, 65536, 10**1000])
def test_invalid_protocol_values_are_refused_without_a_protocol_mismatch(helper, protocol):
    with pytest.raises(ValueError) as caught:
        helper.verify(signed(helper, protocol=protocol))
    assert not isinstance(caught.value, auth.ProtocolMismatch)
    assert not helper.nonces


def test_protocol_is_hmac_covered_and_unknown_request_keys_stay_closed(helper):
    obj = json.loads(signed(helper))
    obj["protocol"] = constants.PROTOCOL + 1
    with pytest.raises(ValueError) as caught:
        helper.verify(auth.canonical(obj) + b"\n")
    assert not isinstance(caught.value, auth.ProtocolMismatch)
    assert not helper.nonces
    with pytest.raises(ValueError):
        helper.verify(signed(helper, unknown="private-fixture"))


@pytest.mark.parametrize("protocol", [0, 2, 65535])
def test_protocol_mismatch_logs_safe_versions_and_never_dispatches_an_action(helper, capsys, protocol):
    helper.cfg["peer_uids"] = [10001]
    helper.dispatch = AsyncMock()
    output = writer()
    reader = SimpleNamespace(readline=AsyncMock(return_value=signed(helper, protocol=protocol, action="parity.check")))
    asyncio.run(helper.client(reader, output))
    helper.dispatch.assert_not_awaited()
    assert json.loads(output.write.call_args.args[0]) == {
        "error": "protocol mismatch", "expected": constants.PROTOCOL, "received": protocol}
    logged = capsys.readouterr().out
    assert f"protocol mismatch expected={constants.PROTOCOL} received={protocol}" in logged
    assert "private-fixture" not in logged and helper.key.decode() not in logged
    assert not helper.busy and not helper.actions and not helper.clients


def test_invalid_protocol_gets_only_a_generic_refusal_and_never_dispatches(helper, capsys):
    helper.cfg["peer_uids"] = [10001]
    helper.dispatch = AsyncMock()
    output = writer()
    reader = SimpleNamespace(readline=AsyncMock(return_value=signed(helper, protocol="private-fixture")))
    asyncio.run(helper.client(reader, output))
    helper.dispatch.assert_not_awaited()
    assert json.loads(output.write.call_args.args[0]) == {"error": "refused"}
    logged = capsys.readouterr().out
    assert "protocol mismatch" not in logged and "private-fixture" not in logged


def test_bad_protocol_signature_gets_no_mismatch_diagnostic(helper, capsys):
    helper.cfg["peer_uids"] = [10001]
    helper.dispatch = AsyncMock()
    raw = json.loads(signed(helper))
    raw["protocol"] = 2
    output = writer()
    asyncio.run(helper.client(SimpleNamespace(readline=AsyncMock(return_value=auth.canonical(raw) + b"\n")), output))
    helper.dispatch.assert_not_awaited()
    assert json.loads(output.write.call_args.args[0]) == {"error": "refused"}
    assert "protocol mismatch" not in capsys.readouterr().out


def test_protocol_mismatch_response_delivery_failure_still_cleans_up_the_connection(helper):
    helper.cfg["peer_uids"] = [10001]
    output = writer()
    output.drain.side_effect = TimeoutError
    asyncio.run(helper.client(SimpleNamespace(readline=AsyncMock(return_value=signed(helper, protocol=2))), output))
    output.close.assert_called_once()
    assert not helper.clients


def test_repeated_protocol_mismatch_logs_once_until_a_valid_signed_request(helper, capsys):
    helper.cfg["peer_uids"] = [10001]
    for nonce in ("a" * 16, "b" * 16):
        output = writer()
        raw = signed(helper, protocol=constants.PROTOCOL + 1, nonce=nonce)
        asyncio.run(helper.client(SimpleNamespace(readline=AsyncMock(return_value=raw)), output))
        assert json.loads(output.write.call_args.args[0]) == {
            "error": "protocol mismatch", "expected": constants.PROTOCOL, "received": constants.PROTOCOL + 1}
    assert capsys.readouterr().out.count("protocol mismatch expected=") == 1
    assert helper.protocol_mismatch == (constants.PROTOCOL, constants.PROTOCOL + 1)
    output = writer()
    asyncio.run(helper.client(SimpleNamespace(readline=AsyncMock(return_value=signed(helper))), output))
    assert json.loads(output.write.call_args_list[0].args[0])["protocol"] == constants.PROTOCOL
    assert helper.protocol_mismatch is None
    raw = signed(helper, protocol=constants.PROTOCOL + 1, nonce="c" * 16)
    asyncio.run(helper.client(SimpleNamespace(readline=AsyncMock(return_value=raw)), writer()))
    assert capsys.readouterr().out.count("protocol mismatch expected=") == 1
    assert not helper.clients and not helper.actions and not helper.busy
