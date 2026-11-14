import hashlib
import json
import os
import pty
import re
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "setup.sh"
SCRIPTS = (SCRIPT, *sorted((ROOT / "tools").glob("setup_*.sh")))
TOKEN = "MTAxMjM0NTY3ODkwMTIzNDU2Nzg5.GaBcDe.abcdefghijklmnopqrstuvwxyz0123456789"
GUILD, STATUS, AUDIT, COMMANDS = "123456789012345678", "223456789012345678", "323456789012345678", "423456789012345678"
APP_ID = "523456789012345678"
SOURCE_FILES = ("VERSION", ".env.example", "config.example.toml", "compose.yaml", "compose.relay.yaml",
                "helper.example.json",
                "docker-proxy/haproxy.cfg", "tools/setup_config.py", "shazzzbot-helper-unraid.py",
                *(str(path.relative_to(ROOT)) for path in sorted((ROOT / "shazzzbot/helper").rglob("*.py"))))
# Continue, relay URL, icon, time zone, UPS, service, forge; then "start now"
BOT_ANSWERS = "y\n\nn\n\n\n\n\ny\n"
RELAY_ANSWERS = "y\n\ny\n"
RELAY_LOG = "relay listening on :8443\nRELAY_URL=https://addr:8443\nRELAY_KEY=k3y\nRELAY_FINGERPRINT=" + "a" * 64 + "\n"

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("bash", "flock", "df", "mktemp", "find", "sort", "tar")),
    reason="setup.sh needs bash, flock, df, mktemp, find, sort, and tar")

DOCKER_STUB = r"""#!/bin/bash
printf '%s\n' "$*" >> "$STUB_LOG"
s="$STUB_ROOT"
printf '%s|%s|%s\n' "${DOCKER_HOST:-}" "${DOCKER_CONTEXT:-unset}" "${COMPOSE_REMOVE_ORPHANS:-unset}" >> "$s/daemon.env"
last="${*: -1}"
case "$1" in
  ps)
    if [ "$#" -eq 2 ] && [ "$2" = -aq ]; then
      [ ! -f "$s/run_shazzzbot" ] || echo aaaaaaaaaaaa
      if [ -f "$s/run_docker-proxy" ] || [ -f "$s/run_shazzzbot-proxy" ]; then echo bbbbbbbbbbbb; fi
      if [ -f "$s/run_relay" ] || [ -f "$s/run_shazzzbot-relay" ]; then echo cccccccccccc; fi
    fi ;;
  info) [ "${2:-}" = --format ] && echo "$s"; exit 0 ;;
  exec) [ ! -f "$s/not_ready" ]; exit ;;
  image)
    [ "$2" != ls ] || exit 0
    [ -f "$s/image" ] || exit 1
    if [[ "$*" == *RepoDigests* ]]; then
      echo "ghcr.io/owner/project@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"; exit 0
    fi
    if [[ "$*" == *"{{.Id}}"* ]]; then
      echo sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa; exit 0
    fi
    [[ "$*" != *--format* ]] || printf '%s\n' "${SHAZZZBOT_INSTALL_ID:-$SHAZZZBOT_APP_DIR}"
    exit 0 ;;
  build) touch "$s/image"; echo sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa ;;
  pull) touch "$s/image" ;;
  compose)
    if [[ " $* " != *" version "* ]]; then
      file=""; previous=""
      for argument in "$@"; do
        if [ "$previous" = -f ]; then file="$argument"; break; fi
        previous="$argument"
      done
      [ -f "$file" ] || { echo 'Compose file is missing or not a regular file' >&2; exit 1; }
    fi
    case " $* " in
      *" version "*) echo 2.29.1 ;;
      *" ps --format "*) cat "$s/state" 2>/dev/null ;;
      *" ps -q "*) [ ! -f "$s/run_$last" ] || echo "$last" ;;
      *" logs "*) cat "$s/logs" ;;
      *" rm "*)
        for service in shazzzbot docker-proxy relay; do
          [[ " $* " != *" $service "* ]] || rm -f "$s/run_$service"
        done
        rm -f "$s/state" ;;
      *" up "*)
        n=0; [ ! -f "$s/ups" ] || n="$(cat "$s/ups")"; echo "$((n + 1))" > "$s/ups"
        [ ! -f "$s/fail_up" ] || { rm "$s/fail_up"; exit 1; }
        [ ! -f "$s/not_ready" ] || { [ "$n" -lt 1 ] || rm "$s/not_ready"; }
        echo running > "$s/state"
        awk '$1 == "image:" {print $2; exit}' "$file" > "$s/running-image"
        if awk '/^  relay:/ {found=1} END {exit !found}' "$file"; then touch "$s/run_relay"
        else touch "$s/run_shazzzbot" "$s/run_docker-proxy"; fi
        printf '%s\n' "${SHAZZZBOT_INSTALL_ID:-$SHAZZZBOT_APP_DIR}" > "$s/owner" ;;
      *" exec "*) [ ! -f "$s/not_ready" ]; exit ;;
    esac ;;
  run)
    prev=""; owned=0
    for a in "$@"; do
      if [ "$prev" = --name ]; then
        touch "$s/run_$a"; printf '%s\n' "${SHAZZZBOT_INSTALL_ID:-$SHAZZZBOT_APP_DIR}" > "$s/owner"; owned=1
      fi
      [[ "$a" != org.shazzzbot.install=* ]] || printf '%s\n' "${a#*=}" > "$s/owner"
      prev="$a"
    done
    [ "$owned" = 0 ] || printf '%s\n' "$last" > "$s/running-image"
    echo id ;;
  inspect)
    if [[ "$*" == *'{{json .Mounts}}'* ]]; then
      python3 - "$s" "$SHAZZZBOT_APP_DIR" "$@" <<'PY'
import json
import sys
from pathlib import Path
root, app = map(Path, sys.argv[1:3])
owner = (root / "owner").read_text().strip() if (root / "owner").is_file() else ""
state = json.loads((app / ".setup-state").read_text()) if (app / ".setup-state").is_file() else {}
version = state.get("ref", "") if state.get("ref", "").startswith("v") else ""
image = ((root / "running-image").read_text().strip() if (root / "running-image").is_file()
         else "ghcr.io/mrchrisneal/shazzzbot:v1.2.3")
for cid in sys.argv[3:]:
    service = {"aaaaaaaaaaaa": "shazzzbot", "bbbbbbbbbbbb": "docker-proxy", "cccccccccccc": "relay"}.get(cid)
    if service is None:
        continue
    labels = {"org.shazzzbot.install": owner, "org.opencontainers.image.version": version}
    if service != "docker-proxy":
        labels["org.opencontainers.image.source"] = "https://github.com/mrchrisneal/shazzzbot"
    if (app / "compose.yaml").is_file() or (app / "compose.relay.yaml").is_file():
        labels.update({"com.docker.compose.project": "shazzzbot_relay" if service == "relay" else "shazzzbot",
                       "com.docker.compose.project.working_dir": str(app), "com.docker.compose.service": service})
    elif service == "docker-proxy":
        labels["com.docker.compose.service"] = service
    print(json.dumps({"id": cid, "name": "/" + service, "image": "haproxy:3" if service == "docker-proxy" else image,
                      "labels": labels, "mounts": [{"Source": str(app), "Destination": "/config"}], "running": True}))
PY
      exit 0
    fi
    if [[ "$*" == *org.shazzzbot.install* ]]; then cat "$s/owner" 2>/dev/null
    elif [[ "$*" == *"{{.Config.Image}}"* ]]; then cat "$s/running-image"
    elif [[ "$*" == *"{{.Image}}"* ]]; then echo sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    else [ -f "$s/run_$last" ] && echo true; fi
    exit 0 ;;
  container) [ -f "$s/run_$last" ]; exit ;;
  rm) rm -f "$s/run_$last" ;;
  logs) cat "$s/logs" ;;
  network)
    if [ "$2" = inspect ]; then
      [ -f "$s/net" ] || exit 1
      if [[ "$*" == *--format* ]]; then
        if [ -f "$s/net-owner" ]; then cat "$s/net-owner"; else printf '%s\n' "$SHAZZZBOT_INSTALL_ID"; fi
      fi
    else touch "$s/net"; printf '%s\n' "$SHAZZZBOT_INSTALL_ID" > "$s/net-owner"; fi ;;
esac
exit 0
"""

