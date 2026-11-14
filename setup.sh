#!/usr/bin/env bash
# Install or upgrade a shazzzbot bot or relay node. Run ./setup.sh --help for the flags.
set -Eeuo pipefail
set +x
umask 077

APP_DIR="${SHAZZZBOT_APP_DIR:-}"
HELPER_DIR="${SHAZZZBOT_HELPER_DIR:-/mnt/user/appdata/shazzzbot-helper}"
HOOK_DIR="${SHAZZZBOT_HELPER_HOOK_DIR:-/boot/config/plugins/user.scripts/scripts/shazzzbot-helper}"
WORK_ROOT=/tmp/shazzzbot-helper
BACKUP_ROOT="${SHAZZZBOT_BACKUP_DIR:-}"
LOCK_FILE="${SHAZZZBOT_LOCK_FILE:-$WORK_ROOT/installer.lock}"
UNRAID_MARKER="${SHAZZZBOT_UNRAID_MARKER:-/etc/unraid-version}"
DOCKER_SOCK="${SHAZZZBOT_DOCKER_SOCK:-/var/run/docker.sock}"
REPO_URL="${SHAZZZBOT_REPO_URL:-https://github.com/mrchrisneal/shazzzbot.git}"
RELEASE_API="${SHAZZZBOT_RELEASE_API:-https://api.github.com/repos/mrchrisneal/shazzzbot/releases/latest}"
SOURCE_URL="${SHAZZZBOT_SOURCE_URL:-https://github.com/mrchrisneal/shazzzbot/releases/download}"
IMAGE="${SHAZZZBOT_IMAGE:-ghcr.io/mrchrisneal/shazzzbot}"
GITHUB_PROBE="${SHAZZZBOT_GITHUB_PROBE:-https://github.com}"
GHCR_PROBE="${SHAZZZBOT_GHCR_PROBE:-https://ghcr.io/v2/}"
BRANCH="${SHAZZZBOT_BRANCH:-main}"
MIN_FREE_MB="${SHAZZZBOT_MIN_FREE_MB:-1024}"
REQUIRE_ROOT="${SHAZZZBOT_REQUIRE_ROOT:-1}"
HEALTH_TIMEOUT="${SHAZZZBOT_HEALTH_TIMEOUT:-180}"
HEALTH_POLL="${SHAZZZBOT_HEALTH_POLL:-5}"
RELAY_PORT=8443
KEEP=5
MODE=setup
ROLE=""
METHOD=""
VERSION_TAG=""
HELPER=ask
HELPER_ONLY=0
HELPER_BUNDLE=""
ACCEPT_FLASH=0
INSTALL_ID=""
PINNED_IMAGE=""
HELPER_RUNTIME=""
RELEASE_MANIFEST=""
HELPER_RELEASE_TAG=""
HELPER_STAGE=""
HELPER_EXPECTED=""
HELPER_SEEN=""
HELPER_PREFLIGHT_DONE=0
RECOVERY_END=0
COMMAND_TIMEOUT=300
DRY_RUN=0
UNATTENDED=0
DEV_IMAGE=""
PREV_IMAGE=""
BOT_UID=10001
METHODS_BOT="compose-image compose-build docker-run bot-only"
METHODS_RELAY="compose-image compose-build docker-run"

