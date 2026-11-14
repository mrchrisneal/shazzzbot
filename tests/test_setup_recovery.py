import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from tests.test_setup_sh import bot, calls, checkout, env, run, runtime_app, source_tar

__all__ = ["env"]


def test_first_clone_failing_health_leaves_no_broken_checkout_and_can_retry(env):
    root = Path(env["STUB_ROOT"])
    template = checkout(env)
    (root / "not_ready").touch()
    result = bot(env, "compose-build", "--unattended", stdin="")
    assert result.returncode != 0, result.stdout
    assert "no previous deployment" in result.stderr, result.stderr
    assert not (runtime_app(env) / ".env").exists()
    assert not (runtime_app(env) / ".git").exists()
    assert not (runtime_app(env) / "source").exists()
    assert (template / "compose.yaml").is_file()
    assert not (Path(env["SHAZZZBOT_BACKUP_DIR"]) / "pending").exists()
    (root / "not_ready").unlink()
    result = bot(env, "compose-build", "--unattended", stdin="")
    assert result.returncode == 0, result.stderr
    assert "local changes" not in result.stderr


def test_failed_source_upgrade_restores_the_running_image_without_persisting_a_checkout(env):
    template = checkout(env)
    assert bot(env, "compose-build", "--unattended", stdin="").returncode == 0
    (template / ".git/HEAD_SHA").write_text("c" * 40)
    (Path(env["STUB_ROOT"]) / "fail_up").touch()
    result = run(env, "--upgrade", "--unattended")
    assert result.returncode != 0, result.stderr
    assert "recovery health verified" in result.stderr
    app = runtime_app(env)
    assert json.loads((app / ".setup-state").read_text()) == {
        "format": 1, "method": "compose-build", "role": "bot", "ref": "a" * 40}
    assert "    image: sha256:" + "a" * 64 + "\n" in (app / "compose.yaml").read_text()
    assert not (app / ".git").exists() and not (app / "source").exists()
    assert "reset --hard" not in calls(env) and "fetch --quiet" not in calls(env)
    assert (template / ".git/HEAD_SHA").read_text() == "c" * 40


def test_missing_proxy_source_fails_before_any_start(env):
    root = Path(env["STUB_ROOT"])
    source_tar(root, "v1.2.3")
    path = root / "src-v1.2.3.tar.gz"
    source = path.read_bytes()
    with tarfile.open(fileobj=io.BytesIO(source), mode="r:gz") as original, tarfile.open(path, "w:gz") as output:
        for item in original.getmembers():
            if not item.name.endswith("docker-proxy/haproxy.cfg"):
                output.addfile(item, original.extractfile(item))
    value = json.loads((root / "manifest-v1.2.3.json").read_text())
    value["source_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    (root / "manifest-v1.2.3.json").write_text(json.dumps(value))
    result = bot(env, "compose-image", "--unattended", stdin="")
    assert result.returncode != 0 and "integrity" in result.stderr
    assert " up " not in calls(env) and "run --rm" not in calls(env)
    assert not (runtime_app(env) / "config.toml").exists()
    assert not (runtime_app(env) / "compose.yaml").exists()


def test_dry_run_does_not_refresh_a_checkout_index_or_create_a_lock(env):
    app = checkout(env)
    index = app / ".git/index"
    index.write_bytes(b"unchanged index")
    before = index.stat()
    result = bot(env, "compose-build", "--dry-run", "--unattended", stdin="")
    assert result.returncode == 0, result.stderr
    assert index.read_bytes() == b"unchanged index" and index.stat().st_mtime_ns == before.st_mtime_ns
    assert not Path(env["SHAZZZBOT_LOCK_FILE"]).exists()
    assert "merge" not in calls(env)


def test_unidentified_plain_container_is_never_removed(env):
    root = Path(env["STUB_ROOT"])
    (root / "run_shazzzbot").touch()
    (root / "owner").write_text("")
    result = bot(env, "docker-run", "--unattended", stdin="")
    assert result.returncode != 0
    assert "not owned" in result.stderr
    assert not any(line.startswith("rm ") for line in calls(env).splitlines())


@pytest.mark.parametrize("method", ["compose-image", "docker-run"])
def test_failed_fresh_image_install_cleans_its_files_and_can_retry(env, method):
    root = Path(env["STUB_ROOT"])
    docker = root / "bin/docker"
    original = docker.read_text()
    docker.write_text(original.replace('rm "$s/not_ready"', ':'))
    (root / "not_ready").touch()
    result = bot(env, method, "--unattended", stdin="")
    assert result.returncode != 0
    app = runtime_app(env)
    assert not (app / "compose.yaml").exists() and not (app / ".env").exists()
    assert list((app / "docker-proxy").iterdir()) == []
    docker.write_text(original)
    (root / "not_ready").unlink()
    retry = bot(env, method, "--unattended", stdin="")
    assert retry.returncode == 0, retry.stderr


def test_operator_changes_to_managed_compose_are_not_overwritten(env):
    assert bot(env, "compose-image", "--unattended", stdin="").returncode == 0
    app = runtime_app(env)
    (app / "compose.yaml").write_text("unrelated application configuration")
    before = calls(env)
    result = bot(env, "compose-image", "--unattended", stdin="")
    assert result.returncode != 0 and "left unchanged" in result.stderr
    assert (app / "compose.yaml").read_text() == "unrelated application configuration"
    assert " up " not in calls(env)[len(before):]
