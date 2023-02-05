#!/bin/bash

set -e

JANUS_ROOT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
RUNTIME_JANUS_ETC_DIR="${JANUS_ROOT_DIR}/runtime/etc/janus"
TPL_JANUS_ETC_DIR="${JANUS_ROOT_DIR}/templates/etc/janus"

# . "${JANUS_ROOT_DIR}/../utils.sh"

mkdir -p "${RUNTIME_JANUS_ETC_DIR}"

embedded_janus_jcfg_folders_section() {
  lib_janus_dir="${JANUS_ROOT_DIR}/rpi_os/lib/janus"
  cat <<EOT
        plugins_folder = "${lib_janus_dir}/plugins"                     # Plugins folder
        transports_folder = "${lib_janus_dir}/transports"       # Transports folder
        events_folder = "${lib_janus_dir}/events"                       # Event handlers folder
        loggers_folder = "${lib_janus_dir}/loggers"
EOT
}

system_janus_jcfg_folders_section() {
  system_janus_jcfg_path=$(dpkg -L janus | grep /janus.jcfg)
  grep -E '^\s*plugins_folder\s*=|^\s*transports_folder\s*=|^\s*events_folder\s*=|^\s*loggers_folder\s*=' "${system_janus_jcfg_path}"
}

janus_jcfg_turns_cred_section() {

  if [ -z "${AUTH_TOKEN}" ]; then
    >&2 echo "AUTH_TOKEN not specified"
    exit 1
  fi

  cat <<EOT
        turn_user = "${AUTH_TOKEN}"
        turn_pwd = "${AUTH_TOKEN}"
EOT
}

gen_janus_jcfg() {
  janus_jcfg_path="${RUNTIME_JANUS_ETC_DIR}/janus.jcfg"

  cat <<EOT >"${janus_jcfg_path}"
general: {
EOT

#  if is_raspberry_pi; then
#    embedded_janus_jcfg_folders_section >>"${janus_jcfg_path}"  # Janus binary is embedded for Raspberry Pi for easier installation
#  else
    system_janus_jcfg_folders_section >>"${janus_jcfg_path}"
#  fi

  cat <<EOT >>"${janus_jcfg_path}"
        admin_secret = "janusoverlord"  # String that all Janus requests must contain
}
nat: {
        turn_server = "turn.obico.io"
        turn_port = 80
        turn_type = "tcp"
EOT

  janus_jcfg_turns_cred_section >>"${janus_jcfg_path}"

  cat <<EOT >>"${janus_jcfg_path}"
        ice_ignore_list = "vmnet"
        ignore_unreachable_ice_server = true
}
plugins: {
        disable = "libjanus_audiobridge.so,libjanus_echotest.so,libjanus_nosip.so,libjanus_sip.so,libjanus_textroom.so,libjanus_videoroom.so,libjanus_duktape.so,libjanus_lua.so,libjanus_recordplay.so,libjanus_videocall.so,libjanus_voicemail.so"
}
transports: {
        disable = "libjanus_mqtt.so,libjanus_nanomsg.so,libjanus_pfunix.so,libjanus_rabbitmq.so,libjanus_http.so"
}
events: {
}
EOT

}

gen_janus_plugin_streaming_jcfg() {
  if [ -z "${VIDEO_ENABLED}" ]; then
    >&2 echo "VIDEO_ENABLED not specified"
    exit 1
  fi

  DEFAULT_CHANNEL_ID=1
  # We are still running Janus 0.x on Raspberry Pi and using channel_id=0 to be compatible with old mobile app versions. To be consolidated to using id 1 when old veresions have faded out.
#  if is_raspberry_pi; then
#    DEFAULT_CHANNEL_ID=0
#  fi

  streaming_jcfg_path="${RUNTIME_JANUS_ETC_DIR}/janus.plugin.streaming.jcfg"
  tpl_streaming_jcfg_path="${TPL_JANUS_ETC_DIR}/janus.plugin.streaming.jcfg.template"
  sed "s/__VIDEO_ENABLED__/${VIDEO_ENABLED}/g" "${tpl_streaming_jcfg_path}" | sed "s/__DEFAULT_CHANNEL_ID__/${DEFAULT_CHANNEL_ID}/g" > "${streaming_jcfg_path}"
}

gen_janus_transport_websocket_jcfg() {
  target_path="${RUNTIME_JANUS_ETC_DIR}/janus.transport.websockets.jcfg"
  tpl_path="${TPL_JANUS_ETC_DIR}/janus.transport.websockets.jcfg.template"
  cp "${tpl_path}" "${target_path}"
}

while getopts "A:V:" arg; do
  case $arg in
    A) AUTH_TOKEN=${OPTARG};;
    V) VIDEO_ENABLED=${OPTARG};;
  esac
done

gen_janus_jcfg
gen_janus_plugin_streaming_jcfg
gen_janus_transport_websocket_jcfg
