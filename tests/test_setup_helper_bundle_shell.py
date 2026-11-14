import hashlib
import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from tools import build_helper

ROOT = Path(__file__).resolve().parents[1]
TAG = "v1.2.3"
MODULES = ("VERSION", "tools/__init__.py", "tools/setup_bundle.py", "tools/setup_artifacts.py", "tools/setup_files.py",
           "shazzzbot/__init__.py", "shazzzbot/helper/__init__.py", "shazzzbot/helper/constants.py",
           "shazzzbot/helper/footprint.py", "shazzzbot/helper/files.py", "shazzzbot/helper/formats.py")
HARNESS = r'''
set -euo pipefail
source "$SHELL_ROOT/tools/setup_helper_only.sh"
source "$SHELL_ROOT/tools/setup_install_helper.sh"
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
bounded() { command timeout --foreground 5 "$@"; }
temp_in() { TMPF="$(mktemp "$@")"; }
curl() { printf 'unexpected network request\n' >&2; return 97; }
docker() { printf 'unexpected Docker call\n' >&2; return 98; }
fs() {
  [ "$1" = verify ] || die 'unexpected filesystem operation'
  bounded python3 -I -B - "$SETUP_HOME" "$2" "$3" <<'PYTHON'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from tools.setup_artifacts import verify
verify(Path(sys.argv[2]), sys.argv[3])
PYTHON
}
helper_local_bundle
if [ "$OPERATION" = stage ]; then
  if [ "$CHANGE_TAG" = 1 ]; then
    bounded python3 -I -B - "$HELPER_BUNDLE/helper-unraid.json" <<'PYTHON'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
value = json.loads(path.read_bytes())
value['version'] = 'v1.2.4'
path.write_text(json.dumps(value))
PYTHON
  fi
  mkdir -m 700 -- "$WORK_ROOT"
  prepare_helper_bundle
fi
printf 'TAG=%s\nSTAGE=%s\nSHA=%s\n' "$HELPER_RELEASE_TAG" "$HELPER_STAGE" "$HELPER_EXPECTED"
'''


@pytest.fixture
def bundle_shell(tmp_path):
    local = tmp_path / "local"
    local.mkdir(mode=0o700)
    build_helper.bundle(local, TAG, "a" * 40)
    work = tmp_path / "work"
    source = tmp_path / "installer"
    for name in MODULES:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = (ROOT / name).read_text()
        if name == "shazzzbot/helper/footprint.py":
            before = 'WORK = Path("/tmp/shazzzbot-helper")'
            assert before in data
            data = data.replace(before, f"WORK = Path({str(work)!r})", 1)
        path.write_text(data)
        path.chmod(0o600)
    return local, source, work


def run_bundle(bundle_shell, *, operation="stage", version="", change=False):
    local, source, work = bundle_shell
    env = {**os.environ, "SHELL_ROOT": str(ROOT), "SETUP_HOME": str(source), "HELPER_BUNDLE": str(local),
           "WORK_ROOT": str(work), "VERSION_TAG": version, "HELPER_RELEASE_TAG": "", "HELPER_STAGE": "",
           "HELPER_EXPECTED": "", "HELPER_PREFLIGHT_DONE": "1", "DETECTED_PLATFORM": "unraid",
           "OPERATION": operation, "CHANGE_TAG": str(int(change)), "HELPER_DIR": str(work.parent / "home")}
    return subprocess.run(["bash", "-c", HARNESS], env=env, input="", text=True, capture_output=True,
                          check=False, timeout=15)


@pytest.mark.parametrize("problem", ["tampered", "extra", "version", "symlink", "writable"])
def test_real_local_bundle_preflight_refuses_before_work_or_home_creation(bundle_shell, problem):
    local, _source, work = bundle_shell
    version = "v1.2.4" if problem == "version" else ""
    if problem == "tampered":
        archive = local / "helper.pyz"
        data = archive.read_bytes()
        archive.write_bytes(b"!" + data[1:])
    elif problem == "extra":
        (local / "extra").write_text("unrelated")
    elif problem == "symlink":
        entry = local / "shazzzbot-helper-unraid.py"
        retained = local.parent / "entry.py"
        entry.rename(retained)
        entry.symlink_to(retained)
    elif problem == "writable":
        local.chmod(0o777)
    result = run_bundle(bundle_shell, version=version)
    assert result.returncode != 0
    assert "invalid local helper bundle" in result.stderr
    assert "unexpected network" not in result.stderr and "unexpected Docker" not in result.stderr
    assert not work.exists() and not (work.parent / "home").exists()


def test_real_helper_functions_stage_a_local_bundle_without_network_or_docker(bundle_shell):
    local, _source, work = bundle_shell
    before = {path.name: path.read_bytes() for path in local.iterdir()}
    result = run_bundle(bundle_shell, version=TAG)
    assert result.returncode == 0, result.stderr
    assert f"TAG={TAG}" in result.stdout
    assert f"SHA={hashlib.sha256(before['helper.pyz']).hexdigest()}" in result.stdout
    stages = list(work.iterdir())
    assert len(stages) == 1
    assert {path.name: path.read_bytes() for path in stages[0].iterdir()} == before
    with zipfile.ZipFile(stages[0] / "helper.pyz") as archive:
        assert archive.read("helper/formats.py") == (ROOT / "shazzzbot/helper/formats.py").read_bytes()
    assert {path.name: path.read_bytes() for path in local.iterdir()} == before
    assert not (work.parent / "home").exists()


def test_stage_revalidates_and_refuses_a_tag_changed_after_preflight(bundle_shell):
    local, _source, work = bundle_shell
    result = run_bundle(bundle_shell, change=True)
    assert result.returncode != 0 and "version does not match --version" in result.stderr
    assert json.loads((local / "helper-unraid.json").read_bytes())["version"] == "v1.2.4"
    stages = list(work.iterdir())
    assert len(stages) == 1 and list(stages[0].iterdir()) == []
    assert not (work.parent / "home").exists()
