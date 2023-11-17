#!/bin/sh

set -e

PRINTER_CONF_FILE="$1"
KLIPPER_CONF_DIR=$(dirname "${PRINTER_CONF_FILE}")

OBICO_DIR=$(readlink -f $(dirname "$0"))/..
MACRO_CFG="${OBICO_DIR}/include_cfgs/moonraker_obico_macros.cfg"

if [ ! -f ${KLIPPER_CONF_DIR}/moonraker_obico_macros.cfg ]; then
  ls "${MACRO_CFG}" > /dev/null # make sure file exists
  ln -sf "${MACRO_CFG}" "${KLIPPER_CONF_DIR}/moonraker_obico_macros.cfg"
fi

if ! grep -q "include moonraker_obico_macros.cfg" "${PRINTER_CONF_FILE}" ; then
  sed -i "1 i [include moonraker_obico_macros.cfg]" "${PRINTER_CONF_FILE}"
fi