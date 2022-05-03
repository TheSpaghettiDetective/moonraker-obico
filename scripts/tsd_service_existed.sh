#!/bin/bash

KLIPPER_CONF_DIR="$1"
SYSTEMDDIR="/etc/systemd/system"
TSD_SERVICE_FILE="${SYSTEMDDIR}/tsd-moonraker.service"

if [[ -f "${SYSTEMDDIR}/tsd-moonraker.service" ]]; then
  cat <<EOF

===================================================================================================
###                                                                                             ###
###                                           NOTICE!                                           ###
###                                                                                             ###
===================================================================================================

You were previously using The Spaghetti Detective for Moonraker.
In order to move from The Spaghetti Detective to Obico, please:

1. Run the following commands:

-------------------------------------------------------------------------------------------------
sudo systemctl stop tsd-moonraker.service
sudo systemctl disable tsd-moonraker.service
sudo rm /etc/systemd/system/tsd-moonraker.service
sudo systemctl daemon-reload
sudo systemctl reset-failed
rm -rf ~/tsd-moonraker
-------------------------------------------------------------------------------------------------

2. Run "systemctl status tsd-moonraker.service".
   Make sure the output is:
   "Unit tsd-moonraker.service could not be found."

3. Rerun:

-------------------------------------------------------------------------------------------------
cd ~/moonraker-obico
./install.sh
-------------------------------------------------------------------------------------------------

EOF
  exit 0
else
  exit 1
fi
