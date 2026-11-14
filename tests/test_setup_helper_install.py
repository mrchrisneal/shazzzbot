import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from tests.test_helper_support import directory_mount_id

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def install(tmp_path):
    source = tmp_path / "installer"
    source.mkdir(mode=0o700)
    shutil.copy(ROOT / "VERSION", source / "VERSION")
    shutil.copy(ROOT / "setup.sh", source / "setup.sh")
    shutil.copy(ROOT / "helper.example.json", source / "helper.example.json")
    shutil.copytree(ROOT / "tools", source / "tools", ignore=shutil.ignore_patterns("__pycache__"))
    (source / "shazzzbot").mkdir(mode=0o700)
    shutil.copy(ROOT / "shazzzbot/__init__.py", source / "shazzzbot/__init__.py")
    shutil.copy(ROOT / "shazzzbot/schema.py", source / "shazzzbot/schema.py")
    shutil.copytree(ROOT / "shazzzbot/helper", source / "shazzzbot/helper",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(ROOT / "shazzzbot-helper-unraid.py", source / "shazzzbot-helper-unraid.py")
    mounts, work, hook = (tmp_path / name for name in ("mnt", "work", "user.scripts/scripts/shazzzbot-helper"))
    pool = mounts / "pool"
    (pool / "appdata").mkdir(parents=True, mode=0o700)
    hook.parent.mkdir(parents=True, mode=0o700)
    home = pool / "appdata/helper"
    info = pool.stat()
    mountinfo = tmp_path / "mountinfo"
    mount_id = directory_mount_id(pool)
    device = f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}"
    mountinfo.write_text(f"{mount_id} 0 {device} / {pool} rw - xfs /dev/fake rw\n")
    marker, machine = tmp_path / "unraid-version", tmp_path / "machine-id"
    marker.write_text('version="7.3.4"\n')
    machine.write_text("a" * 32)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(mode=0o700)
    docker = bin_dir / "docker"
    calls = tmp_path / "docker-calls"
    docker.write_text(f'#!/bin/sh\nprintf called >> "{calls}"\nexit 99\n')
    docker.chmod(0o700)
    testparm = bin_dir / "testparm"
    testparm.write_text("#!/bin/sh\nprintf '[global]\\n'\n")
    testparm.chmod(0o700)
    replacements = {'Path("/mnt")': f'Path({str(mounts)!r})',
                    'Path("/proc/self/mountinfo")': f'Path({str(mountinfo)!r})',
                    'Path("/tmp/shazzzbot-helper")': f'Path({str(work)!r})',
                    'Path("/boot/config/plugins/user.scripts/scripts/shazzzbot-helper/script")':
                    f'Path({str(hook / "script")!r})'}
    footprint = source / "shazzzbot/helper/footprint.py"
    text = footprint.read_text()
    for old, new in replacements.items():
        text = text.replace(old, new, 1)
    footprint.write_text(text)
    for path in (source / "setup.sh", source / "tools/setup_install_helper.sh", source / "tools/setup_prereqs.sh"):
        text = path.read_text().replace("/tmp/shazzzbot-helper", str(work))
        text = text.replace("/usr/bin/python3", sys.executable)
        path.write_text(text.replace("/usr/bin/testparm", str(testparm)))
    platform = source / "shazzzbot/helper/platforms/unraid.py"
    platform.write_text(platform.read_text().replace('"/usr/bin/testparm"', repr(str(testparm))))
    for path in source.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)
    app = tmp_path / "unmanaged-app"
    app.mkdir(mode=0o700)
    (app / "keep").write_bytes(b"user data")
    env = {**os.environ, "PATH": str(bin_dir) + ":" + os.environ["PATH"],
           "SHAZZZBOT_HELPER_DIR": str(home), "SHAZZZBOT_HELPER_HOOK_DIR": str(hook),
           "SHAZZZBOT_UNRAID_MARKER": str(marker), "SHAZZZBOT_MACHINE_ID": str(machine),
           "SHAZZZBOT_APP_DIR": str(app), "SHAZZZBOT_WIZARD_PYTHON": sys.executable,
           "SHAZZZBOT_MIN_FREE_MB": "1"}
    return source, home, hook, app, calls, env


