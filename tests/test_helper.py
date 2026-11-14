import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shazzzbot.helper import config as configuration
from shazzzbot.helper import constants, security
from shazzzbot.helper import files as storage
from shazzzbot.helper import process as execution
from shazzzbot.helper.platforms import unraid
from tests.test_helper_support import FLASH_CHECK


def test_private_files_and_config_defaults(setup, monkeypatch, tmp_path):
    cfg, secret, _, config = setup
    assert cfg["flush_minutes"] == 60 and cfg["enabled"] == [] and secret == b"a" * 32
    assert cfg["log_file"] == str(config.parent / "helper.log")
    assert cfg["update_version"] == ""
    key = config.parent / "api.key"
    key.chmod(0o700)
    assert configuration.load_config(config, unraid.Unraid())[1] == secret
    key.chmod(0o644)
    with pytest.raises(ValueError, match="private file"):
        storage.private_read(key)
    key.chmod(0o600)
    with pytest.raises(ValueError, match="too large"):
        storage.private_read(key, 1)
    link = tmp_path / "link"
    link.symlink_to(key)
    with pytest.raises(OSError):
        storage.private_read(link)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(ValueError, match="private file"):
        storage.private_read(fifo)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(ValueError, match="root"):
        configuration.load_config(config, unraid.Unraid())


def test_private_file_owner_is_checked(setup, monkeypatch):
    _, _, _, config = setup
    real = os.fstat
    def not_root(fd):
        info = real(fd)
        return SimpleNamespace(st_uid=1, st_mode=info.st_mode, st_size=info.st_size)
    monkeypatch.setattr(os, "fstat", not_root)
    with pytest.raises(ValueError, match="root-owned"):
        storage.private_read(config)


@pytest.mark.parametrize("raw", [[], {"unknown": 1}, {"socket_uid": True}, {"output_bytes": 0},
                                 {"output_bytes": 1048577}, {"log_bytes": 1}, {"enabled": "vm"},
                                 {"peer_uids": [-1]}, {"peer_uids": [True]}, {"enabled": ["array"]},
                                 {"userscript_allow": [""]}, {"userscript_allow": ["*"]}, {"userscript_allow": ["?*"]},
                                 {"key_file": "relative"},
                                 {"socket": "/tmp/helper.sock"}, {"socket": "/run/helper.sock"},
                                 {"update_url": "http://example.invalid/code"}, {"update_url": 1},
                                 {"scripts": []}, {"scripts": {"bad": ["/sbin/reboot"]}},
                                 {"update_sha256": "bad"}, {"update_sha256": 1}, {"_comment": 1},
    {"script_binaries": "bad"}, {"script_binaries": [1]}, {"script_binaries": ["relative"]}])
def test_invalid_config_fails_closed(setup, raw):
    _, _, _, config = setup
    config.write_text(json.dumps({"format": 1, **raw} if isinstance(raw, dict) else raw))
    error = TypeError if raw in ({"enabled": "vm"}, {"scripts": []}, {"_comment": 1},
                                 {"script_binaries": "bad"}) else ValueError
    with pytest.raises(error):
        configuration.load_config(config, unraid.Unraid())


@pytest.mark.parametrize("version", [None, False, 1, "v1.2.3", "01.2.3", "1.2", "1.2.3-beta", "bad", "1.2.3\n"])
def test_update_version_rejects_noncanonical_root_config_values(setup, version):
    _, _, _, path = setup
    path.write_text(json.dumps({"format": 1, "update_version": version}))
    with pytest.raises(TypeError if not isinstance(version, str) else ValueError, match="update_version"):
        configuration.load_config(path, unraid.Unraid())


@pytest.mark.parametrize("version", ["", "0.0.0", "1.2.3"])
def test_update_version_accepts_an_empty_or_canonical_target(setup, version):
    _, _, _, path = setup
    path.write_text(json.dumps({"format": 1, "update_version": version}))
    assert configuration.load_config(path, unraid.Unraid())[0]["update_version"] == version


def test_config_scripts_and_key_validation(setup):
    _, _, _, config = setup
    config.write_text(json.dumps({"format": 1, "scripts": {"true": ["/usr/bin/true"]}}))
    with pytest.raises(ValueError, match="not approved"):
        configuration.load_config(config, unraid.Unraid())
    config.write_text(json.dumps({"format": 1, "scripts": {"true": ["/usr/bin/true"]},
                                  "script_binaries": ["/usr/bin/true"]}))
    assert configuration.load_config(config, unraid.Unraid())[0]["scripts"] == {"true": ["/usr/bin/true"]}
    (config.parent / "api.key").write_text("bad!")
    with pytest.raises(ValueError, match="openssl"):
        configuration.load_config(config, unraid.Unraid())


