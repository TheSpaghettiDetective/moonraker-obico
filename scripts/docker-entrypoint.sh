#!/bin/bash

SOURCE_DIR=${OBICO_MACRO_SOURCE_DIR:-moonraker-obico/include_cfgs}
TARGET_DIR=${KLIPPER_CONF_DIR:-printer_data/config}

for macro in "moonraker_obico_macros.cfg"; do
  # Copy macro to TARGET_DIR if it is missing or has been changed
  if ! [ -f ${TARGET_DIR}/${macro} ] || ! diff ${SOURCE_DIR}/${macro} ${TARGET_DIR}/${macro} 2>&1 > /dev/null; then
    echo "ENTRYPOINT: Copying ${macro} to ${TARGET_DIR}"
    cp ${SOURCE_DIR}/${macro} ${TARGET_DIR}/${macro}
  fi
done

echo "ENTRYPOINT: Starting moonraker-obico"
/opt/venv/bin/python -m moonraker_obico.app $@