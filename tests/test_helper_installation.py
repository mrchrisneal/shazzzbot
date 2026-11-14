import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from shazzzbot.helper import config as configuration
from shazzzbot.helper import constants, footprint, formats, installation
from shazzzbot.helper import files as storage
from shazzzbot.helper.platforms import unraid
from tests.test_helper_support import FLASH_CHECK, archive_bytes


def test_policy_stays_persistent_while_socket_uses_ram(setup):
    cfg, _, path, config = setup
    assert installation.socket_root(cfg) == footprint.WORK
    assert installation.policy(cfg, path) == json.loads((config.parent / "policy.json").read_text())
    socket, lock = storage.runtime(cfg)
    try:
        assert socket == footprint.WORK / "socket/helper.sock"
        assert socket.parent.stat().st_mode & 0o777 == 0o750
    finally:
        import os
        os.close(lock)
    storage.write_private(socket.parent / "update.json", b"update")
    storage.clear_update(socket.parent, root=installation.socket_root(cfg))
    assert not (socket.parent / "update.json").exists()
    assert socket.parent.exists()


@pytest.mark.parametrize("changes,error", [({"accept_flash_share": 1}, TypeError),
    ({"installation_policy": 1}, TypeError), ({"installation_policy": ""}, ValueError),
    ({"key_file": "/outside/api.key"}, ValueError), ({"log_file": "/outside/helper.log"}, ValueError)])
def test_installation_settings_fail_closed(setup, changes, error):
    config = setup[3]
    storage.write_private(config, json.dumps({"format": 1, **changes}).encode())
    with pytest.raises(error):
        configuration.load_config(config, unraid.Unraid())


def test_explicit_flash_opt_in_remains_a_valid_checked_setting(setup):
    config = setup[3]
    storage.write_private(config, b'{"format":1,"accept_flash_share":true}')
    assert configuration.load_config(config, unraid.Unraid())[0]["accept_flash_share"] is True


@pytest.mark.parametrize("change", ["object", "keys", "value", "hash", "previous-hash", "artifact", "config", "extra"])
def test_invalid_launch_policy_is_refused(setup, change):
    cfg, _, path, config = setup
    pin = config.parent / "policy.json"
    value = json.loads(pin.read_text())
    if change == "object":
        value = []
    elif change == "keys":
        value.pop("artifact")
    elif change == "value":
        value["artifact"] = 1
    elif change == "hash":
        value["sha256"] = "bad"
    elif change == "previous-hash":
        value["previous_sha256"] = "bad"
    elif change == "artifact":
        value["artifact"] = "/outside/helper.pyz"
    elif change == "config":
        value["config"] = "/outside/config.json"
    else:
        value["extra"] = "bad"
    storage.write_private(pin, json.dumps(value).encode())
    with pytest.raises(ValueError):
        installation.policy(cfg, path)


def test_policy_rejects_an_execution_path_outside_the_home(setup):
    cfg, _, _, _ = setup
    with pytest.raises(ValueError, match="H/helper.pyz"):
        installation.policy(cfg, Path("/outside/helper.pyz"))


@pytest.mark.parametrize("name", ["config.json", "policy.json", ".identity.json"])
def test_newer_helper_metadata_is_refused_without_rewriting(setup, name):
    cfg, _, program, config = setup
    path = config.parent / name
    value = json.loads(path.read_bytes())
    value["format"] = 2
    source = json.dumps(value).encode()
    storage.write_private(path, source)
    with pytest.raises(formats.FormatError, match="written by a newer Shazzzbot"):
        if name == "config.json":
            configuration.load_config(path, unraid.Unraid())
        elif name == "policy.json":
            installation.policy(cfg, program)
        else:
            with footprint.open_home(config.parent) as fd:
                footprint.identity(fd)
    assert path.read_bytes() == source


def publish(setup):
    cfg, _, path, config = setup
    old = path.read_bytes()
    data = archive_bytes()
    storage.write_private(Path(str(path) + ".prev"), old, 0o700)
    storage.write_private(path, data, 0o700)
    return cfg, path, config.parent / "policy.json", old, data


