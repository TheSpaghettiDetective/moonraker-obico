#!/bin/bash -e

GST_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

_term() {
  kill -TERM "$child" 2>/dev/null
}

trap _term SIGTERM

LD_LIBRARY_PATH=$GST_DIR/lib GST_PLUGIN_SCANNER=$GST_DIR/lib/gstreamer-1.0 GST_PLUGIN_SYSTEM_PATH=$GST_DIR/lib/gstreamer-1.0 GST_PLUGIN_PATH=$GST_DIR/lib/gstreamer-1.0 GST_OMX_CONFIG_DIR=$GST_DIR/etc/xdg nice $GST_DIR/bin/gst-launch-1.0 v4l2src device=/dev/video0 ! videoconvert ! "video/x-raw,width=640,height=480" ! videorate ! "video/x-raw,framerate=10/1" ! tee name=t ! queue ! videorate ! video/x-raw,framerate=3/1 ! jpegenc ! multipartmux boundary=spionisto ! tcpserversink host=127.0.0.1 port=14499 t. ! queue ! videoconvert ! omxh264enc target-bitrate=10000000 control-rate=2 interval-intraframes=10 periodicty-idr=10 ! "video/x-h264,profile=baseline" ! rtph264pay ! udpsink host=127.0.0.1 port=8004 &

child=$!
wait "$child"
