# shellcheck shell=bash

layout_activate() {
  if dry; then plan "use $APP_DIR for bot state and $WORK_ROOT for transient code"; return 0; fi
  fs mkdir "$APP_DIR" || die "cannot prepare the bot directory"
  fs mkdir "$BACKUP_ROOT" || die "cannot prepare bot backups"
  fs remember-id "$BACKUP_ROOT" "$APP_DIR" "$INSTALL_ID" || die "cannot preserve installation identity"
  if [ "$HELPER" = yes ]; then
    fs init-helper "$HELPER_DIR" "$INSTALL_ID" || die "cannot initialize mounted helper storage"
    helper_bind_context
    export SHAZZZBOT_HELPER_INSTALLING=1
  fi
  export SHAZZZBOT_INSTALL_ID="$INSTALL_ID" SHAZZZBOT_INSTALL_ROLE="$ROLE"
  temp_in -d "$WORK_ROOT/install.XXXXXX"
  SOURCE_DIR="$TMPF/source"
  DEV_IMAGE="$(fs layout-info "$APP_DIR" dev_image)" || die "cannot read image identity"
  PREV_IMAGE="$(fs layout-info "$APP_DIR" prev_image)" || die "cannot read rollback image identity"
  export SOURCE_DIR DEV_IMAGE PREV_IMAGE
  trust_install
  read_state
}
