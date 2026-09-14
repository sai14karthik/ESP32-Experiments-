# Lab streaming — what was wrong & what’s correct

## Research (MediaMTX official)

| Issue | Cause | Fix |
|-------|--------|-----|
| ESP → MediaMTX | MJPEG HTTP is **not** a native source | FFmpeg `runOnInit` ([#3575](https://github.com/bluenviron/mediamtx/discussions/3575)) |
| Browsers | HLS/WebRTC need **H.264**, not MJPEG | Re-encode baseline, no B-frames |
| Continuous | FFmpeg exits | `runOnInitRestart: yes` ([hooks](https://mediamtx.org/docs/usage/hooks)) |
| **“peer connection closed”** | WebRTC media uses **UDP :8189**; LabPSK isolation/firewall blocks it | Enable **ICE over TCP** `webrtcLocalTCPAddress: :8189` ([WebRTC docs](https://mediamtx.org/docs/features/webrtc-specific-features)) |
| 20s lag then stuck | curl\|ffmpeg **pipe queue** | Latest-JPEG file (no backlog) |
| Stutter on Mini | software x264 load | `h264_videotoolbox` on macOS |

## Correct architecture

```
XIAO CameraWebServerWiFi  (/capture)
  → ffmpeg on Mini (H.264 baseline / VideoToolbox)
  → MediaMTX cam_xiao
  → HLS :8888  |  WebRTC :8889 (+ ICE TCP :8189)  |  RTSP :8554
```

## Files to copy onto Mini

From this laptop `camera_module/` → Mini `ESP32-Experiments-/`:

1. `scripts/publish_xiao.sh`
2. `scripts/mediamtx_run.sh`
3. `mediamtx/mediamtx.yml`

## Run on Mini

```bash
pkill -f mediamtx; pkill -f publish_xiao; pkill -f 'ffmpeg.*cam_xiao'
./scripts/mediamtx_run.sh
```

Healthy: `1 track (H264)` and `HLS … converting`.

## Watch (order for LabPSK)

1. **HLS (most reliable):** http://10.128.93.23:8888/cam_xiao/
2. **WebRTC (after TCP ICE fix):** http://10.128.93.23:8889/cam_xiao/
3. **VLC:** `rtsp://10.128.93.23:8554/cam_xiao`

If WebRTC still says peer connection closed, stay on HLS — signaling works over HTTP but media UDP is blocked; TCP ICE needs the updated `mediamtx.yml` on the Mini.
