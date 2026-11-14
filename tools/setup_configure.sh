# shellcheck shell=bash

env_value() { fs env "$1" "$2"; }

create_configs() {
  local -a create=() args=() pass=() tty=()
  local out name helper_key="" socket_dir="" v token=""
  [ -f "$APP_DIR/.env" ] || create+=(--create .env)
  if [ "$ROLE" = bot ]; then
    if [ -e "$APP_DIR/config.toml" ] && [ ! -f "$APP_DIR/config.toml" ]; then
      die "$APP_DIR/config.toml is not a file (Docker makes a directory when the file is missing); remove it and run again"
    fi
    [ -f "$APP_DIR/config.toml" ] || create+=(--create config.toml)
  fi
  if [ "${#create[@]}" -eq 0 ]; then
    say "configuration files exist; left unchanged"
    return 0
  fi
  [ "$MODE" = setup ] || [ "$ACTION" = repair ] || die "$APP_DIR lacks .env or config.toml; an upgrade never creates them"
  if dry; then
    plan "start the configuration questions and create: ${create[*]//--create /}"
    return 0
  fi
  mark_mutation
  ensure_image
  temp_in -d "$WORK_ROOT/wizard.XXXXXX"
  out="$TMPF"
  if [ "$HELPER" = yes ] && [ -f "$HELPER_DIR/api.key" ]; then
    helper_key="$(fs read "$HELPER_DIR/api.key")" || die "cannot read the private helper key"
    socket_dir="$WORK_ROOT/socket"
  fi
  SHAZZZBOT_HELPER_KEY="$helper_key" SHAZZZBOT_HELPER_SOCKET_DIR="$socket_dir" \
    fs restore-answers "$BACKUP_ROOT" "$out" "$APP_DIR" "$ROLE" "$METHOD" || die "cannot restore wizard answers"
  create=()
  [ -f "$APP_DIR/.env" ] || [ -f "$out/.env" ] || create+=(--create .env)
  if [ "$ROLE" = bot ]; then
    [ -f "$APP_DIR/config.toml" ] || [ -f "$out/config.toml" ] || create+=(--create config.toml)
  fi
  args=(--examples /setup --out /out --role "$ROLE" "${create[@]}")
  [ "$METHOD" = bot-only ] && args+=(--bot-only)
  [ "$UNATTENDED" = 1 ] && args+=(--no-prompt)
  if [ "$ROLE" = bot ]; then
    if [ "$HELPER" = yes ] && [ -f "$HELPER_DIR/api.key" ]; then
      helper_key="$(fs read "$HELPER_DIR/api.key")"
      socket_dir="$WORK_ROOT/socket"
    else
      socket_dir="$WORK_ROOT/socket"
    fi
  fi
  if [ -f "$APP_DIR/.env" ]; then
    helper_key="$(env_value "$APP_DIR/.env" SHAZZZBOT_HELPER_KEY)"
  fi
  for v in SHAZZZBOT_DISCORD_TOKEN SHAZZZBOT_APP_ID SHAZZZBOT_GUILD_ID SHAZZZBOT_STATUS_CHANNEL_ID \
      SHAZZZBOT_AUDIT_CHANNEL_ID SHAZZZBOT_COMMANDS_CHANNEL_ID SHAZZZBOT_TIMEZONE SHAZZZBOT_UPS SHAZZZBOT_RELAY_URL \
      SHAZZZBOT_RELAY_KEY SHAZZZBOT_RELAY_FINGERPRINT SHAZZZBOT_RELAY_ALLOW SHAZZZBOT_RELAY_KEYS SHAZZZBOT_HELPER_KEY \
      SHAZZZBOT_HELPER_SOCKET_DIR SHAZZZBOT_SET_ICON DISCORD_TOKEN; do
    pass+=(-e "$v")
  done
  [ -f "$APP_DIR/.env" ] && token="$(env_value "$APP_DIR/.env" DISCORD_TOKEN)"
  if [ -t 0 ]; then tty=(-t); fi
  if [ "${#create[@]}" -eq 0 ]; then
    say "reusing saved wizard answers"
  elif [ -n "${SHAZZZBOT_WIZARD_PYTHON:-}" ]; then
    args=("${args[@]/#\/setup/$SRC}")
    args=("${args[@]/#\/out/$out}")
    DISCORD_TOKEN="$token" SHAZZZBOT_HELPER_KEY="$helper_key" SHAZZZBOT_HELPER_SOCKET_DIR="$socket_dir" \
      SHAZZZBOT_APP="${SHAZZZBOT_WIZARD_APP:-$SRC}" COMMAND_TIMEOUT=1800 \
      bounded "$SHAZZZBOT_WIZARD_PYTHON" -I -B "$SRC/tools/setup_config.py" "${args[@]}" || die "configuration not created"
  else
    DISCORD_TOKEN="$token" SHAZZZBOT_HELPER_KEY="$helper_key" SHAZZZBOT_HELPER_SOCKET_DIR="$socket_dir" COMMAND_TIMEOUT=1800 \
      docker run --rm -i "${tty[@]+"${tty[@]}"}" --read-only \
      --tmpfs /tmp:rw,noexec,nosuid,size=16m -e TMPDIR=/tmp --cap-drop ALL \
      --security-opt no-new-privileges:true --user 0 "${pass[@]}" \
      -v "$SRC/tools/setup_config.py:/setup/setup_config.py:ro" -v "$SRC/.env.example:/setup/.env.example:ro" \
      -v "$SRC/config.example.toml:/setup/config.example.toml:ro" -v "$out:/out" \
      "$(wizard_image)" python -I -B /setup/setup_config.py "${args[@]}" || die "configuration not created"
  fi
  fs save-answers "$BACKUP_ROOT" "$out" "$APP_DIR" "$ROLE" "$METHOD" || die "cannot preserve wizard answers"
  for name in .env config.toml; do
    [ -f "$out/$name" ] || continue
    fs trust "$out/$name" || die "wizard produced an untrusted file"
    chmod 600 -- "$out/$name"
    if [ "$name" = config.toml ] && [ "$REQUIRE_ROOT" = 1 ]; then chown "$BOT_UID" -- "$out/$name"; fi
    watch_file "$APP_DIR/$name"
    local owner
    owner="$(id -u)"
    if [ "$name" = config.toml ] && [ "$REQUIRE_ROOT" = 1 ]; then owner="$BOT_UID"; fi
    bounded cat -- "$out/$name" | replace_file "$APP_DIR/$name" "$owner" || die "cannot install wizard output"
    seal_file "$APP_DIR/$name"
    # shellcheck disable=SC2034 # read by deploy in setup_deploy.sh; reviewed 2026-11
    CREATED_CFG=1
    say "created $APP_DIR/$name (mode 600)"
  done
}

ensure_dirs() {
  local dir gid data_uid
  if [ ! -d "$APP_DIR/data" ]; then
    if dry; then plan "mkdir $APP_DIR/data"; else
      fs mkdir "$APP_DIR/data" || die "cannot create the data directory"
      if [ "$REQUIRE_ROOT" = 1 ]; then chown "$BOT_UID" -- "$APP_DIR/data"; fi
    fi
  fi
  data_uid="$(id -u)"
  [ "$REQUIRE_ROOT" != 1 ] || data_uid="$BOT_UID"
  dry || fs data-directory "$APP_DIR/data" "$data_uid" || die "untrusted data directory"
  [ "$ROLE" = bot ] || return 0
  dir="$(env_value "$APP_DIR/.env" HELPER_SOCKET_DIR)"
  [ "$dir" = "$WORK_ROOT/socket" ] || die "HELPER_SOCKET_DIR must be $WORK_ROOT/socket"
  gid="$(env_value "$APP_DIR/.env" HELPER_GID)"
  gid="${gid:-10001}"
  fs socket-directory "$dir" "$gid" || die "cannot prepare the helper socket directory in place"

}