GIT_STUB = r"""#!/bin/bash
printf '%s\n' "$*" >> "$STUB_LOG"
if [ "$1" = --version ]; then
  if [ -f "$STUB_ROOT/git_fails" ]; then rm "$STUB_ROOT/git_fails"; exit 1; fi
  echo git version 2.45.0; exit 0
fi
if [ "$1" = clone ]; then
  target="${*: -1}"
  cp -a "$STUB_CLONE_SOURCE/." "$target/"
  exit 0
fi
while [ "$1" = -c ]; do shift 2; done
[ "$1" = -C ] || exit 1
dir="$2"; shift 2
case "$1" in
  status) [ "${GIT_OPTIONAL_LOCKS:-}" = 0 ] || touch "$dir/.git/index" ;;
  config) cat "$dir/.git/origin" ;;
  ls-files) cat "$dir/.git/tracked" ;;
  rev-parse) cat "$dir/.git/HEAD_SHA" ;;
  *) exit 1 ;;
esac
"""

CURL_STUB = r"""#!/bin/bash
printf 'curl %s\n' "${*: -1}" >> "$STUB_LOG"
case " $* " in
  *" -w "*) printf 200 ;;
  *api.test*) printf '{"tag_name": "%s"}' "$(cat "$STUB_ROOT/tag")" ;;
  *code.test*)
    u="${*: -1}"; tag="$(basename "$(dirname "$u")")"
    output=""; prev=""
    for arg in "$@"; do [ "$prev" != -o ] || output="$arg"; prev="$arg"; done
    case "$u" in
      */release.json) file="$STUB_ROOT/manifest-$tag.json" ;;
      *) file="$STUB_ROOT/src-$tag.tar.gz" ;;
    esac
    if [ -n "$output" ]; then cat "$file" > "$output"; else cat "$file"; fi ;;
esac
"""


def private_archive_mode(info):
    info.mode &= ~0o022
    return info


def source_tar(tmp_path, tag):
    with tarfile.open(tmp_path / f"src-{tag}.tar.gz", "w:gz") as tar:
        for name in SOURCE_FILES:
            tar.add(ROOT / name, arcname=f"shazzzbot-{tag[1:]}/{name}", filter=private_archive_mode)
    image = "ghcr.io/mrchrisneal/shazzzbot:" + tag + "@sha256:" + hashlib.sha256(tag.encode()).hexdigest()
    (tmp_path / f"manifest-{tag}.json").write_text(json.dumps({
        "format": 1, "version": tag, "revision": "a" * 40,
        "source_sha256": hashlib.sha256((tmp_path / f"src-{tag}.tar.gz").read_bytes()).hexdigest(),
        "helper_sha256": "c" * 64, "image": image, "platforms": {"unraid": "d" * 64}}))
    (tmp_path / "image-pin").write_text(image)


@pytest.fixture
def env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", DOCKER_STUB), ("git", GIT_STUB), ("curl", CURL_STUB)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    bundle = tmp_path / "bundle"
    (bundle / "tools").mkdir(parents=True)
    for file in ("setup.sh", *(str(path.relative_to(ROOT)) for path in sorted((ROOT / "tools").glob("setup_*"))),
                 "shazzzbot-helper-unraid.py", "tools/build_helper.py", "tools/release_check.py", "VERSION",
                 "tools/__init__.py"):
        shutil.copy(ROOT / file, bundle / file)
        (bundle / file).chmod(0o644)
    (bundle / "shazzzbot").mkdir()
    shutil.copy(ROOT / "shazzzbot/__init__.py", bundle / "shazzzbot/__init__.py")
    shutil.copy(ROOT / "shazzzbot/schema.py", bundle / "shazzzbot/schema.py")
    shutil.copytree(ROOT / "shazzzbot/helper", bundle / "shazzzbot/helper")
    for path in (bundle / "shazzzbot").rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    (tmp_path / "tag").write_text("v1.2.3")
    source_tar(tmp_path, "v1.2.3")
    (tmp_path / "logs").write_text("logged in as test\n")
    (tmp_path / "unraid-version").write_text('version="7.0.0"\n')
    (tmp_path / "machine-id").write_text("1" * 32)
    (tmp_path / "plugins").mkdir()
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(tmp_path / "docker.sock"))
    sock.close()
    values = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STUB_LOG": str(tmp_path / "calls.log"),
        "STUB_ROOT": str(tmp_path),
        "STUB_CLONE_SOURCE": str(tmp_path / "remote-repo"),
        "SHAZZZBOT_APP_DIR": str(tmp_path / "app"),
        "SHAZZZBOT_HELPER_DIR": str(tmp_path / "plugins" / "shazzzbot-helper"),
        "SHAZZZBOT_BACKUP_DIR": str(tmp_path / "backups"),
        "SHAZZZBOT_LOCK_FILE": str(tmp_path / "setup.lock"),
        "SHAZZZBOT_UNRAID_MARKER": str(tmp_path / "unraid-version"),
        "SHAZZZBOT_MACHINE_ID": str(tmp_path / "machine-id"),
        "SHAZZZBOT_DOCKER_SOCK": str(tmp_path / "docker.sock"),
        "SHAZZZBOT_RELEASE_API": "https://api.test/latest",
        "SHAZZZBOT_SOURCE_URL": "https://code.test/tags",
        "SHAZZZBOT_MIN_FREE_MB": "1",
        "SHAZZZBOT_REQUIRE_ROOT": "0",
        "SHAZZZBOT_HEALTH_TIMEOUT": "0",
        "SHAZZZBOT_HEALTH_POLL": "1",
        "SHAZZZBOT_WIZARD_PYTHON": sys.executable,
        "SHAZZZBOT_WIZARD_APP": str(ROOT),
        "SHAZZZBOT_DISCORD_TOKEN": TOKEN,
        "SHAZZZBOT_APP_ID": APP_ID,
        "SHAZZZBOT_GUILD_ID": GUILD,
        "SHAZZZBOT_STATUS_CHANNEL_ID": STATUS,
        "SHAZZZBOT_AUDIT_CHANNEL_ID": AUDIT,
        "SHAZZZBOT_COMMANDS_CHANNEL_ID": COMMANDS,
    }
    checkout(values)
    yield values


