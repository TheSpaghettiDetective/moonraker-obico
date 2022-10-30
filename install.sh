#!/bin/bash

set -e

OBICO_DIR=$(realpath $(dirname "$0"))

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
JSON_PARSE_PY="/tmp/json_parse.py"
OVERWRITE_CONFIG="n"
RECREATE_SERVICE="n"
SKIP_LINKING="n"

usage() {
  if [ -n "$1" ]; then
    echo "${red}${1}${default}"
    echo ""
  fi
  cat <<EOF
Usage: $0 <[global_options]>   # Let me discover moonraker settings. Recommended if you have only 1 printer
       $0 <[global_options]> <[moonraker_setting_options]>   # Recommended for multiple-printer setup

Global options:
          -f   Reset moonraker-obico config file, including removing the linked printer
          -s   Recreate the system service even if one already existed
          -L   Skip the step to link to the Obico server.
          -u   Show uninstallation instructions

Moonraker setting options (${yellow}if any of them are specified, all need to be specified${default}):
          -n   The "name" that will be appended to the end of the system service name and log file. Useful only in multi-printer setup.
          -H   Moonraker server hostname or ip address
          -p   Moonraker server port
          -C   Moonraker config file path
          -l   The directory for moonraker-obico log files, which are rotated based on size.
          -S   The URL of the obico server to link the printer to, e.g., https://app.obico.io
EOF
}