@pytest.mark.parametrize("argv", [[], "x", [1], ["/bin/echo", "\0"], ["relative"], ["/sbin/reboot"],
                                  ["/usr/local/sbin/mdcmd", "start"], ["/usr/local/sbin/mdcmd", "stop"],
                                  ["/usr/local/sbin/emcmd", "cmdStart"], ["/usr/local/sbin/emcmd", "cmdStop"],
                                  ["/bin/sh", "-c", "anything"], ["/usr/bin/env", "reboot"],
    ["/usr/local/sbin/emcmd", "csrf_token=x&cmdStop=Stop"], ["/usr/local/sbin/emcmd", "cmdStart=Start"],
                                  ["/boot/helper", "--update=" + "a" * 64], ["/boot/helper", "--update"],
    ["/usr/bin/cp", "a", "/proc/sysrq-trigger"], ["/usr/bin/curl", "--unix-socket", "emhttpd.socket"]]
                         + [["/usr/bin/" + b, "1"] for b in sorted(security.BANNED) + ["python3", "perl5"]])
def test_forbidden_script_argv_cannot_be_enabled(argv):
    with pytest.raises(ValueError):
        security.checked_argv(argv, argv[:1] if isinstance(argv, list) else ())


@pytest.mark.parametrize("value", [None, "", "-flag", ".", "..", "a/b", "a\n", "x" * 129, "a\x7f"])
def test_bad_target_names(value):
    with pytest.raises(ValueError):
        security.target_name(value)


def test_default_helper_home_is_appdata_not_the_startup_hook_folder():
    from shazzzbot.helper import footprint

    assert footprint.DEFAULT_HOME == Path("/mnt/user/appdata/shazzzbot-helper")


