# shellcheck shell=bash

state_file() { printf '%s\n' "$APP_DIR/.setup-state"; }

read_state() {
  local line=""
  ST_METHOD="" ST_ROLE="" ST_REF=""
  [ -d "$APP_DIR" ] || return 0
  line="$(fs layout-state "$APP_DIR")" || die "cannot read install state safely"
  read -r ST_METHOD ST_ROLE ST_REF <<< "$line" || true
  if [ -n "$ST_METHOD" ]; then
    [[ " $METHODS_BOT " == *" $ST_METHOD "* && ( "$ST_ROLE" = bot || "$ST_ROLE" = relay ) \
      && "$ST_REF" =~ ^(v[0-9]+\.[0-9]+\.[0-9]+|[0-9a-f]{40})$ ]] || die "invalid install state"
  fi
}

resolve_paths() {
  if [ -z "$APP_DIR" ]; then
    if [ "$ROLE" = relay ]; then APP_DIR=/var/lib/shazzzbot-relay
    else APP_DIR=/mnt/user/appdata/shazzzbot; fi
  fi
  APP_DIR="$(fs path "$APP_DIR")" || die "invalid bot directory"
  if [[ "$APP_DIR/" = /mnt/user/* ]]; then
    APP_DIR="$(fs resolve-helper "$APP_DIR")" || die "cannot resolve appdata to its mounted pool"
  fi
  LOGICAL_DIR="$APP_DIR"
  BACKUP_ROOT="${BACKUP_REQUEST:-$APP_DIR/backups}"
  PROJECT=shazzzbot
  # shellcheck disable=SC2034 # read by compose in setup_deploy.sh; reviewed 2026-11
  [ "$ROLE" != relay ] || PROJECT=shazzzbot_relay
  INSTALL_ID="$(fs host "${SHAZZZBOT_MACHINE_ID:-/etc/machine-id}" "$APP_DIR" "$BACKUP_ROOT")" \
    || die "cannot establish installation identity"
  export SHAZZZBOT_INSTALL_ID="$INSTALL_ID" SHAZZZBOT_INSTALL_ROLE="$ROLE"
}

select_role() {
  local answer
  [ -n "$ROLE" ] && return 0
  if [ "$UNATTENDED" = 1 ]; then
    ROLE=bot
    return 0
  fi
  printf 'Which node is this?\n  1) bot    the Unraid server the bot reports on\n  2) relay  a machine outside that network that runs service checks\n'
  answer="$(ask_line "Choose 1 or 2:")" || exit 1
  case "$answer" in
    1|bot) ROLE=bot ;;
    2|relay) ROLE=relay ;;
    *) die "no node type chosen" ;;
  esac
}

find_install() {
  local given="$ROLE" dir="$APP_DIR" backup="$BACKUP_ROOT" r hits=0 pick=""
  for r in ${given:-bot relay}; do
    ROLE="$r" APP_DIR="$dir" BACKUP_ROOT="$backup"
    resolve_paths
    read_state
    if [ -n "$ST_METHOD" ] && [ "$LOGICAL_DIR" != "${pick#*|}" ]; then
      hits=$((hits + 1))
      pick="$ST_ROLE|$LOGICAL_DIR"
    fi
  done
  [ "$hits" -gt 0 ] || die "no install recorded in ${dir:-the default directories}; run setup.sh without --upgrade first"
  [ "$hits" -eq 1 ] || die "a bot install and a relay install both exist; pass --role or --dir"
  ROLE="$given" APP_DIR="${pick#*|}" BACKUP_ROOT="$backup"
  ROLE="${given:-${pick%%|*}}"
  resolve_paths
  read_state
  [ -z "$ST_ROLE" ] && return 0
  if [ -n "$given" ] && [ "$given" != "$ST_ROLE" ]; then
    die "$APP_DIR holds a $ST_ROLE install, not $given; switching the role is a fresh install"
  fi
  ROLE="$ST_ROLE"
}

detect_installs() {
  local report kind value found_dir="" found_role="" found_method=""
  report="$(fs detect "$APP_DIR" "$HELPER_DIR" "$HOOK_DIR" "$IMAGE" "$VERSION_TAG" "$ROLE" "$UNRAID_MARKER")" \
    || die "cannot inspect existing installations; nothing changed"
  while IFS=$'\t' read -r kind value; do
    case "$kind" in
      app) found_dir="$value" ;;
      role) found_role="$value" ;;
      method) found_method="$value" ;;
      current) DETECTED_CURRENT="$value" ;;
      platform)
        # shellcheck disable=SC2034 # read by main in setup.sh; reviewed 2026-11
        DETECTED_PLATFORM="$value" ;;
      state)
        # shellcheck disable=SC2034 # read by select_action in setup_runtime.sh; reviewed 2026-11
        DETECTED_STATE="$value" ;;
      line) say "$value" ;;
    esac
  done <<< "$report"
  if [ -n "$found_dir" ] && { [ -z "$APP_DIR" ] || [ "$APP_DIR" = "$found_dir" ]; }; then
    APP_DIR="$found_dir"
    ROLE="${ROLE:-$found_role}"
    METHOD="${METHOD:-$found_method}"
  fi
  if [ "$APP_DIR" != "$found_dir" ] || [ "$ROLE" != "$found_role" ] || [ "$METHOD" != "$found_method" ]; then
    # shellcheck disable=SC2034 # read by main in setup.sh; reviewed 2026-11
    DETECTED_CURRENT=0
  fi
}

guard_switch() {
  [ -z "$ST_METHOD" ] && return 0
  if [ "$ST_METHOD" != "$METHOD" ] || [ "$ST_ROLE" != "$ROLE" ]; then
    die "$APP_DIR runs $ST_ROLE/$ST_METHOD. To switch, stop it first ($(stop_hint "$ST_METHOD")), remove $(state_file), and run again"
  fi
}

stop_hint() {
  case "$1" in
    compose-*) printf 'cd %s && docker compose down' "$APP_DIR" ;;
    *) printf 'docker rm -f shazzzbot shazzzbot-proxy shazzzbot-relay' ;;
  esac
}

