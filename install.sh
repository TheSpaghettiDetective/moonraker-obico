#!/bin/bash

# Copied from get.rvm.io
shopt -s extglob
set -o errtrace
set -o errexit
set -o pipefail


SYSTEMDDIR="/etc/systemd/system"
OBICO_DIR="${HOME}/tsd-moonraker"
CURRENT_USER=${USER}

# Helper functions
report_status() {
  echo -e "###### $1"
}

# Set up config

klipper_conf_dir_valid() {
  [[ -d "${KLIPPER_CONF_DIR}" ]]
  return $?
}

ensure_klipper_conf_dir() {
  KLIPPER_CONF_DIR="${HOME}/klipper_config"
  if klipper_conf_dir_valid ; then
    report_status "Locating Klipper config dir... found!"
  else
    while ! klipper_conf_dir_valid ; do
      read -p "Enter your klipper config directory path: " -e -i "${KLIPPER_CONF_DIR}" klip_conf_dir
      KLIPPER_CONF_DIR=${klip_conf_dir}
    done
  fi
}

moonraker_config_valid() {
  [[ -f "${MOONRAKER_CONFIG}" ]]
  return $?
}

ensure_moonraker_config() {
  MOONRAKER_CONFIG="${KLIPPER_CONF_DIR}/moonraker.conf"
  if moonraker_config_valid ; then
    report_status "Locating Moonraker config file... found!"
  else
    while ! moonraker_config_valid ; do
      read -p "Enter your Moonraker config file path: " -e -i "${MOONRAKER_CONFIG}" mr_config
      MOONRAKER_CONFIG="${mr_config}"
    done
  fi
}

ensure_venv() {
  if [[ -f "${HOME}/moonraker-env/bin/activate" ]] ; then
    OBICO_ENV="${HOME}/moonraker-env"
  else
    OBICO_ENV="${HOME}/obico-env"

    report_status "Installing required system packages..."
    PKGLIST="python3 python3-pip python3-venv"
    sudo apt-get update --allow-releaseinfo-change
    sudo apt-get install --yes ${PKGLIST}

    report_status "Creating python virtual environment for TSD..."
    mkdir -p "${OBICO_ENV}"
    virtualenv -p /usr/bin/python3 --system-site-packages "${OBICO_ENV}"
    "${OBICO_ENV}"/bin/pip3 install -r "${OBICO_DIR}"/requirements.txt
  fi
}

ensure_log_dir() {
  if [[ -w "${HOME}/klipper_logs" ]] ; then
    LOG_DIR="${HOME}/klipper_logs"
  else
    LOG_DIR="${HOME}/obico_logs"
    mkdir -p "${LOG_DIR}"
  fi
}

create_initial_config() {
  # check if config exists!
  if [[ ! -f "${KLIPPER_CONF_DIR}"/config.ini ]]; then
    report_status "Selecting log path"
    echo -e "\n"
    read -p "Enter your bot log file: " -e -i "${LOG_DIR}" bot_log_path
    LOG_DIR=${bot_log_path}
    report_status "Writing bot logs to ${LOG_DIR}"
    # check if dir exists!
    if [[ ! -d "${LOG_DIR}" ]]; then
      mkdir "${LOG_DIR}"
    fi

    report_status "Creating base config file"
    cp -n "${OBICO_DIR}"/config.sample.ini "${KLIPPER_CONF_DIR}"/config.ini

    sed -i "s+some_log_path+${LOG_DIR}+g" "${KLIPPER_CONF_DIR}"/config.ini
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

create_service() {
  # check if config exists!
  if [[ ! -f "${SYSTEMDDIR}"/tsd-moonraker.service ]]; then
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
WorkingDirectory=${OBICO_DIR}
ExecStart=${OBICO_ENV}/bin/python3 -m tsd_moonraker.app -c ${KLIPPER_CONF_DIR}/config.ini -l ${LOG_DIR}/tsd-moonraker.log
Restart=always
RestartSec=5
EOF

    ### enable instance
    sudo systemctl enable tsd-moonraker.service
    report_status "Single TheSpaghettiDetective Moonraker Plugin instance created!"
  fi
  ### launching instance
  report_status "Launching TheSpaghettiDetective Moonraker Plugin instance ..."
  sudo systemctl start tsd-moonraker
}

#init_config_path
#create_initial_config
#stop_sevice
#install_packages
#create_virtualenv
#create_service

ensure_klipper_conf_dir
ensure_moonraker_config
ensure_venv
ensure_log_dir
