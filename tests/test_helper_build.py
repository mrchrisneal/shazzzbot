import ast
import hashlib
import io
import json
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from shazzzbot.helper import artifact, cli, constants, footprint, platforms, updates
from shazzzbot.helper import files as storage
from shazzzbot.helper.platforms import unraid
from tests.test_helper_support import archive_bytes, update_fixture
from tools import build_helper


def test_release_version_has_one_runtime_source():
    from shazzzbot import __version__

    root = Path(__file__).resolve().parents[1]
    assert (root / "VERSION").read_text().strip() == build_helper.version() == __version__ == constants.VERSION


@pytest.mark.parametrize("value", ["", "v1.0.0", "01.0.0", "1.0.0-beta", "1.0", "1.0.0\nextra"])
def test_invalid_release_version_refuses_before_building(tmp_path, monkeypatch, value):
    (tmp_path / "VERSION").write_text(value)
    monkeypatch.setattr(build_helper, "ROOT", tmp_path)
    output = tmp_path / "helper.pyz"
    with pytest.raises(ValueError, match="canonical"):
        build_helper.build(output)
    assert not output.exists()


def test_changed_canonical_release_version_is_embedded_as_the_archive_authority(tmp_path, monkeypatch):
    major, minor, patch = constants.version_number(constants.VERSION)
    version = f"{major}.{minor}.{patch + 1}"
    raw = (version + "\n").encode()
    (tmp_path / "VERSION").write_bytes(raw)
    source = Path(__file__).resolve().parents[1] / "shazzzbot/helper"
    monkeypatch.setattr(build_helper, "ROOT", tmp_path)
    output = tmp_path / "helper.pyz"
    output.write_bytes(b"keep the prior archive")
    build_helper.build(output, source)
    with zipfile.ZipFile(output) as archive:
        assert archive.read("VERSION") == raw
    assert artifact.source_version(output.read_bytes(), output) == version


def test_build_is_reproducible_stdlib_only_and_runs_without_an_extension(tmp_path):
    path = tmp_path / "helper.pyz"
    digest = build_helper.build(path)
    other = tmp_path / "script"
    assert build_helper.build(other) == digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert other.read_bytes() == path.read_bytes()
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert artifact.source_version(path.read_bytes(), path) == constants.VERSION
    with zipfile.ZipFile(path) as archive:
        assert archive.read("VERSION") == (Path(__file__).resolve().parents[1] / "VERSION").read_bytes()
        for name in archive.namelist():
            if name == "VERSION":
                continue
            tree = ast.parse(archive.read(name), filename=name, feature_version=(3, 9))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    assert all(item.name.split(".")[0] in sys.stdlib_module_names for item in node.names)
                if isinstance(node, ast.ImportFrom) and not node.level:
                    assert node.module.split(".")[0] in sys.stdlib_module_names | {"helper"}
    result = subprocess.run([sys.executable, "-I", "-B", str(other), "--help"],
                            capture_output=True, text=True, timeout=10, check=True)
    assert "--check" in result.stdout and "--update" in result.stdout


def test_builder_cli_prints_one_artifact_hash(tmp_path, monkeypatch, capsys):
    output = tmp_path / "helper.pyz"
    monkeypatch.setattr(sys, "argv", ["build_helper.py", str(output)])
    build_helper.main()
    assert capsys.readouterr().out.strip() == f"{hashlib.sha256(output.read_bytes()).hexdigest()}  {output}"


def test_platform_detection_is_explicit_and_unknown_hosts_refuse_to_start(tmp_path, monkeypatch):
    marker = tmp_path / "unraid-version"
    monkeypatch.setattr(platforms, "UNRAID_VERSION", marker)
    with pytest.raises(SystemExit, match="unsupported host platform"):
        platforms.detect()
    marker.touch()
    host = platforms.detect()
    assert isinstance(host, unraid.Unraid)
    assert host.operations == unraid.OPS
    monkeypatch.setattr(sys, "argv", ["script", "--check"])
    marker.unlink()
    with pytest.raises(SystemExit, match="unsupported host platform"):
        cli.main()


