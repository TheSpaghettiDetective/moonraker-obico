#!/bin/bash -e

JANUS_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

_term() {
  kill -TERM "$child" 2>/dev/null
}

trap _term SIGTERM

LD_LIBRARY_PATH=$JANUS_DIR/lib:$JANUS_DIR/lib/janus:$LD_LIBRARY_PATH nice $JANUS_DIR/bin/janus -o --stun-server=stun.l.google.com:19302 --configs-folder=$JANUS_DIR/etc/janus &

child=$!
wait "$child"
