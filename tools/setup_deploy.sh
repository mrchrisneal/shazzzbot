# shellcheck shell=bash

install_file() {
  local text="$1" dest="$2"
  if [ -f "$dest" ] && [ "$(< "$dest")" = "$text" ]; then return 0; fi
  if dry; then plan "write $dest"; return 0; fi
  mark_mutation
  watch_file "$dest"
  fs mkdir "$(dirname "$dest")" || die "cannot create destination directory"
  printf '%s\n' "$text" | replace_file "$dest" || die "cannot safely replace $dest"
  seal_file "$dest"
  FILES_CHANGED=1
  say "wrote $dest"
}

install_files() {
  local file=compose.yaml text
  if dry; then
    plan "install the $REF compose file and docker-proxy/haproxy.cfg into $APP_DIR as needed"
    return 0
  fi
  [ "$ROLE" = relay ] && file=compose.relay.yaml
  if [ "$METHOD" = compose-image ]; then
    text="$(compose_for_image "$SRC/$file")" || die "cannot prepare the Compose candidate"
    text="$(printf '%s\n' "$text" | fs layout-compose-input "$APP_DIR")" || die "cannot preserve runtime labels"
    install_file "$text" "$APP_DIR/compose.yaml"
  elif [ "$METHOD" = compose-build ]; then
    text="$(fs layout-compose "$APP_DIR" "$SRC/$file" "$SOURCE_DIR")" || die "cannot prepare private build context"
    install_file "$text" "$APP_DIR/$file"
  fi
  if [ "$ROLE" = bot ] && [ "$METHOD" != bot-only ]; then
    if [ ! -f "$APP_DIR/docker-proxy/haproxy.cfg" ] \
        || ! cmp -s "$SRC/docker-proxy/haproxy.cfg" "$APP_DIR/docker-proxy/haproxy.cfg"; then PROXY_CHANGED=1; fi
    text="$(fs read "$SRC/docker-proxy/haproxy.cfg")" || die "cannot read the required proxy configuration"
    [ -n "$text" ] || die "the required proxy configuration is empty"
    install_file "$text" "$APP_DIR/docker-proxy/haproxy.cfg"
  fi
}

proxy_image() {
  local line
  while IFS= read -r line; do
    if [[ "$line" =~ ^[[:space:]]+image:[[:space:]]+(haproxy:[^[:space:]]+@sha256:[0-9a-f]{64})$ ]]; then
      printf '%s\n' "${BASH_REMATCH[1]}"
      return 0
    fi
  done < "$SRC/compose.yaml"
  die "compose.yaml has no digest-pinned haproxy image"
}

ensure_image() {
  local built version
  [ "$IMAGE_READY" = 1 ] && return 0
  case "$METHOD" in
    compose-build)
      version="$(fs read "$SOURCE_DIR/VERSION")" || die "cannot read the build VERSION"
      [[ "$version" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] \
        || die "build VERSION must be canonical MAJOR.MINOR.PATCH"
      if docker image inspect "$DEV_IMAGE" >/dev/null 2>&1; then
        assert_image "$DEV_IMAGE"
        if docker image inspect "$PREV_IMAGE" >/dev/null 2>&1; then assert_image "$PREV_IMAGE"; fi
        docker tag "$DEV_IMAGE" "$PREV_IMAGE" || die "cannot save previous image"
        PREV_READY=1
      elif [ -n "$OLD_BOT_IMAGE" ]; then
        docker tag "$OLD_BOT_IMAGE" "$PREV_IMAGE" || die "cannot preserve the running image"
        # shellcheck disable=SC2034 # read by rollback in setup_prereqs.sh; reviewed 2026-11
        PREV_READY=1
      fi
      say "building the image"
      built="$(cd "$SOURCE_DIR" && COMMAND_TIMEOUT=1800 bounded nice -n 19 ionice -c3 docker build -q \
        --label "org.shazzzbot.install=$INSTALL_ID" --label "org.opencontainers.image.revision=$REF" \
        --label "org.opencontainers.image.version=$version" .)" \
        || die "build failed; the running containers are unchanged"
      [[ "$built" =~ ^sha256:[0-9a-f]{64}$ ]] || die "build returned an invalid image identity"
      docker tag "$built" "$DEV_IMAGE" || die "cannot promote the completed image"
      ;;
    *)
      # shellcheck disable=SC2153 # IMAGE is initialized by setup.sh; reviewed 2026-11
      say "pulling $IMAGE:$REF"
      COMMAND_TIMEOUT=600 docker pull -q "${PINNED_IMAGE:-$IMAGE:$REF}" >/dev/null || die "cannot pull $IMAGE:$REF"
      ;;
  esac
  IMAGE_READY=1
}

