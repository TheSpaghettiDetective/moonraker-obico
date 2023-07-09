#!/bin/bash

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
    print_header " Link Printer to Obico Server "
    cat <<EOF

To link to your Obico Server account, you need to obtain the 6-digit verification code
in the Obico mobile or web app, and enter the code below.

If you need help, head to https://obico.io/docs/user-guides/klipper-setup

EOF
  fi

  export OBICO_ENV # Expose OBICO_ENV to link.py so that it can print out the debugging command.

  debug Running... PYTHONPATH="${OBICO_DIR}:${PYTHONPATH}" ${OBICO_ENV}/bin/python3 -m moonraker_obico.link -c "${OBICO_CFG_FILE}"
  PYTHONPATH="${OBICO_DIR}:${PYTHONPATH}" ${OBICO_ENV}/bin/python3 -m moonraker_obico.link -c "${OBICO_CFG_FILE}"
  return $?
}

success() {
  echo -e "\n\n"
  banner
  echo -e "\n"
  print_header "="
  echo -n "${cyan}"
  array=("" "SUCCESS!!!" "Now enjoy Obico for Klipper!" "")
  print_centered_lines "${array[@]}"
  print_header "="

  cat <<EOF

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

did_not_finish() {
    cat <<EOF
${yellow}
The process to link to your Obico Server account didn't finish.
${default}

To resume the linking process at a later time, run:

-------------------------------------------------------------------------------------------------
cd ~/moonraker-obico
./install.sh
-------------------------------------------------------------------------------------------------

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

if [ -z "${SUFFIX}" ] || [ "${SUFFIX}" == '-' ]; then
  OBICO_SERVICE_NAME="moonraker-obico"
else
  OBICO_SERVICE_NAME="moonraker-obico${SUFFIX}"
fi

if [ -z "${OBICO_CFG_FILE}" ]; then
  usage && exit 1
fi

ensure_venv

link_to_server
link_exit_code=$?
debug link_to_server exited with $link_exit_code

sudo systemctl restart "${OBICO_SERVICE_NAME}"

if [ ! $KEEP_QUIET = "y" ]; then
  case $link_exit_code in
    0)
      prompt_for_sentry
      success
      ;;
    255)
      did_not_finish
      need_help
      ;;
    *)
      oops
      did_not_finish
      need_help
      ;;
  esac
fi
