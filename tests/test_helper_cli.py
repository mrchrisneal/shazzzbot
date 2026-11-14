import errno
import hashlib
import json
import os
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from shazzzbot.helper import cli, constants, footprint
from shazzzbot.helper import files as storage
from tests.test_helper_support import archive_bytes


@pytest.fixture
def descriptor(setup, monkeypatch):
    cfg, _, program, _ = setup
    monkeypatch.chdir(Path.cwd())
    runtime = footprint.ensure_workdir() / "runtime/helper.pyz"
    storage.write_private(runtime, program.read_bytes(), 0o700)
    archive = os.open(runtime, os.O_RDONLY | os.O_NOFOLLOW)
    os.set_inheritable(archive, True)
    monkeypatch.setenv("SHAZZZBOT_HELPER_ARCHIVE_FD", str(archive))
    try:
        yield cfg, Path(f"/proc/self/fd/{archive}"), program, None, archive
    finally:
        try:
            os.close(archive)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise


def test_loaded_archive_is_fd_bound_but_has_one_canonical_identity(descriptor):
    cfg, loaded, program, _home, archive = descriptor
    assert cli.bind_artifact(cfg, loaded) == program
    assert cfg["_loaded_sha256"] == hashlib.sha256(program.read_bytes()).hexdigest()
    assert cfg["_loaded_archive_fd"] == archive and not os.get_inheritable(archive)
    assert "SHAZZZBOT_HELPER_ARCHIVE_FD" not in os.environ


def test_running_archive_keeps_only_a_ram_descriptor_and_no_pool_cwd(descriptor, monkeypatch):
    cfg, loaded, program, _, _ = descriptor
    cli.bind_artifact(cfg, loaded)
    monkeypatch.chdir(footprint.WORK)
    assert Path.cwd() == footprint.WORK
    targets = []
    for entry in Path("/proc/self/fd").iterdir():
        try:
            targets.append(os.readlink(entry))
        except FileNotFoundError:
            continue
    assert not any(target.startswith(str(program.parent)) for target in targets)
    assert str(footprint.WORK / "runtime/helper.pyz") in targets


def test_reexec_archive_fd_without_hook_environment_is_accepted(descriptor, monkeypatch):
    cfg, loaded, program, _, _ = descriptor
    monkeypatch.delenv("SHAZZZBOT_HELPER_ARCHIVE_FD")
    assert cli.bind_artifact(cfg, loaded) == program


def test_verified_previous_archive_can_supply_the_running_version(descriptor, monkeypatch):
    cfg, _, program, _, _ = descriptor
    previous = Path(str(program) + ".prev")
    data = archive_bytes(version="0.0.9")
    storage.write_private(previous, data, 0o700)
    policy = program.parent / "policy.json"
    value = json.loads(policy.read_text())
    value["previous_sha256"] = hashlib.sha256(data).hexdigest()
    storage.write_private(policy, json.dumps(value).encode())
    runtime = footprint.WORK / "runtime/helper.pyz"
    storage.write_private(runtime, data, 0o700)
    archive = os.open(runtime, os.O_RDONLY | os.O_NOFOLLOW)
    monkeypatch.setenv("SHAZZZBOT_HELPER_ARCHIVE_FD", str(archive))
    try:
        assert cli.bind_artifact(cfg, Path(f"/proc/self/fd/{archive}")) == program
        assert cfg["_loaded_sha256"] == hashlib.sha256(data).hexdigest()
    finally:
        os.close(archive)


@pytest.mark.parametrize("change", ["archive-text", "archive-other", "stdin"])
def test_invalid_inherited_descriptors_are_refused(descriptor, monkeypatch, change):
    cfg, loaded, _program, _home, archive = descriptor
    if change == "archive-text":
        monkeypatch.setenv("SHAZZZBOT_HELPER_ARCHIVE_FD", "bad")
    elif change == "archive-other":
        monkeypatch.setenv("SHAZZZBOT_HELPER_ARCHIVE_FD", str(archive + 1))
    else:
        monkeypatch.delenv("SHAZZZBOT_HELPER_ARCHIVE_FD")
        loaded = Path("/proc/self/fd/0")
    with pytest.raises(ValueError):
        cli.bind_artifact(cfg, loaded)


def test_unrelated_open_file_cannot_impersonate_the_archive(descriptor, monkeypatch, tmp_path):
    cfg, _, _program, _, _ = descriptor
    file = tmp_path / "unrelated"
    file.write_bytes(b"x")
    fd = os.open(file, os.O_RDONLY)
    monkeypatch.setenv("SHAZZZBOT_HELPER_ARCHIVE_FD", str(fd))
    try:
        with pytest.raises(ValueError, match="RAM runtime"):
            cli.bind_artifact(cfg, Path(f"/proc/self/fd/{fd}"))
    finally:
        os.close(fd)


