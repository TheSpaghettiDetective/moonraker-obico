#!/bin/bash

DEBUG="n"

green=$(echo -en "\e[92m")
yellow=$(echo -en "\e[93m")
magenta=$(echo -en "\e[35m")
red=$(echo -en "\e[91m")
cyan=$(echo -en "\e[96m")
default=$(echo -en "\e[39m")

ensure_venv() {
  OBICO_ENV="${HOME}/moonraker-obico-env"
  if [ ! -f "${OBICO_ENV}/bin/activate" ] ; then
    report_status "Creating python virtual environment for moonraker-obico..."
    mkdir -p "${OBICO_ENV}"
    virtualenv -p /usr/bin/python3 --system-site-packages "${OBICO_ENV}"
  fi
}

report_status() {
  echo -e "${magenta}###### $*\n${default}"
}

debug() {
  if [ $DEBUG = "y" ]; then
    echo -e "DEBUG: ${magenta}###### $*${default}"
  fi
}

banner() {
  echo -en "${cyan}"
  cat "${OBICO_DIR}/scripts/banner"
  echo -en "${default}"
}

brand() {
  echo -en "${cyan}"
  cat "${OBICO_DIR}/scripts/brand"
  echo -en "${default}"
}

welcome() {
  brand
  echo ""
  print_header "> Obico for Klipper <"
  echo -n "${cyan}"
  array=("" "* AI-Powered Failure Detection" "* Free Remote Monitoring and Access " "* 25FPS High-Def Webcam Streaming " "* Free 4.9-Star Mobile App" "")
  print_centered_lines "${array[@]}"

  print_header "="
  echo ""
}

oops() {
  cat <<EOF

   ____
  / __ \\
 | |  | | ___   ___   ___   ___  _ __  ___
 | |  | |/ _ \\ / _ \\ / _ \\ / _ \\| '_ \\/ __|
 | |__| | (_) | (_) | (_) | (_) | |_) \\__ \\  _   _   _
  \\____/ \\___/ \\___/ \\___/ \\___/| .__/|___/ (_) (_) (_)
                                | |
                                |_|


EOF
}

need_help() {
  cat <<EOF
Need help? Stop by:

- The Obico's help docs: https://obico.io/help/
- The Obico for Klipper support channel: https://obico.io/discord-obico-klipper
- The Obico discord community: https://obico.io/discord/

EOF
}

print_centered_lines() {
  local contents=("$@")

  for line in "${contents[@]}"; do
    local line_length=${#line}
    local padding_length=$(( (65 - line_length) / 2 ))

    local padding=""
    for (( i = 0; i < padding_length; i++ )); do
      padding+=" "
    done

    local centered_line="###${padding}${line} ${padding}###"
    echo -e "$centered_line"
  done
}

print_header() {
  local text="$1"
  local line_length=72
  local text_length=${#text}

  # Calculate the number of "=" characters to add on each side
  local padding_length=$(( (line_length - text_length) / 2 ))

  # Create the padding string filled with "=" characters
  local padding="$(printf '%*s' "$padding_length" | tr ' ' '=')"

  # Print the final line with the centered text
  echo "${cyan}${padding}${text}${padding}${default}"
}
