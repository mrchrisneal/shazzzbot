# shellcheck shell=bash

helper_inspect() {
  bounded python3 -I -B - "$SETUP_HOME" "$@" <<'PYTHON'
import sys
sys.path.insert(0, sys.argv[1])
from tools.setup_helper_only import main
main(sys.argv[2:])
PYTHON
}

helper_transaction() {
  bounded python3 -I -B - "$SETUP_HOME" "$@" <<'PYTHON'
import sys
sys.path.insert(0, sys.argv[1])
from tools.setup_helper_transaction import main
main(sys.argv[2:])
PYTHON
}

helper_field() {
  bounded python3 -I -B -c 'import json,sys; v=json.load(sys.stdin)[sys.argv[1]]; print(v if isinstance(v,str) else json.dumps(v))' "$1"
}

helper_local_bundle() {
  local destination="${1:-}"
  HELPER_RELEASE_TAG="$(bounded python3 -I -B - "$SETUP_HOME" "$HELPER_BUNDLE" \
    "${HELPER_RELEASE_TAG:-$VERSION_TAG}" "$destination" <<'PYTHON'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from tools.setup_bundle import local_bundle, stage_bundle
bundle = local_bundle(Path(sys.argv[2]), sys.argv[3])
if sys.argv[4]:
    stage_bundle(bundle, Path(sys.argv[4]))
print(bundle.version)
PYTHON
)" || die "invalid local helper bundle; no installed helper files were changed"
}

helper_preflight() {
  probe hostpython || die "the host helper needs /usr/bin/python3 3.11 or later; Unraid: Apps > search Python 3 for UNRAID > Install"
  fs lock-parent "$(dirname "$HOOK_DIR")" || die "the User Scripts plugin scripts directory must exist and be trusted"
  HELPER_DIR="$(fs resolve-helper "$HELPER_DIR")" || die "cannot resolve helper storage to its mounted pool"
  export HELPER_RUNTIME="$HELPER_DIR/helper.pyz"
  HELPER_SEEN="$(helper_inspect preflight "$HELPER_DIR")" || die "untrusted helper installation"
  if [ -e "$HELPER_DIR/transactions/pending" ] || [ -L "$HELPER_DIR/transactions/pending" ]; then
    die "an interrupted helper install needs recovery; inspect $HELPER_DIR/transactions/pending"
  fi
  check_helper_launcher
  helper_flash_policy
  export HELPER_PREFLIGHT_DONE=1
}

helper_bind_context() {
  local info expected observed
  info="$(helper_inspect preflight "$HELPER_DIR")" || die "cannot bind helper directory identity"
  observed="$(printf '%s' "$info" | helper_field inode)"
  expected="$(printf '%s' "$HELPER_SEEN" | helper_field inode)"
  [ "$expected" = null ] || [ "$expected" = "$observed" ] || die "helper home changed during setup; rerun"
  [ "$(printf '%s' "$info" | helper_field install_id)" = "$INSTALL_ID" ] \
    || die "helper identity changed during setup; rerun"
  if [ "$expected" = null ]; then HELPER_SEEN="$info"; fi
  SHAZZZBOT_HELPER_INODE="$observed"
  export SHAZZZBOT_HELPER_INODE
  if [ "$HELPER_ONLY" = 1 ]; then
    export SHAZZZBOT_HELPER_HOME="$HELPER_DIR" SHAZZZBOT_HELPER_INSTALLING=1
  fi
}

helper_wire_env() {
  [ "$HELPER" = yes ] || return 0
  if [ "$HELPER_ONLY" = 1 ] && [ -z "$SHAZZZBOT_HELPER_APP" ]; then return 0; fi
  local needed changed
  if [ "$HELPER_ONLY" = 0 ]; then helper_bind_context; fi
  needed="$(helper_inspect env-needed "$APP_DIR" "$HELPER_DIR")" || die "cannot inspect the helper environment"
  [ "$needed" = 1 ] || return 0
  mark_mutation
  changed="$(bounded python3 -I -B - "$SETUP_HOME" "$APP_DIR" "$HELPER_DIR" "$BACKUP_PATH" <<'PYTHON'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from tools.setup_helper_env import sync_env
print(int(sync_env(*map(Path, sys.argv[2:]))))
PYTHON
)" || die "cannot update the managed bot's helper environment"
  [ "$changed" != 1 ] || say "helper environment saved; the bot needs a recreate to read it"
}

