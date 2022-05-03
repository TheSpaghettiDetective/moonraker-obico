#!/bin/bash

KLIPPER_CONF_DIR="$1"
OBICO_ENV="$2"
TSD_MR_CONFIG="${KLIPPER_CONF_DIR}/config.ini"
BK_TSD_MR_CONFIG="${KLIPPER_CONF_DIR}/retired-tsd-moonraker-config.ini"
OBICO_MR_CONFIG="${KLIPPER_CONF_DIR}/moonraker-obico.cfg"

if [[ -f "${TSD_MR_CONFIG}" ]]; then

  cat << EOF | "${OBICO_ENV}/bin/python3"
from configparser import ConfigParser
config = ConfigParser()
config.read(['${TSD_MR_CONFIG}', ])
auth_token = config.get('thespaghettidetective', 'auth_token')
config1 = ConfigParser()
config1.read(['${OBICO_MR_CONFIG}', ])
config1.set('server', 'auth_token', auth_token)
with open('${OBICO_MR_CONFIG}', 'w') as f:
    config1.write(f)
EOF

  retVal=$?
  if [ $retVal -ne 0 ]; then
    exit 1
  else
    mv "${TSD_MR_CONFIG}" "${BK_TSD_MR_CONFIG}"
    cat << EOF

===================================================================================================
###                                                                                             ###
###                                      SUCCESS!!!                                             ###
###                            Now enjoy Obico for Klipper!                                     ###
###                                                                                             ###
===================================================================================================

The printer you previously linked to The Spaghetti Detective has been
successfully migrated to Obico.

Now log into the Obico web app (https://app.obico.io) or mobile app
to make sure everything looks correct.

To remove Obico for Klipper, run:

cd ~/moonraker-obico
./install.sh -u

EOF
  exit 0
  fi
else
  exit 1
fi
