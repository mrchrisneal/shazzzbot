#!/usr/bin/env bash
# Maintainer-only deployment tool; use setup.sh for installation.
set -euo pipefail
set +x
umask 077
usage() { printf 'Usage: %s [cn|an]  (no argument: every target)\n' "$0" >&2; exit 2; }
cd "$(dirname "$0")"
say() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { say "FAILED: $*" >&2; exit 1; }
if [ "$#" -eq 0 ]; then
  status=0
  for file in "$HOME"/.config/shazzzbot-ship-*.env; do
    [ -f "$file" ] || continue
    target="${file##*/shazzzbot-ship-}"
    target="${target%.env}"
    printf '=== %s ===\n' "$target"
    ./ship.sh "$target" || status=1
  done
  exit "$status"
fi
[ "$#" -eq 1 ] || usage
case "$1" in cn|an) TARGET="$1" ;; *) usage ;; esac
SHIP_ENV="$HOME/.config/shazzzbot-ship-$TARGET.env"
HOST="$(sed -n 's/^HOST=//p' "$SHIP_ENV")"
DIR="$(sed -n 's/^DIR=//p' "$SHIP_ENV")"
node_list="$(sed -n 's/^NODES=//p' "$SHIP_ENV")"
node_list="${node_list#\'}"; node_list="${node_list%\'}"
node_list="${node_list#\"}"; node_list="${node_list%\"}"
read -r -a nodes <<< "$node_list"
[[ "$HOST" =~ ^[A-Za-z0-9_.@-]+$ && "$HOST" != -* && "$DIR" = /* ]] \
  || die "HOST and absolute DIR must be set in $SHIP_ENV"
for node in "${nodes[@]}"; do
  [ "$node" = local ] || die "unsupported relay node: $node (supported: local)"
done
[ -z "$(git status --porcelain --untracked-files=no)" ] || die "tracked changes not committed -- commit, then rerun"
timeout --foreground --signal=TERM --kill-after=5 120 git fetch -q forge main || die "forge fetch failed"
SHA="$(git rev-parse HEAD)"
[ "$(git rev-parse forge/main)" = "$SHA" ] || die "HEAD is not pushed to forge/main"
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || die "HEAD is not a full commit id"
BOOTSTRAP="$(git show "$SHA:tools/setup_ship.py")" || die "committed ship bootstrap is missing"
TEMP="$(mktemp -d "${TMPDIR:-/tmp}/shazzzbot-ship.XXXXXX")"
trap 'rm -rf -- "$TEMP"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir "$TEMP/.ship"
git archive --prefix=source/ "$SHA" > "$TEMP/source.tar" || die "source archive failed"
{
  printf '%s\n' "$SHA"
  git rev-parse "$SHA^{tree}"
  git ls-tree -r -t "$SHA" | awk '{print $3}'
} | git pack-objects --stdout > "$TEMP/.ship/git.pack" || die "source pack failed"

add_config() {
  local source="$1" name="$2"
  [ -e "$source" ] || [ -L "$source" ] || return 0
  if [ ! -f "$source" ] || [ -L "$source" ]; then die "$source must be a regular file, not a symlink"; fi
  git check-ignore -q -- "$source" || die "$source must be gitignored"
  cp -- "$source" "$TEMP/.ship/$name"
  chmod 600 "$TEMP/.ship/$name"
}

payload() {
  cp -- "$TEMP/source.tar" "$TEMP/upload.tar"
  tar --append -f "$TEMP/upload.tar" -C "$TEMP" .ship || die "payload assembly failed"
}

add_config "config.$HOST.toml" config.toml
add_config ".env.$HOST" .env
payload
printf -v remote_command 'python3 -I -B -c %q %q %q bot' "$BOOTSTRAP" "$DIR" "$SHA"
say "shipping ${SHA:0:12}; runtime source, config and data stay in private persistent storage"
timeout --foreground --signal=TERM --kill-after=10 4500 \
  ssh -o BatchMode=yes -o ConnectTimeout=20 "$HOST" "$remote_command" < "$TEMP/upload.tar" \
  || die "deployment failed; installer recovery result is above"
for node in "${nodes[@]}"; do
  rm -f -- "$TEMP/.ship/config.toml" "$TEMP/.ship/.env"
  add_config .env .env
  payload
  say "shipping local relay ${SHA:0:12} through the same private installer"
  command=(python3 -I -B -c "$BOOTSTRAP" /var/lib/shazzzbot-relay "$SHA" relay)
  if [ "$(id -u)" -ne 0 ]; then command=(sudo -n "${command[@]}"); fi
  timeout --foreground --signal=TERM --kill-after=10 4500 "${command[@]}" < "$TEMP/upload.tar" \
    || die "local relay deployment failed; installer recovery result is above"
done
say "OK ${SHA:0:12} deployed"
