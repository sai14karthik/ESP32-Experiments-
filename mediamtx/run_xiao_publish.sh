#!/bin/bash
export PUBLISH_ONCE=1
export XIAO_FPS="${XIAO_FPS:-10}"
export XIAO_BITRATE="${XIAO_BITRATE:-1500k}"
exec "/Users/saikarthik/Desktop/camera_module/scripts/publish_xiao.sh" "rtsp://192.168.4.1:554/mjpeg/1"
