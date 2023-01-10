#!/bin/bash

set -e

OBICO_DIR=$(realpath $(dirname "$0")/..)

. "${OBICO_DIR}/scripts/funcs.sh"

SUFFIX=""
KEEP_QUIET="n"

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
          -q   Keep quiet
          -d   Show debugging info
EOF
}


link_to_server() {
  if [ ! $KEEP_QUIET = "y" ]; then
    cat <<EOF
${cyan}
=============================== Link Printer to Obico Server ======================================
${default}
To link to your Obico Server account, you need to obtain the 6-digit verification code
in the Obico mobile or web app, and enter the code below.

If you need help, head to https://obico.io/docs/user-guides/klipper-setup

EOF
  fi

  export OBICO_ENV # Expose OBICO_ENV to link.py so that it can print out the debugging command.

  debug Running... PYTHONPATH="${OBICO_DIR}:${PYTHONPATH}" ${OBICO_ENV}/bin/python3 -m moonraker_obico.link -c "${OBICO_CFG_FILE}"
  if ! PYTHONPATH="${OBICO_DIR}:${PYTHONPATH}" ${OBICO_ENV}/bin/python3 -m moonraker_obico.link -c "${OBICO_CFG_FILE}"; then
    return 1
  fi

  if [ -z "${SUFFIX}" ] || [ "${SUFFIX}" == '-' ]; then
    OBICO_SERVICE_NAME="moonraker-obico"
  else
    OBICO_SERVICE_NAME="moonraker-obico${SUFFIX}"
  fi
  sudo systemctl restart "${OBICO_SERVICE_NAME}"
}

success() {
  echo -e "\n\n\n"
  banner
  cat <<EOF
${cyan}
====================================================================================================
###                                                                                              ###
###                                       SUCCESS!!!                                             ###
###                             Now enjoy Obico for Klipper!                                     ###
###                                                                                              ###
====================================================================================================
${default}
The changes we have made to your system:

- System service: /etc/systemd/system/${OBICO_SERVICE_NAME}
- Config file: ${OBICO_CFG_FILE}
- Update file: ${OBICO_UPDATE_FILE}
- Inserted "[include moonraker-obico-update.cfg]" in the "moonraker.conf" file
- Log file: ${OBICO_LOG_FILE}

To remove Obico for Klipper, run:

cd ~/moonraker-obico
./install.sh -u

EOF

}

prompt_for_sentry() {
	if grep -q "sentry_opt" "${OBICO_CFG_FILE}" ; then
		return 0
  fi
  echo -e "\nOne last thing: Do you want to opt in bug reporting to help us make Obico better?"
  echo -e "The debugging info included in the report will be anonymized.\n"
  read -p "Opt in bug reporting? [Y/n]: " -e -i "Y" opt_in
  echo ""
  if [ "${opt_in^^}" == "Y" ] ; then
		cat <<EOF >> "${OBICO_CFG_FILE}"

[misc]
sentry_opt: in
EOF
  fi
}

while getopts "hqc:n:d" arg; do
    case $arg in
        h) usage && exit 0;;
        c) OBICO_CFG_FILE=${OPTARG};;
        n) SUFFIX="-${OPTARG}";;
        q) KEEP_QUIET="y";;
        d) DEBUG="y";;
        *) usage && exit 1;;
    esac
done

if [ -z "${OBICO_CFG_FILE}" ]; then
  usage && exit 1
fi

ensure_venv

if link_to_server; then
  if [ ! $KEEP_QUIET = "y" ]; then
    prompt_for_sentry
    success
  fi
else
  if [ ! $KEEP_QUIET = "y" ]; then
    oops
    cat <<EOF
${red}
The process to link to your Obico Server account didn't finish.
${default}

To resume the linking process at a later time, run:

-------------------------------------------------------------------------------------------------
cd ~/moonraker-obico
./install.sh
-------------------------------------------------------------------------------------------------

EOF
    need_help
  fi
fi
