#!/bin/bash -e

debian_release() {
  cat /etc/debian_version | cut -d '.' -f1
}

debian_variant() {
  echo $( board_id ).debian.$( debian_release ).$( getconf LONG_BIT )-bit
}

board_id() {
    local model_file="/sys/firmware/devicetree/base/model"

    if [ -f "$model_file" ]; then
        if grep "Raspberry" $model_file >/dev/null; then
            echo "rpi"
        else
            echo "NA"
        fi
    else
        echo "NA"
    fi
}