BACKUP_PATH=""
TMPF=""
TEMP_FILES=()
SRC=""
REF=""
ST_METHOD=""
ST_ROLE=""
ST_REF=""
CODE_CHANGED=0
FILES_CHANGED=0
PROXY_CHANGED=0
CREATED_CFG=0
IMAGE_READY=0
PREV_READY=0
SERVICE_CHANGED=0
CONFIG_DRIFT=0
HELPER_DONE=0
MUTATING=0
COMMITTED=0
HELPER_WAS_RUNNING=0
HELPER_CHANGED=0
OLD_BOT_IMAGE=""
OLD_PROXY_IMAGE=""
ROLLING_BACK=0
PROJECT=""
LOGICAL_DIR=""
SOURCE_DIR=""
BACKUP_REQUEST=""
SHIP_PACK=""
SHIP_REVISION=""
CONFIG_SOURCE=""
DETECTED_CURRENT=0
DETECTED_PLATFORM=""
DETECTED_STATE=""
ACTION=""
CONFIG_CURRENT=0
declare -A OK=()
SETUP_HOME="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
load_tools() {
  local answer
  command -v timeout >/dev/null || die "timeout is required; install coreutils before setup"
  until command timeout --foreground --signal=TERM --kill-after=5 30 python3 -I -B -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1; do
    warn "python3 3.11 or later is required for trusted path checks; install Python 3 for UNRAID or python3"
    [ "$UNATTENDED" = 0 ] || die "python3 prerequisite is missing"
    answer="$(ask_line 'r to recheck Python, or c to cancel:')" || exit 1
    case "$answer" in r|R) continue ;; *) die "setup cancelled" ;; esac
  done
command timeout --foreground --signal=TERM --kill-after=5 30 python3 -I -B - "$SETUP_HOME" <<'PYTHON' || die "installer source validation failed"
import os, stat, sys
from pathlib import Path
root = Path(sys.argv[1])
for path in [root / "tools/__init__.py", root / "tools/setup_prereqs.sh", root / "tools/setup_runtime.sh",
             root / "tools/setup_install_helper.sh", *sorted((root / "tools").glob("setup_*.py")),
             root / "tools/setup_layout.sh",
             root / "tools/setup_source.sh",
             root / "tools/setup_helper_only.sh",
             root / "tools/setup_session.sh",
             root / "tools/setup_detect.sh",
             root / "tools/setup_configure.sh",
             root / "tools/setup_deploy.sh",
             root / "shazzzbot-helper-unraid.py", root / "shazzzbot/__init__.py", root / "shazzzbot/schema.py",
             *sorted((root / "shazzzbot/helper").rglob("*.py"))]:
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for i, part in enumerate(path.parts[1:]):
            child = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK |
                            (os.O_DIRECTORY if i < len(path.parts) - 2 else 0), dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            sticky = i < len(path.parts) - 2 and info.st_uid == 0 and info.st_mode & stat.S_ISVTX
            kind = stat.S_ISDIR if i < len(path.parts) - 2 else stat.S_ISREG
            if info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022 and not sticky or not kind(info.st_mode):
                raise SystemExit("installer source must have trusted ownership and ancestors")
    finally:
        os.close(fd)
PYTHON
exec 8<"$SETUP_HOME/tools/setup_prereqs.sh" || exit 1
command timeout --foreground --signal=TERM --kill-after=5 30 python3 -I -B -c 'import os, stat
info = os.fstat(8)
if info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022 or not stat.S_ISREG(info.st_mode):
    raise SystemExit("untrusted installer source descriptor")' || die "installer source descriptor validation failed"
# shellcheck source=tools/setup_prereqs.sh
. /proc/self/fd/8
exec 8<&-
# shellcheck source=tools/setup_runtime.sh
. "$SETUP_HOME/tools/setup_runtime.sh"
# shellcheck source=tools/setup_install_helper.sh
. "$SETUP_HOME/tools/setup_install_helper.sh"
# shellcheck source=tools/setup_layout.sh
. "$SETUP_HOME/tools/setup_layout.sh"
# shellcheck source=tools/setup_source.sh
. "$SETUP_HOME/tools/setup_source.sh"
# shellcheck source=tools/setup_helper_only.sh
. "$SETUP_HOME/tools/setup_helper_only.sh"
# shellcheck source=tools/setup_session.sh
. "$SETUP_HOME/tools/setup_session.sh"
# shellcheck source=tools/setup_detect.sh
. "$SETUP_HOME/tools/setup_detect.sh"
# shellcheck source=tools/setup_configure.sh
. "$SETUP_HOME/tools/setup_configure.sh"
# shellcheck source=tools/setup_deploy.sh
. "$SETUP_HOME/tools/setup_deploy.sh"
}