def bundle(install, name, fail=False):
    source, *_ = install
    destination = source.parent / name
    destination.mkdir(mode=0o700)
    with zipfile.ZipFile(destination / "helper.pyz", "w") as archive:
        archive.writestr("__main__.py", f"import sys\nsys.exit({int(fail)})\n")
        archive.writestr("helper/constants.py", f'VERSION = "1.2.{int(fail)}"\n')
    shutil.copy(source / "shazzzbot-helper-unraid.py", destination / "shazzzbot-helper-unraid.py")
    files = {}
    for path in destination.iterdir():
        path.chmod(0o600)
        data = path.read_bytes()
        files[path.name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    (destination / "helper-unraid.json").write_text(json.dumps({
        "format": 1, "platform": "unraid", "version": "v1.2.3", "revision": "a" * 40, "files": files}))
    (destination / "helper-unraid.json").chmod(0o600)
    return destination


def run(install, selected, *args):
    source, _, _, _, _, env = install
    return subprocess.run(["bash", str(source / "setup.sh"), "--helper-only", "--helper-bundle", str(selected),
                           "--yes", *args], env=env, text=True, capture_output=True, timeout=90, check=False)


def contents(path):
    return {str(file.relative_to(path)): file.read_bytes() for file in path.rglob("*") if file.is_file()}


def test_fresh_helper_only_and_upgrade_preserve_unmanaged_bot_and_secrets(install):
    _, home, hook, app, calls, _ = install
    selected = bundle(install, "bundle")
    first = run(install, selected)
    assert first.returncode == 0, first.stdout + first.stderr
    assert "NOT running" in first.stdout
    assert contents(app) == {"keep": b"user data"}
    assert not calls.exists()
    assert {"config.json", "api.key", "policy.json", ".identity.json", "helper.pyz",
            "shazzzbot-helper-unraid.py", "helper-unraid.json"} <= contents(home).keys()
    assert {"script", "name", "description"} == contents(hook).keys()
    key, config = (home / "api.key").read_bytes(), (home / "config.json").read_bytes()
    upgraded = bundle(install, "upgrade")
    with zipfile.ZipFile(upgraded / "helper.pyz", "a") as archive:
        archive.writestr("helper/extra.py", "VALUE = 1\n")
    manifest = upgraded / "helper-unraid.json"
    value = json.loads(manifest.read_bytes())
    data = (upgraded / "helper.pyz").read_bytes()
    value["files"]["helper.pyz"] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    manifest.write_text(json.dumps(value))
    second = run(install, upgraded)
    assert second.returncode == 0, second.stdout + second.stderr
    assert (home / "helper.pyz").read_bytes() == data
    assert (home / "api.key").read_bytes() == key and (home / "config.json").read_bytes() == config
    assert contents(app) == {"keep": b"user data"} and not calls.exists()
    assert list((home / "transactions").glob("*/manifest.json"))
    assert not (home / "transactions/pending").exists()


def test_failed_helper_check_restores_previous_archive_config_key_and_hook(install):
    _, home, hook, app, calls, _ = install
    first = run(install, bundle(install, "good"))
    assert first.returncode == 0, first.stdout + first.stderr
    before = {name: (home / name).read_bytes() for name in ("helper.pyz", "config.json", "api.key", "policy.json")}
    old_hook = contents(hook)
    failed = run(install, bundle(install, "bad", fail=True))
    assert failed.returncode != 0
    assert "helper files restored" in failed.stdout + failed.stderr
    assert {name: (home / name).read_bytes() for name in before} == before
    assert contents(hook) == old_hook
    assert contents(app) == {"keep": b"user data"} and not calls.exists()
    assert not (home / "transactions/pending").exists()


def test_current_active_helper_rerun_does_not_rewrite_installed_files(install):
    source, home, hook, _, calls, env = install
    selected = bundle(install, "current")
    first = run(install, selected)
    assert first.returncode == 0, first.stdout + first.stderr
    identity = home / ".identity.json"
    value = json.loads(identity.read_bytes())
    value["active"] = True
    identity.write_text(json.dumps(value))
    schedule = hook.parent.parent / "schedule.json"
    schedule.write_text(json.dumps({"shazzzbot-helper": {"frequency": "boot", "script": str(hook / "script")}}))
    schedule.chmod(0o600)
    for name in ("helper.pyz", "shazzzbot-helper-unraid.py", "helper-unraid.json"):
        assert (home / name).read_bytes() == (selected / name).read_bytes(), name
    assert value["hook"] == str(hook / "script")
    code = ("import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
            "from tools.setup_helper_only import current; print(int(current(Path(sys.argv[2]),Path(sys.argv[3]))))")
    checked = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(source), str(home), str(selected)],
                             env=env, text=True, capture_output=True, timeout=15, check=False)
    assert checked.returncode == 0 and checked.stdout.strip() == "1", checked.stdout + checked.stderr
    script = source / "tools/setup_install_helper.sh"
    text = script.read_text()
    start = text.index("helper_running() {\n")
    end = text.index("\n}\n", start) + 3
    script.write_text(text[:start] + "helper_running() { return 0; }\n" + text[end:])
    before = contents(home)
    timestamps = {str(path.relative_to(home)): path.stat().st_mtime_ns for path in home.rglob("*") if path.is_file()}
    second = run(install, selected)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "helper is current; nothing to install" in second.stdout
    assert contents(home) == before
    after = {str(path.relative_to(home)): path.stat().st_mtime_ns for path in home.rglob("*") if path.is_file()}
    assert after == timestamps
    assert not calls.exists()


