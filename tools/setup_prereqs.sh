# shellcheck shell=bash
# Prerequisite probes, the table, and the method and helper menus for setup.sh, which sources this file.

free_mb() {
  local kb
  kb="$(df -Pk -- "$(existing_parent "$1")" 2>/dev/null | awk 'NR == 2 { print $4 }')"
  [[ "$kb" =~ ^[0-9]+$ ]] && printf '%s\n' "$((kb / 1024))"
}

reach() {
  local code
  command -v curl >/dev/null || return 1
  code="$(curl -sS -o /dev/null --max-time 10 -w '%{http_code}' -- "$1" 2>/dev/null || true)"
  [[ "$code" =~ ^[1-5][0-9][0-9]$ ]]
}

probe() {
  case "$1" in
    root) [ "$REQUIRE_ROOT" = 0 ] || [ "$(id -u)" -eq 0 ] ;;
    unraid) [ -f "$UNRAID_MARKER" ] ;;
    flock) command -v flock >/dev/null ;;
    docker) command -v docker >/dev/null && docker info >/dev/null 2>&1 ;;
    compose)
      local v
      v="$(docker compose version --short 2>/dev/null || true)"
      v="${v#v}"
      [[ "${v%%.*}" =~ ^[0-9]+$ ]] && [ "${v%%.*}" -ge 2 ] ;;
    git) git --version >/dev/null 2>&1 ;;
    curl) command -v curl >/dev/null ;;
    tar) command -v tar >/dev/null ;;
    github) reach "$GITHUB_PROBE" ;;
    ghcr) reach "$GHCR_PROBE" ;;
    space)
      local available storage
      storage="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)" || return 1
      [[ "$storage" = /* ]] || return 1
      available="$(free_mb "$storage")" || return 1
      [ "$available" -ge "$MIN_FREE_MB" ] ;;
    appdata)
      local parent
      parent="$(existing_parent "$APP_DIR")"
      [ -w "$parent" ] && fs external-root "$parent" >/dev/null 2>&1 ;;
    socket) [ -S "$DOCKER_SOCK" ] ;;
    port)
      [ -n "$ST_METHOD" ] && return 0
      command -v ss >/dev/null || return 0
      [ -z "$(ss -ltnH "sport = :$RELAY_PORT" 2>/dev/null)" ] ;;
    values)
      [ "$UNATTENDED" = 0 ] && return 0
      if [ -n "${CONFIG_SOURCE:-}" ] && [ -f "$CONFIG_SOURCE/.env" ] \
          && { [ "$ROLE" = relay ] || [ -f "$CONFIG_SOURCE/config.toml" ]; }; then return 0; fi
      if [ ! -f "$APP_DIR/.env" ]; then
        if [ "$ROLE" = relay ]; then [ -n "${SHAZZZBOT_RELAY_ALLOW:-}" ]
        else [ -n "${SHAZZZBOT_DISCORD_TOKEN:-}" ] && [ -n "${SHAZZZBOT_GUILD_ID:-}" ] \
          && [ -n "${SHAZZZBOT_STATUS_CHANNEL_ID:-}" ]; fi
        return
      fi
      if [ "$ROLE" = relay ]; then
        [ -f "$APP_DIR/.env" ] || [ -n "${SHAZZZBOT_RELAY_ALLOW:-}" ]
      else
        { [ -f "$APP_DIR/.env" ] || [ -n "${SHAZZZBOT_DISCORD_TOKEN:-}" ]; } && { [ -f "$APP_DIR/config.toml" ] \
          || { [ -n "${SHAZZZBOT_GUILD_ID:-}" ] && [ -n "${SHAZZZBOT_STATUS_CHANNEL_ID:-}" ]; }; }
      fi ;;
    python) bounded python3 -I -B -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1 ;;
    hostpython) [ -x /usr/bin/python3 ] \
      && bounded /usr/bin/python3 -I -B -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1 ;;
    userscripts) [ -d "$(dirname "$HOOK_DIR")" ] ;;
    boot) [ -w "$(existing_parent "$HOOK_DIR")" ] ;;
  esac
}

probe_text() {
  case "$1" in
    root) printf 'running as root|Run the script as root: sudo bash setup.sh, or log in as root on Unraid' ;;
    unraid) printf 'Unraid (%s)|The bot node must be an Unraid server; use --role relay elsewhere' "$UNRAID_MARKER" ;;
    flock) printf 'flock|Install util-linux, for example apt install util-linux' ;;
    docker) printf 'Docker engine running|Unraid: Settings > Docker > Enable Docker: Yes. Elsewhere: https://docs.docker.com/engine/install/' ;;
    compose) printf 'docker compose v2|Unraid: Apps > search Docker Compose Manager > Install. Elsewhere: apt install docker-compose-plugin' ;;
    git) printf 'git|Unraid 7 includes git; on Unraid 6 install it from the NerdTools plugin. Elsewhere: apt install git' ;;
    curl) printf 'curl|Install curl, for example apt install curl' ;;
    tar) printf 'tar|Install tar, for example apt install tar' ;;
    github) printf 'network reach to github.com|Check DNS and the firewall: curl -I https://github.com' ;;
    ghcr) printf 'network reach to ghcr.io|Check DNS and the firewall: curl -I https://ghcr.io/v2/' ;;
    space) printf 'free space, %s MiB or more|Free space on the appdata share and in the Docker image (Settings > Docker)' "$MIN_FREE_MB" ;;
    appdata) printf 'install directory writable and trusted (%s)|Use a root-owned directory with root-owned ancestors and no group/other write access, or pass --dir' "$APP_DIR" ;;
    socket) printf 'Docker socket (%s), for docker-proxy|Start Docker, or choose bot-only to skip the docker-proxy' "$DOCKER_SOCK" ;;
    port) printf 'port %s free on this machine|Stop the program on the port: ss -ltnp "sport = :%s". Then open %s in the firewall' \
      "$RELAY_PORT" "$RELAY_PORT" "$RELAY_PORT" ;;
    values)
      if [ "$ROLE" = relay ]; then
        printf 'relay names supplied|Set --relay-allow, or run once without --unattended'
      else
        printf 'Discord token and ids supplied|Set SHAZZZBOT_DISCORD_TOKEN, --guild-id, and --status-channel, or run without --unattended'
      fi ;;
    python) printf 'python3 3.11 or later|Unraid: Apps > search Python 3 for UNRAID > Install' ;;
    hostpython) printf '/usr/bin/python3 3.11 or later, for the helper|Unraid: Apps > search Python 3 for UNRAID > Install' ;;
    userscripts) printf 'User Scripts plugin, for the helper|Unraid: Apps > search User Scripts > Install' ;;
    boot) printf 'flash drive writable (%s), for the helper|Make sure that /boot is mounted read-write' "$(existing_parent "$HOOK_DIR")" ;;
  esac
}

needs() {
  local base="root python flock docker space appdata values"
  [ "$ROLE" = bot ] && base="unraid $base"
  [ "$ROLE" = relay ] && base="$base port"
  case "$1" in
    compose-image) printf '%s compose curl tar github ghcr%s\n' "$base" "$([ "$ROLE" = bot ] && echo ' socket')" ;;
    compose-build)
      if [ -n "${SHIP_PACK:-}" ]; then printf '%s compose git%s\n' "$base" "$([ "$ROLE" = bot ] && echo ' socket')"
      else printf '%s compose git curl github%s\n' "$base" "$([ "$ROLE" = bot ] && echo ' socket')"; fi ;;
    docker-run) printf '%s curl tar github ghcr%s\n' "$base" "$([ "$ROLE" = bot ] && echo ' socket')" ;;
    bot-only) printf '%s curl tar github ghcr\n' "$base" ;;
    helper) printf 'python hostpython userscripts boot\n' ;;
  esac
}

run_probes() {
  local p
  OK=()
  for p in root unraid flock docker compose git curl tar github ghcr space appdata socket port values python \
      hostpython userscripts boot; do
    if probe "$p"; then OK["$p"]=1; else OK["$p"]=0; fi
  done
}

missing() {
  local p out=""
  for p in $(needs "$1"); do
    [ "${OK[$p]}" = 1 ] || out+="$p "
  done
  printf '%s\n' "${out% }"
}

print_table() {
  local p text seen=" " m
  printf '\nPrerequisites (%s node):\n' "$ROLE"
  for m in $([ "$ROLE" = relay ] && echo "$METHODS_RELAY" || echo "$METHODS_BOT") helper; do
    [ "$m" = helper ] && [ "$ROLE" = relay ] && continue
    for p in $(needs "$m"); do
      [[ "$seen" == *" $p "* ]] && continue
      seen+="$p "
      text="$(probe_text "$p")"
      if [ "${OK[$p]}" = 1 ]; then
        printf '  ok       %s\n' "${text%%|*}"
      else
        printf '  MISSING  %s\n           fix: %s\n' "${text%%|*}" "${text#*|}"
      fi
    done
  done
}

select_method() {
  local m answer n
  local -a avail=() names
  read -r -a names <<< "$([ "$ROLE" = relay ] && echo "$METHODS_RELAY" || echo "$METHODS_BOT")"
  [ -z "$METHOD" ] || [[ " ${names[*]} " == *" $METHOD "* ]] || die "method $METHOD is not offered for a $ROLE node"
  while :; do
    run_probes
    print_table
    avail=()
    for m in "${names[@]}"; do
      [ -z "$(missing "$m")" ] && avail+=("$m")
    done
    if [ -n "$METHOD" ]; then
      [ -z "$(missing "$METHOD")" ] && return 0
      say "method $METHOD needs: $(missing "$METHOD")"
      [ "$UNATTENDED" = 0 ] || die "missing prerequisites for $METHOD: $(missing "$METHOD"); see the table above"
    elif [ "$UNATTENDED" = 1 ]; then
      die "--unattended needs --method (available now: ${avail[*]:-none})"
    fi
    printf '\nInstall methods:\n'
    n=0
    for m in "${names[@]}"; do
      if [[ " ${avail[*]} " == *" $m "* ]]; then
        n=$((n + 1))
        printf '  %s) %s\n' "$n" "$m"
      else
        printf '  -  %s (needs: %s)\n' "$m" "$(missing "$m")"
      fi
    done
    answer="$(ask_line "Choose a number, r to recheck, or c to cancel:")" || exit 1
    case "$answer" in
      r|R) continue ;;
      c|C) die "setup cancelled" ;;
      *)
        if [[ "$answer" =~ ^[0-9]+$ ]] && [ "$answer" -ge 1 ] && [ "$answer" -le "${#avail[@]}" ]; then
          METHOD="${avail[$((answer - 1))]}"
          return 0
        fi
        warn "not a choice: $answer" ;;
    esac
  done
}

select_helper() {
  local answer installed=0
  if [ "$ROLE" = relay ]; then
    HELPER=no
    return 0
  fi
  [ "$HELPER" = no ] && return 0
  [ -f "$HELPER_DIR/helper.pyz" ] && installed=1
  if [ "$HELPER" = ask ] && [ "$installed" = 0 ] && { [ "$MODE" = upgrade ] || [ "$UNATTENDED" = 1 ] || dry; }; then
    HELPER=no
    return 0
  fi
  while :; do
    if [ -z "$(missing helper)" ]; then
      if [ "$HELPER" = ask ] && [ "$installed" = 0 ]; then
        if confirm "Install the optional host helper (Tasks section and UPS settings)?" n; then
          HELPER=yes
        else
          HELPER=no
        fi
      else
        HELPER=yes
      fi
      return 0
    fi
    say "the host helper needs: $(missing helper); see the table above"
    if [ "$UNATTENDED" = 1 ]; then
      [ "$HELPER" = yes ] && die "--helper given, but the helper prerequisites are missing"
      warn "host helper update skipped"
      HELPER=no
      return 0
    fi
    answer="$(ask_line "Helper: s to skip it, r to recheck, or c to cancel:")" || exit 1
    case "$answer" in
      s|S) HELPER=no; return 0 ;;
      r|R) run_probes; print_table ;;
      c|C) die "setup cancelled" ;;
    esac
  done
}

fs() {
  case "$1" in
    layout-prepare|source-pack)
      bounded nice -n 19 ionice -c3 python3 -I -B "$SETUP_HOME/tools/setup_manage.py" "$@" ;;
    *) bounded python3 -I -B "$SETUP_HOME/tools/setup_manage.py" "$@" ;;
  esac
}

trust_install() {
  local p
  local -a paths=("$APP_DIR" "$BACKUP_ROOT")
  [ "$HELPER" != yes ] || paths+=("$HELPER_DIR")
  case "$APP_DIR/" in /boot/*) die "application code and Compose files must be outside /boot" ;; esac
  for p in "${paths[@]}"; do
    fs external-root "$(existing_parent "$p")" \
      || die "untrusted install path: $p; external Docker/Git path access still requires trusted ancestors"
    [[ "$p" != / && "$p" != *$'\n'* && "$p" != *$'\r'* && "$p" != *:* && "$p" != *,* ]] \
      || die "unsafe install path: parent traversal, control characters, colons, and commas are forbidden"
  done
  for p in .env config.toml compose.yaml compose.relay.yaml .setup-state .setup-identity docker-proxy/haproxy.cfg; do
    if [ -e "$APP_DIR/$p" ] || [ -L "$APP_DIR/$p" ]; then
      if [[ "$p" = .env || "$p" = config.toml ]]; then
        local owner=0; [ "$p" != config.toml ] || owner="$BOT_UID"
        [ "$REQUIRE_ROOT" = 1 ] || owner="$(id -u)"
        fs private "$APP_DIR/$p" "$owner" || die "untrusted $p: require owner uid $owner and mode 0600 (0700 on vfat)"
      else fs trust "$APP_DIR/$p" || die "untrusted install file: $p"; fi
    fi
  done
  for p in compose.override.yaml compose.override.yml docker-compose.override.yaml docker-compose.override.yml; do
    if [ -e "$APP_DIR/$p" ] || [ -L "$APP_DIR/$p" ]; then die "remove the unreviewed Compose override $p before setup"; fi
  done
}

backup_now() {
  [ -z "$BACKUP_PATH" ] || return 0
  if dry; then BACKUP_PATH=dry; plan "back up and verify the install in $BACKUP_ROOT"; return 0; fi
  if [ "$HELPER_ONLY" = 1 ]; then
    BACKUP_PATH="$(helper_transaction snapshot "$HELPER_DIR" "$SHAZZZBOT_HELPER_APP" "$KEEP")" \
      || die "helper backup failed; nothing will be replaced"
    return 0
  fi
  BACKUP_PATH="$(fs backup "$APP_DIR" "$HELPER_DIR" "$BACKUP_ROOT" "$KEEP" "$OLD_BOT_IMAGE" "$OLD_PROXY_IMAGE")" \
    || die "backup failed; nothing will be replaced"
  say "verified backup: $BACKUP_PATH"
}

mark_mutation() {
  backup_now
  if [ "$HELPER_ONLY" = 1 ]; then
    MUTATING=1
    helper_transaction pending "$HELPER_DIR" "$BACKUP_PATH" || die "cannot save helper recovery marker"
    return 0
  fi
  if [ "$MUTATING" = 0 ]; then
    fs remember-id "$BACKUP_ROOT" "$APP_DIR" "$INSTALL_ID" || die "cannot preserve install identity for recovery"
  fi
  MUTATING=1
  printf '%s\n' "$BACKUP_PATH" | fs write "$BACKUP_ROOT/pending" || die "cannot save recovery marker"
}

audit_configuration() {
  local output status=0
  CONFIG_CURRENT=0
  [ "$ROLE" = bot ] || return 0
  if [ ! -e "$APP_DIR/config.toml" ] && [ ! -L "$APP_DIR/config.toml" ]; then return 0; fi
  if output="$(bounded python3 -I -B - "$SETUP_HOME" "$APP_DIR/config.toml" <<'PYTHON'
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from tools.setup_files import read_safe
from tools.setup_schema import audit

try:
    original = read_safe(Path(sys.argv[2]), private=True)
    report = audit(original)
except (OSError, ValueError) as error:
    print("current\t0")
    print("! " + str(error))
    raise SystemExit(1) from None
print("current\t" + str(int(not report.errors and report.candidate == original)))
print(report.summary())
raise SystemExit(bool(report.errors))
PYTHON
)"; then :; else status=$?; fi
  case "$output" in
    $'current\t1\n'*) CONFIG_CURRENT=1; output="${output#*$'\n'}" ;;
    $'current\t0\n'*) output="${output#*$'\n'}" ;;
  esac
  printf '%s\n' "$output"
  return "$status"
}

migrate_configuration() {
  if dry; then plan "back up config.toml and add available defaults as comments"; return 0; fi
  [ "$ROLE" = bot ] || die "config migration is only available for bot installs"
  if [ ! -f "$APP_DIR/config.toml" ] || [ ! -f "$APP_DIR/.env" ]; then
    die "config migration needs existing config.toml and .env files"
  fi
  audit_configuration || die "config errors must be fixed before migration"
  [ "$CONFIG_CURRENT" = 0 ] || { say "Config is current; nothing to change"; return 0; }
  if [ -z "${SHAZZZBOT_WIZARD_PYTHON:-}" ]; then
    if [ -z "$OLD_BOT_IMAGE" ] || ! docker image inspect "$OLD_BOT_IMAGE" >/dev/null 2>&1; then
      die "config migration needs the installed bot image locally; no image was pulled"
    fi
  fi
  mark_mutation
  watch_file "$APP_DIR/config.toml"
  if [ -n "${SHAZZZBOT_WIZARD_PYTHON:-}" ]; then
    SHAZZZBOT_APP="${SHAZZZBOT_WIZARD_APP:-$SETUP_HOME}" bounded "$SHAZZZBOT_WIZARD_PYTHON" -I -B \
      "$SETUP_HOME/tools/setup_config.py" \
      --migrate-config "$APP_DIR/config.toml" --confirm --transaction "$BACKUP_PATH" || die "config migration failed"
  else
    bounded python3 -I -B - "$SETUP_HOME" "$APP_DIR" "$OLD_BOT_IMAGE" "$BACKUP_PATH" <<'PYTHON' || die "config migration failed"
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from shazzzbot import __version__
from tools.setup_files import read_safe
from tools.setup_schema import audit
from tools.setup_transaction import migrate_config

app = Path(sys.argv[2])
original = read_safe(app / "config.toml", private=True)
env = read_safe(app / ".env", private=True)
report = audit(original)
if report.errors:
    raise SystemExit("\n".join(report.errors))
code = '''import json, sys
sys.path.insert(0, "/setup")
from tools.setup_config import validate_config_bytes
data = json.load(sys.stdin)
validate_config_bytes(data["config"].encode("utf-8"), data["env"].encode("utf-8"))
'''
payload = json.dumps({"config": report.candidate.decode("utf-8"), "env": env.decode("utf-8")}).encode("utf-8")
subprocess.run(["docker", "run", "--rm", "-i", "--read-only", "--network", "none", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges:true", "--user", "0", "-v", sys.argv[1] + ":/setup:ro",
                sys.argv[3], "python", "-I", "-B", "-c", code], input=payload, check=True, timeout=60)
migrate_config(app, original, report.candidate, __version__, Path(sys.argv[4]))
PYTHON
  fi
  seal_file "$APP_DIR/config.toml"
  watch_file "$APP_DIR/.setup-identity"
  fs identity-data "$APP_DIR" "$ROLE" "$METHOD" "$ST_REF" | replace_file "$APP_DIR/.setup-identity" \
    || die "cannot save migrated install identity"
  seal_file "$APP_DIR/.setup-identity"
  rm -f -- "$BACKUP_ROOT/pending"
  COMMITTED=1
  say "Config migrated; running containers were left unchanged"
}

validate_configs() {
  local candidate owner
  dry && return 0
  fs private "$APP_DIR/.env" "$(id -u)" || die "untrusted .env: require root ownership and mode 0600"
  temp_in -d "$WORK_ROOT/candidate.XXXXXX"
  candidate="$TMPF"
  fs read "$APP_DIR/.env" | fs write "$candidate/.env" || die "cannot stage the environment candidate"
  if [ "$ROLE" = bot ]; then
    owner="$BOT_UID"
    [ "$REQUIRE_ROOT" = 1 ] || owner="$(id -u)"
    fs private "$APP_DIR/config.toml" "$owner" || die "untrusted config.toml: require owner uid $owner and mode 0600"
    fs read "$APP_DIR/config.toml" | fs write "$candidate/config.toml" || die "cannot stage the config candidate"
  fi
  validate_candidate "$candidate"
  validate_runtime_paths
}

validate_runtime_paths() {
  local dir gid
  [ "$ROLE" = bot ] || return 0
  dir="$(env_value "$APP_DIR/.env" HELPER_SOCKET_DIR)"
  dir="${dir:-$WORK_ROOT/socket}"
  gid="$(env_value "$APP_DIR/.env" HELPER_GID)"
  gid="${gid:-10001}"
  if ! [[ "$dir" = "$WORK_ROOT/socket" && "$gid" =~ ^[0-9]{1,10}$ ]] \
    || [ "$((10#$gid))" -gt 4294967294 ]; then
    die "invalid helper socket path or group"
  fi
  fs external-root "$(existing_parent "$dir")" || die "helper socket ancestors must be private"
}

validate_candidate() {
  local candidate="$1"
  if [ -n "${SHAZZZBOT_WIZARD_PYTHON:-}" ]; then
    SHAZZZBOT_APP="${SHAZZZBOT_WIZARD_APP:-$SRC}" bounded "$SHAZZZBOT_WIZARD_PYTHON" -I -B \
      "$SRC/tools/setup_config.py" --validate "$candidate" "$ROLE" || die "config validation failed"
  else
    if ! docker image inspect "$(wizard_image)" >/dev/null 2>&1 \
        || { [ "$METHOD" = compose-build ] && [ "$CODE_CHANGED" = 1 ]; }; then
      mark_mutation
      ensure_image
    fi
    docker run --rm --read-only --tmpfs /tmp:rw,noexec,nosuid,size=16m --cap-drop ALL \
      --security-opt no-new-privileges:true --user 0 -e TMPDIR=/tmp \
      -v "$SRC/tools/setup_config.py:/setup/setup_config.py:ro" -v "$candidate:/candidate:ro" \
      "$(wizard_image)" python -I -B /setup/setup_config.py --validate /candidate "$ROLE" \
      || die "config validation failed"
  fi
}

assert_container() {
  local name="$1" label
  if ! docker container inspect "$name" >/dev/null 2>&1; then return 0; fi
  label="$(docker inspect --format '{{index .Config.Labels "org.shazzzbot.install"}}' "$name")" || return 1
  [ "$label" = "$INSTALL_ID" ] || die "container $name is not owned by this install; left unchanged"
}

assert_resources() {
  local name ids
  if [[ "$METHOD" = compose-* ]]; then
    ids="$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT")" || die "cannot list project containers"
    while IFS= read -r name; do [ -z "$name" ] || assert_container "$name"; done <<< "$ids"
    assert_named_resource network "${PROJECT}_default"
  else
    if [ "$ROLE" = bot ]; then assert_named_resource network shazzzbot; fi
    assert_container "$(container)"
    if [ "$ROLE" = bot ] && [ "$METHOD" != bot-only ]; then assert_container shazzzbot-proxy; fi
  fi
}

helper_identity() {
  [ -n "${HELPER_DIR:-}" ] && [ -f "$HELPER_DIR/config.json" ] || return 0
  fs "$1" "$HELPER_DIR/config.json" "$HELPER_RUNTIME"
}

stop_helper() {
  local timeout deadline identity
  timeout="$(helper_identity signal)" || return 1
  [ -n "$timeout" ] || return 0
  deadline=$((SECONDS + timeout))
  local -x REQUEST_END="$deadline"
  while :; do
    identity="$(helper_identity helper)" || return 1
    [ -n "$identity" ] || return 0
    [ "$SECONDS" -lt "$deadline" ] || { warn "helper did not stop before its active drain deadline"; return 1; }
    sleep 1
  done
}

start_helper() {
  check_helper_launcher
  COMMAND_TIMEOUT=86500 fs start-helper "$HOOK_DIR/script" "$HELPER_DIR/config.json" "$HELPER_DIR/helper.pyz"
}

rollback() {
  local since failed=0 restore_helper="$HELPER_CHANGED"
  local -a targets=("$(service)")
  [ "$ROLE" != bot ] || targets+=(docker-proxy)
  export ROLLING_BACK=1
  [ -n "$BACKUP_PATH" ] && [ "$BACKUP_PATH" != dry ] || return 1
  if [ "$HELPER_CHANGED" = 1 ] && ! (stop_helper); then
    warn "helper is still active; helper files will not be restored"
    restore_helper=0
    failed=1
  fi
  if [ -z "$ST_REF" ] && [ "$SERVICE_CHANGED" = 1 ]; then
    if (assert_resources); then
      if [[ "$METHOD" = compose-* ]] && [ -f "$APP_DIR/$(compose_file)" ]; then
        (compose rm -s -f "${targets[@]}") || failed=1
      elif [[ "$METHOD" != compose-* ]]; then
        (replace_container "$(container)") || failed=1
        if [ "$ROLE" = bot ] && [ "$METHOD" != bot-only ]; then
          (replace_container shazzzbot-proxy) || failed=1
        fi
      fi
    else
      failed=1
    fi
  fi
  if [ "$failed" != 0 ]; then warn "recovery stopped before file restoration; active resources remain"; return 1; fi
  fs restore "$BACKUP_PATH" "$APP_DIR" "$HELPER_DIR" "$restore_helper" || return 1
  if [[ "$METHOD" = compose-* ]] && [ -n "$OLD_BOT_IMAGE" ]; then
    fs layout-rollback-compose "$APP_DIR" "$APP_DIR/$(compose_file)" "$OLD_BOT_IMAGE" || return 1
  fi
  if [ "$METHOD" = compose-build ] && [ "$PREV_READY" = 1 ]; then docker tag "$PREV_IMAGE" "$DEV_IMAGE" || return 1; fi
  [ "$failed" = 0 ] || return 1
  if [ "$HELPER_CHANGED" = 1 ] && [ "$HELPER_WAS_RUNNING" = 1 ]; then start_helper || return 1; fi
  if [ -z "$ST_REF" ]; then
    say "no previous deployment; fresh install files rolled back; Docker networks, volumes, and images were retained"
    return 0
  fi
  if [ "$SERVICE_CHANGED" = 0 ]; then healthy now; return; fi
  since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  if [ "$METHOD" = compose-build ] || [ "$METHOD" = compose-image ]; then
    compose up -d --no-build --force-recreate || return 1
  else
    run_containers "$ST_REF" || return 1
  fi
  healthy "$since"
}

recover() {
  if [ "$HELPER_ONLY" = 1 ]; then helper_only_recover; return; fi
  local recovery_seconds=300
  [ "$MUTATING" = 1 ] && [ "$COMMITTED" = 0 ] || return 0
  MUTATING=0
  warn "installation failed; restoring the verified backup"
  [ "$HELPER_CHANGED" != 1 ] || recovery_seconds=86730
  if ( export RECOVERY_END=$((SECONDS + recovery_seconds)); rollback ); then
    if [ -n "$ST_REF" ]; then
      fs identity "$APP_DIR" "$ROLE" "$METHOD" "$ST_REF" || { warn "cannot record recovered identity"; return 1; }
    fi
    rm -f -- "$BACKUP_ROOT/pending"
    if [ -n "$ST_REF" ]; then warn "previous version runs again; recovery health verified"
    else warn "no previous deployment; fresh install files rolled back; Docker networks, volumes, and images were retained"; fi
  else warn "recovery could not be verified; inspect the install and backup $BACKUP_PATH before retrying"; fi
}


readiness() {
  local proxy daemon bot
  if [ "$ROLE" = bot ] && [ "$METHOD" != bot-only ]; then
    if [[ "$METHOD" = compose-* ]]; then proxy="$(compose ps -q docker-proxy)" || return 1
    else proxy=shazzzbot-proxy; fi
    [ -n "$proxy" ] || return 1
    docker exec "$proxy" haproxy -c -f /usr/local/etc/haproxy/haproxy.cfg >/dev/null 2>&1 || return 1
    daemon="$(docker info --format '{{.ID}}')" || return 1
    [ -n "$daemon" ] || return 1
    if [[ "$METHOD" = compose-* ]]; then bot="$(compose ps -q shazzzbot)" || return 1
    else bot=shazzzbot; fi
    [ -n "$bot" ] || return 1
    docker exec "$bot" python -I -B -c \
      'import json,sys,urllib.request; o=urllib.request.build_opener(urllib.request.ProxyHandler({})); r=o.open("http://docker-proxy:2375/info",timeout=5); d=r.read(1048577); sys.exit(len(d)>1048576 or json.loads(d).get("ID")!=sys.argv[1])' \
      "$daemon" >/dev/null 2>&1 || return 1
  fi
  local -a command=(python -I -B -m shazzzbot.discover)
  if [ "$ROLE" = bot ]; then
    command+=(ready /config/config.toml)
  else
    command+=(ready-relay)
  fi
  if [[ "$METHOD" = compose-* ]]; then
    compose exec -T "$(service)" "${command[@]}" >/dev/null 2>&1
  else
    docker exec "$(container)" "${command[@]}" >/dev/null 2>&1
  fi
}

check_pending() {
  if [ -e "$BACKUP_ROOT/pending" ] || [ -L "$BACKUP_ROOT/pending" ]; then
    fs trust "$BACKUP_ROOT/pending" || die "untrusted recovery marker"
    die "an interrupted install needs recovery from $(< "$BACKUP_ROOT/pending"); inspect it before retrying"
  fi
}

assert_image() {
  local label
  label="$(docker image inspect --format '{{index .Config.Labels "org.shazzzbot.install"}}' "$1")" || return 1
  [ "$label" = "$INSTALL_ID" ] || die "image $1 is not owned by this install"
}

capture_images() {
  local name found_image
  [ -n "$ST_METHOD" ] || return 0
  local app_container proxy_container=shazzzbot-proxy
  app_container="$(container)" || die "cannot identify application container"
  if [[ "$METHOD" = compose-* ]]; then
    if [ "${ACTION:-}" = repair ] && [ ! -f "$APP_DIR/$(compose_file)" ]; then
      app_container="$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT" \
        --filter "label=com.docker.compose.service=$(service)")" || die "cannot identify current service"
      if [ "$ROLE" = bot ]; then
        proxy_container="$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT" \
          --filter "label=com.docker.compose.service=docker-proxy")" || die "cannot identify current proxy"
      fi
      if [[ "$app_container" = *$'\n'* || "$proxy_container" = *$'\n'* ]]; then
        die "multiple containers match the installed service; left unchanged"
      fi
    else
      app_container="$(compose ps -q "$(service)")" || die "cannot identify current service"
      if [ "$ROLE" = bot ]; then proxy_container="$(compose ps -q docker-proxy)" || die "cannot identify current proxy"; fi
    fi
  fi
  for name in "$app_container" "$proxy_container"; do
    [ -n "$name" ] || continue
    if docker container inspect "$name" >/dev/null 2>&1; then
      assert_container "$name"
      found_image="$(docker inspect --format '{{.Image}}' "$name")" || die "cannot capture the previous image"
      [[ "$found_image" =~ ^sha256:[0-9a-f]{64}$ ]] || die "invalid previous image identity"
      if [ "$name" = "$proxy_container" ]; then OLD_PROXY_IMAGE="$found_image"; else OLD_BOT_IMAGE="$found_image"; fi
    fi
    if [ "$ROLE" != bot ] || [ "$METHOD" = bot-only ]; then break; fi
  done

}

runtime_matches() {
  local name expected="${PINNED_IMAGE:-$IMAGE:$REF}" expected_id actual_id
  [ "$METHOD" != compose-build ] || expected="$DEV_IMAGE"
  if [[ "$METHOD" = compose-* ]]; then name="$(compose ps -q "$(service)")" || return 1
  else name="$(container)"; fi
  [ -n "$name" ] && [ "$(docker inspect --format '{{.Config.Image}}' "$name")" = "$expected" ] || return 1
  expected_id="$(docker image inspect --format '{{.Id}}' "$expected")" || return 1
  actual_id="$(docker inspect --format '{{.Image}}' "$name")" || return 1
  [[ "$expected_id" =~ ^sha256:[0-9a-f]{64}$ ]] && [ "$expected_id" = "$actual_id" ]
}

assert_named_resource() {
  local kind="$1" name="$2" label
  if ! docker "$kind" inspect "$name" >/dev/null 2>&1; then return 0; fi
  label="$(docker "$kind" inspect --format '{{index .Labels "org.shazzzbot.install"}}' "$name")" || return 1
  [ "$label" = "$INSTALL_ID" ] || die "$kind $name is not owned by this install"
}