def test_commit_only_updates_the_pin_after_the_new_artifact_started(setup):
    cfg, path, pin, old, data = publish(setup)
    main_inode, previous_inode = path.stat().st_ino, Path(str(path) + ".prev").stat().st_ino
    installation.commit(cfg, path, data)
    value = json.loads(pin.read_text())
    assert value["sha256"] == hashlib.sha256(data).hexdigest()
    assert value["previous_sha256"] == hashlib.sha256(old).hexdigest()
    assert path.read_bytes() == data and Path(str(path) + ".prev").read_bytes() == old
    assert path.stat().st_ino == main_inode and Path(str(path) + ".prev").stat().st_ino == previous_inode


def test_commit_preserves_a_verified_previous_fallback_when_running_it(setup):
    cfg, path, pin, old, data = publish(setup)
    value = json.loads(pin.read_text())
    value.update(sha256="c" * 64, previous_sha256=hashlib.sha256(old).hexdigest())
    storage.write_private(pin, json.dumps(value).encode())
    installation.commit(cfg, path, data)
    assert json.loads(pin.read_text())["previous_sha256"] == hashlib.sha256(old).hexdigest()


@pytest.mark.parametrize("failure", ["current", "previous", "size", "pin-write"])
def test_commit_failure_leaves_both_artifacts_untouched(setup, monkeypatch, failure):
    cfg, path, pin, _old, data = publish(setup)
    if failure == "current":
        storage.write_private(path, b"drift", 0o700)
    elif failure == "previous":
        storage.write_private(Path(str(path) + ".prev"), b"drift", 0o700)
    elif failure == "size":
        data = b"x" * (constants.MAX_UPDATE + 1)
    else:
        def failed(*_args, **_kwargs):
            raise OSError("write failed")
        monkeypatch.setattr(storage, "write_private", failed)
    originals = [candidate.read_bytes() for candidate in (path, Path(str(path) + ".prev"), pin)]
    with pytest.raises((ValueError, OSError)):
        installation.commit(cfg, path, data)
    assert [candidate.read_bytes() for candidate in (path, Path(str(path) + ".prev"), pin)] == originals


def test_a_crash_after_pin_publication_keeps_both_archives_verified(setup, monkeypatch):
    cfg, path, pin, old, data = publish(setup)
    write = storage.write_private

    class PowerLoss(BaseException):
        pass

    def crash(*args, **kwargs):
        write(*args, **kwargs)
        raise PowerLoss

    monkeypatch.setattr(storage, "write_private", crash)
    with pytest.raises(PowerLoss):
        installation.commit(cfg, path, data)
    value = json.loads(pin.read_text())
    assert hashlib.sha256(data).hexdigest() == value["sha256"]
    assert hashlib.sha256(old).hexdigest() == value["previous_sha256"]


@pytest.mark.parametrize("accept,rc,truncated,writable,refused", [
    (True, 0, False, True, False), (False, 0, False, True, True),
    (True, 1, False, False, True), (True, 0, True, False, True),
    (True, 0, False, False, False), (False, 0, False, False, False),
])
def test_flash_opt_in_never_bypasses_unreadable_samba(monkeypatch, accept, rc, truncated, writable, refused):
    text = "[global]\n[flash]\nread only = " + ("no" if writable else "yes")
    monkeypatch.setattr(unraid.process, "execute", AsyncMock(return_value=(rc, text.splitlines(), truncated)))
    if refused:
        with pytest.raises(ValueError):
            asyncio.run(FLASH_CHECK(accept))
    else:
        asyncio.run(FLASH_CHECK(accept))


def test_guard_requires_active_hook_for_runtime_but_accepts_prepared_check(setup):
    cfg, _, path, config = setup
    footprint.record_identity(config.parent, cfg["_install_id"], active=False)
    assert installation.policy(cfg, path, active=False)["artifact"] == str(path)
    with pytest.raises(ValueError, match="not active"):
        installation.policy(cfg, path)
