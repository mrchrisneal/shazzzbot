import hashlib
import json
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("kind", ["absent", "empty", "pinned", "unrelated-file", "altered-script",
                                  "unreadable", "mismatched-policy"])
def test_helper_hook_accepts_only_empty_or_current_managed_entries(tmp_path, kind):
    home, entry = tmp_path / "home", tmp_path / "entry"
    home.mkdir(mode=0o700)
    if kind != "absent":
        entry.mkdir(mode=0o700)
    if kind == "unrelated-file":
        (entry / "unrelated.txt").write_text("keep this file")
    if kind in {"pinned", "altered-script", "unreadable", "mismatched-policy"}:
        body = b"verified hook"
        (entry / "script").write_bytes(body if kind != "altered-script" else b"unrelated user command\n")
        (entry / "script").chmod(0o600)
        identity = {"format": 1, "install_id": "a" * 32, "hook": str(entry / "script"),
                    "hook_sha256": hashlib.sha256(body).hexdigest(), "active": False}
        (home / ".identity.json").write_text(json.dumps(identity))
        (home / ".identity.json").chmod(0o600)
        value = {"format": 1, "artifact": str(home / "helper.pyz"),
                 "config": str(home / "config.json"), "sha256": "a" * 64}
        if kind == "mismatched-policy":
            value["artifact"] = str(home / "other.pyz")
        (home / "policy.json").write_text(json.dumps(value))
        (home / "policy.json").chmod(0o600)
    script = '''
source "$1"
HELPER_DIR="$2" HOOK_DIR="$3"
manage="$5"
die() { printf '%s\\n' "$1" >&2; exit 1; }
if [ "$4" = unreadable ]; then fs() { return 1; }; else fs() { python3 "$manage" "$@"; }; fi
check_helper_launcher
'''
    result = subprocess.run(["bash", "-c", script, "policy", str(Path("tools/setup_install_helper.sh").resolve()),
                             str(home), str(entry), kind, str(Path("tools/setup_manage.py").resolve())],
                            check=False, capture_output=True, text=True, timeout=10)
    assert result.returncode == (0 if kind in {"absent", "empty", "pinned"} else 1), result.stderr
    if kind == "unrelated-file":
        assert "entry contains unidentified files" in result.stderr
        assert (entry / "unrelated.txt").read_text() == "keep this file"
    elif kind == "altered-script":
        assert "helper hook does not match its identity" in result.stderr
        assert (entry / "script").read_bytes() == b"unrelated user command\n"
    elif kind in {"unreadable", "mismatched-policy"}:
        assert "existing helper policy leaves its private home" in result.stderr
    assert not result.stdout
