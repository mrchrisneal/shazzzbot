import asyncio
import builtins
import fcntl
import hashlib
import importlib.util
import json
import os
import signal
import stat
import sys
import time
import urllib.request
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from shazzzbot.helper import artifact, auth, constants, footprint, updates
from shazzzbot.helper import audit as audit_log
from shazzzbot.helper import config as configuration
from shazzzbot.helper import files as storage
from shazzzbot.helper.platforms import unraid
from tests.test_helper_support import archive_bytes, update_fixture

NEXT_VERSION = f"{constants.version_number(constants.VERSION)[0] + 1}.0.0"
LATER_VERSION = f"{constants.version_number(constants.VERSION)[0] + 2}.0.0"

FETCH = updates.latest_release


@pytest.mark.parametrize("value", [None, 5, "http://example.invalid/releases", ""])
def test_startup_release_url_must_use_https(setup, value):
    _, _, _, path = setup
    storage.write_private(path, json.dumps({"format": 1, "release_url": value}).encode())
    with pytest.raises(ValueError, match="release_url"):
        configuration.load_config(path, unraid.Unraid())


@pytest.mark.parametrize(("body", "url", "expected"), [
    (b'{"tag_name":"v0.2.0"}', "https://example.invalid/latest", "0.2.0"),
    (b'{"tag_name":"0.1.0"}', "https://example.invalid/latest", "0.1.0"),
    (b'{"tag_name":"v0.2.0"}', "http://example.invalid/latest", None),
    (b"x" * 65537, "https://example.invalid/latest", None),
    (b"not json", "https://example.invalid/latest", None),
    (b"[]", "https://example.invalid/latest", None),
    (b"{}", "https://example.invalid/latest", None),
    (b'{"tag_name":5}', "https://example.invalid/latest", None),
    (b'{"tag_name":"1.2.3;bad"}', "https://example.invalid/latest", None)])
def test_startup_release_fetch_is_bounded_verified_https(monkeypatch, body, url, expected):
    response = MagicMock()
    response.__enter__.return_value = response
    response.geturl.return_value = url
    response.read.return_value = body
    opener = MagicMock()
    opener.open.return_value = response
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: opener)
    if expected:
        assert FETCH("https://example.invalid/latest") == expected
    else:
        with pytest.raises(ValueError):
            FETCH("https://example.invalid/latest")
    request = opener.open.call_args.args[0]
    assert request.full_url == "https://example.invalid/latest"
    assert opener.open.call_args.kwargs == {"timeout": 5}
    if url.startswith("https:"):
        response.read.assert_called_once_with(65537)


@pytest.mark.parametrize(("latest", "pin", "bound", "notice"), [
    (NEXT_VERSION, "b" * 64, NEXT_VERSION, True), (NEXT_VERSION, "b" * 64, LATER_VERSION, True),
    (NEXT_VERSION, "b" * 64, "", True), (NEXT_VERSION, "", NEXT_VERSION, True),
    (NEXT_VERSION, "current", NEXT_VERSION, True),
    (constants.VERSION, "", "", False), ("0.0.1", "", "", False)])
def test_helper_checks_release_at_startup_without_installing(helper, monkeypatch, capsys, latest, pin, bound, notice):
    pin = helper.sha256 if pin == "current" else pin
    helper.cfg["update_sha256"] = pin
    helper.cfg["update_version"] = bound
    monkeypatch.setattr(updates, "release", AsyncMock(return_value=latest))
    original = helper.path.read_bytes()
    asyncio.run(helper.check_release())
    output = capsys.readouterr()
    assert ("update available" in output.out) == notice
    assert ("update available" in output.err) == notice
    if notice:
        if pin and bound == latest and pin != helper.sha256:
            assert f"bash {footprint.HOOK} --update {pin}" in output.err
        else:
            assert "pin update_version and update_sha256" in output.err and "--update" not in output.err
        assert helper.audit.latest == (latest, pin)
        assert "full helper bundle update required" in output.err and "--upgrade --helper" in output.err
        assert "runtime only" in output.err
    assert helper.path.read_bytes() == original


