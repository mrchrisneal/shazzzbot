import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tools/setup_source.sh"

HARNESS = r'''
set -euo pipefail
source "$1"
say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
dry() { [ "$DRY_RUN" = 1 ]; }
plan() { printf '[dry-run] %s\n' "$*"; }
bounded() { command timeout 5 "$@"; }
temp_in() { TMPF="$(mktemp "$@")"; }
curl() {
  printf '%s\n' "$*" >> "$REQUESTS"
  if [ "${*: -1}" = "$RELEASE_API" ]; then
    if [ "$RESPONSE" = oversize ]; then printf 'Maximum file size exceeded\n' >&2; return 63; fi
    if [ "$RESPONSE" = failed ]; then return 22; fi
    printf '%s' "$LATEST_BODY"
  else
    local previous='' argument output=''
    for argument in "$@"; do
      if [ "$previous" = -o ]; then output="$argument"; fi
      previous="$argument"
    done
    printf 'fixture asset\n' > "$output"
  fi
}
fs() {
  case "$1" in
    mkdir) mkdir -m 700 -- "$2" ;;
    release)
      case "$4" in image) printf 'pinned-image\n' ;; source_sha256) printf 'source-digest\n' ;; esac ;;
    extract) printf 'verified extraction\n' ;;
    *) die 'unexpected filesystem command' ;;
  esac
}
fetch_release
printf 'SELECTED=%s\n' "$REF"
'''


def fetch(tmp_path, *, latest="v1.2.3", installed="", explicit="", response="ok", dry=False, git=False):
    app, work = tmp_path / "app", tmp_path / "work"
    app.mkdir(mode=0o700)
    work.mkdir(mode=0o700)
    config = app / "config.toml"
    config.write_bytes(b"operator config retained exactly\n")
    if git:
        (app / ".git").mkdir()
    requests = tmp_path / "requests"
    env = {**os.environ, "APP_DIR": str(app), "WORK_ROOT": str(work), "SETUP_HOME": str(ROOT),
           "VERSION_TAG": explicit, "ST_REF": installed, "DRY_RUN": str(int(dry)), "RESPONSE": response,
           "LATEST_BODY": json.dumps({"tag_name": latest}) if latest is not None else "{}",
           "RELEASE_API": "https://api.example.com/latest", "SOURCE_URL": "https://release.example.com/download",
           "REQUESTS": str(requests)}
    result = subprocess.run(["bash", "-c", HARNESS, "setup-source", str(SOURCE)], env=env, input="", text=True,
                            capture_output=True, timeout=10, check=False)
    assert config.read_bytes() == b"operator config retained exactly\n"
    assert set(app.iterdir()) == {config, *((app / ".git",) if git else ())}
    calls = requests.read_text().splitlines() if requests.exists() else []
    return result, calls, work


def test_implicit_latest_cannot_downgrade_a_newer_installed_release(tmp_path):
    result, calls, work = fetch(tmp_path, installed="v1.10.0", latest="v1.9.0")
    assert result.returncode == 1
    assert "v1.9.0 is older than installed v1.10.0" in result.stderr
    assert "--version" in result.stderr
    assert len(calls) == 1 and calls[0].endswith("https://api.example.com/latest")
    assert list(work.iterdir()) == []


@pytest.mark.parametrize("installed,latest", [("v1.9.0", "v1.9.0"), ("v1.9.0", "v1.10.0"),
                                             ("", "v1.0.0"), ("a" * 40, "v1.0.0")])
def test_equal_newer_fresh_and_commit_based_installs_fetch_release_assets(tmp_path, installed, latest):
    result, calls, work = fetch(tmp_path, installed=installed, latest=latest)
    assert result.returncode == 0, result.stderr
    assert f"SELECTED={latest}" in result.stdout
    assert len(calls) == 3
    assert calls[1].endswith(f"/{latest}/release.json")
    assert calls[2].endswith(f"/{latest}/source.tar.gz")
    assert len(list(work.iterdir())) == 1


def test_explicit_release_still_allows_an_operator_selected_rollback(tmp_path):
    result, calls, _ = fetch(tmp_path, installed="v1.10.0", explicit="v1.9.0", response="failed")
    assert result.returncode == 0, result.stderr
    assert "SELECTED=v1.9.0" in result.stdout
    assert len(calls) == 2 and all("api.example.com" not in call for call in calls)
    assert calls[0].endswith("/v1.9.0/release.json")


@pytest.mark.parametrize("tag", ["v1.2", "1.2.3", "v01.2.3", "v1.02.3", "v1.2.03",
                                 "v1000000000.0.0", "v1.2.3-rc.1", "v1.2.3\nextra"])
@pytest.mark.parametrize("explicit", [False, True])
def test_malformed_selected_tags_are_refused_before_staging_or_asset_requests(tmp_path, tag, explicit):
    result, calls, work = fetch(tmp_path, latest=tag, explicit=tag if explicit else "")
    assert result.returncode == 1
    assert "release tag must be canonical vX.Y.Z" in result.stderr
    assert len(calls) == int(not explicit)
    assert list(work.iterdir()) == []


@pytest.mark.parametrize("response", ["oversize", "failed"])
def test_latest_request_failure_or_oversize_leaves_install_unchanged_and_downloads_no_assets(tmp_path, response):
    result, calls, work = fetch(tmp_path, response=response)
    assert result.returncode == 1
    assert "no public release" in result.stderr and "64 KiB" in result.stderr
    assert "--method compose-build" in result.stderr
    assert len(calls) == 1
    assert "--max-filesize 65536" in calls[0]
    assert "--proto =https" in calls[0] and "--max-time 30" in calls[0]
    assert list(work.iterdir()) == []


def test_successful_latest_request_is_size_capped_and_https_bounded(tmp_path):
    result, calls, _ = fetch(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "--max-filesize 65536" in calls[0]
    assert "--proto =https" in calls[0] and "--max-time 30" in calls[0]


def test_missing_release_tag_is_refused_before_asset_requests(tmp_path):
    result, calls, work = fetch(tmp_path, latest=None)
    assert result.returncode == 1 and "has no tag_name" in result.stderr
    assert len(calls) == 1 and list(work.iterdir()) == []


def test_dry_run_compares_versions_but_creates_no_release_staging(tmp_path):
    result, calls, work = fetch(tmp_path, installed="v1.9.0", latest="v1.10.0", dry=True)
    assert result.returncode == 0, result.stderr
    assert "[dry-run] download the v1.10.0 source files" in result.stdout
    assert len(calls) == 1 and list(work.iterdir()) == []


def test_git_checkout_is_refused_without_any_release_request(tmp_path):
    result, calls, work = fetch(tmp_path, git=True)
    assert result.returncode == 1 and "git checkout" in result.stderr
    assert calls == [] and list(work.iterdir()) == []