usage() {
  cat <<'EOF'
Usage: setup.sh [options]

Install or upgrade shazzzbot. A bot node is the Unraid server the bot reports on.
A relay node is a machine outside that network that runs the public service checks.
The script checks every prerequisite, shows one table, and offers only the
install methods that work. User config is validated before deployment.
ship.sh can supply explicit config replacements. Existing helper settings and
keys are reused. The script never prunes Docker images or volumes, restarts
the server, or touches the array.

Install methods:
  compose-image   Docker Compose with the published image from GHCR
  compose-build   Docker Compose, image built from a git checkout
  docker-run      plain docker run, no Compose needed
  bot-only        the bot container alone: no docker-proxy (bot node only)

Options:
  --role bot|relay      Node type (asked when missing).
  --method NAME         Install method (asked when missing).
  --version vX.Y.Z      Release for the image methods (default: the latest).
  --upgrade             Pull, back up, rebuild or pull, restart, and report.
                        Preserves user settings. Updates an installed helper.
  --action NAME         keep, upgrade, migrate, repair, or reinstall.
                        Every action preserves user data. --yes confirms it.
  --helper              Install or update the optional host helper (bot node).
  --helper-only         Install only the helper. Does not deploy or inspect Docker.
  --helper-bundle DIR   Use a verified local Unraid helper bundle. No helper download.
  --no-helper           Do not install or update the host helper.
  --accept-flash-share  Allow a writable SMB flash export for the helper.
                        This allows anyone who can write the flash share to run code as root.
  -y, --yes             Same as --unattended.
  --unattended          Never prompt. Fail if input or a prerequisite is missing.
  -n, --dry-run         Print every action and change nothing.
  --dir DIR             Bot directory (appdata resolves to its real pool).
  --branch NAME         Git branch for compose-build (default: main).
  --keep N              Backups to keep (default: 5).
  --app-id ID           Discord application id, for the invite link.
  --guild-id ID         Discord server id.
  --status-channel ID   Status channel id.
  --audit-channel ID    Audit channel id.
  --commands-channel ID Commands channel id.
  --timezone NAME       Time zone, for example America/Los_Angeles.
  --relay-allow NAMES   Names a relay may check, separated by spaces.
  --set-icon            Set the shazzzbot icon on the Discord app and bot without asking.
  -h, --help            Show this help.

Secrets come only from the environment or a prompt: SHAZZZBOT_DISCORD_TOKEN,
SHAZZZBOT_RELAY_URL, SHAZZZBOT_RELAY_KEY, SHAZZZBOT_RELAY_FINGERPRINT,
SHAZZZBOT_RELAY_KEYS. SHAZZZBOT_UPS sets a NUT UPS. Paths and endpoints can be
changed with SHAZZZBOT_APP_DIR, SHAZZZBOT_HELPER_DIR, SHAZZZBOT_BACKUP_DIR,
SHAZZZBOT_LOCK_FILE, SHAZZZBOT_UNRAID_MARKER, SHAZZZBOT_DOCKER_SOCK,
SHAZZZBOT_IMAGE, SHAZZZBOT_REPO_URL, SHAZZZBOT_MIN_FREE_MB,
SHAZZZBOT_REQUIRE_ROOT (0 skips the root check and chown),
SHAZZZBOT_HEALTH_TIMEOUT, and SHAZZZBOT_HEALTH_POLL.

Exit status: 0 on success, 1 on any failure or cancel, 2 on a usage error.
Bot config and data stay in --dir. Transient builds use one private /tmp directory.
The helper owns one persistent directory plus its schedule hook.
Build a local helper bundle:
  python3 tools/build_helper.py --bundle-dir DIR --version vX.Y.Z --revision FULL_SHA
EOF
}

say() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { say "WARNING: $*" >&2; }
die() { say "ERROR: $*" >&2; exit 1; }
dry() { [ "$DRY_RUN" = 1 ]; }
plan() { printf '[dry-run] %s\n' "$*"; }

usage_error() {
  printf 'setup.sh: %s\nRun setup.sh --help for the options.\n' "$1" >&2
  exit 2
}

need_input() {
  [ "$UNATTENDED" = 0 ] || die "$1 needs input, and --unattended forbids prompts"
}

ask_line() {
  local answer
  need_input "$1"
  printf '%s ' "$1" >&2
  IFS= read -r answer || die "no input for: $1"
  printf '%s\n' "$answer"
}

confirm() {
  local answer
  if [ "$UNATTENDED" = 1 ]; then
    [ "$2" = y ]
    return
  fi
  if [ "$2" = y ]; then
    answer="$(ask_line "$1 [Y/n]")" || exit 1
  else
    answer="$(ask_line "$1 [y/N]")" || exit 1
  fi
  case "${answer:-$2}" in
    y|Y|yes|YES) return 0 ;;
    *) return 1 ;;
  esac
}

parse_args() {
  local v
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --upgrade) MODE=upgrade ;;
      --helper) HELPER=yes ;;
      --helper-only) HELPER_ONLY=1 ;;
      --accept-flash-share) ACCEPT_FLASH=1 ;;
      --no-helper) HELPER=no ;;
      --set-icon) export SHAZZZBOT_SET_ICON=1 ;;
      -y|--yes|--unattended) UNATTENDED=1 ;;
      -n|--dry-run) DRY_RUN=1 ;;
      -h|--help) usage; exit 0 ;;
      --role|--method|--version|--dir|--branch|--keep|--app-id|--guild-id|--status-channel|--audit-channel|\
