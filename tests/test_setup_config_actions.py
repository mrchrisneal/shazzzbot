import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.setup_schema import audit

ROOT = Path(__file__).resolve().parent.parent
BASE = b'config_format = 1\nguild_id = 1\n[[outputs]]\nchannel = 2\n'


@pytest.fixture
def home(tmp_path):
    tmp_path.chmod(0o700)
    app = tmp_path / "app"
    app.mkdir(mode=0o700)
    (app / "config.toml").write_bytes(BASE)
    (app / "config.toml").chmod(0o600)
    (app / ".env").write_text("DISCORD_TOKEN=example-placeholder\n")
    (app / ".env").chmod(0o600)
    return app


def invoke(home, operation, **extra):
    bin_dir = home.parent / "bin"
    bin_dir.mkdir(mode=0o700, exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text('''#!/usr/bin/env python3
import json
import os
import subprocess
import sys

with open(os.environ["EVENTS"], "a") as stream:
    stream.write("docker" + "".join(" <" + arg + ">" for arg in sys.argv[1:]) + "\\n")
if os.environ.get("DOCKER_RUN_FAIL") == "1":
    raise SystemExit(1)
data = json.load(sys.stdin)
with open(os.environ["PAYLOAD"], "w") as stream:
    json.dump(data, stream)
code = sys.argv[-1].replace('"/setup"', repr(os.environ["SETUP_HOME"]))
subprocess.run([sys.executable, "-I", "-B", "-c", code], input=json.dumps(data).encode(), check=True, timeout=10)
''')
    docker.chmod(0o700)
    env = {**os.environ, "APP_DIR": str(home), "SETUP_HOME": str(ROOT), "ROLE": "bot", "METHOD": "compose-image",
           "DRY_RUN": "0", "BACKUP_ROOT": str(home.parent / "backups"), "COMMITTED": "0", "CONFIG_CURRENT": "0",
           "ST_REF": "v1.0.0", "OLD_BOT_IMAGE": "sha256:" + "a" * 64,
           "PROJECT": "shazzzbot", "INSTALL_ID": "a" * 32,
           "SHAZZZBOT_WIZARD_PYTHON": sys.executable, "EVENTS": str(home.parent / "events"),
           "SHAZZZBOT_HELPER_INSTALLING": "0",
           "PAYLOAD": str(home.parent / "payload.json"), "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
           **extra}
    script = r'''
set -euo pipefail
source "$SETUP_HOME/tools/setup_prereqs.sh"
say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
dry() { [ "$DRY_RUN" = 1 ]; }
plan() { printf '[dry-run] %s\n' "$*"; }
bounded() { "$@"; }
journal_fs() {
  python3 -I -B - "$SETUP_HOME" "$@" <<'PYTHON'
import sys
sys.path.insert(0, sys.argv[1])
from tools.setup_manage import manage
manage(sys.argv[2:])
PYTHON
}
mark_mutation() {
  echo backup >> "$EVENTS"
  BACKUP_PATH="$(journal_fs backup "$APP_DIR" "$APP_DIR/helper" "$BACKUP_ROOT" 2)"
  printf '%s\n' "$BACKUP_PATH" > "$BACKUP_ROOT/pending"
}
watch_file() {
  printf 'watch %s\n' "${1##*/}" >> "$EVENTS"
  journal_fs watch "$BACKUP_PATH" app "${1##*/}"
}
seal_file() {
  printf 'seal %s\n' "${1##*/}" >> "$EVENTS"
  journal_fs seal "$BACKUP_PATH" app "${1##*/}"
}
replace_file() { cat > "$1"; }
fs() { printf 'fs %s\n' "$*" >> "$EVENTS"; printf 'current identity\n'; }
docker() {
  printf 'docker' >> "$EVENTS"
  printf ' <%s>' "$@" >> "$EVENTS"
  printf '\n' >> "$EVENTS"
  [ "${DOCKER_FAIL:-0}" != 1 ] || return 1
  if [ "$1" = run ]; then [ "${DOCKER_RUN_FAIL:-0}" != 1 ]; return; fi
  if [ "$1" = ps ]; then
    case "$*" in
      *com.docker.compose.service=docker-proxy*) printf '%s\n' "${PROXY_IDS:-proxy-id}" ;;
      *) printf '%s\n' "${BOT_IDS:-bot-id}" ;;
    esac
  elif [ "$1" = inspect ]; then
    case "$*" in
      *org.shazzzbot.install*) printf '%s\n' "${DOCKER_OWNER:-$INSTALL_ID}" ;;
      *) printf 'sha256:%s\n' aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa ;;
    esac
  fi
}
container() { echo shazzzbot; }
service() { echo shazzzbot; }
compose_file() { echo compose.yaml; }
compose() { printf 'compose %s\n' "$*" >> "$EVENTS"; return 1; }
"$1"
printf 'committed=%s current=%s\n' "$COMMITTED" "$CONFIG_CURRENT"
'''
    return subprocess.run(["bash", "-c", script, "test", operation], env=env, capture_output=True,
                          text=True, check=False, timeout=15)


def events(home):
    path = home.parent / "events"
    return path.read_text() if path.exists() else ""


def test_audit_is_read_only_and_reports_schema_errors(home):
    original = (home / "config.toml").read_bytes()
    result = invoke(home, "audit_configuration")
    assert result.returncode == 0, result.stderr
    assert "+ interval = 10 (commented)" in result.stdout and "current=0" in result.stdout
    assert (home / "config.toml").read_bytes() == original
    assert events(home) == ""
    (home / "config.toml").write_bytes(b'config_format = 1\nguild_id = 1\nintervl = 10\n')
    result = invoke(home, "audit_configuration")
    assert result.returncode == 1 and "did you mean" in result.stdout
    assert events(home) == ""


@pytest.mark.parametrize("case", ["relay", "missing", "untrusted"])
def test_audit_handles_nonbot_missing_and_untrusted_configs(home, case):
    extra = {}
    if case == "relay":
        extra["ROLE"] = "relay"
    elif case == "missing":
        (home / "config.toml").unlink()
    else:
        (home / "config.toml").chmod(0o666)
    result = invoke(home, "audit_configuration", **extra)
    assert result.returncode == (1 if case == "untrusted" else 0)
    assert events(home) == ""


def test_host_migration_uses_backup_then_seals_config_and_identity(home):
    result = invoke(home, "migrate_configuration")
    assert result.returncode == 0, result.stderr
    assert (home / "config.toml").read_bytes() == audit(BASE).candidate
    assert next(home.glob("config.toml.bak-*")).read_bytes() == BASE
    assert (home / ".env").read_text() == "DISCORD_TOKEN=example-placeholder\n"
    logged = events(home)
    assert logged.startswith("backup\nwatch config.toml\nseal config.toml\nwatch .setup-identity\n")
    assert "fs identity-data " in logged and " bot compose-image v1.0.0\n" in logged
    assert logged.endswith("seal .setup-identity\n")
    assert "committed=1" in result.stdout
    assert not (home.parent / "backups" / "pending").exists()
    assert "containers were left unchanged" in result.stdout


@pytest.mark.parametrize("dry", ["0", "1"])
def test_current_or_dry_migration_does_not_touch_files(home, dry):
    if dry == "0":
        (home / "config.toml").write_bytes(audit(BASE).candidate)
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in home.iterdir()}
    result = invoke(home, "migrate_configuration", DRY_RUN=dry)
    assert result.returncode == 0, result.stderr
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in home.iterdir()} == before
    assert events(home) == ""
    assert not (home.parent / "backups").exists()
    assert "[dry-run]" in result.stdout if dry == "1" else "nothing to change" in result.stdout


