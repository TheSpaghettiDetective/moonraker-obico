#!/bin/bash

set -e

export PIP_DEFAULT_TIMEOUT=1200 # For slow network connection

export OBICO_DIR=$(readlink -f $(dirname "$0"))

. "${OBICO_DIR}/scripts/funcs.sh"

SUFFIX=""
MOONRAKER_CONF_DIR="${HOME}/printer_data/config"
MOONRAKER_CONFIG_FILE="${MOONRAKER_CONF_DIR}/moonraker.conf"
MOONRAKER_LOG_DIR="${HOME}/printer_data/logs"
MOONRAKER_HOST="127.0.0.1"
MOONRAKER_PORT="7125"
OBICO_SERVICE_NAME="moonraker-obico"
OBICO_REPO="https://github.com/TheSpaghettiDetective/moonraker-obico.git"
CURRENT_USER=${USER}
OVERWRITE_CONFIG="n"
SKIP_LINKING="n"

usage() {
  if [ -n "$1" ]; then
    echo "${red}${1}${default}"
    echo ""
  fi
  cat <<EOF
Usage: $0 <[global_options]>   # Interactive installation to get moonraker-obico set up. Recommended if you have only 1 printer
       $0 <[global_options]> <[moonraker_setting_options]>   # Recommended for multiple-printer setup

Global options:
          -f   Reset moonraker-obico config file, including removing the linked printer
          -L   Skip the step to link to the Obico server.
          -u   Show uninstallation instructions
          -d   Show debugging info
          -U   Update moonraker-obico to the latest version

Moonraker setting options (${yellow}if any of them are specified, all need to be specified${default}):
          -n   The "name" that will be appended to the end of the system service name and log file. Useful only in multi-printer setup.
          -H   Moonraker server hostname or ip address
          -p   Moonraker server port
          -C   Moonraker config file path
          -l   The directory for moonraker-obico log files, which are rotated based on size.
          -S   The URL of the obico server to link the printer to, e.g., https://app.obico.io
EOF
}

ensure_not_octoprint() {
  if curl -s "http://127.0.0.1:5000" >/dev/null ; then
    cat <<EOF
${red}It looks like you are running OctoPrint.
Please note this program only works for Moonraker/Mainsail/Fluidd with Klipper.
If you are using OctoPrint with Klipper, such as OctoKlipper, please install "Obico for OctoPrint" instead.
${default}
EOF
    read -p "Continue anyway? [y/N]: " -e -i "N" cont
    echo ""

    if [ "${cont^^}" != "Y" ] ; then
      exit 0
    fi
  fi
}

prompt_for_settings() {
  print_header " Moonraker Info"

cat <<EOF

We need info about your Moonraker. If you are not sure, just leave them as defaults.

EOF

  read -p "Moonraker host: " -e -i "${MOONRAKER_HOST}" user_input
  eval MOONRAKER_HOST="${user_input}"
  read -p "Moonraker port: " -e -i "${MOONRAKER_PORT}" user_input
  eval MOONRAKER_PORT="${user_input}"
  read -p "Moonraker config file: " -e -i "${MOONRAKER_CONFIG_FILE}" user_input
  eval MOONRAKER_CONFIG_FILE="${user_input}"
  MOONRAKER_CONF_DIR=$(dirname "${MOONRAKER_CONFIG_FILE}")
  read -p "Klipper log directory: " -e -i "${MOONRAKER_LOG_DIR}" user_input
  eval MOONRAKER_LOG_DIR="${user_input}"
  echo ""
}

ensure_deps() {
  report_status "Installing required system packages... You may be prompted to enter password."

  PKGLIST="python3 python3-pip python3-virtualenv ffmpeg"
  # https://forum.openmediavault.org/index.php?thread/51664-http-apt-armbian-com-buster-release-does-not-have-a-release-file/
  sudo sed -i '/^deb http:\/\/apt.armbian.com buster main buster-utils buster-desktop/s/^/# /' /etc/apt/sources.list.d/armbian.list 2>/dev/null || true
  sudo apt-get --allow-releaseinfo-change -o Acquire::Check-Valid-Until=false -o Acquire::Check-Date=false update
  sudo apt-get install --yes ${PKGLIST}
  ensure_venv
  debug Running... "${OBICO_ENV}"/bin/pip3 install -q -r "${OBICO_DIR}"/requirements.txt
  "${OBICO_ENV}"/bin/pip3 install -q -r "${OBICO_DIR}"/requirements.txt
  echo ""
}

ensure_writtable() {
  dest_path="$1"
  if [ ! -w "$1" ] ; then
    exit_on_error "$1 doesn't exist or can't be changed."
  fi
}

recreate_service() {
  sudo systemctl stop "${OBICO_SERVICE_NAME}" 2>/dev/null || true

  report_status "Creating moonraker-obico systemctl service... You may need to enter password to run sudo."
  sudo /bin/sh -c "cat > /etc/systemd/system/${OBICO_SERVICE_NAME}.service" <<EOF
#Systemd service file for moonraker-obico
[Unit]
Description=Moonraker-Obico
After=network-online.target moonraker.service

[Install]
WantedBy=multi-user.target

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${OBICO_DIR}
ExecStart=${OBICO_ENV}/bin/python3 -m moonraker_obico.app -c ${OBICO_CFG_FILE}
Restart=always
RestartSec=5
EOF

  sudo systemctl enable "${OBICO_SERVICE_NAME}"
  sudo systemctl daemon-reload
}

