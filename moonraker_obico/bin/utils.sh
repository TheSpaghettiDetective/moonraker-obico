#!/bin/bash -e

debian_release() {
  cat /etc/debian_version | cut -d '.' -f1
}

debian_variant() {
  echo $( debian_release ).$( getconf LONG_BIT )-bit
}
