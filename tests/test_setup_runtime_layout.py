import os
import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
IMAGE = "sha256:" + "c" * 64


@pytest.fixture
def shell(tmp_path):
    definitions = tmp_path / "definitions.sh"
    source = (ROOT / "setup.sh").read_text()
    assert source.endswith('main "$@"\n')
    definitions.write_text(source.removesuffix('main "$@"\n'))
    prefix = "\n".join("source " + shlex.quote(str(path)) for path in (
        definitions, ROOT / "tools/setup_runtime.sh", ROOT / "tools/setup_prereqs.sh",
        ROOT / "tools/setup_layout.sh", ROOT / "tools/setup_source.sh",
        ROOT / "tools/setup_session.sh", ROOT / "tools/setup_detect.sh",
        ROOT / "tools/setup_configure.sh", ROOT / "tools/setup_deploy.sh"))
    trace = tmp_path / "trace"

    def run(body):
        result = subprocess.run(["bash", "-c", prefix + '\ntrace() { printf "%s\\n" "$*" >> "$TRACE"; }\n' + body],
                                env={**os.environ, "STATE_ROOT": str(tmp_path), "TRACE": str(trace)},
                                capture_output=True, text=True, timeout=15, check=False)
        return result, trace.read_text().splitlines() if trace.exists() else []

    return run


@pytest.mark.parametrize("result", ["success", "decline", "build-failed", "health-failed"])
def test_deploy_builds_before_start_and_decline_never_starts(shell, result):
    script = r'''
ROLE=bot METHOD=compose-build MODE=setup ST_METHOD=compose-build ST_ROLE=bot ST_REF=old REF=new
APP_DIR="$STATE_ROOT/app" BACKUP_ROOT="$STATE_ROOT/backups"
CODE_CHANGED=1
mkdir -p "$APP_DIR" "$BACKUP_ROOT"
prepared_identity() { trace prepared; }
confirm() { trace confirm; CONTINUE; }
mark_mutation() { trace backup; }
ensure_image() { trace build; BUILD; }
ensure_dirs() { trace directories; }
start() { trace start; }
healthy() { trace healthy; HEALTH; }
watch_file() { trace watch; }
state_file() { printf '%s\n' "$APP_DIR/.setup-state"; }
replace_file() { cat >/dev/null; trace replace; }
seal_file() { trace seal; }
fs() { [ "$1" != drift ] || return 1; printf '{}\n'; }
logs_since() { printf 'safe log\n'; }
deploy
printf 'COMMITTED:%s\n' "$COMMITTED"
'''
    script = script.replace("CONTINUE", "false" if result == "decline" else "true")
    script = script.replace("BUILD;", "return 1;" if result == "build-failed" else "true;")
    script = script.replace("HEALTH;", "return 1;" if result == "health-failed" else "true;")
    response, trace = shell(script)
    if result == "decline":
        assert response.returncode == 0 and "COMMITTED:0" in response.stdout
        assert trace == ["prepared", "confirm"]
    elif result == "build-failed":
        assert response.returncode != 0
        assert trace == ["prepared", "confirm", "backup", "build"]
    elif result == "health-failed":
        assert response.returncode != 0 and "COMMITTED:1" not in response.stdout
        assert trace == ["prepared", "confirm", "backup", "build", "directories", "start", "healthy"]
    else:
        assert response.returncode == 0, response.stderr
        assert "COMMITTED:1" in response.stdout
        assert trace[:7] == ["prepared", "confirm", "backup", "build", "directories", "start", "healthy"]
        assert trace.count("replace") == 2


@pytest.mark.parametrize("entry", ["directory", "symlink"])
@pytest.mark.parametrize("function", ["sync_repo", "ship_source"])
def test_source_refuses_an_existing_staging_entry(shell, entry, function):
    response, trace = shell(r'''
SOURCE_DIR="$STATE_ROOT/source" SHIP_PACK="$STATE_ROOT/git.pack" SHIP_REVISION=revision
ENTRY
fs() { trace "$*"; }
git() { trace forbidden-git; return 1; }
FUNCTION
'''.replace("ENTRY", 'mkdir "$SOURCE_DIR"' if entry == "directory" else 'ln -s missing "$SOURCE_DIR"')
       .replace("FUNCTION", function))
    assert response.returncode == 1
    assert "already exists; left unchanged" in response.stderr
    assert not any(row.startswith("source-pack ") for row in trace)
    assert "forbidden-git" not in trace


@pytest.mark.parametrize("function", ["sync_repo", "ship_source"])
def test_source_allows_a_missing_staging_entry(shell, function):
    response, trace = shell(r'''
SOURCE_DIR="$STATE_ROOT/source" SHIP_PACK="$STATE_ROOT/git.pack" SHIP_REVISION=revision
REPO_URL=https://forge.example/repo.git BRANCH=main
fs() { trace "$*"; }
git() { trace "$*"; }
FUNCTION
'''.replace("FUNCTION", function))
    assert response.returncode == 0, response.stderr
    assert any(row.startswith("mkdir ") for row in trace)
    operation = "clone " if function == "sync_repo" else "source-pack "
    assert any(row.startswith(operation) for row in trace)