def checkout(env):
    app = Path(env["STUB_CLONE_SOURCE"])
    if (app / ".git").exists():
        return app
    for name in SOURCE_FILES:
        (app / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / name, app / name)
        (app / name).chmod(0o644)
    (app / ".git").mkdir()
    (app / ".git" / "origin").write_text("https://github.com/mrchrisneal/shazzzbot.git")
    (app / ".git" / "HEAD_SHA").write_text("a" * 40 + "\n")
    (app / ".git" / "HEAD").write_text("a" * 40 + "\n")
    (app / ".git" / "config").write_text('[remote "origin"]\nurl = https://github.com/mrchrisneal/shazzzbot.git\n')
    (app / ".git" / "tracked").write_bytes("\0".join(SOURCE_FILES).encode() + b"\0")
    return app


def run(env, *args, stdin=""):
    script = Path(env["STUB_ROOT"]) / "bundle" / "setup.sh"
    result = subprocess.run(["bash", str(script), *args], env=env, input=stdin, capture_output=True,
                            text=True, timeout=90, check=False)
    assert TOKEN not in result.stdout + result.stderr
    assert TOKEN not in calls(env)
    return result


def calls(env):
    log = Path(env["STUB_LOG"])
    return log.read_text() if log.exists() else ""


def app_file(env, name):
    return (runtime_app(env) / name).read_text()


def runtime_app(env):
    return Path(env["SHAZZZBOT_APP_DIR"])


def runtime_metadata(env):
    app = runtime_app(env)
    saved = app / ".setup-identity"
    if not saved.exists():
        saved = Path(env["SHAZZZBOT_BACKUP_DIR"]) / "install-identity.json"
    identifier = json.loads(saved.read_bytes())["install_id"]
    repository = "shazzzbot-" + hashlib.sha256(identifier.encode()).hexdigest()[:16]
    return {"dev_image": repository + ":dev", "prev_image": repository + ":prev"}


def backups(env):
    root = Path(env["SHAZZZBOT_BACKUP_DIR"])
    return sorted(path for path in root.iterdir() if path.is_dir()) if root.exists() else []


def bot(env, method, *extra, stdin=BOT_ANSWERS):
    return run(env, "--role", "bot", "--method", method, "--no-helper", *extra, stdin=stdin)


def test_help_and_usage_errors(env):
    result = run(env, "--help")
    assert result.returncode == 0
    for flag in ("--role", "--method", "--version", "--upgrade", "--helper", "--no-helper", "--unattended",
                 "--dry-run", "--dir", "--keep", "--set-icon", "--relay-allow", "--help"):
        assert flag in result.stdout
    for args in (["--bogus"], ["--keep", "0"], ["--role", "x"], ["--method", "x"], ["--version", "1.0"],
                 ["--dir", "rel"]):
        assert run(env, *args).returncode == 2


def test_dry_run_changes_nothing(env):
    result = bot(env, "compose-image", "--dry-run", stdin="")
    assert result.returncode == 0, result.stderr
    assert "[dry-run]" in result.stdout
    assert not runtime_app(env).exists()
    assert not re.search(r"^(compose (up|pull)|run|pull)", calls(env), re.MULTILINE)
    assert not backups(env)
    assert not Path(env["SHAZZZBOT_LOCK_FILE"]).exists()


def test_table_lists_every_prerequisite(env):
    result = bot(env, "compose-image", "--dry-run", stdin="")
    for label in ("running as root", "Unraid", "flock", "Docker engine running", "docker compose v2", "git",
                  "curl", "tar", "github.com", "ghcr.io", "free space", "install directory writable",
                  "Docker socket", "Discord token and ids", "python3 3.11", "User Scripts plugin",
                  "flash drive writable"):
        assert label in result.stdout, label


def test_compose_image_setup_is_idempotent(env):
    first = bot(env, "compose-image")
    assert first.returncode == 0, first.stderr
    app = runtime_app(env)
    for name in (".env", "config.toml"):
        assert stat.S_IMODE((app / name).stat().st_mode) == 0o600
    from tools.setup_config import dotenv

    assert dotenv(app_file(env, ".env"))["DISCORD_TOKEN"] == TOKEN
    config = app_file(env, "config.toml")
    assert f"guild_id = {GUILD}\n" in config and f"\nchannel = {STATUS}\n" in config
    assert f"audit_channel_id = {AUDIT}\n" in config and f"commands_channel_id = {COMMANDS}\n" in config
    compose = app_file(env, "compose.yaml")
    assert "image: ghcr.io/mrchrisneal/shazzzbot:v1.2.3" in compose and "build: ." not in compose
    assert app_file(env, "docker-proxy/haproxy.cfg") == (ROOT / "docker-proxy/haproxy.cfg").read_text()
    assert json.loads(app_file(env, ".setup-state")) == {
        "format": 1, "method": "compose-image", "role": "bot", "ref": "v1.2.3"}
    assert f"https://discord.com/channels/{GUILD}/{STATUS}" in first.stderr
    assert f"client_id={APP_ID}" in first.stderr and "kb" not in first.stderr.lower()
    assert re.search(r"^compose .* -p shazzzbot -f .* up -d --no-build$", calls(env), re.MULTILINE)
    before, made = calls(env), backups(env)
    second = bot(env, "compose-image", stdin="")
    assert second.returncode == 0, second.stderr
    assert "nothing to change" in second.stdout
    assert backups(env) == made
    assert not re.search(r"^compose .* up ", calls(env)[len(before):], re.MULTILINE)
    assert not list(app.glob(".setup.*"))


def test_compose_build_uses_the_checkout(env):
    template = checkout(env)
    result = bot(env, "compose-build")
    assert result.returncode == 0, result.stderr
    assert re.search(r"^build -q --label .* \.$", calls(env), re.MULTILINE)
    metadata = runtime_metadata(env)
    assert "tag sha256:" + "a" * 64 + " " + metadata["dev_image"] in calls(env)
    assert not (runtime_app(env) / ".git").exists()
    assert not (runtime_app(env) / "source").exists()
    clone = next(line for line in calls(env).splitlines() if line.startswith("clone "))
    source = Path(clone.split()[-1])
    assert source.parent.parent == Path("/tmp/shazzzbot-helper") and not source.exists()
    assert source != template
    assert (template / ".git/HEAD_SHA").read_text().strip() == "a" * 40
    assert json.loads((runtime_app(env) / ".setup-state").read_text()) == {
        "format": 1, "method": "compose-build", "role": "bot", "ref": "a" * 40}
    before, saved = calls(env), backups(env)
    same = bot(env, "compose-build", "--unattended", stdin="")
    assert same.returncode == 0, same.stderr
    assert "nothing to change" in same.stdout and backups(env) == saved
    assert not re.search(r"^build ", calls(env)[len(before):], re.MULTILINE)