helper_only_recover() {
  local -x ROLLING_BACK=1
  [ "$MUTATING" = 1 ] && [ "$COMMITTED" = 0 ] || return 0
  MUTATING=0
  warn "helper installation failed; restoring its verified backup"
  if (
    export RECOVERY_END=$((SECONDS + 86730))
    if [ "$HELPER_CHANGED" = 1 ] && [ -f "$HELPER_DIR/config.json" ]; then stop_helper || exit 1; fi
    helper_transaction restore "$BACKUP_PATH" "$SHAZZZBOT_HELPER_APP" "$HELPER_DIR" "$HELPER_CHANGED" || exit 1
    if [ "$HELPER_WAS_RUNNING" = 1 ]; then start_helper || exit 1; fi
    helper_transaction commit "$HELPER_DIR"
  ); then
    warn "helper files restored; bot containers and user data were untouched"
  else
    warn "helper recovery could not be verified; keep the pending transaction and inspect $BACKUP_PATH"
  fi
}

helper_only() {
  local seen info managed="" original_dir="$APP_DIR"
  [ "$REQUIRE_ROOT" = 0 ] || [ "$(id -u)" = 0 ] || die "helper setup must run as root"
  [ -f "$UNRAID_MARKER" ] || die "not supported yet: the host helper requires Unraid"
  export DETECTED_PLATFORM=unraid
  export SHAZZZBOT_HELPER_HOOK_DIR="$HOOK_DIR"
  if [ -n "$APP_DIR" ]; then
    APP_DIR="$(fs path "$APP_DIR")" || die "invalid bot directory"
    if [[ "$APP_DIR/" = /mnt/user/* ]]; then
      APP_DIR="$(fs resolve-helper "$APP_DIR")" || die "cannot resolve the bot appdata pool"
    fi
    managed="$(helper_inspect managed "$APP_DIR")" || die "untrusted bot install identity"
  fi
  export SHAZZZBOT_BOT_INSTALL_ID="$managed" SHAZZZBOT_HELPER_APP=""
  [ -z "$managed" ] || export SHAZZZBOT_HELPER_APP="$APP_DIR"
  helper_preflight
  [ -z "$HELPER_BUNDLE" ] || helper_local_bundle
  INSTALL_ID="$(printf '%s' "$HELPER_SEEN" | helper_field install_id)"
  export SHAZZZBOT_INSTALL_ID="$INSTALL_ID" SHAZZZBOT_INSTALL_ROLE=bot
  if dry; then
    plan "install only the helper in $HELPER_DIR; bot directory ${original_dir:-not selected} stays untouched"
    return 0
  fi
  confirm "Install only the host helper? No files have changed." y || { say "helper unchanged"; return 0; }
  fs work-root "$WORK_ROOT" || die "untrusted working directory"
  take_lock
  info="$(helper_inspect preflight "$HELPER_DIR")" || die "helper changed during setup; rerun"
  for seen in fingerprint inode; do
    [ "$(printf '%s' "$info" | helper_field "$seen")" = "$(printf '%s' "$HELPER_SEEN" | helper_field "$seen")" ] \
      || die "helper changed during setup; rerun"
  done
  if [ -n "$managed" ]; then
    [ "$(helper_inspect managed "$APP_DIR")" = "$managed" ] || die "bot identity changed during setup; rerun"
  fi
  if [ "$(printf '%s' "$HELPER_SEEN" | helper_field fresh)" = false ]; then helper_bind_context; fi
  prepare_helper_bundle
  helper_private_dir
  helper_bind_context
  helper_transaction check-pending "$HELPER_DIR" || die "an interrupted helper install needs recovery"
  export BACKUP_ROOT="$HELPER_DIR/transactions"
  install_helper
  helper_wire_env
  if [ -n "$BACKUP_PATH" ]; then helper_transaction commit "$HELPER_DIR" || die "cannot finish helper transaction"; fi
  COMMITTED=1
  say "helper socket directory: $WORK_ROOT/socket"
  say "helper key file: $HELPER_DIR/api.key"
  if [ -z "$managed" ]; then
    say "configure SHAZZZBOT_HELPER_KEY and HELPER_SOCKET_DIR on the bot; no bot files were changed"
  fi
}