@pytest.mark.parametrize("previous,changed", [("revision", "0"), ("older", "1")])
def test_disposable_source_does_not_force_a_rebuild_of_the_same_revision(shell, previous, changed):
    response, _ = shell('ST_REF=' + shlex.quote(previous) + r'''
METHOD=compose-build SHIP_PACK="" SOURCE_DIR="$STATE_ROOT/source"
sync_repo() { CODE_CHANGED=1; }
git_app() { printf 'revision\n'; }
prepare_source
printf 'CHANGED:%s\n' "$CODE_CHANGED"
''')
    assert response.returncode == 0, response.stderr
    assert response.stdout.strip() == "CHANGED:" + changed


def test_unattended_values_can_come_from_private_candidates(shell):
    response, _trace = shell(r'''
ROLE=bot UNATTENDED=1 APP_DIR="$STATE_ROOT/missing"
CONFIG_SOURCE="$STATE_ROOT/candidates"
mkdir -p "$CONFIG_SOURCE"
touch "$CONFIG_SOURCE/.env" "$CONFIG_SOURCE/config.toml"
probe values
''')
    assert response.returncode == 0, response.stderr


def test_no_helper_install_does_not_inspect_unused_fuse_helper_path(shell):
    response, trace = shell(r'''
APP_DIR="$STATE_ROOT/app" BACKUP_ROOT="$STATE_ROOT/backups" HELPER_DIR=/mnt/user/appdata/shazzzbot-helper
HELPER=no
existing_parent() { printf '%s\n' "$1"; }
fs() { trace "$*"; [[ "$*" != *"/mnt/user/"* ]]; }
trust_install
''')
    assert response.returncode == 0, response.stderr
    assert len(trace) == 2
    assert all("/mnt/user/" not in row for row in trace)


@pytest.mark.parametrize(("socket", "gid", "okay"), [("", "", True),
                                                       ("/tmp/shazzzbot-helper/socket", "10001", True),
                                                       ("/tmp/shazzzbot-helper/socket", "4294967295", False),
                                                       ("/tmp/shazzzbot-helper/socket", "invalid", False),
                                                       ("/private/socket", "10001", False),
                                                       ("/run/helper", "10001", False),
                                                       ("//run/helper", "10001", False),
                                                       ("/", "10001", False),
                                                       ("relative", "10001", False),
                                                       ("/private/socket", "9999999999", False),
                                                       ("/private/socket", "99999999999", False),
                                                       ("/private/socket", "invalid", False)])
def test_helper_socket_path_requires_the_fixed_work_directory(shell, socket, gid, okay):
    body = r'''
ROLE=bot APP_DIR="$STATE_ROOT/app"
env_value() { if [ "$2" = HELPER_SOCKET_DIR ]; then printf '%s\n' "$SOCKET"; else printf '%s\n' "$GROUP"; fi; }
existing_parent() { printf '%s\n' "$1"; }
fs() { trace "$*"; }
validate_runtime_paths
trace validation-passed
'''
    response, trace = shell("SOCKET=" + shlex.quote(socket) + "\nGROUP=" + shlex.quote(gid) + "\n" + body)
    assert response.returncode == int(not okay), response.stderr
    assert ("validation-passed" in trace) == okay
    if not okay:
        assert "invalid helper socket path or group" in response.stderr


@pytest.mark.parametrize("function", ["ship_source", "ship_configs"])
def test_private_shipping_dry_run_does_not_touch_files_or_docker(shell, function):
    response, trace = shell(r'''
DRY_RUN=1 SOURCE_DIR=/private/source SHIP_REVISION=revision CONFIG_SOURCE=/run/candidates
fs() { trace forbidden-fs; return 1; }
docker() { trace forbidden-docker; return 1; }
FUNCTION
'''.replace("FUNCTION", function))
    assert response.returncode == 0 and "[dry-run]" in response.stdout, response.stderr
    assert trace == []


@pytest.mark.parametrize("same", [False, True])
def test_idempotency_checks_the_actual_image_behind_a_mutable_tag(shell, same):
    response, _trace = shell(r'''
METHOD=compose-build DEV_IMAGE=private:dev
compose() { printf 'owned-container\n'; }
docker() {
  if [[ "$*" == *Config.Image* ]]; then printf 'private:dev\n'
  elif [ "$1" = image ]; then printf 'EXPECTED\n'
  else printf 'ACTUAL\n'; fi
}
runtime_matches
'''.replace("EXPECTED", IMAGE).replace("ACTUAL", IMAGE if same else "sha256:" + "d" * 64))
    assert response.returncode == int(not same), response.stderr


def test_fresh_relay_private_data_is_writable_by_the_image_uid(shell, tmp_path):
    response, trace = shell(r'''
ROLE=relay REQUIRE_ROOT=1 APP_DIR="$STATE_ROOT/app"
fs() { trace "$*"; [ "$1" != mkdir ] || mkdir -p -- "$2"; }
docker() { trace forbidden-docker; return 1; }
ensure_dirs
''')
    assert response.returncode == 0, response.stderr
    data = tmp_path / "app/data"
    assert data.stat().st_uid == 10001 and data.stat().st_mode & 0o777 == 0o700
    assert data.parent.stat().st_uid == 0
    assert not any("forbidden-docker" in row for row in trace)
