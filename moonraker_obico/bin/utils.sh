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
        if grep -q -i -e "Raspberry" $model_file; then
            echo "rpi"
        elif grep -q -i -e "Makerbase" -e "roc-rk3328-cc" $model_file; then # Somehow some mks boards have 'Firefly roc-rk3328-cc' in /sys/firmware/devicetree/base/model
	    echo "mks"
        else
            echo "NA"
        fi
    else
        echo "NA"
    fi
}