@pytest.mark.parametrize("runtime,bundle,latest,bundle_old,runtime_old", [
    ("0.2.0", "0.1.0", "0.2.0", True, False),
    ("0.1.0", "0.2.0", "0.2.0", False, True),
    ("0.2.0", "0.2.0", "0.2.0", False, False),
    ("0.3.0", "0.1.0", "0.2.0", True, False),
    ("0.3.0", "0.3.0", "0.2.0", False, False),
    ("0.1.0", "0.1.0", "0.2.0", True, True),
])
def test_startup_and_notice_track_runtime_and_bundle_independently_until_both_current(
        helper, monkeypatch, capsys, runtime, bundle, latest, bundle_old, runtime_old):
    monkeypatch.setattr(audit_log, "VERSION", runtime)
    manifest_path = helper.path.parent / footprint.BUNDLE_FILES[1]
    manifest = json.loads(manifest_path.read_bytes())
    manifest["version"] = "v" + bundle
    storage.write_private(manifest_path, json.dumps(manifest).encode())
    helper.cfg.update(update_version=latest, update_sha256="b" * 64)
    monkeypatch.setattr(updates, "release", AsyncMock(return_value=latest))
    now = [1.0]
    monkeypatch.setattr(audit_log.time, "monotonic", lambda: now[0])
    asyncio.run(helper.check_release())
    output = capsys.readouterr()
    outdated = bundle_old or runtime_old
    assert ("update available" in output.err) is outdated
    assert ("full helper bundle update required" in output.err) is bundle_old
    assert ("runtime only; run: bash " in output.err) is runtime_old
    assert ("--upgrade --helper" in output.err) is outdated
    assert helper.audit.latest == ((latest, "b" * 64) if outdated else None)
    helper.audit.notice(latest, "b" * 64, helper.sha256, helper.path)
    repeated = capsys.readouterr()
    assert repeated.out == repeated.err == ""
    if outdated:
        now[0] += 60 * helper.cfg["flush_minutes"]
        helper.audit.notice(latest, "b" * 64, helper.sha256, helper.path)
        assert "update available" in capsys.readouterr().err
    monkeypatch.setattr(audit_log, "VERSION", latest)
    manifest["version"] = "v" + latest
    storage.write_private(manifest_path, json.dumps(manifest).encode())
    helper.audit.notice(latest, "b" * 64, helper.sha256, helper.path)
    assert helper.audit.latest is None
    cleared = capsys.readouterr()
    assert cleared.out == cleared.err == ""


@pytest.mark.parametrize("error", [OSError("offline"), ValueError("invalid release"), TimeoutError("timeout")])
def test_failed_startup_check_is_logged_without_stopping(helper, monkeypatch, capsys, error):
    monkeypatch.setattr(updates, "release", AsyncMock(side_effect=error))
    asyncio.run(helper.check_release())
    assert "startup update check unavailable" in capsys.readouterr().out
    assert not helper.stopping and helper.audit.latest is None


def test_manual_update_verifies_compiles_replaces_and_signals(helper, monkeypatch, capsys):
    data, digest, _response = update_fixture(helper, monkeypatch)
    updates.update(helper.cfg, helper.path, digest.upper())
    assert helper.path.read_bytes() == data and stat.S_IMODE(helper.path.stat().st_mode) == 0o700
    assert artifact.source_version(Path(str(helper.path) + ".prev").read_bytes(), helper.path) == "0.1.0"
    assert updates.wait_update.call_args.args[0] is helper.cfg
    assert updates.wait_update.call_args.args[1].name == "update.result"
    assert updates.wait_update.call_args.args[2:] == (digest, helper.cfg["action_timeout"] + 35)
    assert signal.pidfd_send_signal.call_args.args[1] == signal.SIGUSR1
    assert "manual update 0.1.0 -> 0.2.0" in capsys.readouterr().out
    assert (Path(helper.cfg["socket"]).parent / "update.json").read_text() == "manual update 0.1.0 -> 0.2.0"


@pytest.mark.parametrize("pin", ["", None, "0.3.0", "0.1.0", "v0.2.0", "0.2.0-beta"])
def test_manual_update_requires_the_exact_nonempty_version_pin_before_publication(helper, monkeypatch, pin):
    _data, digest, _response = update_fixture(helper, monkeypatch)
    helper.cfg["update_version"] = pin
    original = helper.path.read_bytes()
    previous = helper.path.with_name("helper.pyz.prev")
    event = Path(helper.cfg["socket"]).parent / "update.json"
    with pytest.raises(ValueError, match="VERSION"):
        updates.update(helper.cfg, helper.path, digest)
    assert helper.path.read_bytes() == original
    assert not previous.exists() and not event.exists()
    assert all(call.args[1] != signal.SIGUSR1 for call in signal.pidfd_send_signal.call_args_list)
    updates.wait_update.assert_not_called()
    if not pin:
        urllib.request.build_opener.assert_not_called()


