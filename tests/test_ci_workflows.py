import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".forgejo" / "workflows" / "forge.yml"
FEEDS = ("Refresh CodeCharta coverage overlay", "Refresh Dependency-Track SBOM")
CLEANUP = "Remove temporary coverage volume"
CC_DIGEST = "ae97d9425ef81c00b52947a1d9d1c7cad1346204a3a288ef4fb90f07b7efd2b1"


def suite_steps():
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]["suite"]["steps"]


def release_job():
    return yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())["jobs"]["image"]


def test_release_requires_successful_ci_for_the_exact_sha_before_mutating_the_registry():
    job = release_job()
    assert job["permissions"]["actions"] == "read"
    body = next(item["run"] for item in job["steps"] if item.get("name") == "Build and push")
    assert 'head_sha=$SHA&event=push&status=success&per_page=1' in body
    assert 'timeout 30 gh api' in body
    admission = body.index('python3 -m tools.release_check ci "$SHA" "$checks"')
    assert admission < body.index("docker login") < body.index("docker build") < body.index("docker push")


def test_release_checks_the_built_bot_and_helper_versions_before_publishing():
    body = next(item["run"] for item in release_job()["steps"] if item.get("name") == "Build and push")
    assert 'version="$(python3 -m tools.release_check version VERSION)"' in body
    assert "from shazzzbot import __version__" in body
    assert "--network none --entrypoint python" in body
    assert 'sys.path.insert(0, "helper.pyz"); from helper import VERSION' in body
    validation = body.index(
        'python3 -m tools.release_check versions "$TAG" "$version" "$bot_version" "$helper_version"')
    assert body.index("docker build") < body.index("tools/build_helper.py") < validation
    assert validation < body.index("docker push") < body.index("gh release upload")


def test_release_keeps_the_release_draft_until_remote_assets_and_checksums_match():
    body = next(item["run"] for item in release_job()["steps"] if item.get("name") == "Build and push")
    assert '--generate-notes' not in body
    assert 'releases/tags/' not in body
    assert 'gh release view "$TAG" --json isDraft,assets' in body
    assert "--jq '{draft:.isDraft, assets:.assets}'" in body
    assert 'python3 -m tools.release_check notes "$TAG" CHANGELOG.md > "$notes"' in body
    create = body.index('gh release create "$TAG" --verify-tag --draft --latest=false --notes-file "$notes"')
    draft = body.rindex('python3 -m tools.release_check draft "$release_info"')
    upload = body.index('gh release upload "$TAG"')
    assets = body.index('python3 -m tools.release_check assets "$release_info" .')
    download = body.index('timeout 180 gh release download "$TAG" --dir "$downloads"')
    checksums = body.index('python3 -m tools.release_check downloads . "$downloads"')
    publish = body.index('gh release edit "$TAG" --draft=false --latest=false --notes-file "$notes"')
    assert create < draft < upload < assets < download < checksums < publish
    assert body.index('python3 -m tools.release_check draft "$release_info"') < body.index("docker login")
    assert publish < body.index('if [ "$TAG" = "$newest" ]')


def test_github_ci_runs_the_full_root_container_gate_before_node_checks():
    job = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]["check"]
    assert job["timeout-minutes"] == 60
    body = next(item["run"] for item in job["steps"] if item.get("name") == "Tests with the coverage floor")
    assert './test.sh --cov --cov-report=term' in body
    assert 'python -m pytest' not in body
    assert body.index('./test.sh') < body.index('node docs/demo/loader-test.mjs')


def test_release_publishes_the_pinned_platform_bundle_with_all_integrity_assets():
    body = next(item["run"] for item in release_job()["steps"] if item.get("name") == "Build and push")
    assert 'tools/build_helper.py --bundle-dir . --version "$TAG" --revision "$SHA"' in body
    assert 'data["platforms"] = {"unraid": hashlib.sha256(Path("helper-unraid.json").read_bytes()).hexdigest()}' in body
    checksums = body.split("sha256sum ", 1)[1].split(" > SHA256SUMS", 1)[0].split()
    assert checksums == ["source.tar.gz", "helper.pyz", "helper-unraid.json",
                         "shazzzbot-helper-unraid.py", "release.json"]
    publish = body.split('gh release upload "$TAG" ', 1)[1].split("--clobber", 1)[0]
    assert set(publish.replace("\\\n", " ").split()) == {*checksums, "SHA256SUMS"}


