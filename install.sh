#!/bin/bash

# Copied from get.rvm.io. Not sure what they do
shopt -s extglob
set -o errtrace
set -o errexit
set -o pipefail

green=$(echo -en "\e[92m")
yellow=$(echo -en "\e[93m")
red=$(echo -en "\e[91m")
cyan=$(echo -en "\e[96m")
default=$(echo -en "\e[39m")

SUFFIX=""
KLIPPER_CONF_DIR="${HOME}/klipper_config"
MOONRAKER_CONFIG_FILE="${KLIPPER_CONF_DIR}/moonraker.conf"
MOONRAKER_HOST="127.0.0.1"
MOONRAKER_PORT="7125"
LOG_DIR="${HOME}/klipper_logs"
OBICO_DIR="${HOME}/moonraker-obico"
OBICO_SERVER="https://app.obico.io"
OBICO_REPO="https://github.com/TheSpaghettiDetective/moonraker-obico.git"
OBICO_SERVICE_NAME="moonraker-obico"
CURRENT_USER=${USER}
JSON_PARSE_PY="/tmp/json_parse.py"
RESET_CONFIG="n"
UPDATE_SETTINGS="n"
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
          -u   Show uninstallation instructions

Moonraker setting options (${yellow}if any of them are specified, all need to be specified${default}):
          -H   Moonraker server hostname or ip address
          -p   Moonraker server port
          -c   Moonraker config file path
          -l   The directory for moonraker-obico log files, which are rotated based on size.
EOF
  exit 0
}

manual_setting_warning() {
  cat <<EOF
${yellow}
!!!WARNING: You are manually specifying the Moonraker settings. This can be error prone.
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
  report_status "Detecting the softwares and settings of your Klipper system ...\n"

  if ! mr_database=$(curl -s "http://${MOONRAKER_HOST}:${MOONRAKER_PORT}/server/database/list") ; then
    return 1
  fi

  if echo $mr_database | grep -qi 'mainsail' ; then
    has_mainsail=true
  fi

  if echo $mr_database | grep -qi 'fluidd' ; then
    has_fluidd=true
  fi

  if [ "${has_mainsail}" = true && "${has_fluidd}" = true ] ; then
    return 1
  fi

  if ! mr_info=$(curl -s "http://${MOONRAKER_HOST}:${MOONRAKER_PORT}/server/config") ; then
    return 1
  fi

  # It seems that config can be in either config.server or config.file_manager
  if ! mr_config_path=$(echo $mr_info | ${OBICO_ENV}/bin/python3 ${JSON_PARSE_PY} 'result.config.server.config_path') ; then
    if ! mr_config_path=$(echo $mr_info | ${OBICO_ENV}/bin/python3 ${JSON_PARSE_PY} 'result.config.file_manager.config_path') ; then
      return 1
    fi
  fi

  # It seems that log_path can be in either config.server or config.file_manager
  if ! mr_log_path=$(echo $mr_info | ${OBICO_ENV}/bin/python3 ${JSON_PARSE_PY} 'result.config.server.log_path') ; then
    if ! mr_log_path=$(echo $mr_info | ${OBICO_ENV}/bin/python3 ${JSON_PARSE_PY} 'result.config.file_manager.log_path') ; then
      return 1
    fi
  fi

  eval mr_config_path="${mr_config_path}"
  eval mr_log_path="${mr_log_path}"

  mr_config_file="${mr_config_path}/moonraker.conf"

  if [ ! -f "${mr_config_file}" ] ; then
    return 1
  fi

  if [ "${has_mainsail}" = true ] ; then
    toolchain_msg='Mainsail'
  fi

  if [ "${has_fluidd}" = true ] ; then
    toolchain_msg='Fluidd'
  fi

  echo -e "The following have been detected:\n"
  echo -e "- Web Server: Moonraker"
  echo -e "- Web Frontend: ${toolchain_msg}"
  echo -e "- Moonraker port: ${MOONRAKER_PORT}\n"
  read -p "Is this correct? [Y/n]: " -e -i "Y" correct
  echo ""

  if [ "${correct^^}" == "Y" ] ; then
    KLIPPER_CONF_DIR="${mr_config_path}"
    LOG_DIR="${mr_log_path}"
    MOONRAKER_CONFIG_FILE="${mr_config_file}"
    return 0
  fi
  return 1
}