def test_update_failures_preserve_old_file(helper, monkeypatch):
    data, digest, response = update_fixture(helper, monkeypatch)
    original = helper.path.read_bytes()
    for wanted in ("bad", "0" * 64):
        with pytest.raises(ValueError):
            updates.update(helper.cfg, helper.path, wanted)
        assert helper.path.read_bytes() == original
    helper.cfg["update_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        updates.update(helper.cfg, helper.path, "0" * 64)
    helper.cfg["update_sha256"] = digest
    response.geturl = lambda: "http://example.invalid/helper"
    with pytest.raises(ValueError, match="https"):
        updates.update(helper.cfg, helper.path, digest)
    response.geturl = lambda: helper.cfg["update_url"]
    response.read = lambda n: b"x" * (constants.MAX_UPDATE + 1)
    with pytest.raises(ValueError, match="size"):
        updates.update(helper.cfg, helper.path, digest)
    response.read = lambda n: archive_bytes(source="not valid python !!")
    wanted = hashlib.sha256(response.read(1)).hexdigest()
    helper.cfg["update_sha256"] = wanted
    with pytest.raises(SyntaxError):
        updates.update(helper.cfg, helper.path, wanted)
    helper.cfg["update_sha256"] = digest
    response.read = lambda n: data
    signal.pidfd_send_signal.side_effect = [None, ProcessLookupError()]
    with pytest.raises(ProcessLookupError):
        updates.update(helper.cfg, helper.path, digest)
    assert helper.path.read_bytes() == original and not (Path(helper.cfg["socket"]).parent / "update.json").exists()


def test_update_identity_and_version_validation(helper, monkeypatch):
    _data, digest, _response = update_fixture(helper, monkeypatch)
    monkeypatch.setattr(storage, "process_start", lambda pid: "changed")
    with pytest.raises(ValueError, match="identity"):
        updates.update(helper.cfg, helper.path, digest)
    with pytest.raises(ValueError, match="VERSION"):
        artifact.source_version(archive_bytes(version=""), helper.path)
    with pytest.raises(ValueError, match="VERSION"):
        artifact.source_version(archive_bytes(version="1"), helper.path)
    monkeypatch.setattr(storage, "process_start", Mock(side_effect=["100", "100", "changed"]))
    with pytest.raises(ValueError, match="changed during"):
        updates.update(helper.cfg, helper.path, digest)
    identity = Path(helper.cfg["socket"]).parent / "helper.pid"
    details = json.loads(identity.read_text())
    details["action_timeout"] = 0
    storage.write_private(identity, auth.canonical(details))
    with pytest.raises(ValueError, match="timeout"):
        updates.update(helper.cfg, helper.path, digest)


def test_https_redirect_rejects_downgrades():
    redirect, req = updates.HTTPSRedirect(), urllib.request.Request("https://example.invalid/old")
    with pytest.raises(ValueError, match="https"):
        redirect.redirect_request(req, None, 302, "found", {}, "http://example.invalid/new")
    new = redirect.redirect_request(req, None, 302, "found", {}, "https://example.invalid/new")
    assert new.full_url == "https://example.invalid/new"


def test_process_identity_and_atomic_write_cleanup(tmp_path, monkeypatch):
    assert storage.process_start(os.getpid()).isdigit()
    path = tmp_path / "script"
    path.write_bytes(b"old")
    monkeypatch.setattr(os, "replace", Mock(side_effect=OSError("cannot replace")))
    with pytest.raises(OSError):
        storage.write_private(path, b"new")
    assert path.read_bytes() == b"old" and not list(tmp_path.glob(".helper-*"))


def test_atomic_update_rejects_a_reused_pid_before_downloading(helper, monkeypatch):
    _, digest, _ = update_fixture(helper, monkeypatch)
    monkeypatch.setattr(storage, "process_start", Mock(side_effect=["100", "different"]))
    with pytest.raises(ValueError, match="before download"):
        updates.update(helper.cfg, helper.path, digest)
    urllib.request.build_opener.assert_not_called()


@pytest.mark.parametrize("version", ["0.1.0", "0.0.9", "0.2.0-beta"])
def test_update_rejects_equal_older_or_unorderable_versions(helper, monkeypatch, version):
    _, _, response = update_fixture(helper, monkeypatch)
    original, data = helper.path.read_bytes(), archive_bytes(version=version)
    response.read = lambda n: data
    digest = hashlib.sha256(data).hexdigest()
    helper.cfg["update_sha256"] = digest
    helper.cfg["update_version"] = version
    with pytest.raises(ValueError, match="(?i)version"):
        updates.update(helper.cfg, helper.path, digest)
    assert helper.path.read_bytes() == original
    assert updates.version_number("0.10.0") > updates.version_number("0.9.9")


def test_update_requires_root_config_pin_and_restores_failed_start(helper, monkeypatch):
    _data, digest, _ = update_fixture(helper, monkeypatch)
    original = helper.path.read_bytes()
    helper.cfg["update_sha256"] = ""
    with pytest.raises(ValueError, match="pinned"):
        updates.update(helper.cfg, helper.path, digest)
    urllib.request.build_opener.assert_not_called()
    helper.cfg["update_sha256"] = digest
    storage.write_private(Path(helper.cfg["socket"]).parent / "update.result", b"stale")
    updates.wait_update.side_effect = ValueError("failed startup")
    with pytest.raises(ValueError, match="failed startup"):
        updates.update(helper.cfg, helper.path, digest)
    assert (helper.path.read_bytes() == original) and (Path(str(helper.path) + ".prev").read_bytes() == original)
    assert not (Path(helper.cfg["socket"]).parent / "update.result").exists()


@pytest.mark.parametrize("result", [None, {"ok": True, "sha256": "b" * 64}, {"ok": False, "sha256": "b" * 64}])
def test_startup_acknowledgement_or_timeout(helper, tmp_path, monkeypatch, result):
    path = tmp_path / "update.result"
    if result is not None:
        storage.write_private(path, auth.canonical(result))
    monkeypatch.setattr(time, "sleep", Mock())
    monkeypatch.setattr(time, "monotonic", Mock(side_effect=[0, 1, 36]))
    if result and result["ok"]:
        updates.wait_update(helper.cfg, path, "b" * 64)
    else:
        with pytest.raises(ValueError, match="startup"):
            updates.wait_update(helper.cfg, path, "b" * 64)


def test_atomic_replacement_fsyncs_file_then_parent(tmp_path, monkeypatch):
    modes = []
    monkeypatch.setattr(os, "fsync", lambda fd: modes.append(stat.S_ISDIR(os.fstat(fd).st_mode)))
    storage.write_private(tmp_path / "data", b"safe")
    assert modes == [False, True]


@pytest.mark.parametrize("outcome", ["ready", "error", "exit", "hash", "runtime", "timeout", "wrong-ready"])
def test_isolated_reexec_acknowledges_startup_or_rolls_back(tmp_path, monkeypatch, outcome):
    path, result = tmp_path / "helper.pyz", tmp_path / "socket/update.result"
    result.parent.mkdir(mode=0o750)
    config = tmp_path / "config.json"
    storage.write_private(config, b'{"format":1}')
    old = archive_bytes(version="0.1.0")
    storage.write_private(Path(str(path) + ".prev"), old, 0o700)
    storage.write_private(tmp_path / "policy.json",
                          json.dumps({"format": 1, "sha256": hashlib.sha256(old).hexdigest()}).encode())
    source = ('import builtins, hashlib, pathlib, sys\n'
              'builtins._shazzzbot_ready(hashlib.sha256(pathlib.Path(sys.argv[0]).read_bytes()).hexdigest())\n')
    if outcome == "error":
        source = 'raise RuntimeError("startup failed")\n'
    if outcome == "exit":
        source = "pass\n"
    if outcome == "runtime":
        source += 'raise RuntimeError("after startup")\n'
    if outcome == "wrong-ready":
        source = 'import builtins\nbuiltins._shazzzbot_ready("bad")\n'
    data = archive_bytes(main=source)
    digest = hashlib.sha256(data).hexdigest()
    storage.write_private(path, data, 0o700)
    monkeypatch.setattr(builtins, "_shazzzbot_ready", None, raising=False)
    monkeypatch.setattr(sys, "argv", ["-c", str(path), str(config), digest, str(result)])
    execute = Mock()
    monkeypatch.setattr(os, "execve", execute)
    monkeypatch.setattr(signal, "signal", Mock())
    alarm = Mock(side_effect=[TimeoutError(), None] if outcome == "timeout" else None)
    monkeypatch.setattr(signal, "alarm", alarm)
    bootstrap = tmp_path / "bootstrap.py"
    bootstrap.write_text(updates.REEXEC)
    spec = importlib.util.spec_from_file_location("helper_guard", bootstrap)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    if outcome == "hash":
        sys.argv[3] = "0" * 64
    if outcome == "runtime":
        with pytest.raises(RuntimeError, match="after startup"):
            spec.loader.exec_module(module)
    else:
        spec.loader.exec_module(module)
    with pytest.raises(TimeoutError):
        module.timeout(0, None)
    ok = outcome in {"ready", "runtime"}
    assert (json.loads(result.read_text())["ok"] is ok) and (execute.call_count == (0 if ok else 1))
    if not ok:
        assert (path.read_bytes() == old) and (execute.call_args.args[1][1:3] == ["-I", "-B"])


def test_version_inspection_never_evaluates_expressions_or_calls(tmp_path):
    marker = tmp_path / "executed"
    for expression in ('"0.2." + "0"', '__import__("pathlib").Path(' + repr(str(marker)) + ').touch()'):
        with pytest.raises(ValueError, match="VERSION"):
            artifact.source_version(archive_bytes(version=expression), tmp_path / "helper.pyz")
    assert not marker.exists()
    source = '__import__("pathlib").Path(' + repr(str(marker)) + ').touch()\n'
    assert artifact.source_version(archive_bytes(source=source), tmp_path / "helper.pyz") == "0.2.0"
    assert not marker.exists()


@pytest.mark.parametrize("pid", [0, 1, -1, True, "../../etc/passwd"])
def test_process_identity_only_accepts_a_positive_numeric_pid(pid):
    with pytest.raises(ValueError, match="process id"):
        storage.process_start(pid)


def test_update_cleanup_is_confined_to_fixed_names_in_a_trusted_runtime_directory(helper, tmp_path):
    root = footprint.ensure_workdir()
    nested = Path(helper.cfg["socket"]).parent
    for name in ("update.result", "update.json", "keep"):
        (nested / name).write_text("keep")
    storage.clear_update(nested, result=True, root=root)
    storage.clear_update(nested, root=root)
    assert [p.name for p in nested.iterdir()] == ["keep"]
    with pytest.raises(FileNotFoundError):
        storage.clear_update(nested, root=root)
    with pytest.raises(ValueError, match="unsafe update directory"):
        storage.clear_update(root.parent, root=root)
    link = root / "link"
    link.symlink_to(nested, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe update directory"):
        storage.clear_update(link, root=root)
    nested.chmod(0o777)
    with pytest.raises(ValueError, match="untrusted"):
        storage.clear_update(nested, root=root)


@pytest.mark.parametrize("unsafe", ["mode", "owner", "fifo"])
def test_update_refuses_unsafe_locks_before_download(helper, monkeypatch, unsafe):
    _, digest, _ = update_fixture(helper, monkeypatch)
    path = Path(helper.cfg["socket"]).parent / "update.lock"
    if unsafe == "fifo":
        os.mkfifo(path, 0o600)
    else:
        storage.write_private(path, b"keep")
        if unsafe == "mode":
            path.chmod(0o644)
        else:
            os.chown(path, 1000, 1000)
    with pytest.raises(ValueError, match="unsafe update lock"):
        updates.update(helper.cfg, helper.path, digest)
    urllib.request.build_opener.assert_not_called()


def test_concurrent_manual_updates_are_refused(helper, monkeypatch):
    _, digest, _ = update_fixture(helper, monkeypatch)
    path = Path(helper.cfg["socket"]).parent / "update.lock"
    storage.write_private(path, b"")
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            updates.update(helper.cfg, helper.path, digest)
    finally:
        os.close(fd)
    urllib.request.build_opener.assert_not_called()


def test_loaded_archive_descriptor_is_private_and_size_bounded(helper, monkeypatch):
    fd = os.open(helper.path, os.O_RDONLY)
    helper.cfg["_loaded_archive_fd"] = fd
    try:
        assert updates._loaded_bytes(helper.cfg, helper.path) == helper.path.read_bytes()
        helper.path.chmod(0o644)
        with pytest.raises(ValueError, match="unsafe loaded"):
            updates._loaded_bytes(helper.cfg, helper.path)
        helper.path.chmod(0o700)
        monkeypatch.setattr(updates, "MAX_UPDATE", 8)
        with pytest.raises(ValueError, match="size limit"):
            updates._loaded_bytes(helper.cfg, helper.path)
    finally:
        os.close(fd)


def test_modified_canonical_archive_cannot_overwrite_a_trusted_previous_copy(helper, monkeypatch):
    _, digest, _ = update_fixture(helper, monkeypatch)
    previous = Path(str(helper.path) + ".prev")
    original = helper.path.read_bytes()
    storage.write_private(previous, original, 0o700)
    storage.write_private(helper.path, archive_bytes(version="0.1.1"), 0o700)
    with pytest.raises(ValueError, match="pinned SHA-256"):
        updates.update(helper.cfg, helper.path, digest)
    assert previous.read_bytes() == original
    urllib.request.build_opener.assert_not_called()


def test_update_uses_the_verified_loaded_previous_archive_when_current_is_broken(helper, monkeypatch):
    data, digest, _ = update_fixture(helper, monkeypatch)
    previous = Path(str(helper.path) + ".prev")
    original = helper.path.read_bytes()
    storage.write_private(previous, original, 0o700)
    policy_path = Path(helper.cfg["installation_policy"])
    policy = json.loads(policy_path.read_bytes())
    policy["previous_sha256"] = policy["sha256"]
    policy["sha256"] = "d" * 64
    storage.write_private(policy_path, json.dumps(policy).encode())
    fd = os.open(previous, os.O_RDONLY)
    helper.cfg["_loaded_archive_fd"] = fd
    storage.write_private(helper.path, b"broken current artifact", 0o700)
    try:
        updates.update(helper.cfg, helper.path, digest)
    finally:
        os.close(fd)
    assert helper.path.read_bytes() == data and previous.read_bytes() == original
    pins = json.loads(policy_path.read_bytes())
    assert pins["sha256"] == digest and pins["previous_sha256"] == hashlib.sha256(original).hexdigest()


@pytest.mark.parametrize("already_exited", [False, True])
def test_update_home_loss_stops_only_its_verified_daemon_and_recreates_nothing(helper, monkeypatch, already_exited):
    import shutil

    _, digest, _ = update_fixture(helper, monkeypatch)
    home = helper.path.parent
    def lose(*_args):
        shutil.rmtree(home)
        raise ValueError("home unavailable")
    def signal_owned(_fd, sig):
        if sig == signal.SIGTERM and already_exited:
            raise ProcessLookupError
    monkeypatch.setattr(updates, "wait_update", lose)
    signal.pidfd_send_signal.side_effect = signal_owned
    with pytest.raises(ValueError, match="home unavailable"):
        updates.update(helper.cfg, helper.path, digest)
    assert not home.exists()
    assert signal.pidfd_send_signal.call_args.args[1] == signal.SIGTERM


@pytest.mark.parametrize("plain_installed", [False, True])
def test_plain_python_archive_is_rejected_before_publication_or_restart(helper, monkeypatch, plain_installed):
    _data, digest, response = update_fixture(helper, monkeypatch)
    plain = b'VERSION = "0.2.0"\nraise RuntimeError("never execute")\n'
    policy = Path(helper.cfg["installation_policy"])
    if plain_installed:
        plain = b'VERSION = "0.1.0"\n'
        storage.write_private(helper.path, plain, 0o700)
        value = json.loads(policy.read_bytes())
        value["sha256"] = hashlib.sha256(plain).hexdigest()
        storage.write_private(policy, json.dumps(value).encode())
    else:
        response.read = lambda n: plain
        digest = hashlib.sha256(plain).hexdigest()
        helper.cfg["update_sha256"] = digest
    previous = Path(str(helper.path) + ".prev")
    storage.write_private(previous, helper.path.read_bytes(), 0o700)
    before = {path: path.read_bytes() for path in (helper.path, previous, policy)}
    with pytest.raises(ValueError, match="zipapp"):
        updates.update(helper.cfg, helper.path, digest)
    assert {path: path.read_bytes() for path in before} == before
    assert all(call.args[1] == 0 for call in signal.pidfd_send_signal.call_args_list)
    updates.wait_update.assert_not_called()
