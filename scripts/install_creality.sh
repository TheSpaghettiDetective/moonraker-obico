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

usage() {
  if [ -n "$1" ]; then
    echo "${red}${1}${default}"
    echo ""
  fi
  cat <<EOF
Usage: $0 <[global_options]>   # Interactive installation to get moonraker-obico set up. Recommended if you have only 1 printer

Global options:
          -u   Show uninstallation instructions
EOF
}

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

recreate_service() {
  cp "${OBICO_DIR}"/scripts/openwrt_init.d/moonraker_obico_service /etc/init.d
  ln -s ../init.d/moonraker_obico_service /etc/rc.d/S67moonraker_obico_service
  ln -s ../init.d/moonraker_obico_service /etc/rc.d/K1moonraker_obico_service
}

uninstall() {
  cat <<EOF

To uninstall Moonraker-Obico, please run:

rm -rf $OBICO_DIR
rm -rf $OBICO_DIR/../moonraker-obico-env
rm -f /etc/init.d/moonraker_obico_service
rm -f /etc/rc.d/S67moonraker_obico_service
rm -f /etc/rc.d/K1moonraker_obico_service

EOF

  exit 0
}

trap 'unknown_error' ERR
trap 'unknown_error' INT

# Parse command line arguments
while getopts "u" arg; do
    case $arg in
        u) uninstall ;;
        *) usage && exit 1;;
    esac
done

welcome
ensure_deps

if ! cfg_existed ; then
  create_config
fi

recreate_service
recreate_update_file

trap - ERR
trap - INT

if [ $SKIP_LINKING != "y" ]; then
  debug Running... "${OBICO_DIR}/scripts/link.sh" -c "${OBICO_CFG_FILE}" -n \"${SUFFIX:1}\" -S
  "${OBICO_DIR}/scripts/link.sh" -c "${OBICO_CFG_FILE}" -n "${SUFFIX:1}" -S
fi