wizard_image() {
  if [ "$METHOD" = compose-build ]; then printf '%s\n' "$DEV_IMAGE"; else printf '%s\n' "$PINNED_IMAGE"; fi
}

compose_file() {
  if [ "$ROLE" = relay ] && [ "$METHOD" = compose-build ]; then printf 'compose.relay.yaml\n'
  else printf 'compose.yaml\n'; fi
}

compose() {
  (cd "$APP_DIR" && SHAZZZBOT_INSTALL_ID="$INSTALL_ID" \
    docker compose --project-directory "$APP_DIR" --env-file "$APP_DIR/.env" \
    -p "$PROJECT" -f "$APP_DIR/$(compose_file)" "$@")
}

service() { if [ "$ROLE" = relay ]; then echo relay; else echo shazzzbot; fi; }
container() { if [ "$ROLE" = relay ]; then echo shazzzbot-relay; else echo shazzzbot; fi; }

running() {
  case "$METHOD" in
    compose-*) [ "$(compose ps --format '{{.State}}' "$(service)" 2>/dev/null || true)" = running ] ;;
    *) [ "$(docker inspect --format '{{.State.Running}}' "$(container)" 2>/dev/null || true)" = true ] ;;
  esac
}

logs_since() {
  case "$METHOD" in
    compose-*) compose logs --no-log-prefix --since "$1" "$(service)" 2>&1 || true ;;
    *) docker logs --since "$1" "$(container)" 2>&1 || true ;;
  esac
}

replace_container() {
  if docker container inspect "$1" >/dev/null 2>&1; then
    assert_container "$1"
    docker rm -f "$1" >/dev/null || return 1
  fi
}