def test_writable_flash_dry_run_refuses_without_creating_helper_or_work(install):
    from shazzzbot.helper.platforms.unraid import flash_writable

    source, home, hook, app, calls, _ = install
    text = "[global]\n[flash]\npath = /boot\nread only = no\n"
    assert flash_writable(text)
    stub = source.parent / "bin/testparm"
    stub.write_text("#!/bin/sh\nprintf '[global]\\n[flash]\\npath = /boot\\nread only = no\\n'\n")
    failed = run(install, bundle(install, "flash"), "--dry-run")
    assert failed.returncode != 0
    assert "flash is writable" in failed.stdout + failed.stderr
    assert not home.exists() and not hook.exists() and not (source.parent / "work").exists()
    assert contents(app) == {"keep": b"user data"} and not calls.exists()


def test_helper_only_wires_managed_bot_env_without_rewriting_its_identity(install):
    source, home, _, app, calls, _ = install
    original = b'# private comment\r\nHELPER_GID=12000\r\nUNRELATED_SECRET="keep this exact value"\r\n'
    env_file = app / ".env"
    env_file.write_bytes(original)
    env_file.chmod(0o600)
    identity = json.dumps({"format": 2, "host": "a" * 32, "app": str(app), "install_id": "b" * 32,
                           "role": "bot", "method": "compose-image",
                           "files": {".env": hashlib.sha256(original).hexdigest()}}).encode()
    identity_file = app / ".setup-identity"
    identity_file.write_bytes(identity)
    identity_file.chmod(0o600)
    result = run(install, bundle(install, "managed"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads((home / "config.json").read_bytes())["socket_gid"] == 12000
    key = (home / "api.key").read_bytes().strip()
    candidate = env_file.read_bytes()
    assert candidate.startswith(original)
    assert b'SHAZZZBOT_HELPER_KEY="' + key + b'"\r\n' in candidate
    socket = str(source.parent / "work/socket").encode()
    assert b'HELPER_SOCKET_DIR="' + socket + b'"\r\n' in candidate
    assert identity_file.read_bytes() == identity
    assert json.loads((home / ".identity.json").read_bytes())["install_id"] != "b" * 32
    assert not calls.exists()
