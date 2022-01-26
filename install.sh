#!/bin/bash
# This script installs TheSpaghettiDetective Moonraker Plugin
set -eu

SYSTEMDDIR="/etc/systemd/system"
MOONRAKER_BOT_ENV="${HOME}/tsd-moonraker"
MOONRAKER_BOT_DIR="${HOME}/tsd-moonraker/tsd_moonraker"
MOONRAKER_BOT_LOG="${HOME}/klipper_logs"
KLIPPER_CONF_DIR="${HOME}/klipper_config"
CURRENT_USER=${USER}

# Helper functions
report_status() {
  echo -e "\n###### $1"
}

# Main functions
init_config_path() {
  if [ -z ${klipper_cfg_loc+x} ]; then
    report_status "Selecting config path"
    echo -e "\n"
    read -p "Enter your klipper configs path: " -e -i "${KLIPPER_CONF_DIR}" klip_conf_dir
    KLIPPER_CONF_DIR=${klip_conf_dir}
  else
    KLIPPER_CONF_DIR=${klipper_cfg_loc}
  fi
  report_status "Using configs from ${KLIPPER_CONF_DIR}"
}

create_initial_config() {
  # check in config exists!
  if [[ ! -f "${KLIPPER_CONF_DIR}"/config.ini ]]; then
    report_status "Selecting log path"
    echo -e "\n\n\n"
    read -p "Enter your bot log file: " -e -i "${MOONRAKER_BOT_LOG}" bot_log_path
    MOONRAKER_BOT_LOG=${bot_log_path}
    report_status "Writing bot logs to ${MOONRAKER_BOT_LOG}"

    report_status "Creating base config file"
    cp -n "${MOONRAKER_BOT_DIR}"/config.sample.ini "${KLIPPER_CONF_DIR}"/config.ini

    sed -i "s+some_log_path+${MOONRAKER_BOT_LOG}+g" "${KLIPPER_CONF_DIR}"/config.ini
  fi
}

stop_sevice() {
  serviceName="tsd-moonraker"
  if sudo systemctl --all --type service --no-legend | grep "$serviceName" | grep -q running; then
    ## stop existing instance
    report_status "Stopping TheSpaghettiDetective Moonraker Plugin instance ..."
    sudo systemctl stop tsd-moonraker
  else
    report_status "$serviceName service does not exist or not running."
  fi
}

install_packages() {
  PKGLIST="python3 python3-pip python3-venv"

  report_status "Running apt-get update..."
  sudo apt-get update --allow-releaseinfo-change

  report_status "Installing packages..."
  sudo apt-get install --yes ${PKGLIST}
}

create_virtualenv() {
  report_status "Installing python virtual environment..."

  mkdir -p "${HOME}"/space
  virtualenv -p /usr/bin/python3 --system-site-packages "${MOONRAKER_BOT_ENV}"
  export TMPDIR=${HOME}/space
  "${MOONRAKER_BOT_ENV}"/bin/pip3 install -r "${MOONRAKER_BOT_ENV}"/requirements.txt
}

create_service() {
  ### create systemd service file
  sudo /bin/sh -c "cat > ${SYSTEMDDIR}/tsd-moonraker.service" <<EOF
#Systemd service file for TheSpaghettiDetective Moonraker Plugin
[Unit]
Description=Starts TheSpaghettiDetective Moonraker Plugin on startup
After=network-online.target moonraker.service

[Install]
WantedBy=multi-user.target

[Service]
Type=simple
User=${CURRENT_USER}
ExecStart=${MOONRAKER_BOT_ENV}/bin/python3 -m ${MOONRAKER_BOT_DIR}/tsd_moonraker.app -c ${KLIPPER_CONF_DIR}/config.ini -l ${MOONRAKER_BOT_LOG}/tsd-moonraker.log
Restart=always
RestartSec=5
EOF

  ### enable instance
  sudo systemctl enable tsd-moonraker.service
  report_status "Single TheSpaghettiDetective Moonraker Plugin instance created!"

  ### launching instance
  report_status "Launching TheSpaghettiDetective Moonraker Plugin instance ..."
  sudo systemctl start tsd-moonraker
}

init_config_path
create_initial_config
stop_sevice
install_packages
create_virtualenv
create_service