manual_setting_warning() {
  cat <<EOF
${yellow}
We couldn't automatically detect the settings. Please enter them below to continue.

!!!WARNING: Manually entering the Moonraker settings can be error prone.
We highly recommend using KIAUH if you have a non-standard Klipper installation, e.g., running multiple Moonraker instances.
${default}
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

discover_sys_settings() {
  report_status "Detecting the softwares and settings of your Klipper system ..."

  mr_config_file="${HOME}/printer_data/config/moonraker.conf"
  mr_log_path="${HOME}/printer_data/logs"

  # In case it's moonraker before https://github.com/Arksine/moonraker/pull/491
  if [ ! -f "${mr_config_file}" -o ! -d "${mr_log_path}" ]; then
    mr_config_file="${HOME}/klipper_config/moonraker.conf"
    mr_log_path="${HOME}/klipper_logs"
  fi

  if [ ! -f "${mr_config_file}" -o ! -d "${mr_log_path}" ]; then
    return 1
  fi

  if ! mr_port=$(${OBICO_ENV}/bin/python3 -c "import configparser; c = configparser.ConfigParser(); c.read('${mr_config_file}'); print(c['server']['port'])"); then
    return 1
  fi

  if ! mr_database=$(curl -s "http://${MOONRAKER_HOST}:${mr_port}/server/database/list") ; then
    return 1
  fi

  toolchain_msg=""
  if echo $mr_database | grep -qi 'mainsail' ; then
    toolchain_msg="${toolchain_msg} Mainsail installed"
  fi

  if echo $mr_database | grep -qi 'fluidd' ; then
    toolchain_msg="${toolchain_msg} Fluidd installed"
  fi

  if [ -z "${toolchain_msg}" ]; then
    toolchain_msg=" Not detected"
  fi

  echo -e "The following have been detected:\n"
  echo -e "- Web Server: Moonraker"
  echo -e "- Web Frontend:${toolchain_msg}"
  echo -e "- Moonraker host: ${MOONRAKER_HOST}"
  echo -e "- Moonraker port: ${mr_port}\n"
  read -p "Is this correct? [Y/n]: " -e -i "Y" correct
  echo ""

  if [ "${correct^^}" == "Y" ] ; then
    MOONRAKER_CONFIG_FILE="${mr_config_file}"
    MOONRAKER_CONF_DIR=$(dirname "${MOONRAKER_CONFIG_FILE}")
    MOONRAKER_LOG_DIR="${mr_log_path}"
    MOONRAKER_PORT="${mr_port}"
    return 0
  fi
  return 1
}

prompt_for_settings() {
  manual_setting_warning
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

  PKGLIST="python3 python3-pip python3-venv ffmpeg"
  sudo apt-get update --allow-releaseinfo-change
  sudo apt-get install --yes ${PKGLIST}

  echo -e ""
  ensure_venv
  "${OBICO_ENV}"/bin/pip3 install -q -r "${OBICO_DIR}"/requirements.txt
  echo ""
}

ensure_writtable() {
  dest_path="$1"
  if [ ! -w "$1" ] ; then
    exit_on_error "$1 doesn't exist or can't be changed."
  fi
}

cfg_existed() {
  if [ -f "${OBICO_CFG_FILE}" ] ; then
    if [ $OVERWRITE_CONFIG = "y" ]; then
      backup_config_file="${OBICO_CFG_FILE}-$(date '+%Y-%m-%d')"
      echo -e "${yellow}\n!!!WARNING: Overwriting ${OBICO_CFG_FILE}..."
      cp  ${OBICO_CFG_FILE} ${backup_config_file}
      echo -e "Old file moved to ${backup_config_file}\n${default}"
      return 1
    else
      return 0
    fi
  else
    return 1
  fi
}

create_config() {
  if [ -z "${OBICO_SERVER}" ]; then
    cat <<EOF
${cyan}
================================= Obico Server URL ==============================================
${default}
Now tell us what Obico Server you want to link your printer to.
You can use a self-hosted Obico Server or the Obico Cloud. For more information, please visit: https://obico.io.
For self-hosted server, specify "http://server_ip:port". For instance, http://192.168.0.5:3334.

EOF
    read -p "The Obico Server (Don't change unless you are linking to a self-hosted Obico Server): " -e -i "https://app.obico.io" user_input
    echo ""
    OBICO_SERVER="${user_input}"
  fi

  report_status "Creating config file ${OBICO_CFG_FILE} ..."
  cat <<EOF > "${OBICO_CFG_FILE}"
[server]
url = ${OBICO_SERVER}

[moonraker]
host = ${MOONRAKER_HOST}
port = ${MOONRAKER_PORT}
# api_key = <grab one or set trusted hosts in moonraker>

[webcam]
disable_video_streaming = False

# CAUTION: Don't modify the settings below unless you know what you are doing
#   In most cases webcam configuration will be automatically retrived from moonraker
#
# snapshot_url = http://127.0.0.1:8080/?action=snapshot
# stream_url = http://127.0.0.1:8080/?action=stream
# flip_h = False
# flip_v = False
# rotate_90 = False
# aspect_ratio_169 = False

[logging]
path = ${OBICO_LOG_FILE}
# level = INFO
EOF
}

service_existed() {
  if [ -f "/etc/systemd/system/${OBICO_SERVICE_NAME}.service" ]; then
    if [ $RECREATE_SERVICE = "y" ]; then
      report_status "Stopping ${OBICO_SERVICE_NAME}..."
      sudo systemctl stop "${OBICO_SERVICE_NAME}"
      return 1
    else
      return 0
    fi
  else
    return 1
  fi
}

recreate_service() {
  report_status "Creating moonraker-obico systemctl service... You may need to enter password to run sudo."
  sudo /bin/sh -c "cat > /etc/systemd/system/${OBICO_SERVICE_NAME}.service" <<EOF
#Systemd service file for moonraker-obico
[Unit]
Description=Obico for Moonraker
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
  echo ""
  report_status "${OBICO_SERVICE_NAME} service created and enabled."
  report_status "Launching ${OBICO_SERVICE_NAME} service..."
  sudo systemctl start "${OBICO_SERVICE_NAME}"
}

recreate_update_file() {
  cat <<EOF > "${OBICO_UPDATE_FILE}"
[update_manager ${OBICO_SERVICE_NAME}]
type: git_repo
path: ~/moonraker-obico
origin: ${OBICO_REPO}
env: ${OBICO_ENV}/bin/python
requirements: requirements.txt
install_script: install.sh
managed_services:
  ${OBICO_SERVICE_NAME}
EOF

  if ! grep -q "include moonraker-obico-update.cfg" "${MOONRAKER_CONFIG_FILE}" ; then
    echo "" >> "${MOONRAKER_CONFIG_FILE}"
    echo "[include moonraker-obico-update.cfg]" >> "${MOONRAKER_CONFIG_FILE}"
	fi
}

ensure_json_parser() {
cat <<EOF > ${JSON_PARSE_PY}
def find(element, json):
    try:
        keys = element.split('.')
        rv = json
        for key in keys:
            try:
                key = int(key)
            except:
                pass
            rv = rv[key]
        return rv
    except:
        return None

if __name__ == '__main__':
    import sys, json
    ret = find(sys.argv[1], json.load(sys.stdin))
    if ret is None:
        sys.exit(1)

    print(ret)
EOF
}

# Helper functions

exit_on_error() {
  oops
  cat <<EOF

The installation has run into an error:

${red}${1}${default}

Please fix the error above and re-run this setup script:

-------------------------------------------------------------------------------------------------
cd ~/moonraker-obico
./install.sh
-------------------------------------------------------------------------------------------------

EOF
  need_help
  exit 1
}

unknown_error() {
  exit_on_error "Installation interrupted by user or for unknown error."
}

uninstall() {
  cat <<EOF

To uninstall Obico for Klipper, please run:

sudo systemctl stop "${OBICO_SERVICE_NAME}"
sudo systemctl disable "${OBICO_SERVICE_NAME}"
sudo rm "/etc/systemd/system/${OBICO_SERVICE_NAME}.service"
sudo systemctl daemon-reload
sudo systemctl reset-failed
rm -rf ~/moonraker-obico

EOF

  exit 0
}

## Main flow for installation starts here:

trap 'unknown_error' ERR
trap 'unknown_error' INT

# Parse command line arguments
while getopts "hc:n:H:p:C:l:S:fLus" arg; do
    case $arg in
        h) usage && exit 0;;
        H) mr_host=${OPTARG};;
        p) mr_port=${OPTARG};;
        C) mr_config=${OPTARG};;
        l) log_path=${OPTARG%/};;
        n) SUFFIX="-${OPTARG}";;
        S) OBICO_SERVER="${OPTARG}";;
        f) OVERWRITE_CONFIG="y";;
        s) RECREATE_SERVICE="y";;
        L) SKIP_LINKING="y";;
        u) uninstall ;;
        *) usage && exit 1;;
    esac
