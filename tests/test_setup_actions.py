import re
import stat
from pathlib import Path

from shazzzbot import __version__
from tests import test_setup_sh as installer

env = installer.env


def tree(values):
    files = {}
    for name in ("SHAZZZBOT_APP_DIR", "SHAZZZBOT_BACKUP_DIR", "SHAZZZBOT_LOCK_FILE"):
        root = Path(values[name])
        for path in [root, *root.rglob("*")] if root.is_dir() else [root]:
            if path.exists():
                info = path.stat()
                files[str(path)] = (stat.S_IMODE(info.st_mode), info.st_mtime_ns,
                                    path.read_bytes() if path.is_file() else None)
    return files


def installed(values):
    result = installer.bot(values, "compose-image")
    assert result.returncode == 0, result.stderr
    return installer.runtime_app(values)


def assert_no_deploy(log):
    assert not re.search(r"^(?:run|pull|build|rm|network create|compose .*\b(?:up|pull|rm)\b)", log, re.MULTILINE)


def test_keep_and_default_current_choice_are_read_only(env):
    installed(env)
    before = tree(env)
    for args, answers in ((["--action", "keep", "--yes"], ""), ([], "1\n"), ([], "")):
        previous = installer.calls(env)
        result = installer.bot(env, "compose-image", *args, stdin=answers)
        assert result.returncode == 0, result.stderr
        assert "nothing to change" in result.stdout or "installation unchanged" in result.stdout
        assert tree(env) == before
        assert_no_deploy(installer.calls(env)[len(previous):])


def test_reinstall_and_menu_upgrade_need_confirmation_before_changes(env):
    installed(env)
    before = tree(env)
    for args, answers in ((["--action", "reinstall"], "n\n"), ([], "2\nn\n")):
        previous = installer.calls(env)
        result = installer.bot(env, "compose-image", *args, stdin=answers)
        assert result.returncode == 0 and "installation unchanged" in result.stdout, result.stderr
        assert tree(env) == before
        assert_no_deploy(installer.calls(env)[len(previous):])


def test_explicit_keep_of_an_unready_install_never_repairs_it(env):
    installed(env)
    Path(env["STUB_ROOT"], "not_ready").touch()
    before = tree(env)
    previous = installer.calls(env)
    result = installer.bot(env, "compose-image", "--action", "keep", "--yes", stdin="")
    assert result.returncode == 0 and "installation unchanged" in result.stdout, result.stderr
    assert "readiness check failed" in result.stdout
    assert tree(env) == before
    assert_no_deploy(installer.calls(env)[len(previous):])


def test_all_mutating_actions_dry_run_leave_the_existing_install_unchanged(env):
    installed(env)
    before = tree(env)
    for action in ("upgrade", "migrate", "repair", "reinstall"):
        previous = installer.calls(env)
        result = installer.bot(env, "compose-image", "--action", action, "--dry-run", stdin="")
        assert result.returncode == 0, result.stderr
        assert "dry-run" in result.stdout
        assert tree(env) == before
        assert_no_deploy(installer.calls(env)[len(previous):])


def test_confirmed_migration_keeps_existing_bytes_and_secrets_without_restarting(env):
    app = installed(env)
    config = app / "config.toml"
    original = config.read_bytes()
    secrets = (app / ".env").read_bytes()
    previous = installer.calls(env)
    result = installer.bot(env, "compose-image", "--action", "migrate", "--yes", stdin="")
    assert result.returncode == 0, result.stderr
    assert config.read_bytes().startswith(original)
    assert config.read_bytes() != original
    backup = app / f"config.toml.bak-{__version__}"
    assert backup.read_bytes() == original
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert (app / ".env").read_bytes() == secrets
    assert_no_deploy(installer.calls(env)[len(previous):])
    before = tree(env)
    result = installer.bot(env, "compose-image", "--action", "migrate", "--yes", stdin="")
    assert result.returncode == 0 and "current" in result.stdout, result.stderr
    assert tree(env) == before


def test_repair_recreates_a_missing_proxy_file_without_changing_user_data(env):
    app = installed(env)
    config = (app / "config.toml").read_bytes()
    secrets = (app / ".env").read_bytes()
    data = app / "data" / "keep.txt"
    data.write_text("operator data")
    proxy = app / "docker-proxy" / "haproxy.cfg"
    proxy.unlink()
    result = installer.bot(env, "compose-image", "--action", "repair", "--yes", stdin="")
    assert result.returncode == 0, result.stderr
    assert proxy.read_bytes() == (installer.ROOT / "docker-proxy/haproxy.cfg").read_bytes()
    assert (app / "config.toml").read_bytes() == config
    assert (app / ".env").read_bytes() == secrets
    assert data.read_text() == "operator data"


def test_reinstall_recreates_only_owned_services_and_preserves_settings_and_data(env):
    app = installed(env)
    config = (app / "config.toml").read_bytes()
    secrets = (app / ".env").read_bytes()
    data = app / "data" / "keep.txt"
    data.write_text("operator data")
    previous = installer.calls(env)
    result = installer.bot(env, "compose-image", "--action", "reinstall", "--yes", stdin="")
    assert result.returncode == 0, result.stderr
    log = installer.calls(env)[len(previous):]
    assert "up -d --no-build --force-recreate" in log
    assert not re.search(r"^(?:rm|volume|network rm|system prune|image prune)\b", log, re.MULTILINE)
    assert (app / "config.toml").read_bytes() == config
    assert (app / ".env").read_bytes() == secrets
    assert data.read_text() == "operator data"