def test_docker_run_starts_proxy_and_bot_without_compose(env):
    result = bot(env, "docker-run")
    assert result.returncode == 0, result.stderr
    log = calls(env)
    assert "network create --label" in log and "compose" not in log.replace("compose version", "")
    proxy = next(line for line in log.splitlines() if "--name shazzzbot-proxy" in line)
    assert "--network-alias docker-proxy" in proxy and "haproxy.cfg:ro" in proxy and "@sha256:" in proxy
    bot_line = next(line for line in log.splitlines() if "--name shazzzbot " in line)
    assert "--read-only" in bot_line and "--cap-drop ALL" in bot_line
    assert "ghcr.io/mrchrisneal/shazzzbot:v1.2.3" in bot_line
    assert (runtime_app(env) / "data").is_dir()


def test_bot_only_runs_no_proxy_and_needs_no_socket(env):
    os.unlink(env["SHAZZZBOT_DOCKER_SOCK"])
    result = bot(env, "bot-only")
    assert result.returncode == 0, result.stderr
    assert "shazzzbot-proxy" not in calls(env)
    assert not (runtime_app(env) / "docker-proxy").exists()
    assert '# docker_proxy = "http://docker-proxy:2375"' in app_file(env, "config.toml")
    assert bot(env, "docker-run", "--unattended").returncode == 1


@pytest.mark.parametrize("method", ["compose-image", "docker-run"])
def test_relay_prints_the_bot_lines(env, method):
    Path(env["STUB_ROOT"], "logs").write_text(RELAY_LOG)
    result = run(env, "--role", "relay", "--method", method, "--relay-allow", "example.com,192.0.2.1",
                 stdin=RELAY_ANSWERS)
    assert result.returncode == 0, result.stderr
    assert "RELAY_URL=https://addr:8443" in result.stdout and "RELAY_FINGERPRINT=" in result.stdout
    assert app_file(env, ".env").splitlines()[1] == "RELAY_ALLOW=example.com 192.0.2.1"
    assert not (runtime_app(env) / "config.toml").exists()
    if method == "compose-image":
        assert "8443" in app_file(env, "compose.yaml")
    else:
        assert "-p 0.0.0.0:8443:8443" in calls(env)


def test_menu_offers_only_working_methods_and_rechecks(env):
    Path(env["STUB_ROOT"], "git_fails").touch()
    result = run(env, "--role", "bot", "--no-helper", "--dry-run", stdin="r\n2\n")
    assert result.returncode == 0, result.stderr
    first, second = result.stdout.split("Install methods:")[1:3]
    assert "-  compose-build (needs: git)" in first
    assert "2) compose-build" in second
    assert "MISSING  git" in result.stdout and "fix: Unraid 7 includes git" in result.stdout
    assert "method compose-build" in result.stdout


def test_cancel_and_unattended_need_a_method(env):
    result = run(env, "--role", "bot", "--no-helper", stdin="y\nc\n")
    assert result.returncode == 1 and "setup cancelled" in result.stderr
    result = run(env, "--role", "bot", "--no-helper", "--unattended")
    assert result.returncode == 1 and "--unattended needs --method" in result.stderr
    Path(env["STUB_ROOT"], "git_fails").touch()
    result = bot(env, "compose-build", "--unattended", stdin="")
    assert result.returncode == 1 and "missing prerequisites for compose-build: git" in result.stderr


def test_unattended_setup_takes_values_from_the_environment(env):
    result = bot(env, "docker-run", "--unattended", "--timezone", "America/Los_Angeles", stdin="")
    assert result.returncode == 0, result.stderr
    assert 'timezone = "America/Los_Angeles"' in app_file(env, "config.toml")
    missing = {**env, "SHAZZZBOT_APP_DIR": env["SHAZZZBOT_APP_DIR"] + "2", "SHAZZZBOT_GUILD_ID": ""}
    result = bot(missing, "docker-run", "--unattended", stdin="")
    assert result.returncode == 1 and "Discord token and ids supplied" in result.stdout


def test_bad_ids_are_refused(env):
    result = bot({**env, "SHAZZZBOT_GUILD_ID": "12"}, "compose-image")
    assert result.returncode == 1
    assert not (runtime_app(env) / "config.toml").exists()
    assert not list(runtime_app(env).glob(".setup.*"))


def test_helper_prerequisites_offer_skip(env):
    shutil.rmtree(Path(env["STUB_ROOT"], "plugins"))
    result = run(env, "--role", "bot", "--method", "compose-image", "--dry-run", stdin="s\n")
    assert result.returncode == 0, result.stderr
    assert "MISSING  User Scripts plugin" in result.stdout and "helper no" in result.stdout


def test_image_upgrade_backs_up_keeps_config_and_rolls_back(env):
    assert bot(env, "compose-image").returncode == 0
    original = app_file(env, ".env"), app_file(env, "config.toml")
    for n, tag in enumerate(["v1.2.4", "v1.2.5", "v1.2.6"]):
        source_tar(Path(env["STUB_ROOT"]), tag)
        Path(env["STUB_ROOT"], "tag").write_text(tag)
        result = run(env, "--upgrade", "--unattended", "--keep", "2")
        assert result.returncode == 0, result.stderr
        assert f"shazzzbot:{tag}" in app_file(env, "compose.yaml")
        assert (app_file(env, ".env"), app_file(env, "config.toml")) == original
        assert len(backups(env)) == min(n + 2, 2)
    for backup in backups(env):
        assert stat.S_IMODE(backup.stat().st_mode) == 0o700
        for path in (backup / "app").rglob("*"):
            assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
    Path(env["STUB_ROOT"], "not_ready").touch()
    source_tar(Path(env["STUB_ROOT"]), "v1.3.0")
    Path(env["STUB_ROOT"], "tag").write_text("v1.3.0")
    Path(env["STUB_ROOT"], "ups").write_text("0")
    result = run(env, "--upgrade", "--unattended")
    assert result.returncode == 1 and "previous version runs again" in result.stderr
    assert "    image: sha256:" + "a" * 64 + "\n" in app_file(env, "compose.yaml")
    assert json.loads(app_file(env, ".setup-state")) == {
        "format": 1, "method": "compose-image", "role": "bot", "ref": "v1.2.6"}


def test_compose_build_upgrade_failure_rolls_back(env):
    checkout(env)
    assert bot(env, "compose-build").returncode == 0
    Path(env["STUB_ROOT"], "not_ready").touch()
    Path(env["STUB_ROOT"], "ups").write_text("0")
    (checkout(env) / ".git/HEAD_SHA").write_text("e" * 40)
    result = run(env, "--upgrade", "--unattended")
    assert result.returncode == 1 and "previous version runs again" in result.stderr
    metadata = runtime_metadata(env)
    assert f"tag {metadata['prev_image']} {metadata['dev_image']}" in calls(env)


