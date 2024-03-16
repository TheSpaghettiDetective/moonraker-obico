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
  awk '/-- SAVE_CONFIG --/ && !f {print "[include moonraker_obico_macros.cfg]"; f++} 1; END{if(!f)print "[include moonraker_obico_macros.cfg]"}' "${PRINTER_CONF_FILE}" > /tmp/printer.tmp
  mv /tmp/printer.tmp "${PRINTER_CONF_FILE}"
fi