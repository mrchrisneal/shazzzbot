# shellcheck shell=bash

cleanup() {
  local f
  for f in "${TEMP_FILES[@]+"${TEMP_FILES[@]}"}"; do
    rm -rf -- "$f"
  done
}

on_exit() {
  local rc=$?
  trap - EXIT
  trap 'warn "recovery interrupted; inspect the pending marker before retrying"; exit 130' INT TERM
  if [ "$rc" -ne 0 ]; then recover; fi
  cleanup
  [ "$rc" -eq 0 ] || say "setup stopped with exit code $rc" >&2
  exit "$rc"
}

temp_in() {
  TMPF="$(mktemp "$@")" || die "cannot create a private temporary file"
  TEMP_FILES+=("$TMPF")
}

take_lock() {
  dry && return 0
  command -v flock >/dev/null || die "flock is missing"
  fs lock-parent "$(existing_parent "$(dirname "$LOCK_FILE")")" || die "untrusted lock directory"
  if [ -e "$LOCK_FILE" ] || [ -L "$LOCK_FILE" ]; then fs trust "$LOCK_FILE" || die "untrusted lock file"; fi
  exec 9>>"$LOCK_FILE" || die "cannot open the lock file $LOCK_FILE"
  flock -n 9 || die "another setup.sh run holds $LOCK_FILE"
}

existing_parent() {
  local p="$1"
  while [ ! -e "$p" ]; do
    p="$(dirname "$p")"
  done
  printf '%s\n' "$p"
}