done


welcome
ensure_not_octoprint
ensure_deps
ensure_json_parser

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

  if ! discover_sys_settings ; then
    prompt_for_settings
  fi

fi

if [ -z "${SUFFIX}" -a "${MOONRAKER_PORT}" -ne "7125" ]; then
  SUFFIX="-${MOONRAKER_PORT}"
fi

ensure_writtable "${MOONRAKER_CONF_DIR}"
ensure_writtable "${MOONRAKER_CONFIG_FILE}"
ensure_writtable "${MOONRAKER_LOG_DIR}"

[ -z "${OBICO_CFG_FILE}" ] && OBICO_CFG_FILE="${MOONRAKER_CONF_DIR}/moonraker-obico.cfg"
OBICO_UPDATE_FILE="${MOONRAKER_CONF_DIR}/moonraker-obico-update.cfg"
OBICO_LOG_FILE="${MOONRAKER_LOG_DIR}/moonraker-obico.log"
OBICO_SERVICE_NAME="moonraker-obico${SUFFIX}"
OBICO_LOG_FILE="${MOONRAKER_LOG_DIR}/moonraker-obico${SUFFIX}.log"

if ! service_existed ; then
  recreate_service
fi

if ! cfg_existed ; then
  create_config
fi

recreate_update_file

if "${OBICO_DIR}/scripts/migrated_from_tsd.sh" "${MOONRAKER_CONF_DIR}" "${OBICO_ENV}"; then
  exit 0
fi

trap - ERR
trap - INT

if [ $SKIP_LINKING != "y" ]; then
  "${OBICO_DIR}/scripts/link.sh" -c "${OBICO_CFG_FILE}" -n "${SUFFIX:1}"
fi
