# ESP32 toolkit

| | |
|---|---|
| **Fresh install** (ZIP / clone) | [docs/install.md](docs/install.md) — camera + CSI |
| **CSI methods** 4.1 / 4.2 / 4.3 | [docs/CSI_METHODS.md](docs/CSI_METHODS.md) |
| **Presence detection** (N× C5 CSI) | [presence_detection/README.md](presence_detection/README.md) |
| **CSI capture / Mini ingest** | [csi_pipeline_new/README.md](csi_pipeline_new/README.md) |
| **MediaMTX** (XIAO MJPEG → Mini ffmpeg → RTSP) | [mediamtx/README.md](mediamtx/README.md) |
| **S3 Sense multimodal** (cam+mic+CSI flash) | [firmware/CameraWebServerWiFiSense/README.md](firmware/CameraWebServerWiFiSense/README.md) |
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
```
