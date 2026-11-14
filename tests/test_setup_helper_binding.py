import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
IDENTIFIER = "a" * 32
HARNESS = r'''
set -euo pipefail
source "$SHELL_ROOT/tools/setup_helper_only.sh"
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
bounded() { command timeout --foreground 5 "$@"; }
helper_inspect() { printf '%s\n' "$OBSERVED"; }
helper_bind_context
printf '%s\n' "$SHAZZZBOT_HELPER_INODE" > "$MARKER"
if [ "$HELPER_ONLY" = 1 ]; then
  [ "$SHAZZZBOT_HELPER_HOME" = "$HELPER_DIR" ]
  [ "$SHAZZZBOT_HELPER_INSTALLING" = 1 ]
fi
'''


@pytest.mark.parametrize("helper_only", [0, 1])
@pytest.mark.parametrize("case", ["matching", "changed-inode", "changed-id", "new-home", "new-home-wrong-id"])
def test_helper_binding_keeps_the_recorded_home(tmp_path, helper_only, case):
    seen = {"inode": None if case.startswith("new-home") else [1, 2], "install_id": IDENTIFIER}
    observed = {"inode": [1, 3] if case == "changed-inode" else [1, 2],
                "install_id": "b" * 32 if case in {"changed-id", "new-home-wrong-id"} else IDENTIFIER}
    marker = tmp_path / "bound"
    env = {**os.environ, "SHELL_ROOT": str(ROOT), "HELPER_DIR": str(tmp_path / "home"),
           "HELPER_ONLY": str(helper_only), "HELPER_SEEN": json.dumps(seen),
           "OBSERVED": json.dumps(observed), "INSTALL_ID": IDENTIFIER, "MARKER": str(marker)}
    result = subprocess.run(["bash", "-c", HARNESS], env=env, capture_output=True, text=True, timeout=10, check=False)
    if case in {"matching", "new-home"}:
        assert result.returncode == 0, result.stderr
        assert json.loads(marker.read_text()) == observed["inode"]
    else:
        assert result.returncode == 1
        expected = "helper home changed" if case == "changed-inode" else "helper identity changed"
        assert expected in result.stderr
        assert not marker.exists()


@pytest.mark.parametrize("helper_only", [0, 1])
def test_new_helper_binding_cannot_adopt_a_second_home(tmp_path, helper_only):
    first = {"inode": [1, 2], "install_id": IDENTIFIER}
    second = {"inode": [1, 3], "install_id": IDENTIFIER}
    marker = tmp_path / "rebound"
    harness = HARNESS.replace('printf \'%s\\n\' "$SHAZZZBOT_HELPER_INODE" > "$MARKER"', r'''
[ "$HELPER_SEEN" = "$OBSERVED" ]
OBSERVED="$SECOND"
helper_bind_context
printf '%s\n' "$SHAZZZBOT_HELPER_INODE" > "$MARKER"
''')
    env = {**os.environ, "SHELL_ROOT": str(ROOT), "HELPER_DIR": str(tmp_path / "home"),
           "HELPER_ONLY": str(helper_only), "HELPER_SEEN": json.dumps({"inode": None}),
           "OBSERVED": json.dumps(first), "SECOND": json.dumps(second), "INSTALL_ID": IDENTIFIER,
           "MARKER": str(marker)}
    result = subprocess.run(["bash", "-c", harness], env=env, capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 1
    assert "helper home changed" in result.stderr
    assert not marker.exists()
