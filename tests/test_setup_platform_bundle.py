import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TAG = "v1.2.3"


def bundle(tmp_path):
    remote = tmp_path / "remote"
    stage = tmp_path / "stage"
    remote.mkdir(mode=0o700)
    stage.mkdir(mode=0o700)
    assets = {"helper.pyz": b"verified archive", "shazzzbot-helper-unraid.py": b"print('fixture')\n"}
    for name, data in assets.items():
        (remote / name).write_bytes(data)
    platform = {"format": 1, "platform": "unraid", "version": TAG, "revision": "a" * 40,
                "files": {name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                          for name, data in assets.items()}}
    (remote / "helper-unraid.json").write_text(json.dumps(platform))
    release = {"format": 1, "version": TAG, "revision": "a" * 40, "source_sha256": "b" * 64,
               "helper_sha256": platform["files"]["helper.pyz"]["sha256"],
               "image": "ghcr.io/example/shazzzbot:v1.2.3@sha256:" + "c" * 64,
               "platforms": {"unraid": hashlib.sha256((remote / "helper-unraid.json").read_bytes()).hexdigest()}}
    (tmp_path / "release.json").write_text(json.dumps(release))
    return remote, stage, assets


def run(tmp_path, action="bundle", platform="unraid"):
    script = r'''
set -eu
source "$ROOT/tools/setup_install_helper.sh"
SETUP_HOME="$ROOT" SOURCE_URL=https://example.com/releases/download
HELPER_RELEASE_TAG=v1.2.3 VERSION_TAG=v1.2.3 DETECTED_PLATFORM="$PLATFORM"
HELPER_BUNDLE=""
RELEASE_MANIFEST="$TEST_ROOT/release.json" METHOD=compose-image
die() { printf '%s\n' "$*" >&2; exit 1; }
bounded() { "$@"; }
curl() {
  local previous= output= url="${*: -1}"
  for argument in "$@"; do
    if [ "$previous" = -o ]; then output="$argument"; fi
    previous="$argument"
  done
  printf '%s\n' "$url" >> "$TEST_ROOT/fetches"
  cp -- "$TEST_ROOT/remote/${url##*/}" "$output"
}
fs() {
  if [ "$1" = release-tag ]; then
    python3 -I -B - "$ROOT" "$2" <<'PYTHON'
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from shazzzbot.helper.formats import load
print(load(json.loads(Path(sys.argv[2]).read_bytes()), 'release manifest', 1)['version'])
PYTHON
    return
  fi
  [ "$1" = verify ] || return 89
  shift
  python3 -I -B - "$ROOT" "$@" <<'PYTHON'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from tools.setup_artifacts import verify
verify(Path(sys.argv[2]), sys.argv[3])
PYTHON
}
if [ "$ACTION" = release ]; then helper_release_manifest
else helper_platform_bundle "$TEST_ROOT/stage"; fi
'''
    return subprocess.run(["bash", "-c", script], env={**os.environ, "ROOT": str(ROOT), "TEST_ROOT": str(tmp_path),
                          "ACTION": action, "PLATFORM": platform}, capture_output=True, text=True,
                          timeout=10, check=False)


def fetches(tmp_path):
    path = tmp_path / "fetches"
    return path.read_text().splitlines() if path.exists() else []


def test_downloads_only_the_pinned_unraid_manifest_and_its_verified_files(tmp_path):
    _remote, stage, assets = bundle(tmp_path)
    result = run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert fetches(tmp_path) == ["https://example.com/releases/download/v1.2.3/" + name
                                for name in ("helper-unraid.json", *assets)]
    assert {path.name for path in stage.iterdir()} == {"helper-unraid.json", *assets}
    for name, data in assets.items():
        assert (stage / name).read_bytes() == data


@pytest.mark.parametrize("problem", ["sha", "path"])
def test_bad_manifest_digest_or_asset_path_refuses_before_asset_fetch(tmp_path, problem):
    remote, stage, _assets = bundle(tmp_path)
    manifest = remote / "helper-unraid.json"
    if problem == "sha":
        manifest.write_bytes(manifest.read_bytes() + b" ")
    else:
        value = json.loads(manifest.read_bytes())
        value["files"]["../outside"] = value["files"].pop("shazzzbot-helper-unraid.py")
        manifest.write_text(json.dumps(value))
        release = json.loads((tmp_path / "release.json").read_bytes())
        release["platforms"]["unraid"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
        (tmp_path / "release.json").write_text(json.dumps(release))
    result = run(tmp_path)
    assert result.returncode != 0 and "invalid platform helper manifest" in result.stderr
    assert fetches(tmp_path) == ["https://example.com/releases/download/v1.2.3/helper-unraid.json"]
    assert [path.name for path in stage.iterdir()] == ["helper-unraid.json"]
    assert not (tmp_path / "outside").exists()


def test_asset_hash_mismatch_stops_before_fetching_or_running_the_entry(tmp_path):
    remote, stage, _assets = bundle(tmp_path)
    (remote / "helper.pyz").write_bytes(b"corrupted archive")
    result = run(tmp_path)
    assert result.returncode != 0 and "differs from its released digest" in result.stderr
    assert fetches(tmp_path) == ["https://example.com/releases/download/v1.2.3/" + name
                                for name in ("helper-unraid.json", "helper.pyz")]
    assert not (stage / "shazzzbot-helper-unraid.py").exists()


def test_unsupported_helper_platform_refuses_before_any_fetch(tmp_path):
    bundle(tmp_path)
    result = run(tmp_path, action="release", platform="debian")
    assert result.returncode != 0 and "not supported yet: helper platform debian" in result.stderr
    assert fetches(tmp_path) == []


def test_cached_release_rejects_invalid_tag_before_any_fetch(tmp_path):
    bundle(tmp_path)
    path = tmp_path / "release.json"
    value = json.loads(path.read_bytes())
    value["version"] = "../outside"
    path.write_text(json.dumps(value))
    result = run(tmp_path, action="release")
    assert result.returncode != 0 and "invalid helper release version" in result.stderr and not fetches(tmp_path)