def step(name):
    return next(item for item in suite_steps() if item.get("name") == name)


def test_optional_feeds_follow_the_suite_and_are_bounded_main_only_best_effort():
    steps = suite_steps()
    tests = next(index for index, item in enumerate(steps) if "coverage floor" in item.get("name", ""))
    for name in FEEDS:
        item = step(name)
        assert steps.index(item) > tests
        assert "success()" in item["if"] and "refs/heads/main" in item["if"]
        assert item["continue-on-error"] is True
        assert 0 < item["timeout-minutes"] <= 10
    cleanup = step(CLEANUP)
    assert steps.index(cleanup) > max(steps.index(step(name)) for name in FEEDS)
    assert "always()" in cleanup["if"]
    assert cleanup["continue-on-error"] is True
    assert 0 < cleanup["timeout-minutes"] <= 5


def test_feed_tools_are_pinned_and_read_the_real_runtime_lock():
    cc = step(FEEDS[0])
    dt = step(FEEDS[1])
    assert "sha256:" + CC_DIGEST in cc["run"]
    assert "cyclonedx-bom==7.4.0" in dt["run"]
    assert "requirements.txt" in dt["run"] and "requirements-dev.txt" not in dt["run"]
    assert '"projectName": "shazzzbot"' in dt["run"]
    assert '"projectVersion": "main"' in dt["run"]
    assert '-v "$COVERAGE_VOLUME:/coverage:ro"' in cc["run"]
    assert cc["env"]["CC_DATA_DIR"] == "${{ secrets.CC_DATA_DIR }}"
    assert dt["env"]["DT_API_KEY"] == "${{ secrets.DT_API_KEY }}"
    assert dt["env"]["DT_ENDPOINT"] == "${{ secrets.DT_ENDPOINT }}"
    for item in (cc, dt):
        text = yaml.safe_dump(item)
        assert not re.search(r"\b(?:100|192\.168|10)\.\d+\.\d+", text)
        assert "/mnt/user/" not in text and "/home/" not in text


def test_suite_source_is_read_only_and_coverage_uses_a_dedicated_temporary_volume():
    tests = next(item for item in suite_steps() if "coverage floor" in item.get("name", ""))
    assert '--volumes-from "$HOSTNAME:ro"' in tests["run"]
    assert "coverage xml" in tests["run"] and "-o /coverage/shazzzbot.xml" in tests["run"]
    assert "docker volume create" in tests["run"]
    assert "COVERAGE_VOLUME" in tests["run"]
    assert "COVERAGE_VOLUME" in step(FEEDS[0])["run"]
    assert "COVERAGE_VOLUME" in step(CLEANUP)["run"]
    assert "docker volume rm" in step(CLEANUP)["run"]
    assert "docker volume prune" not in step(CLEANUP)["run"]


@pytest.mark.parametrize("name,key,endpoint", [(FEEDS[0], "", ""), (CLEANUP, "", ""),
                                              (FEEDS[1], "", ""), (FEEDS[1], "fixture", ""),
                                              (FEEDS[1], "", "https://example.com/api/v1/bom")])
def test_unconfigured_feed_steps_exit_successfully_without_docker_or_network(tmp_path, name, key, endpoint):
    marker = tmp_path / "unexpected-command"
    commands = tmp_path / "bin"
    commands.mkdir()
    for command in ("docker", "curl", "python", "python3", "pip", "pip3"):
        stub = commands / command
        stub.write_text('#!/bin/sh\nprintf "%s\\n" "$0" >> "$COMMAND_MARKER"\nexit 97\n')
        stub.chmod(0o700)
    env = {**os.environ, "PATH": str(commands) + ":/usr/bin:/bin", "HOME": str(tmp_path),
           "COMMAND_MARKER": str(marker), "CC_DATA_DIR": "", "DT_API_KEY": key, "DT_ENDPOINT": endpoint,
           "COVERAGE_VOLUME": "", "GITHUB_REF": "refs/heads/main", "GITHUB_ENV": str(tmp_path / "env")}
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", step(name)["run"]],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not marker.exists(), result.stdout + result.stderr


