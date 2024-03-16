#!/bin/bash

set -e

FFMPEG_ROOT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

. "${FFMPEG_ROOT_DIR}/../utils.sh"

PRECOMPILED_DIR="${FFMPEG_ROOT_DIR}/precomplied/$( debian_variant )"

# We need patched ffmpeg for some systems that is distributed with defected ffmpeg, such as h264_v4l2m2m in debian 11 (bullseye, 32-bit)
if [ -d "${PRECOMPILED_DIR}" ]; then
  FFMPEG_CMD="${PRECOMPILED_DIR}/bin/ffmpeg"
else
  FFMPEG_CMD="ffmpeg"
fi

exec nice "${FFMPEG_CMD}" "$@"
