#!/bin/bash

# Copied from get.rvm.io. Not sure what they do
shopt -s extglob
set -o errtrace
set -o errexit
set -o pipefail

SYSTEMDDIR="/etc/systemd/system"
KLIPPER_CONF_DIR="${HOME}/klipper_config"
MOONRAKER_CONFIG_FILE="${KLIPPER_CONF_DIR}/moonraker.conf"
MOONRAKER_HOST="127.0.0.1"
MOONRAKER_PORT="7125"
LOG_DIR="${HOME}/klipper_logs"
OBICO_DIR="${HOME}/moonraker-obico"
OBICO_SERVER="https://app.obico.io"
CURRENT_USER=${USER}
JSON_PARSE_PY="/tmp/json_parse.py"
RESET_CONFIG="n"
UPDATE_SETTINGS="n"

# Helper functions
report_status() {
  echo -e "###### $1"
}

discover_sys_settings() {
  if ! mr_database=$(curl -s "http://${MOONRAKER_HOST}:${MOONRAKER_PORT}/server/database/list") ; then
    return 1
  fi

  if echo $mr_database | grep -qi 'mainsail' ; then
    has_mainsail=true
  fi

  if echo $mr_database | grep -qi 'fluidd' ; then
    has_fluidd=true
  fi

  if [[ "${has_mainsail}" = true && "${has_fluidd}" = true ]] ; then
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

  if [[ ! -f "${mr_config_file}" ]] ; then
    return 1
  fi

  if [[ "${has_mainsail}" = true ]] ; then
    toolchain_msg='Mainsail/Moonraker/Klipper'
  fi

  if [[ "${has_fluidd}" = true ]] ; then
    toolchain_msg='Fluidd/Moonraker/Klipper'
  fi

  read -p "${toolchain_msg} is detected. Moonraker is on port: ${MOONRAKER_PORT}. Is this correct? [Y/n]: " -e -i "Y" correct

  if [[ "${correct^^}" == "Y" ]] ; then
    KLIPPER_CONF_DIR="${mr_config_path}"
    LOG_DIR="${mr_log_path}"
    MOONRAKER_CONFIG_FILE="${mr_config_file}"
    return 0
  fi
  return 1
}

prompt_for_settings() {
  read -p "The port Moonraker is on: " -e -i "${MOONRAKER_PORT}" user_input
  MOONRAKER_PORT="${user_input}"
  read -p "The path of Moonraker's config file: " -e -i "${MOONRAKER_CONFIG_FILE}" user_input
  MOONRAKER_CONFIG_FILE="${user_input}"
  read -p "The directory for Obico's log files: " -e -i "${LOG_DIR}" user_input
  LOG_DIR="${user_input}"
}

ensure_venv() {
  if [[ -f "${HOME}/moonraker-env/bin/activate" ]] ; then
    OBICO_ENV="${HOME}/moonraker-env"
  else
    OBICO_ENV="${HOME}/moonraker-obico-env"

    report_status "Installing required system packages... You may be prompted to enter password."
    PKGLIST="python3 python3-pip python3-venv"
    sudo apt-get update --allow-releaseinfo-change
    sudo apt-get install --yes ${PKGLIST}

    report_status "Creating python virtual environment for moonraker-obico..."
    mkdir -p "${OBICO_ENV}"
    virtualenv -p /usr/bin/python3 --system-site-packages "${OBICO_ENV}"
    "${OBICO_ENV}"/bin/pip3 install -r "${OBICO_DIR}"/requirements.txt
  fi
}

ensure_writtable() {
  dest_path="$1"
  if [[ ! -w "$1" ]] ; then
    echo "$1 doesn't exist or can't be changed."
    echo "Please make sure $1 exits and can be changed. Then re-run this setup."
    exit 1
  fi
}

