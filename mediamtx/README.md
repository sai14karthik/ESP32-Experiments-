# MediaMTX — best RTSP path (Lab / Orlando)

```
XIAO CameraWebServerWiFi
  http://10.128.93.25:81/stream   (MJPEG)
       │
       ▼  ffmpeg on Mini (libx264 ultrafast / zerolatency)
MediaMTX :8554/cam_xiao           (H.264 RTSP)
       │
       ▼
VLC / colleague
  rtsp://10.128.93.23:8554/cam_xiao   (TCP)
```

ESP32-S3 has no hardware H.264. Raw Micro-RTSP MJPEG is what felt robotic in VLC.
This remux is the same pattern MediaMTX / go2rtc use for MJPEG cameras.

## One command (Mini)

```bash
cd ~/Desktop/ESP32-Experiments-/   # or this repo on Mini
./scripts/mediamtx_run.sh
```

Board must be flashed with **CameraWebServerWiFi** (not CameraRTSPWiFi).

## Watch

- VLC → Open Network → `rtsp://127.0.0.1:8554/cam_xiao`  
  Prefer **TCP**; Tools → Preferences → Input/Codecs → Network caching **50–100 ms**
- Colleague: `rtsp://10.128.93.23:8554/cam_xiao`
- HLS backup: `http://10.128.93.23:8888/cam_xiao/`

## Override ESP URL

```bash
XIAO_MJPEG_URL=http://10.128.93.25:81/stream ./scripts/mediamtx_run.sh
```
