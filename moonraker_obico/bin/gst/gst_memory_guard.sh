#!/bin/bash

while true; do
  sleep 5
  gstPid=$(pgrep gst-launch-1.0)
  if [ -z "$gstPid" ]; then
    continue
  fi
  resMem=$(ps -o rss= $gstPid)
  if [ -z "$resMem" ]; then
    continue
  fi

  if [ $resMem -gt $1 ]; then
      kill $gstPid
  fi
done