def test_method_switch_is_refused(env):
    assert bot(env, "compose-image").returncode == 0
    result = bot(env, "docker-run", stdin="")
    assert result.returncode == 1 and "To switch, stop it first" in result.stderr


def test_lock_blocks_a_second_run(env):
    import fcntl

    with open(env["SHAZZZBOT_LOCK_FILE"], "a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = bot(env, "compose-image", "--unattended")
    assert result.returncode == 1 and "another setup.sh run" in result.stderr


@pytest.mark.parametrize("path", SCRIPTS)
def test_scripts_never_touch_power_array_or_prune(path):
    text = path.read_text()
    assert not re.search(r"\b(reboot|poweroff|halt|mdcmd|emcmd|shutdown|prune|rmi)\b", text)
    assert not re.search(r"\b(volume|image) rm\b|\beval\b|curl[^\n]*\|\s*(ba)?sh", text)
    assert text.isascii()


def test_upgrade_keeps_the_recorded_role(env):
    Path(env["STUB_ROOT"], "logs").write_text(RELAY_LOG)
    assert run(env, "--role", "relay", "--method", "docker-run", "--relay-allow", "example.com",
               stdin=RELAY_ANSWERS).returncode == 0
    assert json.loads(app_file(env, ".setup-state")) == {
        "format": 1, "method": "docker-run", "role": "relay", "ref": "v1.2.3"}
    result = run(env, "--upgrade", "--role", "bot", "--unattended")
    assert result.returncode == 1 and "holds a relay install, not bot" in result.stderr
    source_tar(Path(env["STUB_ROOT"]), "v1.2.4")
    Path(env["STUB_ROOT"], "tag").write_text("v1.2.4")
    result = run(env, "--upgrade", "--unattended")
    assert result.returncode == 0, result.stderr
    assert "relay node, method docker-run" in result.stdout
    assert json.loads(app_file(env, ".setup-state")) == {
        "format": 1, "method": "docker-run", "role": "relay", "ref": "v1.2.4"}
    assert "--name shazzzbot-relay" in calls(env).split("v1.2.3")[-1]


def test_relay_default_directory_is_explicit_private_storage(env):
    home = {key: value for key, value in env.items() if key != "SHAZZZBOT_APP_DIR"}
    home.update(SHAZZZBOT_UNRAID_MARKER=env["STUB_ROOT"] + "/no-unraid")
    result = run(home, "--role", "relay", "--method", "docker-run", "--relay-allow", "example.com",
                 "--dry-run", "--unattended")
    assert result.returncode == 0, result.stderr
    assert "install directory /var/lib/shazzzbot-relay" in result.stdout
    assert not re.search(r"^(run|pull|rm) ", calls(env), re.MULTILINE)


@pytest.mark.parametrize("unsafe", ["owner", "write", "symlink", "ancestor", "secret"])
def test_untrusted_paths_refuse_before_mutation(env, unsafe):
    if unsafe == "ancestor":
        parent = Path(env["STUB_ROOT"]) / "private"
        parent.mkdir(mode=0o700)
        env = {**env, "SHAZZZBOT_APP_DIR": str(parent / "app")}
    assert bot(env, "compose-image", "--unattended", stdin="").returncode == 0
    app = runtime_app(env)
    before, saved = calls(env), backups(env)
    if unsafe == "owner":
        if os.geteuid() != 0:
            pytest.skip("requires chown")
        os.chown(app, 10001, -1)
    elif unsafe == "write":
        app.chmod(0o777)
    elif unsafe == "symlink":
        app.rename(app.with_name("original-app"))
        app.symlink_to(Path(env["STUB_ROOT"]), target_is_directory=True)
    elif unsafe == "ancestor":
        app.parent.chmod(0o777)
    else:
        (app / ".env").write_text("DISCORD_TOKEN=private")
        (app / ".env").chmod(0o644)
    result = bot(env, "compose-image", "--unattended")
    assert result.returncode == 1 and "untrusted" in result.stderr
    assert not re.search(r"^(pull|run|rm|compose .* up) ", calls(env)[len(before):], re.MULTILINE)
    assert backups(env) == saved


def test_compose_uses_only_explicit_project_file_and_env(env):
    result = bot({**env, "COMPOSE_FILE": "/evil.yaml", "COMPOSE_PROJECT_NAME": "other",
                  "COMPOSE_REMOVE_ORPHANS": "1", "COMPOSE_PROFILES": "foreign",
                  "DOCKER_HOST": "tcp://192.0.2.1:2375", "DOCKER_CONTEXT": "foreign"}, "compose-image")
    assert result.returncode == 0, result.stderr
    lines = [line for line in calls(env).splitlines() if line.startswith("compose ") and "version" not in line]
    assert lines and all("-p shazzzbot -f " in line for line in lines if not line.startswith("compose ps"))
    assert all("evil" not in line and "other" not in line for line in lines)
    evidence = (Path(env["STUB_ROOT"]) / "daemon.env").read_text().splitlines()
    assert evidence and set(evidence) == {f"unix://{env['SHAZZZBOT_DOCKER_SOCK']}|unset|0"}


def test_override_is_refused_instead_of_executed_as_root(env):
    assert bot(env, "compose-image", "--unattended", stdin="").returncode == 0
    app = runtime_app(env)
    saved, before = backups(env), calls(env)
    (app / "compose.override.yaml").write_text("services: {}")
    result = bot(env, "compose-image", "--unattended")
    assert result.returncode == 1 and "unreviewed Compose override" in result.stderr
    assert backups(env) == saved
    assert " up " not in calls(env)[len(before):]


def test_unowned_container_is_not_removed(env):
    Path(env["STUB_ROOT"], "run_shazzzbot").touch()
    Path(env["STUB_ROOT"], "owner").write_text("another install")
    result = bot(env, "docker-run", "--unattended")
    assert result.returncode == 1 and "not owned by this install" in result.stderr
    assert not re.search(r"^rm ", calls(env), re.MULTILINE)
    assert not backups(env)


def test_backup_failure_does_not_replace_any_file(env):
    assert bot(env, "compose-image").returncode == 0
    app = runtime_app(env)
    original = (app / "compose.yaml").read_bytes()
    root = Path(env["STUB_ROOT"])
    (root / "bin" / "python3").write_text(
        '#!/bin/bash\nfor arg in "$@"; do [ "$arg" != backup ] || exit 1; done\nexec ' + sys.executable + ' "$@"\n')
    (root / "bin" / "python3").chmod(0o755)
    source_tar(root, "v1.2.4")
    (root / "tag").write_text("v1.2.4")
    before = calls(env)
    result = run(env, "--upgrade", "--unattended")
    assert result.returncode == 1 and "backup failed" in result.stderr
    assert (app / "compose.yaml").read_bytes() == original
    assert " up " not in calls(env)[len(before):]


def test_failed_start_uses_central_rollback_and_verifies_health(env):
    assert bot(env, "compose-image").returncode == 0
    root = Path(env["STUB_ROOT"])
    source_tar(root, "v1.2.4")
    (root / "tag").write_text("v1.2.4")
    (root / "fail_up").touch()
    result = run(env, "--upgrade", "--unattended")
    assert result.returncode == 1
    assert "recovery health verified" in result.stderr
    assert "    image: sha256:" + "a" * 64 + "\n" in app_file(env, "compose.yaml")
    assert json.loads(app_file(env, ".setup-state")) == {
        "format": 1, "method": "compose-image", "role": "bot", "ref": "v1.2.3"}
    assert "--force-recreate" in calls(env)


def test_same_release_config_drift_recreates_instead_of_skipping(env):
    assert bot(env, "compose-image").returncode == 0
    app = runtime_app(env)
    with (app / "config.toml").open("a") as stream:
        stream.write("\n# operator edit\n")
    before = calls(env)
    result = bot(env, "compose-image", "--unattended")
    assert result.returncode == 0, result.stderr
    assert "nothing to change" not in result.stdout
    assert " up " in calls(env)[len(before):] and "--force-recreate" in calls(env)[len(before):]


def test_same_release_not_ready_cannot_pass_idempotency_guard(env):
    assert bot(env, "compose-image").returncode == 0
    Path(env["STUB_ROOT"], "not_ready").touch()
    Path(env["STUB_ROOT"], "ups").write_text("0")
    result = bot(env, "compose-image", "--unattended")
    assert result.returncode == 1 and "nothing to change" not in result.stdout
    assert "ready /config/config.toml" in calls(env)


@pytest.mark.parametrize("tag,newest,ci_success", [
    ("v1.9.0", "v1.10.0", True), ("v1.10.0", "v1.10.0", True), ("v1.10.0", "v1.10.0", False)])
def test_release_latest_only_moves_to_the_highest_semver(env, tag, newest, ci_success):
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    assert "group: release-images" in workflow and "release-${{ github.ref }}" not in workflow
    body = textwrap.dedent(workflow.split("        run: |\n", 1)[1])
    git = Path(env["STUB_ROOT"]) / "bin" / "git"
    git.write_text('#!/bin/bash\nprintf "hash\trefs/tags/v1.9.0\\nhash\trefs/tags/v1.10.0\\n"\n')
    git.chmod(0o755)
    work = Path(env["STUB_ROOT"]) / "release-work"
    (work / "tools").mkdir(parents=True)
    (work / "VERSION").write_text(tag[1:] + "\n")
    (work / "CHANGELOG.md").write_text(f"# Changes\n\n## {tag[1:]}\n\nExact release notes.\n")
    for name in ("__init__.py", "build_helper.py", "release_check.py", "setup_artifacts.py", "setup_files.py"):
        shutil.copy(ROOT / "tools" / name, work / "tools" / name)
    shutil.copy(ROOT / "shazzzbot-helper-unraid.py", work / "shazzzbot-helper-unraid.py")
    shutil.copytree(ROOT / "shazzzbot/helper", work / "shazzzbot/helper")
    shutil.copy(ROOT / "shazzzbot/__init__.py", work / "shazzzbot/__init__.py")
    docker = Path(env["STUB_ROOT"]) / "bin/docker"
    docker.write_text(DOCKER_STUB.replace('case "$1" in\n',
                      'case "$1" in\n  run) printf "%s\\n" "${TAG#v}"; exit 0 ;;\n', 1))
    gh = Path(env["STUB_ROOT"]) / "bin/gh"
    gh.write_text(textwrap.dedent(r'''#!/bin/bash
printf 'gh %s\n' "$*" >> "$STUB_LOG"
if [ "$1" = api ]; then
  case "$2" in
    */actions/*)
      if [ "$CI_SUCCESS" = 1 ]; then
        printf '{"workflow_runs":[{"head_sha":"%s","event":"push","status":"completed","conclusion":"success"}]}\n' \
          "$SHA"
      else printf '{"workflow_runs":[]}\n'; fi ;;
    */releases/tags/*) echo 'draft is not available through the published-tag endpoint' >&2; exit 77 ;;
  esac
elif [ "$1" = release ] && [ "$2" = view ]; then
  if [[ " $* " != *" --json "* ]]; then exit 1; fi
  python3 - <<'PYTHON'
import json
from pathlib import Path
names = ('source.tar.gz', 'helper.pyz', 'helper-unraid.json', 'shazzzbot-helper-unraid.py',
         'release.json', 'SHA256SUMS')
print(json.dumps({'draft': True, 'assets': [
    {'name': name, 'state': 'uploaded', 'size': Path(name).stat().st_size} for name in names]}))
PYTHON
elif [ "$1" = release ] && [ "$2" = download ]; then
  cp -- source.tar.gz helper.pyz helper-unraid.json shazzzbot-helper-unraid.py release.json SHA256SUMS "$5/"
fi
'''))
    gh.chmod(0o755)
    result = subprocess.run(["bash", "-c", body], env={**env, "TOKEN": TOKEN, "ACTOR": "test", "TAG": tag,
                            "SHA": "a" * 40, "GITHUB_REPOSITORY": "owner/project",
                            "CI_SUCCESS": str(int(ci_success))}, capture_output=True,
                            text=True, check=False, timeout=10, cwd=work)
    if not ci_success:
        assert result.returncode != 0 and "no completed successful push CI run" in result.stderr
        assert "push ghcr.io/owner/project:" not in calls(env)
        assert "gh release" not in calls(env)
        return
    assert result.returncode == 0, result.stderr
    assert ("push ghcr.io/owner/project:latest" in calls(env)) == (tag == newest)
    assert (f"gh release edit {tag} --latest" in calls(env)) == (tag == newest)
    assert f"push ghcr.io/owner/project:{tag}" in calls(env)
    assert TOKEN not in result.stdout + result.stderr + calls(env)
    manifest = json.loads((work / "release.json").read_text())
    assert manifest["image"].startswith(f"ghcr.io/owner/project:{tag}@sha256:")
    for name, artifact in (("source", "source.tar.gz"), ("helper", "helper.pyz")):
        assert manifest[name + "_sha256"] == hashlib.sha256((work / artifact).read_bytes()).hexdigest()
    assert manifest["platforms"] == {
        "unraid": hashlib.sha256((work / "helper-unraid.json").read_bytes()).hexdigest()}
    platform = json.loads((work / "helper-unraid.json").read_bytes())
    assert platform == {
        "format": 1, "platform": "unraid", "version": tag, "revision": "a" * 40,
        "files": {name: {"sha256": hashlib.sha256((work / name).read_bytes()).hexdigest(),
                         "size": (work / name).stat().st_size}
                  for name in ("helper.pyz", "shazzzbot-helper-unraid.py")}}
    assert (work / "shazzzbot-helper-unraid.py").read_bytes() == (ROOT / "shazzzbot-helper-unraid.py").read_bytes()
    checksums = (work / "SHA256SUMS").read_text().splitlines()
    assets = {"source.tar.gz", "helper.pyz", "helper-unraid.json", "shazzzbot-helper-unraid.py", "release.json"}
    assert {line.split()[1] for line in checksums} == assets
    for line in checksums:
        digest, artifact = line.split()
        assert digest == hashlib.sha256((work / artifact).read_bytes()).hexdigest()
    assert f"gh release upload {tag} source.tar.gz helper.pyz helper-unraid.json shazzzbot-helper-unraid.py " \
        "release.json SHA256SUMS --clobber" in calls(env)
    assert f"gh release create {tag} --verify-tag --draft --latest=false --notes-file" in calls(env)
    assert f"gh release edit {tag} --draft=false --latest=false --notes-file" in calls(env)
    assert calls(env).index(f"gh release download {tag}") < calls(env).index(
        f"gh release edit {tag} --draft=false")
    assert "queue: max" in workflow


def test_all_container_profiles_bound_resources_and_rotate_logs():
    relay = (ROOT / "compose.relay.yaml").read_text()
    assert "mem_limit: 256m" in relay and "pids_limit: 128" in relay
    assert 'options: {max-size: 10m, max-file: "3"}' in relay
    template = (ROOT / "unraid/shazzzbot.xml").read_text()
    assert "--log-driver json-file --log-opt max-size=10m --log-opt max-file=3" in template
    assert "--memory 256m --pids-limit 128 -e SHAZZZBOT_ROLE=relay" in (
        ROOT / "tools/setup_deploy.sh").read_text()


def test_template_requires_explicit_private_paths_and_read_only_host_mounts():
    template = ET.fromstring((ROOT / "unraid/shazzzbot.xml").read_text())
    paths = {item.attrib["Target"]: item for item in template.findall("Config") if item.attrib["Type"] == "Path"}
    for target in ("/config/config.toml", "/data"):
        assert paths[target].attrib["Default"] == ""
        assert not paths[target].text
    assert paths["/config/config.toml"].attrib["Required"] == "false"
    assert paths["/config/config.toml"].attrib["Mode"] == "ro"
    assert paths["/run/shazzzbot-helper"].attrib["Mode"] == "ro"
    assert paths["/run/shazzzbot-helper"].attrib["Default"] == "/tmp/shazzzbot-helper/socket"
    assert paths["/data"].attrib["Mode"] == "rw"
    assert all(item.attrib["Mode"] == "ro" for target, item in paths.items() if target.startswith("/host/"))
    assert "/var/run/docker.sock" not in paths
    assert template.findtext("Privileged") == "false"


def test_ctrl_c_after_mutation_restores_the_verified_backup(env):
    assert bot(env, "compose-image").returncode == 0
    root = Path(env["STUB_ROOT"])
    source_tar(root, "v1.2.4")
    (root / "tag").write_text("v1.2.4")
    stub = root / "bin" / "docker"
    body = stub.read_text().replace('  info)', '''  info)
    if [ ! -f "$s/installer.pid" ]; then
      awk '/^PPid:/ {print $2}' "/proc/$PPID/status" > "$s/installer.pid"
    fi
''')
    stub.write_text(body.replace(
        '*" up "*)', '*" up "*)\n        if [ -f "$s/interrupt" ]; then '
        'rm "$s/interrupt"; kill -INT "$(cat "$s/installer.pid")"; exit 1; fi'))
    (root / "interrupt").touch()
    result = run(env, "--upgrade", "--unattended")
    assert result.returncode == 130
    assert "recovery health verified" in result.stderr
    assert "    image: sha256:" + "a" * 64 + "\n" in app_file(env, "compose.yaml")
    assert json.loads(app_file(env, ".setup-state")) == {
        "format": 1, "method": "compose-image", "role": "bot", "ref": "v1.2.3"}
    assert not (Path(env["SHAZZZBOT_BACKUP_DIR"]) / "pending").exists()


def test_failed_rollback_never_claims_the_old_version_is_healthy(env):
    assert bot(env, "compose-image").returncode == 0
    root = Path(env["STUB_ROOT"])
    source_tar(root, "v1.2.4")
    (root / "tag").write_text("v1.2.4")
    stub = root / "bin" / "docker"
    stub.write_text(stub.read_text().replace('rm "$s/not_ready"', ':'))
    (root / "not_ready").touch()
    result = run(env, "--upgrade", "--unattended")
    assert result.returncode == 1 and "recovery could not be verified" in result.stderr
    assert "previous version runs again" not in result.stderr
    backup_root = Path(env["SHAZZZBOT_BACKUP_DIR"])
    pending = backup_root / "pending"
    assert pending.exists()
    app = runtime_app(env)
    unchanged = {name: (app / name).read_bytes() for name in ("compose.yaml", ".setup-state")}
    saved = {path.relative_to(backup_root): path.read_bytes()
             for path in backup_root.rglob("*") if path.is_file()}
    before = calls(env)
    retry = run(env, "--upgrade", "--unattended")
    assert retry.returncode == 1 and "interrupted install needs recovery" in retry.stderr
    assert pending.exists()
    assert {name: (app / name).read_bytes() for name in unchanged} == unchanged
    assert {path.relative_to(backup_root): path.read_bytes()
            for path in backup_root.rglob("*") if path.is_file()} == saved
    retry_calls = calls(env)[len(before):]
    assert not re.search(r"^(?:build|pull|tag|rm|run|start|stop|restart|kill|exec|network (?:create|rm)|"
                         r"volume (?:create|rm))\b", retry_calls, re.MULTILINE)
    assert not re.search(r"^compose .*\b(?:up|down|rm|restart|stop|start|pull|build|create|kill)\b",
                         retry_calls, re.MULTILINE)


def test_existing_config_is_validated_before_any_restart(env):
    assert bot(env, "compose-image").returncode == 0
    app = runtime_app(env)
    with (app / "config.toml").open("a") as stream:
        stream.write("\nnot valid toml\n")
    root = Path(env["STUB_ROOT"])
    source_tar(root, "v1.2.4")
    (root / "tag").write_text("v1.2.4")
    original, saved = (app / "compose.yaml").read_bytes(), backups(env)
    before = calls(env)
    result = bot(env, "compose-image", "--unattended")
    assert result.returncode == 1 and "config validation failed" in result.stderr
    assert " up " not in calls(env)[len(before):]
    assert (app / "compose.yaml").read_bytes() == original and backups(env) == saved


@pytest.mark.parametrize("resource", ["network", "image"])
def test_unowned_network_and_image_are_not_reused(env, resource):
    root = Path(env["STUB_ROOT"])
    if resource == "network":
        (root / "net").touch()
        (root / "net-owner").write_text("another install")
        result = bot(env, "compose-image", "--unattended", stdin="")
        assert result.returncode == 1 and "network shazzzbot_default is not owned" in result.stderr
        assert "network create" not in calls(env) and not backups(env)
    else:
        (root / "image").touch()
        stub = root / "bin" / "docker"
        pattern = 'printf \'%s\\n\' "${SHAZZZBOT_INSTALL_ID:-$SHAZZZBOT_APP_DIR}"'
        stub.write_text(stub.read_text().replace(pattern, 'echo another-image'))
        result = bot(env, "compose-build", "--unattended", stdin="")
        image = runtime_metadata(env)["dev_image"]
        assert result.returncode == 1 and f"image {image} is not owned" in result.stderr
        assert not re.search(r"^tag ", calls(env), re.MULTILINE)


@pytest.mark.skipif(not os.environ.get("SHAZZZBOT_WIZARD_IMAGE"), reason="requires the local wizard test image")
def test_wizard_in_the_real_read_only_container(tmp_path):
    image = os.environ["SHAZZZBOT_WIZARD_IMAGE"]
    out = tmp_path / "out"
    out.mkdir()
    if os.geteuid() != 0:
        out.chmod(0o703)
    names = {"SHAZZZBOT_DISCORD_TOKEN": TOKEN, "SHAZZZBOT_GUILD_ID": GUILD,
             "SHAZZZBOT_STATUS_CHANNEL_ID": STATUS}
    command = ["docker", "run", "--rm", "-i", "--read-only", "--network", "none", "--tmpfs",
               "/tmp:rw,noexec,nosuid,size=16m", "-e", "TMPDIR=/tmp", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges:true", "--user", "0"]
    for name in names:
        command += ["-e", name]
    for source, target in (("tools/setup_config.py", "setup_config.py"), (".env.example", ".env.example"),
                           ("config.example.toml", "config.example.toml")):
        command += ["-v", f"{ROOT / source}:/setup/{target}:ro"]
    command += ["-v", f"{out}:/out", image, "python", "-I", "-B", "/setup/setup_config.py", "--examples", "/setup",
                "--out", "/out", "--role", "bot", "--create", ".env", "--create", "config.toml", "--no-prompt"]
    result = subprocess.run(command, env={**os.environ, **names}, capture_output=True, text=True,
                            check=False, timeout=60)
    assert result.returncode == 0, result.stderr
    assert TOKEN not in result.stdout + result.stderr + " ".join(command)
    assert sorted(p.name for p in out.iterdir()) == [".env", "config.toml"]
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 and p.stat().st_size for p in out.iterdir())
    configure = (ROOT / "tools/setup_configure.sh").read_text()
    launch = configure.split('docker run --rm -i ', 1)[1].split('"$(wizard_image)"', 1)[0]
    assert "--read-only" in launch and "--tmpfs /tmp:rw,noexec,nosuid,size=16m" in launch
    assert "-e TMPDIR=/tmp" in launch and "--user 0" in launch


def test_secret_prompts_keep_a_tty_when_output_is_captured(env):
    fresh = {key: value for key, value in env.items() if key != "SHAZZZBOT_WIZARD_PYTHON"}
    master, slave = pty.openpty()
    try:
        os.write(master, b"y\n")
        script = Path(env["STUB_ROOT"]) / "bundle" / "setup.sh"
        result = subprocess.run(["bash", str(script), "--role", "bot", "--method", "compose-image", "--no-helper"],
                                env=fresh, stdin=slave, capture_output=True, text=True, check=False, timeout=90)
    finally:
        os.close(master)
        os.close(slave)
    assert result.returncode == 1 and "untrusted .env" in result.stderr
    assert "run --rm -i -t --read-only" in calls(env)
    assert TOKEN not in result.stdout + result.stderr + calls(env)


@pytest.mark.skipif(not os.environ.get("SHAZZZBOT_WIZARD_IMAGE"), reason="requires the local wizard test image")
def test_validation_uses_root_private_staging_in_the_real_read_only_container(tmp_path):
    image = os.environ["SHAZZZBOT_WIZARD_IMAGE"]
    source, stage = tmp_path / "source", tmp_path / "stage"
    for folder in (source, stage):
        folder.mkdir(mode=0o703)
        folder.chmod(0o703)
    common = ["docker", "run", "--rm", "--read-only", "--network", "none", "--tmpfs",
              "/tmp:rw,noexec,nosuid,size=16m", "-e", "TMPDIR=/tmp", "--cap-drop", "ALL",
              "--security-opt", "no-new-privileges:true"]
    factory = common + ["--user", "10001", "-e", "DISCORD_TOKEN", "-e", "CANDIDATE_CONFIG", "-v",
                        f"{source}:/candidate", image, "python", "-I", "-B", "-c",
                        ("import os,pathlib; os.umask(0o077); root=pathlib.Path('/candidate'); "
                        "(root/'.env').write_text('DISCORD_TOKEN='+os.environ['DISCORD_TOKEN']); "
                        "(root/'config.toml').write_text(os.environ['CANDIDATE_CONFIG'])")]
    result = subprocess.run(factory, env={**os.environ, "DISCORD_TOKEN": TOKEN,
                            "CANDIDATE_CONFIG": (f"config_format = 1\nguild_id = {GUILD}\n"
                                                 f"[[outputs]]\nchannel = {STATUS}\n")},
                            capture_output=True, text=True, check=False, timeout=60)
    assert result.returncode == 0, result.stderr
    assert (source / "config.toml").stat().st_uid == 10001
    assert (source / "config.toml").stat().st_mode & 0o077 == 0

    def validate(folder):
        command = common + ["--user", "0", "-v", f"{ROOT / 'tools/setup_config.py'}:/setup/setup_config.py:ro",
                            "-v", f"{folder}:/candidate:ro", image, "python", "-I", "-B", "/setup/setup_config.py",
                            "--validate", "/candidate", "bot"]
        return subprocess.run(command, capture_output=True, text=True, check=False, timeout=60)
    assert validate(source).returncode != 0
    prepare = common + ["--cap-add", "DAC_OVERRIDE", "--user", "0", "-v", f"{source}:/source:ro",
                        "-v", f"{stage}:/stage", image, "python", "-I", "-B", "-c",
                        ("import os,pathlib; os.umask(0o077); "
                        "[(pathlib.Path('/stage')/n).write_bytes((pathlib.Path('/source')/n).read_bytes()) "
                        "for n in ('.env','config.toml')]")]
    result = subprocess.run(prepare, capture_output=True, text=True, check=False, timeout=60)
    assert result.returncode == 0, result.stderr
    result = validate(stage)
    assert result.returncode == 0, result.stderr
    assert TOKEN not in result.stdout + result.stderr + " ".join(factory + prepare)
    body = (ROOT / "tools/setup_prereqs.sh").read_text()
    assert '-v "$candidate:/candidate:ro"' in body


def test_inherited_shell_tracing_cannot_print_the_bot_token(env):
    result = bot({**env, "SHELLOPTS": "xtrace"}, "docker-run", "--unattended", stdin="")
    assert result.returncode == 0, result.stderr
    assert TOKEN not in result.stdout + result.stderr + calls(env)