def test_cli_default_config_resolves_the_appdata_home(setup, monkeypatch, capsys):
    monkeypatch.chdir(Path.cwd())
    _, _, program, _ = setup
    monkeypatch.setattr(footprint, "DEFAULT_HOME", footprint.MNT / "user/appdata/shazzzbot-helper")
    runtime = footprint.ensure_workdir() / "runtime/helper.pyz"
    storage.write_private(runtime, program.read_bytes(), 0o700)
    monkeypatch.setattr(cli, "artifact_path", lambda: runtime)
    monkeypatch.setattr(sys, "argv", ["script", "--check"])
    cli.main()
    assert capsys.readouterr().out == "enabled: hello \n"


def test_archive_validation_compiles_every_module_without_executing(tmp_path):
    path = tmp_path / "script"
    data = archive_bytes([("helper/other.py", 'raise RuntimeError("do not run")\n')])
    assert artifact.source_version(data, path) == "0.2.0"
    with pytest.raises(SyntaxError):
        artifact.source_version(archive_bytes([("helper/other.py", "not valid python !!")]), path)


@pytest.mark.parametrize("name", ["other.py", "helper/data.json", "helper/../bad.py", "helper//bad.py",
                                  "helper/./bad.py", "helper/a\\b.py"])
def test_archive_paths_and_non_source_members_fail_closed(tmp_path, name):
    with pytest.raises(ValueError, match="archive member"):
        artifact.source_version(archive_bytes([(name, "pass\n")]), tmp_path / "script")


def test_archive_member_and_uncompressed_size_limits_and_duplicates(tmp_path):
    path = tmp_path / "script"
    oversized = [("helper/large.py", "#" + "x" * constants.MAX_UPDATE)]
    excessive = [(f"helper/m{i}.py", "pass\n") for i in range(128)]
    for entries in (oversized, excessive):
        with pytest.raises(ValueError, match="invalid helper archive"):
            artifact.source_version(archive_bytes(entries), path)
    with pytest.warns(UserWarning, match="Duplicate name"):
        duplicate = archive_bytes([("helper/other.py", "pass\n"), ("helper/other.py", "pass\n")])
    with pytest.raises(ValueError, match="invalid helper archive"):
        artifact.source_version(duplicate, path)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("unrelated.py", "pass\n")
    with pytest.raises(ValueError, match="invalid helper archive"):
        artifact.source_version(stream.getvalue(), path)


@pytest.mark.parametrize("missing", ["VERSION", "__main__.py", "helper/constants.py"])
def test_archive_requires_the_version_file_and_both_entry_modules(tmp_path, missing):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, data in (("VERSION", b"0.2.0\n"), ("__main__.py", b"pass\n"),
                           ("helper/constants.py", b"pass\n")):
            if name != missing:
                archive.writestr(name, data)
    with pytest.raises(ValueError, match="invalid helper archive"):
        artifact.source_version(stream.getvalue(), tmp_path / "helper.pyz")


def test_archive_version_cannot_be_duplicated_or_a_symlink(tmp_path):
    with pytest.warns(UserWarning, match="Duplicate name"):
        data = archive_bytes([("VERSION", b"9.9.9\n")])
    with pytest.raises(ValueError, match="invalid helper archive"):
        artifact.source_version(data, tmp_path / "helper.pyz")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        link = zipfile.ZipInfo("VERSION")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, b"0.2.0\n")
        archive.writestr("__main__.py", b"pass\n")
        archive.writestr("helper/constants.py", b"pass\n")
    with pytest.raises(ValueError, match="archive member"):
        artifact.source_version(stream.getvalue(), tmp_path / "helper.pyz")


def test_version_member_is_data_and_constants_do_not_override_it(tmp_path):
    source = 'VERSION = "9.9.9"\nraise RuntimeError("do not execute")\n'
    assert artifact.source_version(archive_bytes(source=source, version="0.2.0"), tmp_path / "helper.pyz") == "0.2.0"


