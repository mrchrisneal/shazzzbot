import json
import subprocess
from pathlib import Path

import pytest

from tests.test_setup_sh import bot, calls, env, runtime_app

__all__ = ["env"]


def test_declining_start_prepares_an_identity_that_can_resume(env):
    answers = "y\n\nn\n\n\n\n\nn\n"
    result = bot(env, "compose-image", stdin=answers)
    assert result.returncode == 0 and "not started" in result.stdout, result.stderr
    app = runtime_app(env)
    value = json.loads((app / ".setup-identity").read_text())
    assert len(value["install_id"]) == 32
    assert not (app / ".setup-state").exists()
    retry = bot(env, "compose-image", "--unattended", stdin="")
    assert retry.returncode == 0 and "already runs" not in retry.stdout, retry.stderr
    assert json.loads((app / ".setup-identity").read_text())["install_id"] == value["install_id"]


def test_dry_run_keeps_an_existing_pending_marker_and_never_creates_the_lock(env):
    backup = Path(env["SHAZZZBOT_BACKUP_DIR"])
    backup.mkdir()
    pending = backup / "pending"
    pending.write_text("manual recovery required")
    before = pending.stat()
    result = bot(env, "compose-image", "--dry-run", "--unattended", stdin="")
    assert result.returncode != 0 and "needs recovery" in result.stderr
    assert pending.read_text() == "manual recovery required" and pending.stat().st_mtime_ns == before.st_mtime_ns
    assert not Path(env["SHAZZZBOT_LOCK_FILE"]).exists()


@pytest.mark.parametrize("failure", ["assert", "remove"])
def test_failed_fresh_resource_cleanup_never_removes_files_needed_by_live_containers(tmp_path, failure):
    marker = tmp_path / "restored"
    script = '''
source tools/setup_prereqs.sh
HELPER_CHANGED=0 BACKUP_PATH=verified ST_REF= SERVICE_CHANGED=1 METHOD=compose-image ROLE=bot
APP_DIR="$1" HELPER_DIR="$1/helper"
root="$1"
touch "$APP_DIR/compose.yaml"
service() { echo shazzzbot; }
compose_file() { echo compose.yaml; }
failure="$2"
assert_resources() { [ "$failure" != assert ]; }
compose() { return 1; }
fs() { touch "$root/restored"; }
warn() { printf '%s\\n' "$1" >&2; }
rollback
'''
    result = subprocess.run(["bash", "-c", script, "rollback", str(tmp_path), failure],
                            check=False, capture_output=True, text=True)
    assert result.returncode == 1 and "before file restoration" in result.stderr
    assert not marker.exists() and (tmp_path / "compose.yaml").exists()


def test_selected_docker_socket_is_the_compose_proxy_bind_source():
    assert '${SHAZZZBOT_DOCKER_SOCK:-/var/run/docker.sock}:/var/run/docker.sock' in Path("compose.yaml").read_text()
    source = Path("tools/setup_prereqs.sh").read_text()
    assert 'docker info --format \'{{.ID}}\'' in source and 'http://docker-proxy:2375/info' in source


def test_missing_public_release_suggests_a_source_build(env):
    curl = Path(env["STUB_ROOT"]) / "bin/curl"
    curl.write_text(curl.read_text().replace('*api.test*)', '*api.test*) exit 22;'))
    result = bot(env, "compose-image", "--unattended", stdin="")
    assert result.returncode != 0 and "no public release" in result.stderr and "compose-build" in result.stderr
    assert " up " not in calls(env)