cfg_existed() {
  if [[ -f "${OBICO_CFG_FILE}" ]] ; then
    if [[ $RESET_CONFIG = "y" ]]; then
      backup_config_file="${OBICO_CFG_FILE}-$(date '+%Y-%m-%d')"
      echo "!!!WARNING: Overwriting ${OBICO_CFG_FILE}..."
      echo "Old file moved to ${backup_config_file}"
      cp  ${OBICO_CFG_FILE} ${backup_config_file}
      return 1
    else
      return 0
    fi
  else
    return 1
  fi
}

create_config() {
  read -p "URL for the Obico server (Don't change unless you are connecting to a self-hosted Obico server): " -e -i "${OBICO_SERVER}" user_input
  OBICO_SERVER="${user_input}"
  cat <<EOF > "${OBICO_CFG_FILE}"
[server]
url = ${OBICO_SERVER}

[moonraker]
host = ${MOONRAKER_HOST}
port = ${MOONRAKER_PORT}
# api_key = <grab one or set trusted hosts in moonraker>

[webcam]
# CAUTION: Don't set this section unless you know what you are doing
#   In most cases webcam configuration will be automatically retrived from moonraker
#
# snapshot_url = http://127.0.0.1:8080/?action=snapshot
# stream_url = http://127.0.0.1:8080/?action=stream
# flip_h = False
# flip_v = False
# rotate_90 = False
# aspect_ratio_169 = False

[logging]
path = ${LOG_DIR}/moonraker-obico-${MOONRAKER_PORT}.log
# level = INFO
EOF
}

service_existed() {
  if systemctl --all --type service --no-legend | grep -q moonraker-obico ; then
    if [[ $UPDATE_SETTINGS = "y" ]]; then
      report_status "Stopping moonraker-obico service..."
      systemctl stop moonraker-obico
      return 1
    else
      return 0
    fi
  else
    return 1
  fi
}

recreate_service() {
  echo "Creating systemctl service moonraker-obico... You may need to enter password to run sudo."
  sudo /bin/sh -c "cat > ${SYSTEMDDIR}/moonraker-obico.service" <<EOF
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

  sudo systemctl enable moonraker-obico.service
  sudo systemctl daemon-reload
  report_status "moonraker-obico service created and enabled."
  report_status "Launching moonraker-obico service..."
  systemctl start moonraker-obico
}

recreate_update_file() {
  cat <<EOF > "${OBICO_UPDATE_FILE}"
[update_manager moonraker-obico]
type: git_repo
path: ~/moonraker-obico
origin: https://github.com/TheSpaghettiDetective/tsd-moonraker.git
env: ${OBICO_ENV}/bin/python
requirements: requirements.txt
install_script: install.sh
is_system_service: True
EOF

  if ! grep -q "include moonraker-obico-update.cfg" "${MOONRAKER_CONFIG_FILE}" ; then
    echo "" >> "${MOONRAKER_CONFIG_FILE}"
    echo "[include moonraker-obico-update.cfg]" >> "${MOONRAKER_CONFIG_FILE}"
	fi
}

resume_linking() {
  echo "The process to link to the Obico Server is interrupted."
  echo "To resume the linking process at a later time, run:"
  echo "${OBICO_DIR}/install.sh"
}

link_to_server() {
  trap resume_linking INT

  ${OBICO_ENV}/bin/python3 -m moonraker_obico.link -c /home/pi/klipper_config/moonraker-obico.cfg

  trap - INT
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

# Parse command line arguments
while getopts "fu" arg; do
    case $arg in
        f) RESET_CONFIG="y";;
        u) UPDATE_SETTINGS="y";;
    esac
done

ensure_venv
ensure_json_parser

if ! discover_sys_settings ; then
  prompt_for_settings
fi

ensure_writtable "${KLIPPER_CONF_DIR}"
ensure_writtable "${MOONRAKER_CONFIG_FILE}"
ensure_writtable "${LOG_DIR}"

OBICO_CFG_FILE="${KLIPPER_CONF_DIR}/moonraker-obico.cfg"
OBICO_UPDATE_FILE="${KLIPPER_CONF_DIR}/moonraker-obico-update.cfg"

if ! service_existed ; then
  recreate_service
  recreate_update_file
fi

if ! cfg_existed ; then
  create_config
fi

link_to_server
