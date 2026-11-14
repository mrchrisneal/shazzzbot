# shellcheck shell=bash

bounded() {
  local seconds="$COMMAND_TIMEOUT" rc
  if [ "$RECOVERY_END" -gt 0 ]; then
    seconds=$((RECOVERY_END - SECONDS))
    [ "$seconds" -gt 0 ] || { warn "recovery deadline reached"; return 124; }
    [ "$seconds" -le "$COMMAND_TIMEOUT" ] || seconds="$COMMAND_TIMEOUT"
  fi
  if [ "${REQUEST_END:-0}" -gt 0 ]; then
    local remaining=$((REQUEST_END - SECONDS))
    [ "$remaining" -gt 0 ] || { warn "request deadline reached"; return 124; }
    [ "$seconds" -le "$remaining" ] || seconds="$remaining"
  fi
  if command timeout --foreground --signal=TERM --kill-after=5 "$seconds" "$@" 9>&-; then return 0; else rc=$?; fi
  warn "${1##*/} failed (exit $rc)"
  return "$rc"
}

docker() { bounded docker "$@"; }
git() {
  GIT_HTTP_LOW_SPEED_LIMIT=1024 GIT_HTTP_LOW_SPEED_TIME=30 GIT_TERMINAL_PROMPT=0 \
    bounded nice -n 19 ionice -c3 git "$@"
}
curl() { bounded curl "$@"; }

sanitize_environment() {
  local name
  for name in ${!COMPOSE_@}; do unset "$name"; done
  export COMPOSE_REMOVE_ORPHANS=0 COMPOSE_IGNORE_ORPHANS=1 COMPOSE_PARALLEL_LIMIT=1
  unset DOCKER_HOST DOCKER_CONTEXT DOCKER_TLS DOCKER_TLS_VERIFY DOCKER_CERT_PATH
  export DOCKER_HOST="unix://$DOCKER_SOCK"
  export SHAZZZBOT_DOCKER_SOCK="$DOCKER_SOCK"
  for name in ${!GIT_@}; do unset "$name"; done
  export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null GIT_ATTR_NOSYSTEM=1
}

select_action() {
  local existing=0 answer
  if [ -n "${ST_METHOD:-}" ] || [[ "${DETECTED_STATE:-}" = installed || "${DETECTED_STATE:-}" = partial ]]; then
    existing=1
  fi
  if [ -z "${ACTION:-}" ]; then
    if [ "$MODE" = upgrade ]; then
      ACTION=upgrade
    elif [ "$existing" = 0 ]; then
      ACTION=install
    elif [ "$UNATTENDED" = 1 ] || [ "$DRY_RUN" = 1 ]; then
      if [ "${DETECTED_STATE:-}" = partial ]; then
        ACTION=repair
      elif [ "$DETECTED_CURRENT" = 1 ] && [ -z "$SHIP_PACK$CONFIG_SOURCE" ] && [ "$HELPER" != yes ]; then
        ACTION=keep
      else
        ACTION=upgrade
      fi
    else
      printf '\nActions:\n  1) Keep unchanged\n  2) Upgrade\n  3) Migrate config\n  4) Repair\n  5) Reinstall\n'
      while :; do
        printf 'Choose 1-5 [1]: ' >&2
        IFS= read -r answer || answer=keep
        case "${answer:-keep}" in
          1|keep) ACTION=keep; break ;;
          2|upgrade) ACTION=upgrade; break ;;
          3|migrate) ACTION=migrate; break ;;
          4|repair) ACTION=repair; break ;;
          5|reinstall) ACTION=reinstall; break ;;
          *) warn "choose keep, upgrade, migrate, repair, or reinstall" ;;
        esac
      done
    fi
  fi
  case "$ACTION" in
    keep) ;;
    install) [ "$existing" = 0 ] || die "install needs a new install; choose upgrade, repair, or reinstall" ;;
    upgrade|migrate|repair|reinstall)
      [ "$existing" = 1 ] || die "$ACTION needs an existing install; nothing changed"
      ;;
    *) die "unknown setup action: $ACTION" ;;
  esac
  case "$ACTION" in upgrade|repair|reinstall) MODE=upgrade ;; esac
  return 0
}

prepared_identity() {
  if [ ! -f "$APP_DIR/.setup-identity" ]; then
    mark_mutation
    fs identity-data "$APP_DIR" "$ROLE" "$METHOD" "$REF" | replace_file "$APP_DIR/.setup-identity" \
      || die "cannot save prepared install identity"
  fi
}

watch_file() {
  local path="$1" area=app name
  if [ -n "$APP_DIR" ] && [[ "$path" = "$APP_DIR/"* ]]; then name="${path#"$APP_DIR/"}"
  elif [[ "$path" = "$HELPER_DIR/"* ]]; then area=helper; name="${path#"$HELPER_DIR/"}"
  elif [[ "$path" = "$HOOK_DIR/"* ]]; then area=hook; name="${path#"$HOOK_DIR/"}"
  else die "file is outside the transaction write set"; fi
  fs watch "$BACKUP_PATH" "$area" "$name" || die "cannot snapshot $name before replacement"
}

seal_file() {
  local path="$1" area=app name
  if [ -n "$APP_DIR" ] && [[ "$path" = "$APP_DIR/"* ]]; then name="${path#"$APP_DIR/"}"
  elif [[ "$path" = "$HELPER_DIR/"* ]]; then area=helper; name="${path#"$HELPER_DIR/"}"
  elif [[ "$path" = "$HOOK_DIR/"* ]]; then area=hook; name="${path#"$HOOK_DIR/"}"
  else die "file is outside the transaction write set"; fi
  fs seal "$BACKUP_PATH" "$area" "$name" || die "cannot record the replacement for $name"
}

replace_file() {
  local path="$1" area=app name
  if [ -n "$APP_DIR" ] && [[ "$path" = "$APP_DIR/"* ]]; then name="${path#"$APP_DIR/"}"
  elif [[ "$path" = "$HELPER_DIR/"* ]]; then area=helper; name="${path#"$HELPER_DIR/"}"
  elif [[ "$path" = "$HOOK_DIR/"* ]]; then area=hook; name="${path#"$HOOK_DIR/"}"
  else die "file is outside the transaction write set"; fi
  fs replace "$BACKUP_PATH" "$area" "$name" "${@:2}"
}

trust_identity() {
  if [ -f "$APP_DIR/.setup-identity" ]; then
    fs owner "$APP_DIR" || die "install identity belongs to another host or directory"
    if [ "$METHOD" != compose-build ]; then
      fs managed "$APP_DIR" "$ROLE" "$METHOD" || die "managed application files changed; left unchanged"
    fi
  elif [ -n "$ST_METHOD" ]; then
    die "existing install has no host identity; left unchanged"
  elif [ -f "$APP_DIR/compose.yaml" ] || [ -f "$APP_DIR/compose.relay.yaml" ]; then
    die "existing application files have no install identity; left unchanged"
  elif [ -e "$APP_DIR/docker-proxy" ] || [ -L "$APP_DIR/docker-proxy" ]; then
    fs empty "$APP_DIR/docker-proxy" || die "existing proxy files have no install identity; left unchanged"
  fi
}