update() {
  ensure_deps
}

# Helper functions

uninstall() {
  cat <<EOF

To uninstall Moonraker-Obico, please

1. Run these commands:

--------------------------

sudo systemctl stop "${OBICO_SERVICE_NAME}"
sudo systemctl disable "${OBICO_SERVICE_NAME}"
sudo rm "/etc/systemd/system/${OBICO_SERVICE_NAME}.service"
sudo systemctl daemon-reload
sudo systemctl reset-failed
rm -rf ~/moonraker-obico
rm -rf ~/moonraker-obico-env

-------------------------


2. Remove this line in "printer.cfg":

[include moonraker_obico_macros.cfg]


3. Remove this line in "moonraker.conf":

[include moonraker-obico-update.cfg]


EOF

  exit 0
}

## Main flow for installation starts here:

trap 'unknown_error' ERR
trap 'unknown_error' INT

# Parse command line arguments
while getopts "hn:H:p:C:l:S:fLusdU" arg; do
    case $arg in
        h) usage && exit 0;;
        H) mr_host=${OPTARG};;
        p) mr_port=${OPTARG};;
        C) mr_config=${OPTARG};;
        l) log_path=${OPTARG%/};;
        n) SUFFIX="-${OPTARG}";;
        S) OBICO_SERVER="${OPTARG}";;
        f) OVERWRITE_CONFIG="y";;
        s) ;; # Backward compatibility for kiauh
        L) SKIP_LINKING="y";;
        d) DEBUG="y";;
        u) uninstall ;;
        U) update && exit 0;;
        *) usage && exit 1;;
    esac
done


welcome
ensure_not_octoprint
ensure_deps

if "${OBICO_DIR}/scripts/tsd_service_existed.sh" ; then
  exit 0
fi

if [ -n "${mr_host}" ] || [ -n "${mr_port}" ] || [ -n "${mr_config}" ] || [ -n "${log_path}" ]; then

  if ! { [ -n "${mr_host}" ] && [ -n "${mr_port}" ] && [ -n "${mr_config}" ] && [ -n "${log_path}" ]; }; then
    usage "Please specify all Moonraker setting options. See usage below." && exit 1
  else
    MOONRAKER_HOST="${mr_host}"
    MOONRAKER_PORT="${mr_port}"
    eval MOONRAKER_CONFIG_FILE="${mr_config}"
    eval MOONRAKER_CONF_DIR=$(dirname "${MOONRAKER_CONFIG_FILE}")
    eval MOONRAKER_LOG_DIR="${log_path}"
  fi

else
  prompt_for_settings
  debug MOONRAKER_CONFIG_FILE: "${MOONRAKER_CONFIG_FILE}"
  debug MOONRAKER_CONF_DIR: "${MOONRAKER_CONF_DIR}"
  debug MOONRAKER_LOG_DIR: "${MOONRAKER_LOG_DIR}"
  debug MOONRAKER_PORT: "${MOONRAKER_PORT}"
fi

if [ -z "${SUFFIX}" -a "${MOONRAKER_PORT}" -ne "7125" ]; then
  SUFFIX="-${MOONRAKER_PORT}"
fi
debug SUFFIX: "${SUFFIX}"

ensure_writtable "${MOONRAKER_CONF_DIR}"
ensure_writtable "${MOONRAKER_CONFIG_FILE}"
ensure_writtable "${MOONRAKER_LOG_DIR}"

[ -z "${OBICO_CFG_FILE}" ] && OBICO_CFG_FILE="${MOONRAKER_CONF_DIR}/moonraker-obico.cfg"
OBICO_UPDATE_FILE="${MOONRAKER_CONF_DIR}/moonraker-obico-update.cfg"
OBICO_SERVICE_NAME="moonraker-obico${SUFFIX}"
OBICO_LOG_FILE="${MOONRAKER_LOG_DIR}/moonraker-obico${SUFFIX}.log"

if ! cfg_existed ; then
  create_config
fi

recreate_service
recreate_update_file

if "${OBICO_DIR}/scripts/migrated_from_tsd.sh" "${MOONRAKER_CONF_DIR}" "${OBICO_ENV}"; then
  exit 0
fi

trap - ERR
trap - INT

if [ $SKIP_LINKING != "y" ]; then
  debug Running... "${OBICO_DIR}/scripts/link.sh" -c "${OBICO_CFG_FILE}" -n \"${SUFFIX:1}\"
  "${OBICO_DIR}/scripts/link.sh" -c "${OBICO_CFG_FILE}" -n "${SUFFIX:1}"
else
  report_status "Launching ${OBICO_SERVICE_NAME} service..."
  sudo systemctl restart "${OBICO_SERVICE_NAME}"
fi