def test_banned_executable_symlink_cannot_be_a_config_script(tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to("/sbin/reboot")
    with pytest.raises(ValueError):
        security.checked_argv([str(alias)])


@pytest.mark.parametrize("filename", ["compose.yaml", ".env"])
@pytest.mark.parametrize("kind", ["symlink", "owner", "directory", "writable"])
def test_compose_inputs_must_be_root_owned_regular_files(helper, monkeypatch, tmp_path, filename, kind):
    project = tmp_path / "project"
    project.mkdir()
    (project / "compose.yaml").touch()
    path = project / filename
    path.unlink(missing_ok=True)
    if kind == "symlink":
        path.symlink_to(helper.path)
    elif kind == "directory":
        path.mkdir()
    else:
        path.touch()
        if kind == "owner":
            os.chown(path, 1, 1)
        else:
            path.chmod(0o666)
    monkeypatch.setattr(unraid, "PROJECTS", tmp_path)
    with pytest.raises(ValueError, match="untrusted"):
        asyncio.run(helper.resolve("stack", "up", "project"))


@pytest.mark.parametrize("text,writable", [
    ("[global]", False), ("[global]\n[other]\nread only = no", False),
    ("[global]\n[flash]", False), ("[global]\n[FLASH]\nread only = no", True),
    ("[global]\n[flash]\nwrite list = root", True), ("[global]\n[flash]\nadmin users = root", True),
    ("[global]\nread only = no\n[flash]", True),
    ("[global]\n[flash]\navailable = no\nread only = no", False),
    ("[global]\n[alias]\npath = /boot\nread only = no", True),
    ("[global]\n[alias]\npath = /boot/config\nread only = no", True),
    ("[global]\n[alias]\npath = /\nread only = no", True),
    ("[global]\n[alias]\npath = /bootleg\nread only = no", False),
    ("[global]\n[alias]\npath = /boot\nread only = yes", False),
    ("[global]\n\tidmap config * : backend = tdb\n\tidmap config * : range = 3000-7999\n[flash]", False)])
def test_flash_export_write_access_is_rejected(text, writable):
    assert unraid.flash_writable(text) is writable


@pytest.mark.parametrize("text", ["not ini", "[flash]", "[global]\n[x]\npath = %H",
                                  "[global]\n[x]\npath = relative"])
def test_unreadable_smb_configuration_fails_closed(text):
    with pytest.raises(ValueError, match="SMB"):
        unraid.flash_writable(text)


@pytest.mark.parametrize("rc,text,refused", [(0, "[global]", False), (1, "[global]", True),
                                           (0, "[global]\n[flash]\nread only = no", True)])
def test_flash_check_uses_live_effective_samba_configuration(monkeypatch, rc, text, refused):
    execute = AsyncMock(return_value=(rc, text.splitlines(), False))
    monkeypatch.setattr(execution, "execute", execute)
    if refused:
        with pytest.raises(ValueError, match="flash SMB"):
            asyncio.run(FLASH_CHECK())
    else:
        asyncio.run(FLASH_CHECK())
    execute.assert_awaited_once_with(["/usr/bin/testparm", "-s"], 5, constants.MAX_UPDATE, errors=False)


def test_required_trusted_file_cannot_be_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        storage.trusted_file(tmp_path / "missing")
    for path in (Path("relative"), tmp_path / ".." / "file"):
        with pytest.raises(ValueError, match="untrusted"):
            storage.trusted_path(path)


@pytest.mark.parametrize("parent_kind", ["writable", "owner", "symlink", "file-owner", "file-symlink"])
@pytest.mark.parametrize("family", ["script", "userscript"])
def test_config_and_user_scripts_require_trusted_ancestors(helper, monkeypatch, tmp_path, parent_kind, family):
    parent = tmp_path / "unsafe"
    parent.mkdir()
    filename = "approved-tool" if family == "script" else "script"
    script = parent / "backup" / filename
    script.parent.mkdir()
    script.write_text("exit 0\n")
    if parent_kind == "writable":
        parent.chmod(0o777)
    elif parent_kind == "owner":
        os.chown(parent, 1, 1)
    elif parent_kind == "file-owner":
        os.chown(script, 1, 1)
    elif parent_kind == "file-symlink":
        target = tmp_path / "approved-target"
        target.write_text("exit 0\n")
        script.unlink()
        script.symlink_to(target)
    else:
        alias = tmp_path / "alias"
        alias.symlink_to(parent, target_is_directory=True)
        parent, script = alias, alias / "backup" / filename
    monkeypatch.setattr(unraid, "USER_SCRIPTS", parent)
    helper.cfg["userscript_allow"] = ["backup"]
    with pytest.raises(ValueError, match="untrusted"):
        if family == "script":
            security.checked_argv([str(script)], [str(script)])
        else:
            asyncio.run(helper.resolve("userscript", "run", "backup"))


@pytest.mark.parametrize("body", [b'include: other.yaml', b'extends: {file: other.yaml}',
                                  b'env_file: ../secret', b'{"env_file": "secret"}', b'"inc\\lude": file',
                                  b"? !!binary ZW52X2ZpbGU=\n: secret", b"\0", b"\xff",
                                  b"build: /mnt/user/appdata/app", b"dockerfile: /mnt/user/x/Dockerfile",
                                  b"configs: {c: {file: /mnt/user/c}}", b"secrets: {s: {file: /mnt/user/s}}",
                                  b"label_file: /mnt/user/labels",
                                  pytest.param(b"x" * (constants.MAX_UPDATE + 1), id="large")])
def test_compose_cannot_load_external_untrusted_files(helper, body):
    helper.path.write_bytes(body)
    with pytest.raises(ValueError):
        unraid.checked_compose(helper.path)


@pytest.mark.parametrize("global_options,shares,refused", [
    ("wide links = yes", "[data]\nread only = no\nforce user = root", True),
    ("", "[data]\nwide links = yes\nread only = no\nforce user = root", True),
    ("wide links = yes\nforce user = root\nread only = no", "[data]", True),
    ("", "[links]\nwide links = yes\n[data]\nread only = no\nforce user = root", True),
    ("wide links = yes", "[data]\nwide links = no\nread only = no\nforce user = root", True),
    ("wide links = yes", "[data]\nwrite list = alice\nforce user = root", True),
    ("wide links = yes", "[data]\nadmin users = alice\nforce user = root", True),
    ("wide links = YeS", '[data]\nread only = no\nforce user = "root"', True),
    ("wide links = yes\navailable = no", "[data]\navailable = yes\nread only = no\nforce user = root", True),
    ("wide links = no", "[data]\nread only = no\nforce user = root", False),
    ("", "[data]\nread only = no\nforce user = root", False),
    ("wide links = yes", "[data]\nread only = yes\nforce user = root", False),
    ("wide links = yes", "[data]\nread only = no\nforce user = nobody", False),
    ("wide links = yes", "[data]\nread only = no", False),
    ("wide links = yes", "[data]\navailable = no\nread only = no\nforce user = root", False),
    ("", "[links]\navailable = no\nwide links = yes\n[data]\nread only = no\nforce user = root", True),
    ("wide links = yes\nforce user = root", "[data]\nread only = no\nforce user = nobody", False),
    ("wide links = yes\nforce user = root\nread only = no", "", False),
    ("wide links = yes\navailable = no\nforce user = root\nread only = no", "[data]", False)])
def test_wide_links_and_root_forced_writes_fail_closed(monkeypatch, global_options, shares, refused):
    text = f"[global]\n{global_options}\n{shares}"
    assert unraid.flash_writable(text) is refused
    execute = AsyncMock(return_value=(0, text.splitlines(), False))
    monkeypatch.setattr(execution, "execute", execute)
    if refused:
        with pytest.raises(ValueError, match="flash SMB"):
            asyncio.run(FLASH_CHECK())
    else:
        asyncio.run(FLASH_CHECK())
    execute.assert_awaited_once_with(["/usr/bin/testparm", "-s"], 5, constants.MAX_UPDATE, errors=False)