prompt_for_settings() {
  echo -e "We couldn't automatically detect the settings. Please enter them below to continue:\n"
  read -p "Moonraker port: " -e -i "${MOONRAKER_PORT}" user_input
  eval MOONRAKER_PORT="${user_input}"
  read -p "Moonraker config file: " -e -i "${MOONRAKER_CONFIG_FILE}" user_input
  eval MOONRAKER_CONFIG_FILE="${user_input}"
  KLIPPER_CONF_DIR=$(dirname "${MOONRAKER_CONFIG_FILE}")
  read -p "Klipper log directory: " -e -i "${LOG_DIR}" user_input
  eval LOG_DIR="${user_input}"
  echo ""
}

ensure_deps() {
  report_status "Installing required system packages... You may be prompted to enter password."

  PKGLIST="python3 python3-pip python3-venv ffmpeg"
  sudo apt-get update --allow-releaseinfo-change
  sudo apt-get install --yes ${PKGLIST}

  echo -e ""
  if [ -f "${HOME}/moonraker-env/bin/activate" ] ; then
    OBICO_ENV="${HOME}/moonraker-env"
  else
    OBICO_ENV="${HOME}/moonraker-obico-env"
    report_status "Creating python virtual environment for moonraker-obico..."
    mkdir -p "${OBICO_ENV}"
    virtualenv -p /usr/bin/python3 --system-site-packages "${OBICO_ENV}"
  fi
  "${OBICO_ENV}"/bin/pip3 install -r "${OBICO_DIR}"/requirements.txt
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
    if [ $RESET_CONFIG = "y" ]; then
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
  cat <<EOF

================================= Obico Server URL ==============================================

EOF

  echo -e "Now tell us what Obico Server you want to link your printer to."
  echo -e "You can use a self-hosted Obico Server or the Obico Cloud. For more information, please visit: https://obico.io\n"
  echo -e "For self-hosted server, specify \`http://server_ip:port\`. For instance, http://192.168.0.5:3334.\n"
  read -p "The Obico Server (Don't change unless you are linking to a self-hosted Obico Server): " -e -i "${OBICO_SERVER}" user_input
  echo ""
  OBICO_SERVER="${user_input}"
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
    if [ $UPDATE_SETTINGS = "y" ]; then
      report_status "Stopping ${OBICO_SERVICE_NAME}..."
      systemctl stop "${OBICO_SERVICE_NAME}"
      return 1
    else
      report_status "moonraker-obico systemctl service already existed. Skipping..."
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
  systemctl start "${OBICO_SERVICE_NAME}"
}

recreate_update_file() {
  cat <<EOF > "${OBICO_UPDATE_FILE}"
[update_manager moonraker-obico]
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

link_to_server() {
  cat <<EOF

=============================== Link Printer to Obico Server ======================================

EOF
  PYTHONPATH=$(dirname "$0"):${PYTHONPATH} ${OBICO_ENV}/bin/python3 -m moonraker_obico.link -c "${OBICO_CFG_FILE}"
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
report_status() {
  echo -e "###### $1"
}

welcome() {
  cat <<EOF

=====================================================================================================
###                                                                                               ###
###                       Install and Configure Obico for Klipper                                 ###
###                                                                                               ###
=====================================================================================================

EOF
}

oops() {
  cat <<EOF

   ____
  / __ \\
 | |  | | ___   ___   ___   ___  _ __  ___
 | |  | |/ _ \\ / _ \\ / _ \\ / _ \\| '_ \\/ __|
 | |__| | (_) | (_) | (_) | (_) | |_) \\__ \\  _   _   _
  \\____/ \\___/ \\___/ \\___/ \\___/| .__/|___/ (_) (_) (_)
                                | |
                                |_|


EOF
}

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

Need help? Stop by:

- The Obico's help docs: https://obico.io/help/
- The Obico community: https://discord.gg/hsMwGpD

EOF
  exit 1
}

