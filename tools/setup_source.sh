# shellcheck shell=bash

git_app() { GIT_OPTIONAL_LOCKS=0 git -C "$SOURCE_DIR" "$@"; }

sync_repo() {
  local checkout="$SOURCE_DIR"
  if dry; then plan "clone $REPO_URL ($BRANCH) in the transient working directory"; return 0; fi
  if [ -e "$checkout" ] || [ -L "$checkout" ]; then
    die "transient source path already exists; left unchanged"
  fi
  say "cloning $REPO_URL ($BRANCH) into $checkout"
  fs mkdir "$checkout" || die "cannot create the owned source staging directory"
  git clone --quiet --branch "$BRANCH" -- "$REPO_URL" "$checkout" || die "clone failed"
}

fetch_release() {
  local body re='"tag_name"[[:space:]]*:[[:space:]]*"([^"]+)"'
  [ ! -d "$APP_DIR/.git" ] || die "$APP_DIR is a git checkout; use --method compose-build or another --dir"
  REF="$VERSION_TAG"
  if [ -z "$REF" ]; then
    body="$(curl -fsSL --proto '=https' --max-time 30 --max-filesize 65536 -- "$RELEASE_API")" \
      || die "no public release, failed request, or reply above 64 KiB; try --method compose-build"
    [[ "$body" =~ $re ]] || die "the GitHub release answer has no tag_name"
    REF="${BASH_REMATCH[1]}"
  fi
  if ! bounded python3 -I -B - "$SETUP_HOME" "$REF" "$ST_REF" "$VERSION_TAG" <<'PYTHON'
import sys

sys.path.insert(0, sys.argv[1])
from shazzzbot.helper.constants import version_number

selected, installed, explicit = sys.argv[2:]
try:
    chosen = version_number(selected)
except ValueError:
    raise SystemExit("release tag must be canonical vX.Y.Z") from None
if selected != "v" + ".".join(map(str, chosen)):
    raise SystemExit("release tag must be canonical vX.Y.Z")
if not explicit:
    try:
        previous = version_number(installed)
    except ValueError:
        previous = None
    if previous is not None and installed == "v" + ".".join(map(str, previous)) and chosen < previous:
        raise SystemExit(f"latest release {selected} is older than installed {installed}; "
                         "use --version to select an explicit rollback")
PYTHON
  then die "release version validation failed; no release assets were downloaded"; fi
  say "release $REF"
  if dry; then
    plan "download the $REF source files from $SOURCE_URL/$REF"
    return 0
  fi
  temp_in -d "$WORK_ROOT/release.XXXXXX"
  SRC="$TMPF/source"
  fs mkdir "$SRC" || die "cannot prepare the release staging directory"
  RELEASE_MANIFEST="$TMPF/release.json"
  curl -fsSL --proto '=https' --max-time 30 --max-filesize 65536 -o "$RELEASE_MANIFEST" \
    -- "$SOURCE_URL/$REF/release.json" || die "cannot download the release digest manifest"
  PINNED_IMAGE="$(fs release "$RELEASE_MANIFEST" "$REF" image)" || die "invalid image digest"
  local source_sha
  source_sha="$(fs release "$RELEASE_MANIFEST" "$REF" source_sha256)" || die "invalid source digest"
  curl -fsSL --proto '=https' --max-time 120 --max-filesize 16777216 -o "$TMPF/source.tar.gz" \
    -- "$SOURCE_URL/$REF/source.tar.gz" || die "cannot download the release source archive"
  fs extract "$TMPF/source.tar.gz" "$SRC" "$source_sha" || die "release integrity or extraction failed"

}