def test_coverage_xml_keeps_repository_relative_package_paths(tmp_path):
    for package in ("shazzzbot", "tools"):
        folder = tmp_path / package
        folder.mkdir()
        (folder / "__init__.py").write_text("")
        (folder / "measured.py").write_text("value = 1\n")
    (tmp_path / "measure.py").write_text("import shazzzbot.measured\nimport tools.measured\n")
    (tmp_path / ".coveragerc").write_text("[run]\nsource = shazzzbot, tools\nbranch = True\n")
    env = {**os.environ, "COVERAGE_FILE": str(tmp_path / ".coverage")}
    for args in (("run", "measure.py"), ("xml", "--rcfile=/dev/null", "-o", "coverage.xml")):
        subprocess.run([sys.executable, "-m", "coverage", *args], cwd=tmp_path, env=env,
                       capture_output=True, text=True, check=True, timeout=15)
    filenames = {entry.attrib["filename"] for entry in ET.parse(tmp_path / "coverage.xml").findall(".//class")}
    assert {"shazzzbot/measured.py", "tools/measured.py"} <= filenames


@pytest.mark.parametrize("ref,configured,failed", [("refs/heads/main", True, True),
                                                 ("refs/heads/main", False, False),
                                                 ("refs/heads/feature", True, False)])
def test_optional_coverage_volume_cannot_fail_or_run_outside_its_guard(tmp_path, ref, configured, failed):
    commands = tmp_path / "bin"
    commands.mkdir()
    log = tmp_path / "docker.log"
    docker = commands / "docker"
    docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$COMMAND_LOG"\n'
                      'if [ "$1 $2" = "volume create" ]; then exit 97; fi\nexit 0\n')
    docker.chmod(0o700)
    env = {**os.environ, "PATH": str(commands) + ":/usr/bin:/bin", "COMMAND_LOG": str(log),
           "GITHUB_REF": ref, "CC_DATA_DIR": "/configured" if configured else "",
           "GITHUB_ENV": str(tmp_path / "env")}
    tests = next(item for item in suite_steps() if "coverage floor" in item.get("name", ""))
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", tests["run"]], cwd=ROOT,
                            env=env, capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = log.read_text().splitlines()
    assert any(line.startswith("run ") for line in calls), "the required suite still runs"
    assert any(line.startswith("volume create ") for line in calls) is failed
    assert not (tmp_path / "env").exists()
    assert not any(":/coverage" in line for line in calls)
    assert ("could not be created" in result.stderr) is failed


def test_optional_xml_export_failure_still_runs_mypy(tmp_path):
    commands = tmp_path / "bin"
    commands.mkdir()
    log = tmp_path / "commands.log"
    for name in ("pip", "python"):
        executable = commands / name
        executable.write_text('#!/bin/sh\nprintf "%s %s\\n" "$0" "$*" >> "$COMMAND_LOG"\n'
                              'if [ "$1 $2" = "-m coverage" ]; then exit 23; fi\nexit 0\n')
        executable.chmod(0o700)
    coverage = tmp_path / "coverage"
    coverage.mkdir()
    tests = next(item for item in suite_steps() if "coverage floor" in item.get("name", ""))
    child = tests["run"].rsplit("sh -ec '", 1)[1].rsplit("'", 1)[0].replace("/coverage", str(coverage))
    env = {**os.environ, "PATH": str(commands) + ":/usr/bin:/bin", "COMMAND_LOG": str(log)}
    result = subprocess.run(["sh", "-ec", child], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = log.read_text()
    assert "-m pytest" in calls and "-m coverage xml" in calls and "-m mypy" in calls
    assert "coverage XML could not be exported" in result.stderr
