from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


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
