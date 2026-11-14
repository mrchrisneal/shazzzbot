import os
import subprocess
from pathlib import Path

import pytest

INSTALL_ID = "a" * 32


@pytest.mark.parametrize("label", [INSTALL_ID, "b" * 32, "", "<no value>", "manual", "invalid:identity"])
def test_container_requires_the_exact_current_install_label(tmp_path, label):
    script = Path("tools/setup_prereqs.sh").resolve()
    trace = tmp_path / "calls"
    command = '''
set -eu
source "$1"
PROJECT=shazzzbot ST_METHOD=""
die() { printf '%s\\n' "$*" >&2; exit 3; }
docker() {
  printf '%s\\n' "$*" >> "$TRACE"
  case "$*" in
    "container inspect candidate") return 0 ;;
    *org.shazzzbot.install*) printf '%s\\n' "$LABEL" ;;
    *) return 4 ;;
  esac
}
assert_container candidate
'''
    result = subprocess.run(["bash", "-c", command, "ownership", str(script)],
                            env={**os.environ, "INSTALL_ID": INSTALL_ID, "LABEL": label, "TRACE": str(trace)},
                            capture_output=True, text=True, check=False, timeout=5)
    assert result.returncode == (0 if label == INSTALL_ID else 3), result.stderr
    calls = trace.read_text().splitlines()
    assert len(calls) == 2 and calls[0] == "container inspect candidate"
    assert "org.shazzzbot.install" in calls[1]
    assert not any("com.docker.compose" in line for line in calls)
    assert not result.stdout
    if label != INSTALL_ID:
        assert "not owned by this install" in result.stderr or "belongs to another install" in result.stderr


def test_absent_container_needs_no_ownership_query(tmp_path):
    script = Path("tools/setup_prereqs.sh").resolve()
    trace = tmp_path / "calls"
    command = '''
set -eu
source "$1"
PROJECT=shazzzbot
docker() { printf '%s\\n' "$*" >> "$TRACE"; return 1; }
assert_container candidate
'''
    result = subprocess.run(["bash", "-c", command, "ownership", str(script)],
                            env={**os.environ, "INSTALL_ID": INSTALL_ID, "TRACE": str(trace)},
                            capture_output=True, text=True, check=False, timeout=5)
    assert result.returncode == 0, result.stderr
    assert trace.read_text().splitlines() == ["container inspect candidate"]
