#!/usr/bin/env bash
# Test, sync the build inputs and secrets to an Unraid host, then rebuild and restart the stack there.
# config.<host>.toml and .env.<host>, when present, are used for that host instead of config.toml and .env.
# The proxy is always recreated: its config is a single-file bind mount that rsync replaces.
set -euo pipefail
host=${1:?usage: deploy.sh <ssh-host> [remote-dir]}
dir=${2:-/mnt/user/appdata/shazzzbot}
cd "$(dirname "$0")"
cfg=config.toml env=.env
[[ -f "config.$host.toml" ]] && cfg="config.$host.toml"
[[ -f ".env.$host" ]] && env=".env.$host"
[[ -f $env && -f $cfg ]] || { echo "deploy: $env and $cfg are required" >&2; exit 1; }
install_id=$(sed -n 's/^SHAZZZBOT_INSTALL_ID=//p' "$env" | tail -n 1)
install_id=${install_id//[[:space:]]/}
[[ -n $install_id && $install_id != '""' && $install_id != "''" ]] || {
    echo "deploy: $env requires a non-empty SHAZZZBOT_INSTALL_ID from setup.sh" >&2
    exit 1
}
./test.sh
q=$(printf %q "$dir")
# The remote command is built here on purpose; $q is already shell-quoted. reviewed 2026-11
# shellcheck disable=SC2029
ssh "$host" "mkdir -p $q"
rsync -rlt --delete --exclude __pycache__ VERSION compose.yaml Dockerfile .dockerignore requirements.txt shazzzbot docker-proxy "$host:$dir/"
rsync -rltR --delete --exclude pyodide/ --exclude frames/ --exclude frames.json docs/demo tools/preview_assets.py "$host:$dir/"
rsync -t --chmod=F600 "$env" "$host:$dir/.env"
rsync -t --chmod=F600 "$cfg" "$host:$dir/config.toml"
# shellcheck disable=SC2029
ssh "$host" "cd $q && test ! -L data && mkdir -p data && chown 10001 data config.toml && docker compose up -d --build --remove-orphans && docker compose up -d --force-recreate docker-proxy && docker compose ps"