@pytest.mark.parametrize("change", ["public", "size", "pin"])
def test_loaded_code_must_be_private_bounded_and_pinned(descriptor, change):
    cfg, loaded, _program, _, _ = descriptor
    program = footprint.WORK / "runtime/helper.pyz"
    if change == "public":
        program.chmod(0o644)
    elif change == "size":
        with program.open("wb") as stream:
            stream.write(b"x" * (constants.MAX_UPDATE + 1))
    else:
        with program.open("wb") as stream:
            stream.write(b"not the pinned bytes")
    with pytest.raises(ValueError):
        cli.bind_artifact(cfg, loaded)


def test_checks_accept_only_a_private_work_candidate(setup):
    cfg, _, program, _ = setup
    work = footprint.ensure_workdir()
    stage = work / "candidate.pyz"
    storage.write_private(stage, program.read_bytes(), 0o700)
    assert cli.bind_artifact(cfg, stage, checking=True) == program
    os.close(cfg["_loaded_archive_fd"])
    with pytest.raises(ValueError, match="startup hook"):
        cli.bind_artifact(cfg, program)
    with pytest.raises(ValueError, match="startup hook"):
        cli.bind_artifact(cfg, Path("/boot/helper.pyz"), checking=True)


def test_archive_path_preserves_the_loaded_descriptor(monkeypatch):
    monkeypatch.setattr(cli, "__loader__", SimpleNamespace(archive="/proc/self/fd/42"))
    assert cli.artifact_path() == Path("/proc/self/fd/42")
    monkeypatch.setattr(cli, "__loader__", None)
    assert cli.artifact_path() == Path(cli.__file__)


@pytest.mark.parametrize("mode", ["check", "update", "serve", "reexec"])
def test_cli_uses_pinned_code_and_the_canonical_config(descriptor, monkeypatch, capsys, mode):
    cfg, loaded, program, _, _ = descriptor
    monkeypatch.setattr(cli, "artifact_path", lambda: loaded)
    monkeypatch.setattr(cli.config, "load_config", lambda *_args: (cfg, b"a" * 32))
    monkeypatch.setattr(cli.platforms, "detect", lambda: object())
    helper = object()
    monkeypatch.setattr(cli.server, "Helper", Mock(return_value=helper))
    monkeypatch.setattr(cli.server, "serve", AsyncMock(return_value=mode == "reexec"))
    monkeypatch.setattr(cli.updates, "update", Mock())
    monkeypatch.setattr(cli.os, "execve", Mock())
    args = ["helper.pyz", "--config", cfg["_config"]]
    if mode == "check":
        args.append("--check")
    elif mode == "update":
        args.extend(["--update", "b" * 64])
    monkeypatch.setattr(sys, "argv", args)
    cli.main()
    if mode == "check":
        assert "enabled: hello" in capsys.readouterr().out
    elif mode == "update":
        cli.updates.update.assert_called_once_with(cfg, program, "b" * 64)
    else:
        cli.server.serve.assert_awaited_once_with(helper)
        if mode == "reexec":
            _, argv, environment = cli.os.execve.call_args.args
            assert cfg["_config"] in argv and str(program) in argv and environment == cli.updates.environment(cfg)
        else:
            cli.os.execve.assert_not_called()


def test_cli_default_config_resolves_the_real_pool_home(descriptor, monkeypatch, capsys):
    cfg, loaded, _program, _, _ = descriptor
    monkeypatch.setattr(footprint, "DEFAULT_HOME", footprint.MNT / "user/appdata/shazzzbot-helper")
    monkeypatch.setattr(cli, "artifact_path", lambda: loaded)
    monkeypatch.setattr(sys, "argv", ["helper.pyz", "--check"])
    cli.main()
    assert "enabled: hello" in capsys.readouterr().out
    assert cfg["_config"].startswith(str(footprint.MNT / "cache"))


def test_cli_errors_are_private_and_exit_nonzero(setup, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["helper.pyz", "--config", str(setup[3]), "--check"])
    monkeypatch.setattr(cli, "artifact_path", lambda: Path("/boot/helper.pyz"))
    with pytest.raises(SystemExit, match="1"):
        cli.main()
    assert "helper failed: ValueError" in capsys.readouterr().err


def test_newer_config_reports_a_safe_format_error_without_rewriting(setup, monkeypatch, capsys):
    config = setup[3]
    source = json.dumps({"format": 2, "_comment": "private-config-must-not-be-printed"}).encode()
    storage.write_private(config, source)
    monkeypatch.setattr(sys, "argv", ["helper.pyz", "--config", str(config), "--check"])
    with pytest.raises(SystemExit, match="1"):
        cli.main()
    error = capsys.readouterr().err
    assert "written by a newer Shazzzbot" in error
    assert "private-config-must-not-be-printed" not in error
    assert config.read_bytes() == source


def test_package_entrypoint_calls_the_cli_once_and_propagates_its_exit(monkeypatch):
    main = Mock(side_effect=SystemExit(23))
    monkeypatch.setattr(cli, "main", main)
    with pytest.raises(SystemExit) as caught:
        runpy.run_module("shazzzbot.helper", run_name="__main__")
    assert caught.value.code == 23
    main.assert_called_once_with()