run_containers() {
  local emhttp socket_dir gid bind normalized proxy="" image="${PINNED_IMAGE:-$IMAGE:$1}"
  if [ "$ROLLING_BACK" = 1 ] && [ -n "$OLD_BOT_IMAGE" ]; then image="$OLD_BOT_IMAGE"; fi
  if [ "$ROLE" = bot ] && [ "$METHOD" = docker-run ]; then
    if [ "$ROLLING_BACK" = 1 ] && [ -n "$OLD_PROXY_IMAGE" ]; then proxy="$OLD_PROXY_IMAGE"
    else proxy="$(proxy_image)" || return 1; fi
  fi
  temp_in -d "$WORK_ROOT/env.XXXXXX"
  normalized="$TMPF/.env"
  fs env-file "$APP_DIR/.env" "$normalized" || die "cannot normalize the environment file"
  local -a common=(--label "org.shazzzbot.install=$INSTALL_ID" --restart unless-stopped --read-only --tmpfs /tmp --cap-drop ALL
    --log-driver json-file
    --security-opt no-new-privileges:true --log-opt max-size=10m --log-opt max-file=3)
  if [ "$ROLE" = relay ]; then
    bind="$(env_value "$APP_DIR/.env" RELAY_BIND)"
    replace_container shazzzbot-relay || return 1
    docker run -d --name shazzzbot-relay "${common[@]}" --memory 256m --pids-limit 128 -e SHAZZZBOT_ROLE=relay --env-file "$normalized" \
      -p "${bind:-0.0.0.0}:$RELAY_PORT:$RELAY_PORT" -v "$APP_DIR/data:/data" "$image" >/dev/null
    return
  fi
  if ! docker network inspect shazzzbot >/dev/null 2>&1; then
    docker network create --label "org.shazzzbot.install=$INSTALL_ID" shazzzbot >/dev/null || return 1
  else
    [ "$(docker network inspect --format '{{index .Labels "org.shazzzbot.install"}}' shazzzbot)" = "$INSTALL_ID" ] \
      || die "network shazzzbot is not owned by this install"
  fi
  if [ "$METHOD" = docker-run ]; then
    replace_container shazzzbot-proxy || return 1
    docker run -d --name shazzzbot-proxy --network shazzzbot --network-alias docker-proxy "${common[@]}" \
      --user 0 --memory 128m --pids-limit 64 \
      -v "$APP_DIR/docker-proxy/haproxy.cfg:/usr/local/etc/haproxy/haproxy.cfg:ro" \
      -v "$DOCKER_SOCK:/var/run/docker.sock" "$proxy" >/dev/null || return 1
  fi
  emhttp="$(env_value "$APP_DIR/.env" EMHTTP_DIR)"
  socket_dir="$(env_value "$APP_DIR/.env" HELPER_SOCKET_DIR)"
  [ -n "$socket_dir" ] || die "HELPER_SOCKET_DIR must name the prepared socket directory"
  gid="$(env_value "$APP_DIR/.env" HELPER_GID)"
  replace_container shazzzbot || return 1
  docker run -d --name shazzzbot --network shazzzbot "${common[@]}" --memory 512m --pids-limit 128 \
    --group-add "${gid:-10001}" --env-file "$normalized" -e SHAZZZBOT_ROLE=bot \
    -v /proc:/host/proc:ro -v /sys:/host/sys:ro -v "${emhttp:-/var/local/emhttp}:/host/emhttp:ro" \
    -v "$APP_DIR/config.toml:/config/config.toml:ro" -v "$APP_DIR/data:/data" \
    -v "$socket_dir:/run/shazzzbot-helper:ro" \
    "$image" >/dev/null
}

start() {
  local -a extra=()
  if [ "$CONFIG_DRIFT" = 1 ] || { [ "$PROXY_CHANGED" = 1 ] && [ -n "$ST_REF" ]; }; then extra=(--force-recreate); fi
  case "$METHOD" in
    compose-build|compose-image)
      [ "$METHOD" = compose-image ] && { compose pull -q || die "docker compose pull failed"; }
      compose up -d --no-build "${extra[@]}" || die "docker compose up failed"
      ;;
    *) run_containers "$REF" || die "docker run failed" ;;
  esac
}

healthy() {
  local since="$1" deadline=$((SECONDS + (HEALTH_TIMEOUT > 0 ? HEALTH_TIMEOUT : 5))) COMMAND_TIMEOUT
  local -x REQUEST_END="$deadline"
  while :; do
    COMMAND_TIMEOUT=$((deadline - SECONDS))
    [ "$COMMAND_TIMEOUT" -gt 0 ] || COMMAND_TIMEOUT=1
    if running && readiness; then
      if [ "$ROLE" = relay ] || [ "$since" = now ] || logs_since "$since" | grep -q 'logged in as'; then return 0; fi
    fi
    if [ "$HEALTH_TIMEOUT" = 0 ] || [ "$SECONDS" -ge "$deadline" ]; then break; fi
    sleep "$HEALTH_POLL"
  done
  say "readiness not confirmed since $since" >&2
  return 1
}

