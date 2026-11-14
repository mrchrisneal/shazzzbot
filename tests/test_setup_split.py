import os
import re
import shlex
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from tests import test_setup_helper_install as installation
from tests import test_setup_sh as harness

env = harness.env
install = installation.install

MODULES = ("setup_session.sh", "setup_detect.sh", "setup_configure.sh", "setup_deploy.sh")


def invoke(script, env, cwd, *args):
    result = subprocess.run(["bash", str(script), *args], env=env, cwd=cwd, input="", capture_output=True,
                            text=True, timeout=90, check=False)
    assert harness.TOKEN not in result.stdout + result.stderr
    return result


def private_targets(env):
    return [Path(env[name]) for name in ("SHAZZZBOT_APP_DIR", "SHAZZZBOT_BACKUP_DIR", "SHAZZZBOT_HELPER_DIR",
                                        "SHAZZZBOT_LOCK_FILE")]


def impostor(root, marker):
    (root / "tools").mkdir(parents=True)
    text = "printf source-impostor > " + shlex.quote(str(marker)) + "\nexit 79\n"
    for name in MODULES:
        (root / "tools" / name).write_text(text)


def test_standalone_help_needs_no_installer_tools_python_or_docker(tmp_path):
    script = tmp_path / "setup.sh"
    shutil.copy(harness.SCRIPT, script)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    marker = tmp_path / "forbidden"
    for name in ("python3", "docker"):
        path = binaries / name
        path.write_text("#!/bin/sh\nprintf called > " + shlex.quote(str(marker)) + "\nexit 99\n")
        path.chmod(0o700)
    result = invoke(script, {**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"]}, tmp_path,
                    "--help")
    assert result.returncode == 0 and "Usage: setup.sh" in result.stdout
    assert not (tmp_path / "tools").exists() and not marker.exists()


@pytest.mark.parametrize("module", MODULES)
@pytest.mark.parametrize("state", ["missing", "symlink", "group-writable", "other-writable"])
@pytest.mark.parametrize("helper_only", [False, True])
def test_each_required_module_is_validated_before_any_source_or_mutation(env, module, state, helper_only):
    source = Path(env["STUB_ROOT"]) / "bundle"
    selected = source / "tools" / module
    if state == "missing":
        selected.unlink()
    elif state == "symlink":
        original = selected.with_suffix(".trusted")
        selected.rename(original)
        selected.symlink_to(original.name)
    else:
        selected.chmod(0o620 if state == "group-writable" else 0o602)
    marker = source.parent / "sourced-before-validation"
    earlier = source / "tools/setup_prereqs.sh"
    earlier.write_text(earlier.read_text() + "\nprintf sourced > " + shlex.quote(str(marker)) + "\n")
    before = [(path, path.exists()) for path in private_targets(env)]
    args = ("--helper-only", "--yes") if helper_only else ("--role", "bot", "--method", "compose-image", "--no-helper")
    result = harness.run(env, *args)
    assert result.returncode != 0 and "installer source validation failed" in result.stderr
    assert not harness.calls(env) and not marker.exists()
    assert [(path, path.exists()) for path, _ in before] == before


def test_normal_dry_run_uses_installer_tree_not_current_directory(env, tmp_path):
    cwd, marker = tmp_path / "other-directory", tmp_path / "cwd-module-ran"
    impostor(cwd, marker)
    script = Path(env["STUB_ROOT"]) / "bundle/setup.sh"
    result = invoke(script, env, cwd, "--role", "bot", "--method", "compose-image", "--no-helper", "--dry-run")
    assert result.returncode == 0 and "[dry-run]" in result.stdout, result.stderr
    assert not marker.exists() and not harness.runtime_app(env).exists() and not harness.backups(env)
    assert not Path(env["SHAZZZBOT_LOCK_FILE"]).exists()
    assert not re.search(r"^(compose (up|pull)|run|pull)", harness.calls(env), re.MULTILINE)


def test_helper_bundle_and_current_directory_cannot_supply_installer_modules(install):
    source, home, hook, _, docker_calls, env = install
    selected = installation.bundle(install, "local-helper")
    marker = source.parent / "bundle-module-ran"
    impostor(selected, marker)
    for name in MODULES:
        (selected / name).write_text("exit 79\n")
    unrelated = source.parent / "other-directory"
    impostor(unrelated, marker)
    result = invoke(source / "setup.sh", env, unrelated, "--helper-only", "--helper-bundle", str(selected),
                    "--dry-run", "--yes")
    assert result.returncode != 0 and "local helper bundle must contain exactly" in result.stderr
    assert not marker.exists() and not home.exists() and not hook.exists() and not docker_calls.exists()
    for path in (selected / "tools", *(selected / name for name in MODULES)):
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    result = invoke(source / "setup.sh", env, unrelated, "--helper-only", "--helper-bundle", str(selected),
                    "--dry-run", "--yes")
    assert result.returncode == 0 and "[dry-run]" in result.stdout, result.stderr
    assert not marker.exists() and not home.exists() and not hook.exists() and not docker_calls.exists()


def test_complete_installer_archive_includes_modules_and_extracted_dry_run_is_read_only(env, tmp_path):
    source = Path(env["STUB_ROOT"]) / "bundle"
    archive_path = tmp_path / "installer.tar"
    with tarfile.open(archive_path, "w") as archive:
        archive.add(source, arcname="installer")
    destination = tmp_path / "unpacked"
    destination.mkdir()
    with tarfile.open(archive_path) as archive:
        assert {"installer/tools/" + name for name in MODULES} <= set(archive.getnames())
        archive.extractall(destination, filter="data")
    result = invoke(destination / "installer/setup.sh", env, destination,
                    "--role", "bot", "--method", "compose-image", "--no-helper", "--dry-run")
    assert result.returncode == 0 and "[dry-run]" in result.stdout, result.stderr
    assert not harness.runtime_app(env).exists() and not harness.backups(env)
    assert not Path(env["SHAZZZBOT_LOCK_FILE"]).exists()
    assert not re.search(r"^(compose (up|pull)|run|pull)", harness.calls(env), re.MULTILINE)


@pytest.mark.parametrize("module", MODULES)
def test_new_modules_only_define_functions_without_side_effects(tmp_path, module):
    path = harness.ROOT / "tools" / module
    body = 'before=$(env); source "$1"; after=$(env); [ "$before" = "$after" ]'
    result = subprocess.run(["bash", "-c", body, "bash", str(path)], cwd=tmp_path,
                            capture_output=True, text=True, timeout=5, check=False)
    assert result.returncode == 0 and not result.stdout and not result.stderr
    assert not list(tmp_path.iterdir())


def test_installer_and_shell_modules_stay_below_responsibility_limits():
    assert len(harness.SCRIPT.read_text().splitlines()) < 500
    for path in (harness.ROOT / "tools").glob("setup_*.sh"):
        assert len(path.read_text().splitlines()) < 700, path.name
