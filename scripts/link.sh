#!/bin/bash

set -e

OBICO_DIR=$(realpath $(dirname "$0")/..)

. "${OBICO_DIR}/scripts/funcs.sh"

SUFFIX=""

usage() {
  if [ -n "$1" ]; then
    echo "${red}${1}${default}"
    echo ""
  fi
  cat <<EOF
Usage: $0 <[global_options]>

Link or re-link a printer to the Obico Server

Global options:
          -c   The path to the moonraker-obico.cfg file
          -n   The "name" that will be appended to the end of the system service name and log file. Useful only in multi-printer setup.
EOF
}


link_to_server() {
  cat <<EOF

=============================== Link Printer to Obico Server ======================================

EOF
  PYTHONPATH="${OBICO_DIR}:${PYTHONPATH}" ${OBICO_ENV}/bin/python3 -m moonraker_obico.link -c "${OBICO_CFG_FILE}"

  OBICO_SERVICE_NAME="moonraker-obico${SUFFIX}"
  systemctl restart "${OBICO_SERVICE_NAME}"
}

while getopts "hc:n:" arg; do
    case $arg in
        h) usage && exit 0;;
        c) OBICO_CFG_FILE=${OPTARG};;
        n) SUFFIX="-${OPTARG}";;
        *) usage && exit 1;;
    esac
done

if [ -z "${OBICO_CFG_FILE}" ]; then
  usage && exit 1
fi

ensure_venv
link_to_server