--commands-channel|--timezone|--relay-allow|--ship-pack|--ship-revision|--config-source|--action|--helper-bundle)
        [ "$#" -ge 2 ] || usage_error "$1 needs a value"
        v="$2"
        case "$1" in
          --role) ROLE="$v" ;;
          --method) METHOD="$v" ;;
          --version) VERSION_TAG="$v" ;;
          --dir) APP_DIR="$v" ;;
          --branch) BRANCH="$v" ;;
          --keep) KEEP="$v" ;;
          --app-id) export SHAZZZBOT_APP_ID="$v" ;;
          --guild-id) export SHAZZZBOT_GUILD_ID="$v" ;;
          --status-channel) export SHAZZZBOT_STATUS_CHANNEL_ID="$v" ;;
          --audit-channel) export SHAZZZBOT_AUDIT_CHANNEL_ID="$v" ;;
          --commands-channel) export SHAZZZBOT_COMMANDS_CHANNEL_ID="$v" ;;
          --timezone) export SHAZZZBOT_TIMEZONE="$v" ;;
          --relay-allow) export SHAZZZBOT_RELAY_ALLOW="$v" ;;
          --ship-pack) SHIP_PACK="$v" ;;
          --ship-revision) SHIP_REVISION="$v" ;;
          --config-source) CONFIG_SOURCE="$v" ;;
          --action) ACTION="$v" ;;
          --helper-bundle) HELPER_BUNDLE="$v" ;;
        esac
        shift ;;
      *) usage_error "unknown option: $1" ;;
    esac
    shift
  done
  if ! [[ "$KEEP" =~ ^[1-9][0-9]{0,2}$ ]] || [ "$KEEP" -gt 100 ]; then usage_error "--keep must be 1 to 100"; fi
  if ! [[ "$HEALTH_POLL" =~ ^[1-9][0-9]{0,2}$ ]] || [ "$HEALTH_POLL" -gt 300 ]; then
    usage_error "SHAZZZBOT_HEALTH_POLL must be 1 to 300"
  fi
  if ! [[ "$HEALTH_TIMEOUT" =~ ^[0-9]{1,5}$ ]] || [ "$HEALTH_TIMEOUT" -gt 86400 ]; then
    usage_error "SHAZZZBOT_HEALTH_TIMEOUT must be 0 to 86400"
  fi
  [[ "$MIN_FREE_MB" =~ ^[0-9]{1,9}$ ]] || usage_error "SHAZZZBOT_MIN_FREE_MB must be a bounded whole number"
  HEALTH_TIMEOUT=$((10#$HEALTH_TIMEOUT))
  [[ "$REPO_URL" != http*://*@* ]] || usage_error "repository URLs must not contain credentials"
  [[ "$BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]] || usage_error "--branch has characters that are not allowed"
  [[ -z "$ROLE" || "$ROLE" =~ ^(bot|relay)$ ]] || usage_error "--role must be bot or relay"
  [[ -z "$METHOD" || " $METHODS_BOT " == *" $METHOD "* ]] || usage_error "unknown --method: $METHOD"
  [[ -z "$ACTION" || "$ACTION" =~ ^(keep|upgrade|migrate|repair|reinstall)$ ]] || usage_error "unknown --action: $ACTION"
  if [ "$MODE" = upgrade ] && [ -n "$ACTION" ] && [ "$ACTION" != upgrade ]; then
    usage_error "--upgrade conflicts with --action $ACTION"
  fi
  [ "$ACTION" != upgrade ] || MODE=upgrade
  [[ -z "$VERSION_TAG" || "$VERSION_TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || usage_error "--version must be vX.Y.Z"
  [[ -z "$APP_DIR" || "$APP_DIR" = /* ]] || usage_error "--dir must be an absolute path"
  [[ "$HELPER_DIR" = /* ]] || usage_error "SHAZZZBOT_HELPER_DIR must be an absolute path"
  if [ -n "$SHIP_PACK$SHIP_REVISION" ]; then
    [[ "$SHIP_PACK" = "$WORK_ROOT/"* && "$SHIP_REVISION" =~ ^[0-9a-f]{40}$ && "$METHOD" = compose-build ]] \
      || usage_error "shipping requires a private work-directory pack, a full commit id, and compose-build"
  fi
  [[ -z "$CONFIG_SOURCE" || "$CONFIG_SOURCE" = "$WORK_ROOT/"* ]] || usage_error "configuration input must be private RAM staging"
  if [ "$HELPER_ONLY" = 1 ] || [ -n "$HELPER_BUNDLE" ]; then
    [ "$ROLE" != relay ] || usage_error "helper options cannot be used with --role relay"
    [ "$HELPER" != no ] || usage_error "helper options conflict with --no-helper"
    ROLE=bot HELPER=yes
  fi
  if [ "$HELPER_ONLY" = 1 ]; then
    [ -z "$METHOD$ACTION$SHIP_PACK$SHIP_REVISION$CONFIG_SOURCE" ] && [ "$MODE" = setup ] \
      || usage_error "--helper-only conflicts with bot deployment actions"
  fi
  [[ -z "$HELPER_BUNDLE" || "$HELPER_BUNDLE" = /* ]] || usage_error "--helper-bundle needs an absolute directory"
}

main() {
  local state_seen config_valid=1 requested_action
  parse_args "$@"
  BACKUP_REQUEST="$BACKUP_ROOT"
  case "$SETUP_HOME/" in /boot/*) die "copy the installer to a root-only directory outside /boot before running it" ;; esac
  load_tools
  trap on_exit EXIT
  trap 'warn "command failed during installation (exit $?)"' ERR
  trap 'exit 130' INT
  trap 'exit 143' TERM
  dry && say "dry run: nothing will change"
  sanitize_environment
  unset SHAZZZBOT_HELPER_HOME SHAZZZBOT_HELPER_INODE SHAZZZBOT_HELPER_APP SHAZZZBOT_BOT_INSTALL_ID
  if [ "$HELPER_ONLY" = 1 ]; then helper_only; return; fi
  [ -z "$HELPER_BUNDLE" ] || helper_local_bundle
  detect_installs
  if [ "$MODE" = upgrade ]; then
    find_install
    METHOD="${METHOD:-${ST_METHOD:-compose-build}}"
  else
    select_role
    resolve_paths
    read_state
  fi
  if [ "$ROLE" = bot ] && [ "$DETECTED_PLATFORM" != unraid ]; then
    die "not supported yet: bot/helper on ${DETECTED_PLATFORM:-unknown}; only Unraid is supported; relays use --role relay"
  fi
  METHOD="${METHOD:-$ST_METHOD}"
  guard_switch
  check_pending
  audit_configuration || config_valid=0
  requested_action="$ACTION"
  select_action
  if [ "$ACTION" = keep ] && [ "$DETECTED_CURRENT" = 1 ]; then
    trust_install
    if HEALTH_TIMEOUT=0 healthy now; then
      say "the installed version is current; nothing to change"
      return 0
    fi
    say "partial or broken install: runtime readiness check failed"
    if [ -z "$requested_action" ] && { [ "$UNATTENDED" = 1 ] || dry; }; then
      ACTION=repair MODE=upgrade
    fi
  fi
  if [ "$ACTION" = keep ]; then say "installation unchanged"; return 0; fi
  if [ "$ACTION" = migrate ] && [ "$ROLE" != bot ]; then die "config migration is only available for bot installs"; fi
  if [ "$ACTION" = migrate ] && [ -z "$ST_REF" ]; then die "config migration needs a recorded deployment; choose repair"; fi
  [ "$config_valid" = 1 ] || die "config validation failed: fix reported errors before changing this install"
  if [ "$ACTION" = migrate ] && [ "$CONFIG_CURRENT" = 1 ]; then say "Config is current; nothing to change"; return 0; fi
  trust_identity
  if [ "$HELPER" = yes ]; then helper_preflight; fi
  state_seen="$(fs state-fingerprint "$APP_DIR" "$BACKUP_ROOT")" || die "cannot inspect installation state"
  if ! dry && [ "$UNATTENDED" = 0 ] && ! confirm "Continue with $ROLE $ACTION? No files have changed." n; then
    say "installation unchanged"
    return 0
  fi
  if ! dry; then fs work-root "$WORK_ROOT" || die "untrusted working directory"; fi
  take_lock
  if [ "$state_seen" != "$(fs state-fingerprint "$APP_DIR" "$BACKUP_ROOT")" ]; then
    die "install changed during setup; rerun"
  fi
  read_state
  guard_switch
  check_pending
  audit_configuration || die "config changed during setup; fix the reported errors and rerun"
  if [ "$ACTION" = migrate ]; then
    trust_install
    trust_identity
    assert_resources
    capture_images
    migrate_configuration
    return 0
  fi
  select_method
  select_helper
  if [ "$HELPER" = yes ]; then
    if [ "$HELPER_PREFLIGHT_DONE" = 0 ]; then helper_preflight; fi
    HELPER_DIR="$(fs resolve-helper "$HELPER_DIR")" || die "cannot resolve helper storage to its mounted pool"
    HELPER_RUNTIME="$HELPER_DIR/helper.pyz"
  fi
  export SHAZZZBOT_HELPER_HOOK_DIR="$HOOK_DIR"
  guard_switch
  check_pending
  layout_activate
  trust_identity
  say "$ROLE node, method $METHOD, install directory $APP_DIR, helper $HELPER"
  assert_resources
  capture_images
  [ "$ACTION" != reinstall ] || CONFIG_DRIFT=1
  prepare_source
  ship_configs
  if [ "$ROLE" = bot ] && [ "$HELPER" = yes ] && [ ! -f "$APP_DIR/.env" ]; then
    install_helper
  fi
  create_configs
  validate_configs
  install_files
  install_helper
  helper_wire_env
  deploy
  report
  if ! dry; then rm -f -- "$BACKUP_ROOT/pending"; fi
  COMMITTED=1
  say "done"
}

main "$@"