print_relay_lines() {
  local lines
  lines="$(logs_since "$1" | grep -E '^RELAY_(URL|KEY|FINGERPRINT)=' || true)"
  if [ -n "$lines" ]; then
    printf '\nAdd these lines to the .env of the bot. Put this machine'"'"'s public address in RELAY_URL:\n\n%s\n\n' "$lines"
    printf 'Then forward or open TCP port %s to this machine, and recreate the bot.\n' "$RELAY_PORT"
  else
    say "the relay kept its key from an earlier start; its fingerprint is in the relay log"
  fi
}

deploy() {
  local since
  if dry; then
    plan "prepare the image, start the $ROLE with $METHOD, and wait for it to report healthy"
    return 0
  fi
  if [ -n "$ST_METHOD" ] && ! fs drift "$APP_DIR" "$ROLE" "$METHOD" "$REF" >/dev/null 2>&1; then
    CONFIG_DRIFT=1
  fi
  if [ "$CODE_CHANGED" = 0 ] && [ "$CREATED_CFG" = 0 ] && [ "$FILES_CHANGED" = 0 ] && [ "$ST_METHOD" = "$METHOD" ] \
      && [ "$ST_ROLE" = "$ROLE" ] && [ "$ST_REF" = "$REF" ] && running && runtime_matches \
      && [ "$CONFIG_DRIFT" = 0 ] && healthy "now"; then
    say "the $ROLE already runs ${REF:0:12}; nothing to change"
    return 0
  fi
  prepared_identity
  if [ "$MODE" = setup ] && ! confirm "Start the $ROLE now?" y; then
    say "not started; run setup.sh again to start it"
    return 0
  fi
  mark_mutation
  ensure_image
  ensure_dirs
  since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  say "starting the $ROLE ($METHOD)"
  # shellcheck disable=SC2034 # read by rollback in setup_prereqs.sh; reviewed 2026-11
  SERVICE_CHANGED=1
  start
  say "waiting up to ${HEALTH_TIMEOUT}s for the $ROLE to report healthy"
  if healthy "$since"; then
    watch_file "$(state_file)"
    fs layout-state-data "$APP_DIR" "$METHOD" "$ROLE" "$REF" | replace_file "$(state_file)" \
      || die "cannot save deployment state"
    seal_file "$(state_file)"
    watch_file "$APP_DIR/.setup-identity"
    fs identity-data "$APP_DIR" "$ROLE" "$METHOD" "$REF" | replace_file "$APP_DIR/.setup-identity" \
      || die "cannot save install identity"
    seal_file "$APP_DIR/.setup-identity"
    rm -f -- "$BACKUP_ROOT/pending"
    # shellcheck disable=SC2034 # read by recover in setup_prereqs.sh; reviewed 2026-11
    COMMITTED=1
    say "OK: the $ROLE runs ${REF:0:12}"
    [ "$ROLE" = relay ] && print_relay_lines "$since"
    return 0
  fi
  logs_since "$since" | grep -v -E '^RELAY_KEY=' | tail -n 20 | sed 's/^/  log: /' >&2 || true
  die "the $ROLE did not report healthy within ${HEALTH_TIMEOUT}s; check its log"
}

report() {
  dry && return 0
  case "$METHOD" in
    compose-*) compose ps 2>&1 | sed 's/^/  /' || true ;;
    *) docker ps --filter name=shazzzbot 2>&1 | sed 's/^/  /' || true ;;
  esac
  say "images retained by this install (no cleanup performed):"
  docker image ls --filter "reference=$IMAGE*" \
    --filter "label=org.opencontainers.image.source=${REPO_URL%.git}" \
    --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}' | sed 's/^/  /' || warn "cannot list retained images"
  docker image ls --filter "label=org.shazzzbot.install=$INSTALL_ID" \
    --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}' | sed 's/^/  /' || warn "cannot list retained build images"
  [ -z "$BACKUP_PATH" ] || [ "$BACKUP_PATH" = none ] || say "backup of the previous files: $BACKUP_PATH"
}

