#!/bin/sh

set -e

KLIPPER_CONF_DIR="$1"
PRINTER_CONF_FILE="${KLIPPER_CONF_DIR}/printer.cfg"

OBICO_DIR=$(readlink -f $(dirname "$0"))/..
MACRO_CFG="${OBICO_DIR}/include_cfgs/moonraker_obico_macros.cfg"

ls "${MACRO_CFG}" > /dev/null # make sure file exists
ln -sf "${MACRO_CFG}" "${KLIPPER_CONF_DIR}/moonraker_obico_macros.cfg"

if ! grep -q "include moonraker_obico_macros.cfg" "${PRINTER_CONF_FILE}" ; then
  sed -i "1 i [include moonraker_obico_macros.cfg]" "${PRINTER_CONF_FILE}"
fi