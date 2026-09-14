#!/bin/bash
export PUBLISH_ONCE=1
export PUBLISH_MODE="capture"
exec "/Users/saikarthik/Desktop/camera_module/scripts/publish_xiao.sh" "http://10.128.93.25:81/stream"