@pytest.mark.parametrize("case, message", [
    ("relay", "only available for bot"),
    ("missing", "needs existing"),
    ("invalid", "config errors"),
    ("image", "installed bot image locally"),
    ("no-image", "installed bot image locally"),
])
def test_refused_migration_never_starts_a_transaction(home, case, message):
    extra = {}
    if case == "relay":
        extra["ROLE"] = "relay"
    elif case == "missing":
        (home / ".env").unlink()
    elif case == "invalid":
        (home / "config.toml").write_bytes(b"intervl = 10\n")
    else:
        extra.update(SHAZZZBOT_WIZARD_PYTHON="", DOCKER_FAIL="1")
        if case == "no-image":
            extra["OLD_BOT_IMAGE"] = ""
    result = invoke(home, "migrate_configuration", **extra)
    assert result.returncode != 0 and message in result.stderr
    assert "backup" not in events(home)


def test_container_migration_has_no_network_or_pulls_and_uses_existing_image(home):
    os.chown(home / "config.toml", 10001, 10001)
    result = invoke(home, "migrate_configuration", SHAZZZBOT_WIZARD_PYTHON="")
    assert result.returncode == 0, result.stderr
    logged = events(home)
    assert "<image> <inspect> <sha256:" in logged
    assert "<run> <--rm> <-i> <--read-only> <--network> <none>" in logged
    assert "<--cap-drop> <ALL> <--security-opt> <no-new-privileges:true> <--user> <0>" in logged
    assert f"<{ROOT}:/setup:ro>" in logged and f"<{home}:" not in logged
    assert "<--cap-add>" not in logged and "/candidate" not in logged
    assert "example-placeholder" not in logged
    assert (home / "config.toml").read_bytes() == audit(BASE).candidate
    assert (home / "config.toml").stat().st_uid == 10001
    assert next(home.glob("config.toml.bak-*")).read_bytes() == BASE
    assert next(home.glob("config.toml.bak-*")).stat().st_uid == 0
    import json

    payload = json.loads((home.parent / "payload.json").read_text())
    assert payload == {"config": audit(BASE).candidate.decode(), "env": "DISCORD_TOKEN=example-placeholder\n"}
    assert "<pull>" not in logged and "<stop>" not in logged and "<restart>" not in logged


