from pathlib import Path

import pytest

from tests import test_setup_helper_install as installation
from tests import test_setup_sh as harness

env = harness.env
install = installation.install
calls = harness.calls
run = harness.run


def private_paths(values):
    root = Path(values["STUB_ROOT"])
    work = root / "work"
    hook = root / "hook"
    values["SHAZZZBOT_HELPER_HOOK_DIR"] = str(hook)
    script = root / "bundle/setup.sh"
    script.write_text(script.read_text().replace("WORK_ROOT=/tmp/shazzzbot-helper", f"WORK_ROOT={work}"))
    return [Path(values["SHAZZZBOT_APP_DIR"]), Path(values["SHAZZZBOT_HELPER_DIR"]), work, hook,
            Path(values["SHAZZZBOT_BACKUP_DIR"]), Path(values["SHAZZZBOT_LOCK_FILE"])]


def tree_bytes(path):
    if not path.exists():
        return None
    return {str(item.relative_to(path)): item.read_bytes() for item in path.rglob("*") if item.is_file()}


def test_unmanaged_compose_refuses_before_any_target_write(env):
    targets = private_paths(env)
    app = targets[0]
    app.mkdir(mode=0o700)
    (app / "compose.yaml").write_text("services: {}\n")
    (app / "user-data").write_bytes(b"keep exactly\n")
    before = [tree_bytes(path) for path in targets]
    result = run(env, "--role", "bot", "--method", "compose-image", "--no-helper", "--yes")
    assert result.returncode != 0
    assert "no install identity; left unchanged" in result.stderr
    assert [tree_bytes(path) for path in targets] == before
    assert not any(line.startswith(("pull ", "run ", "compose up ")) for line in calls(env).splitlines())


@pytest.mark.parametrize("flags", [
    ["--helper-only", "--role", "relay"],
    ["--helper-only", "--method", "compose-image"],
    ["--helper-only", "--action", "upgrade"],
    ["--helper-only", "--no-helper"],
    ["--helper-bundle", "/tmp/bundle", "--role", "relay"],
])
def test_helper_flag_conflicts_refuse_without_target_writes(env, flags):
    targets = private_paths(env)
    result = run(env, *flags)
    assert result.returncode == 2
    assert result.stderr.strip()
    assert all(not path.exists() for path in targets)
    assert calls(env) == ""


def test_helper_only_dry_run_needs_no_docker_and_writes_nothing(install):
    source, home, hook, app, docker_calls, _ = install
    selected = installation.bundle(install, "dry-bundle")
    before = installation.contents(app)
    result = installation.run(install, selected, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "dry" in result.stdout.lower()
    assert installation.contents(app) == before
    assert all(not path.exists() for path in (home, hook, source.parent / "work"))
    assert not docker_calls.exists()