finished() {
  echo -e "\n\n\n"
  cat $(dirname "$0")/scripts/banner
  cat <<EOF
===================================================================================================
###                                                                                             ###
###                                      SUCCESS!!!                                             ###
###                            Now enjoy Obico for Klipper!                                     ###
###                                                                                             ###
===================================================================================================

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

unknown_error() {
  exit_on_error "Installation interrupted by user or for unknown error."
}

uninstall() {
  cat <<EOF

To uninstall Obico for Klipper, please run:

sudo systemctl stop "${OBICO_SERVICE_NAME}"
sudo systemctl disable "${OBICO_SERVICE_NAME}"
sudo rm "/etc/systemd/system/${OBICO_SERVICE_NAME}"
sudo systemctl daemon-reload
sudo systemctl reset-failed
rm -rf ~/moonraker-obico

EOF

  exit 0
}

## Main flow for installation starts here:

trap 'unknown_error' ERR
trap 'unknown_error' INT

OBICO_DIR=$(realpath $(dirname "$0"))

# Parse command line arguments
while getopts "hn:H:p:c:l:fLus" arg; do
    case $arg in
        h) usage && exit 0;;
        H) mr_host=${OPTARG};;
        p) mr_port=${OPTARG};;
        c) mr_config=${OPTARG};;
        l) log_path=${OPTARG%/};;
        f) RESET_CONFIG="y";;
        n) SUFFIX="-${OPTARG}";;
        s) UPDATE_SETTINGS="y";;
        L) SKIP_LINKING="y";;
        u) uninstall ;;
        *) usage && exit 0;;
    esac
done


welcome
ensure_not_octoprint
ensure_deps
ensure_json_parser

if $(dirname "$0")/scripts/tsd_service_existed.sh ; then
  exit 0
fi

if [ -n "${mr_host}" ] || [ -n "${mr_port}" ] || [ -n "${mr_config}" ] || [ -n "${log_path}" ]; then

  if ! { [ -n "${mr_host}" ] && [ -n "${mr_port}" ] && [ -n "${mr_config}" ] && [ -n "${log_path}" ]; }; then
    usage "Please specify all Moonraker setting options. See usage below." && exit 1
  else
    MOONRAKER_HOST="${mr_host}"
    MOONRAKER_PORT="${mr_port}"
    eval MOONRAKER_CONFIG_FILE="${mr_config}"
    eval KLIPPER_CONF_DIR=$(dirname "${MOONRAKER_CONFIG_FILE}")
    eval LOG_DIR="${log_path}"
  fi

else

  if ! discover_sys_settings ; then
    manual_setting_warning
    prompt_for_settings
  fi

fi

ensure_writtable "${KLIPPER_CONF_DIR}"
ensure_writtable "${MOONRAKER_CONFIG_FILE}"
ensure_writtable "${LOG_DIR}"

if [ -z "${SUFFIX}" -a "${MOONRAKER_PORT}" -ne "7125" ]; then
  SUFFIX="-${MOONRAKER_PORT}"
fi

OBICO_CFG_FILE="${KLIPPER_CONF_DIR}/moonraker-obico.cfg"
OBICO_UPDATE_FILE="${KLIPPER_CONF_DIR}/moonraker-obico-update.cfg"
OBICO_LOG_FILE="${LOG_DIR}/moonraker-obico.log"
OBICO_SERVICE_NAME="moonraker-obico${SUFFIX}"
OBICO_LOG_FILE="${LOG_DIR}/moonraker-obico${SUFFIX}.log"

if ! service_existed ; then
  recreate_service
fi

if ! cfg_existed ; then
  create_config
fi

recreate_update_file

if $(dirname "$0")/scripts/migrated_from_tsd.sh "${KLIPPER_CONF_DIR}" "${OBICO_ENV}"; then
  exit 0
fi

if [ $SKIP_LINKING != "y" ]; then
  trap - ERR
  trap - INT

  if link_to_server ; then
    systemctl restart "${OBICO_SERVICE_NAME}"
    prompt_for_sentry
  fi
fi

finished