def test_cli_failure_keeps_transaction_pending_and_does_not_refresh_identity(home):
    (home / ".env").write_bytes(b"")
    result = invoke(home, "migrate_configuration")
    assert result.returncode != 0 and "config migration failed" in result.stderr
    assert events(home) == "backup\nwatch config.toml\n"
    assert (home.parent / "backups" / "pending").exists()
    assert not (home / ".setup-identity").exists()
    assert (home / "config.toml").read_bytes() == BASE


def test_container_cli_failure_does_not_report_committed(home):
    result = invoke(home, "migrate_configuration", SHAZZZBOT_WIZARD_PYTHON="", DOCKER_RUN_FAIL="1")
    assert result.returncode != 0 and "config migration failed" in result.stderr
    assert "seal config.toml" not in events(home)
    assert (home.parent / "backups" / "pending").exists()
    assert (home / "config.toml").read_bytes() == BASE


def test_repair_discovers_only_exact_project_service_members_when_compose_is_missing(home):
    result = invoke(home, "capture_images", ACTION="repair", ST_METHOD="compose-image")
    assert result.returncode == 0, result.stderr
    logged = events(home)
    assert ("<label=com.docker.compose.project=shazzzbot> <--filter> "
            "<label=com.docker.compose.service=shazzzbot>") in logged
    assert "<label=com.docker.compose.service=docker-proxy>" in logged
    assert "compose " not in logged
    for name in ("bot-id", "proxy-id"):
        ownership = f'<--format> <{{{{index .Config.Labels "org.shazzzbot.install"}}}}> <{name}>'
        image = f'<--format> <{{{{.Image}}}}> <{name}>'
        assert logged.index(ownership) < logged.index(image)


@pytest.mark.parametrize("case", ["multiple", "unowned", "not-repair", "has-compose"])
def test_partial_container_fallback_refuses_ambiguity_foreign_members_and_nonrepair(home, case):
    extra = {"ACTION": "repair", "ST_METHOD": "compose-image"}
    if case == "multiple":
        extra["BOT_IDS"] = "first\nsecond"
    elif case == "unowned":
        extra["DOCKER_OWNER"] = "b" * 32
    elif case == "not-repair":
        extra["ACTION"] = "upgrade"
    else:
        (home / "compose.yaml").write_text("owned compose file\n")
    result = invoke(home, "capture_images", **extra)
    assert result.returncode != 0
    expected = "multiple containers" if case == "multiple" else \
        "not owned by this install" if case == "unowned" else "cannot identify current service"
    assert expected in result.stderr
    assert "<{{.Image}}>" not in events(home)
