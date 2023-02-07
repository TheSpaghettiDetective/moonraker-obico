#!/bin/bash

set -e

FFMPEG_ROOT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

. "${FFMPEG_ROOT_DIR}/../utils.sh"

PRECOMPILED_DIR="${FFMPEG_ROOT_DIR}/precomplied/debian.$( debian_variant )"

# We need patched ffmpeg for some systems that is distributed with defected ffmpeg, such as h264_v4l2m2m in debian 11 (bullseye, 32-bit)
if [ -d "${PRECOMPILED_DIR}" ]; then
  FFMPEG_CMD="${PRECOMPILED_DIR}/bin/ffmpeg"
else
  FFMPEG_CMD="ffmpeg"
fi

#_term() {
#  kill -TERM "$child" 2>/dev/null
#}
#
#trap _term SIGTERM
#
# nice "${FFMPEG_CMD}" -d 7 -o --stun-server=stun.l.google.com:19302 --configs-folder="${RUNTIME_JANUS_ETC_DIR}" &
exec nice "${FFMPEG_CMD}" "$@"

#child=$!
#wait "$child"
