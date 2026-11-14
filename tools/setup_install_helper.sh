# shellcheck shell=bash

helper_running() {
  local identity
  identity="$(helper_identity helper)" || die "helper identity could not be verified"
  [ -n "$identity" ]
}


helper_private_dir() {
  if [ "$HELPER_ONLY" = 1 ] && [ -n "${SHAZZZBOT_HELPER_HOME:-}" ]; then
    helper_transaction check-pending "$HELPER_DIR" || die "helper home changed or needs transaction recovery"
    export HELPER_RUNTIME="$HELPER_DIR/helper.pyz"
    return 0
  fi
  HELPER_DIR="$(fs resolve-helper "$HELPER_DIR")" || die "cannot resolve the helper appdata pool"
  fs init-helper "$HELPER_DIR" "$INSTALL_ID" || die "cannot initialize the helper private home"
  HELPER_RUNTIME="$HELPER_DIR/helper.pyz"
  export HELPER_RUNTIME
}

helper_release_manifest() {
  local tag body re='"tag_name"[[:space:]]*:[[:space:]]*"([^"]+)"'
  [ "$DETECTED_PLATFORM" = unraid ] || die "not supported yet: helper platform $DETECTED_PLATFORM"
  tag="$VERSION_TAG"
  if [ -n "$RELEASE_MANIFEST" ]; then
    tag="$(fs release-tag "$RELEASE_MANIFEST")" \
      || die "cannot read the helper release version"
  else
    if [ -z "$tag" ]; then
      body="$(curl -fsSL --proto '=https' --max-time 30 --max-filesize 65536 -- "$RELEASE_API")" \
        || die "cannot read the helper release"
      [[ "$body" =~ $re ]] || die "the helper release has no version"
      tag="${BASH_REMATCH[1]}"
    fi
    [[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "invalid helper release version"
    temp_in -d "$WORK_ROOT/helper-manifest.XXXXXX"
    RELEASE_MANIFEST="$TMPF/release.json"
    curl -fsSL --proto '=https' --max-time 30 --max-filesize 65536 -o "$RELEASE_MANIFEST" \
      -- "$SOURCE_URL/$tag/release.json" || die "cannot download the helper digest manifest"
  fi
  [[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "invalid helper release version"
  [ -z "$VERSION_TAG" ] || [ "$tag" = "$VERSION_TAG" ] || die "helper release version mismatch"
  fs release "$RELEASE_MANIFEST" "$tag" helper_sha256 >/dev/null || die "invalid helper release manifest"
  HELPER_RELEASE_TAG="$tag"
  if [ "$METHOD" = compose-build ]; then
    [ "$(fs release "$RELEASE_MANIFEST" "$tag" revision)" = "$REF" ] \
      || die "the checkout has no matching released helper digest; use --no-helper or a released revision"
  fi
}

helper_platform_bundle() {
  local stage="$1" manifest rows name digest size
  [ "$DETECTED_PLATFORM" = unraid ] || die "not supported yet: helper platform $DETECTED_PLATFORM"
  if [ -n "$HELPER_BUNDLE" ]; then helper_local_bundle "$stage"; return; fi
  manifest="$stage/helper-unraid.json"
  curl -fsSL --proto '=https' --max-time 30 --max-filesize 65536 -o "$manifest" \
    -- "$SOURCE_URL/$HELPER_RELEASE_TAG/helper-unraid.json" || die "cannot download the Unraid helper manifest"
  rows="$(bounded python3 -I -B - "$SETUP_HOME" "$RELEASE_MANIFEST" "$manifest" "$HELPER_RELEASE_TAG" "$DETECTED_PLATFORM" <<'PYTHON'
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from tools.setup_artifacts import platform_manifest, release

value = platform_manifest(Path(sys.argv[3]), release(Path(sys.argv[2]), sys.argv[4]), sys.argv[5])
for name, record in value['files'].items():
    print(name, record['sha256'], record['size'], sep='\t')
PYTHON
)" || die "invalid platform helper manifest; nothing was executed"
  while IFS=$'\t' read -r name digest size; do
    curl -fsSL --proto '=https' --max-time 120 --max-filesize "$size" -o "$stage/$name" \
      -- "$SOURCE_URL/$HELPER_RELEASE_TAG/$name" || die "cannot download verified helper file $name"
    fs verify "$stage/$name" "$digest" || die "helper file differs from its released digest: $name"
  done <<< "$rows"
}

helper_flash_policy() {
  local writable
  fs code "$SETUP_HOME" || die "installer code is not trusted"
  writable="$(bounded python3 -I -B - "$SETUP_HOME" <<'PYTHON'
import asyncio, sys
sys.path.insert(0, sys.argv[1])
from shazzzbot.helper import process
from shazzzbot.helper.constants import ENV, MAX_UPDATE
from shazzzbot.helper.platforms.unraid import flash_writable, nfs_flash_writable

async def inspect():
    rc, lines, truncated = await process.execute(["/usr/bin/testparm", "-s"], 5, MAX_UPDATE, errors=False,
        env={**ENV, "HOME": "/", "TMPDIR": "/"})
    if rc or truncated:
        raise ValueError("flash SMB configuration unavailable")
    smb, nfs = flash_writable("\n".join(lines)), nfs_flash_writable()
    return smb or nfs

print(int(asyncio.run(inspect())))
PYTHON
)" || die "cannot prove the flash SMB share is safe; no helper secrets were created"
  if [ "$writable" = 1 ]; then
    [ "$ACCEPT_FLASH" = 1 ] || die "flash is writable over SMB/NFS; disable write access or pass --accept-flash-share"
    warn "--accept-flash-share allows anyone who can write the flash share to run code as root"
    confirm "Accept that anyone who can write the flash share can run code as root?" n \
      || { [ "$UNATTENDED" = 1 ] || die "writable flash was not accepted"; }
  fi
}

check_helper_launcher() {
  if [ -f "$HELPER_DIR/policy.json" ]; then
    fs helper-policy "$HELPER_DIR" || die "existing helper policy leaves its private home"
  fi
  if [ -e "$HOOK_DIR/script" ] || [ -L "$HOOK_DIR/script" ]; then
    fs check-launcher "$HOOK_DIR" "$HELPER_DIR" || die "existing helper hook does not match its identity"
  elif [ -d "$HOOK_DIR" ]; then
    fs empty "$HOOK_DIR" || die "the User Scripts entry contains unidentified files"
  fi
}

prepare_helper_bundle() {
  [ -z "$HELPER_STAGE" ] || return 0
  if [ "$HELPER_PREFLIGHT_DONE" = 0 ]; then helper_flash_policy; fi
  if [ -z "$HELPER_BUNDLE" ]; then helper_release_manifest; fi
  temp_in -d "$WORK_ROOT/helper-bundle.XXXXXX"
  helper_platform_bundle "$TMPF"
  HELPER_STAGE="$TMPF/helper.pyz"
  if [ -n "$HELPER_BUNDLE" ]; then
    HELPER_EXPECTED="$(bounded python3 -I -B -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' \
      "$HELPER_STAGE")" || die "cannot read the local helper digest"
  else
    HELPER_EXPECTED="$(bounded python3 -I -B -c 'import json,sys; print(json.load(open(sys.argv[1]))["helper_sha256"])' \
      "$RELEASE_MANIFEST")" || die "cannot read the released helper digest"
  fi
  fs verify "$HELPER_STAGE" "$HELPER_EXPECTED" || die "helper differs from its verified digest; nothing was executed"
}

install_helper() {
  local stage expected text gid is_current previous="" was_running=0 was_active=0 template="$SRC/helper.example.json"
  [ "$HELPER" = yes ] && [ "$HELPER_DONE" = 0 ] || return 0
  HELPER_DONE=1
  if dry; then
    plan "verify the helper in $WORK_ROOT, install in real-pool appdata, and prepare one User Scripts hook"
    return 0
  fi
  prepare_helper_bundle
  stage="$HELPER_STAGE" expected="$HELPER_EXPECTED"
  helper_private_dir
  check_helper_launcher
  is_current="$(helper_inspect current "$HELPER_DIR" "${stage%/*}")" || die "cannot verify the current helper"
  if [ "$is_current" = 1 ] && helper_running; then
    bounded env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8 /usr/bin/python3 -I -B \
      "$stage" --config "$HELPER_DIR/config.json" --check || die "the installed helper configuration is invalid"
    say "helper is current; nothing to install"
    return 0
  fi
  if [ -e "$HELPER_DIR/helper.pyz" ] || [ -L "$HELPER_DIR/helper.pyz" ] \
      || [ -e "$HELPER_DIR/helper.pyz.prev" ] || [ -L "$HELPER_DIR/helper.pyz.prev" ]; then
    backup_now
    previous="$(fs select-helper "$HELPER_DIR" "${stage%/*}/rollback.pyz" "$BACKUP_PATH")" \
      || die "no pinned helper archive is available for rollback"
  fi
  was_active="$(fs read "$HELPER_DIR/.identity.json" | bounded python3 -I -B -c \
    'import json,sys; print(int(json.load(sys.stdin)["active"] is True))')" || die "cannot read the helper identity"
  if [ "$was_active" = 1 ]; then
    was_active="$(fs hook-active "$HOOK_DIR")" || die "cannot read the helper startup schedule"
  fi
  if [ -f "$HELPER_DIR/config.json" ] && helper_running; then was_running=1; fi
  mark_mutation
  export HELPER_CHANGED=1
  if [ "$was_running" = 1 ]; then
    export HELPER_WAS_RUNNING=1
    stop_helper || die "helper did not drain; its installed files are unchanged"
  fi
  if [ "$was_active" = 1 ]; then
    fs helper-identity "$HELPER_DIR" "$INSTALL_ID" "$HOOK_DIR/script" 0 \
      | replace_file "$HELPER_DIR/.identity.json" || die "cannot pause the helper startup hook"
  fi
  if [ ! -f "$HELPER_DIR/config.json" ]; then
    gid=""
    if [ "$HELPER_ONLY" = 1 ]; then template="$SETUP_HOME/helper.example.json"; fi
    if [ "$HELPER_ONLY" = 0 ] || [ -n "${SHAZZZBOT_HELPER_APP:-}" ]; then
      gid="$(env_value "$APP_DIR/.env" HELPER_GID)"
    fi
    text="$(bounded python3 -I -B - "$template" "$HELPER_DIR" "${gid:-10001}" "$ACCEPT_FLASH" "$SETUP_HOME" <<'PYTHON'
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[5])
from shazzzbot.helper.formats import dump, load
cfg = load(json.loads(Path(sys.argv[1]).read_text()), 'helper config', 1)
root = Path(sys.argv[2])
cfg.update(enabled=['tasks', 'ups_shutdown'], key_file=str(root / 'api.key'),
           log_file=str(root / 'helper.log'), socket='/tmp/shazzzbot-helper/socket/helper.sock',
           socket_gid=int(sys.argv[3]), installation_policy=str(root / 'policy.json'))
if sys.argv[4] == '1':
    cfg['accept_flash_share'] = True
print(json.dumps(dump(cfg, 1), indent=2))
PYTHON
)" || die "cannot prepare the helper configuration"
    install_file "$text" "$HELPER_DIR/config.json"
  fi
  if [ ! -f "$HELPER_DIR/api.key" ]; then
    bounded python3 -I -B -c 'import secrets; print(secrets.token_hex(32))' \
      | replace_file "$HELPER_DIR/api.key" || die "cannot create the helper key"
  fi
  text="$(bounded python3 -I -B - "$HELPER_DIR/helper.pyz" "$expected" "$HELPER_DIR/config.json" "$SETUP_HOME" <<'PYTHON'
import json, sys
sys.path.insert(0, sys.argv[4])
from shazzzbot.helper.formats import dump
print(json.dumps(dump(dict(zip(('artifact', 'sha256', 'config'), sys.argv[1:4])), 1)))
PYTHON
)" || die "cannot prepare the helper pin"
  if [ -n "$previous" ]; then
    fs read "${stage%/*}/rollback.pyz" | replace_file "$HELPER_DIR/helper.pyz.prev" || die "cannot save the old helper"
    text="$(printf '%s' "$text" | bounded python3 -I -B -c \
      'import json,sys; p=json.load(sys.stdin); p["previous_sha256"]=sys.argv[1]; print(json.dumps(p))' "$previous")" \
      || die "cannot prepare the previous helper pin"
  fi
  install_file "$text" "$HELPER_DIR/policy.json"
  fs read "$stage" | replace_file "$HELPER_DIR/helper.pyz" || die "cannot install the verified helper"
  fs read "${stage%/*}/shazzzbot-helper-unraid.py" | replace_file "$HELPER_DIR/shazzzbot-helper-unraid.py" \
    || die "cannot install the verified Unraid helper entry"
  fs read "${stage%/*}/helper-unraid.json" | replace_file "$HELPER_DIR/helper-unraid.json" \
    || die "cannot install the verified Unraid helper manifest"
  text="$(fs launcher-text "$HELPER_DIR" "$HOOK_DIR/script" "${stage%/*}/shazzzbot-helper-unraid.py")" \
    || die "cannot prepare the helper boot hook"
  install_file "$text" "$HOOK_DIR/script"
  install_file shazzzbot-helper "$HOOK_DIR/name"
  install_file 'Verified read-only host checks for Shazzzbot' "$HOOK_DIR/description"
  fs helper-identity "$HELPER_DIR" "$INSTALL_ID" "$HOOK_DIR/script" 0 \
    | replace_file "$HELPER_DIR/.identity.json" || die "cannot record the prepared helper"
  bounded env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8 /usr/bin/python3 -I -B \
    "$stage" --config "$HELPER_DIR/config.json" --check || die "the verified helper failed its configuration check"
  if [ "$was_active" = 1 ] || confirm "Schedule the helper At Startup of Array in User Scripts?" n; then
    fs schedule "$HOOK_DIR" "$BACKUP_PATH" || die "cannot save the User Scripts startup schedule"
    fs helper-identity "$HELPER_DIR" "$INSTALL_ID" "$HOOK_DIR/script" 1 \
      | replace_file "$HELPER_DIR/.identity.json" || die "cannot activate the helper hook"
  else
    say "helper installed but NOT running; rerun setup.sh --helper to enable its startup hook"
    return 0
  fi
  if [ "$was_running" = 1 ] || confirm "Start the host helper now?" n; then
    start_helper || die "helper startup failed; inspect $HELPER_DIR/helper.log"
    say "helper running; storage $HELPER_DIR, socket $WORK_ROOT/socket"
  else
    say "helper installed but NOT running; start it with: /bin/bash $(printf '%q' "$HOOK_DIR/script")"
  fi
}