def test_archive_symlinks_corruption_and_invalid_version_are_refused(tmp_path):
    path = tmp_path / "script"
    link = zipfile.ZipInfo("helper/alias.py")
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(ValueError, match="archive member"):
        artifact.source_version(archive_bytes([(link, "other.py")]), path)
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("VERSION", "0.2.0\n")
        archive.writestr("__main__.py", "corrupt_me = 1\n")
        archive.writestr("helper/constants.py", "pass\n")
    with pytest.raises(ValueError, match="invalid helper archive"):
        artifact.source_version(data.getvalue().replace(b"corrupt_me", b"corrupt_no"), path)
    with pytest.raises(ValueError, match="VERSION"):
        artifact.source_version(archive_bytes(version="bad version"), path)


def test_manual_update_replaces_one_archive_and_keeps_previous_archive(helper, monkeypatch):
    build_helper.build(helper.path)
    original = helper.path.read_bytes()
    policy = json.loads(Path(helper.cfg["installation_policy"]).read_bytes())
    policy["sha256"] = hashlib.sha256(original).hexdigest()
    storage.write_private(Path(helper.cfg["installation_policy"]), json.dumps(policy).encode())
    _, _, response = update_fixture(helper, monkeypatch)
    major, minor, patch = constants.version_number(constants.VERSION)
    next_version = f"{major}.{minor}.{patch + 1}"
    data = archive_bytes(version=next_version)
    digest = hashlib.sha256(data).hexdigest()
    response.read = lambda n: data
    helper.cfg["update_sha256"] = digest
    helper.cfg["update_version"] = next_version
    updates.update(helper.cfg, helper.path, digest)
    assert helper.path.read_bytes() == data
    assert Path(str(helper.path) + ".prev").read_bytes() == original
    assert artifact.source_version(original, helper.path) == constants.VERSION
    assert artifact.source_version(data, helper.path) == next_version
    assert updates.wait_update.call_args.args[0] is helper.cfg
    assert updates.wait_update.call_args.args[1].name == "update.result"
    assert updates.wait_update.call_args.args[2:] == (digest, helper.cfg["action_timeout"] + 35)


@pytest.mark.parametrize("ready", [False, True])
def test_reexec_starts_a_zipapp_and_rolls_back_the_whole_artifact(tmp_path, monkeypatch, ready):
    import builtins
    import importlib.util
    import os
    import signal

    path, result = tmp_path / "helper.pyz", tmp_path / "socket/update.result"
    result.parent.mkdir(mode=0o750)
    config = tmp_path / "config.json"
    storage.write_private(config, b'{"format":1}')
    old = archive_bytes(version="0.1.0")
    storage.write_private(Path(str(path) + ".prev"), old, 0o700)
    storage.write_private(tmp_path / "policy.json",
                          json.dumps({"format": 1, "sha256": hashlib.sha256(old).hexdigest()}).encode())
    source = ('import builtins, hashlib, pathlib, sys\n'
              'builtins._shazzzbot_ready(hashlib.sha256(pathlib.Path(sys.argv[0]).read_bytes()).hexdigest())\n') \
        if ready else 'raise RuntimeError("startup failed")\n'
    storage.write_private(path, archive_bytes(main=source), 0o700)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(sys, "argv", ["-c", str(path), str(config), digest, str(result)])
    monkeypatch.setattr(builtins, "_shazzzbot_ready", None, raising=False)
    execute = Mock()
    monkeypatch.setattr(os, "execve", execute)
    monkeypatch.setattr(signal, "signal", Mock())
    monkeypatch.setattr(signal, "alarm", Mock())
    guard = tmp_path / "guard.py"
    guard.write_text(updates.REEXEC)
    spec = importlib.util.spec_from_file_location("helper_guard", guard)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(importlib.util.module_from_spec(spec))
    assert json.loads(result.read_bytes()) == {"ok": ready, "sha256": digest}
    assert execute.call_count == (0 if ready else 1)
    if not ready:
        assert path.read_bytes() == old
        assert execute.call_args.args[1][1:3] == ["-I", "-B"]
