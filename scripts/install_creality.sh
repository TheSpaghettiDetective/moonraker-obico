#!/bin/bash

set -e

export OBICO_DIR=$(readlink -f $(dirname "$0"))/..

. "${OBICO_DIR}/scripts/funcs.sh"

SUFFIX=""
MOONRAKER_CONF_DIR="/mnt/UDISK/printer_config"
MOONRAKER_CONFIG_FILE="${MOONRAKER_CONF_DIR}/moonraker.conf"
MOONRAKER_LOG_DIR="/mnt/UDISK/printer_logs"
MOONRAKER_HOST="127.0.0.1"
MOONRAKER_PORT="7125"
OBICO_CFG_FILE="${MOONRAKER_CONF_DIR}/moonraker-obico.cfg"
OBICO_UPDATE_FILE="${MOONRAKER_CONF_DIR}/moonraker-obico-update.cfg"
OBICO_LOG_FILE="${MOONRAKER_LOG_DIR}/moonraker-obico.log"
OVERWRITE_CONFIG="n"
SKIP_LINKING="n"


ensure_deps() {
  report_status "Installing required system packages..."
  PKGLIST="python3 python3-pip"
  opkg install ${PKGLIST}
  pip3 install virtualenv
  ensure_venv
  debug Running... "${OBICO_ENV}"/bin/pip3 install -q -r "${OBICO_DIR}"/requirements.txt
  "${OBICO_ENV}"/bin/pip3 install -q --require-virtualenv -r "${OBICO_DIR}"/requirements.txt
  echo ""
}

welcome
ensure_deps

if ! cfg_existed ; then
  create_config
fi

recreate_update_file

trap - ERR
trap - INT

if [ $SKIP_LINKING != "y" ]; then
  debug Running... "${OBICO_DIR}/scripts/link.sh" -c "${OBICO_CFG_FILE}" -n \"${SUFFIX:1}\" -S
  "${OBICO_DIR}/scripts/link.sh" -c "${OBICO_CFG_FILE}" -n "${SUFFIX:1}" -S
fi