prepare_source() {
  if dry; then REF="${ST_REF:-${SHIP_REVISION:-${VERSION_TAG:-planned}}}"; fi
  if [ "$METHOD" = compose-build ]; then
    if [ -n "$SHIP_PACK" ]; then ship_source; else sync_repo; fi
    SRC="$SOURCE_DIR"
    if ! dry; then REF="$(git_app rev-parse HEAD)"; fi
    if dry && [ -n "$SHIP_PACK" ]; then REF="$SHIP_REVISION"; fi
  else
    fetch_release
    dry || fs code "$SRC" || die "release contains untrusted code"
    if ! dry && [ ! -d "$APP_DIR" ]; then
      mark_mutation
      fs mkdir "$APP_DIR" || die "cannot create install directory"
    fi
  fi
  if ! dry; then
    if [ "$ST_REF" = "$REF" ]; then CODE_CHANGED=0; else CODE_CHANGED=1; fi
  fi
  export CODE_CHANGED
}

ship_source() {
  if dry; then plan "stage the committed $SHIP_REVISION source in $SOURCE_DIR"; return 0; fi
  fs external-root "$(dirname "$SHIP_PACK")" || die "untrusted shipping pack directory"
  fs trust "$SHIP_PACK" || die "untrusted shipping pack"
  if [ -e "$SOURCE_DIR" ] || [ -L "$SOURCE_DIR" ]; then
    die "transient source path already exists; left unchanged"
  fi
  fs mkdir "$SOURCE_DIR" || die "cannot prepare the shipped source staging directory"
  COMMAND_TIMEOUT=300 fs source-pack "$SOURCE_DIR" "$SHIP_PACK" "$SHIP_REVISION" "$REPO_URL" \
    || die "cannot install verified committed source"
}

ship_configs() {
  local candidate name owner
  [ -n "$CONFIG_SOURCE" ] || return 0
  if dry; then plan "validate supplied configuration before publishing it in private runtime storage"; return 0; fi
  fs external-root "$CONFIG_SOURCE" || die "untrusted configuration input directory"
  temp_in -d "$WORK_ROOT/candidate.XXXXXX"
  candidate="$TMPF"
  for name in .env config.toml; do
    [ "$ROLE" = bot ] || [ "$name" != config.toml ] || continue
    if [ -f "$CONFIG_SOURCE/$name" ]; then
      fs private "$CONFIG_SOURCE/$name" "$(id -u)" || die "untrusted configuration candidate"
      fs read "$CONFIG_SOURCE/$name" | fs write "$candidate/$name" || die "cannot stage supplied configuration"
    elif [ -f "$APP_DIR/$name" ]; then
      fs read "$APP_DIR/$name" | fs write "$candidate/$name" || die "cannot stage current configuration"
    else die "deployment lacks $name; supply per-host configuration before shipping"; fi
  done
  fs layout-env "$APP_DIR" "$candidate" || die "cannot prepare the helper socket candidate"
  validate_candidate "$candidate"
  for name in .env config.toml; do
    [ -f "$candidate/$name" ] || continue
    if [ -f "$APP_DIR/$name" ] && cmp -s "$candidate/$name" "$APP_DIR/$name"; then continue; fi
    mark_mutation
    watch_file "$APP_DIR/$name"
    owner="$(id -u)"
    if [ "$name" = config.toml ] && [ "$REQUIRE_ROOT" = 1 ]; then owner="$BOT_UID"; fi
    fs read "$candidate/$name" | replace_file "$APP_DIR/$name" "$owner" || die "cannot publish validated configuration"
    seal_file "$APP_DIR/$name"
    export CREATED_CFG=1
  done
}

compose_for_image() {
  local line out="" built=0 imaged=0
  while IFS= read -r line || [ -n "$line" ]; do
    if [ "$line" = "    build: ." ]; then
      built=1
      continue
    fi
    if [ "$line" = "    image: shazzzbot:dev" ]; then
      line="    image: $PINNED_IMAGE"
      imaged=1
    fi
    out+="$line"$'\n'
  done < "$1"
  if [ "$built" != 1 ] || [ "$imaged" != 1 ]; then die "${1##*/} has an unexpected layout; cannot pin it to $IMAGE:$REF"; fi
  printf '%s' "${out%$'\n'}"
}

