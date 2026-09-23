# ESP32 toolkit

| | |
|---|---|
| **Fresh install** (ZIP / clone) | [docs/install.md](docs/install.md) — camera + CSI |
| **CSI methods** 4.1 / 4.2 / 4.3 | [docs/CSI_METHODS.md](docs/CSI_METHODS.md) |
| **Presence detection** (N× C5 CSI) | [presence_detection/README.md](presence_detection/README.md) |
| **CSI capture / Mini ingest** | [csi_pipeline_new/README.md](csi_pipeline_new/README.md) |
| **MediaMTX** (XIAO / Sense → Mini → RTSP) | [mediamtx/README.md](mediamtx/README.md) |
| **Record RTSP** (MP4 video+audio) | `./scripts/record_rtsp.py` — see [mediamtx/README.md](mediamtx/README.md#record-video--audio) |
| **S3 Sense** (cam + mic + CSI) | [firmware/CameraWebServerWiFiSense/README.md](firmware/CameraWebServerWiFiSense/README.md) |
| **Live Whisper captions** (Sense mic → Mini) | [firmware/CameraWebServerWiFiSense/README.md](firmware/CameraWebServerWiFiSense/README.md#live-voice-recognition-whisper-on-mini) |
| **Papers / reading** | [materials/](materials/) |

## Common commands

```bash
# Presence (preferred front door on Mini)
cd presence_detection
./run_presence.sh clients
./run_presence.sh capture empty_01
./run_presence.sh train && ./run_presence.sh calibrate
# stop ingest, then:
./run_presence.sh live

# Low-level capture / CSI
cd csi_pipeline_new && ./run_multi_ingest.sh --label empty_01
cd csi_pipeline_new && ./count_csi_clients.sh

./scripts/monitor_csi.sh          # 4.1 IDF monitor (USB debug)
./scripts/set_csi_tcp_host.sh …   # flash C5 TCP → Mini

# Sense A/V + live captions (Mini; keep CSI separate)
uv sync --group whisper
SENSE_AV_URL=http://10.128.93.25 ./scripts/mediamtx_run.sh   # VLC: …/cam_sense
./scripts/sense_whisper_live.sh                              # UDP :19055, model turbo
./scripts/record_rtsp.py rtsp://127.0.0.1:8554/cam_sense      # save MP4 (A/V); Ctrl-C to stop
# details: firmware/CameraWebServerWiFiSense/README.md · mediamtx/README.md
```
