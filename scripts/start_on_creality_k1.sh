#!/bin/sh

CONF_FILE=/usr/data/printer_data/config/moonraker-obico.cfg

PYTHONPATH=/usr/data/moonraker-obico /usr/data/moonraker-obico-env/bin/python3 -B -m moonraker_obico.app -c "$CONF_FILE"
