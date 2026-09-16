# ESP32 toolkit

| | |
|---|---|
| **Fresh install** (ZIP / clone) | [docs/install.md](docs/install.md) — camera + CSI |
| **CSI methods** 4.1 / 4.2 / 4.3 | [docs/CSI_METHODS.md](docs/CSI_METHODS.md) |
| **CSI pipeline** (Mini ingest / detect) | [csi_pipeline_new/README.md](csi_pipeline_new/README.md) |
| **MediaMTX** (XIAO RTSP → Mini → VLC / ffplay) | [mediamtx/README.md](mediamtx/README.md) |
| **S3 Sense multimodal** (cam+mic+CSI flash) | [firmware/CameraWebServerWiFiSense/README.md](firmware/CameraWebServerWiFiSense/README.md) |
| **Papers / reading** | [materials/](materials/) |

## Common commands

```bash
./scripts/monitor_csi.sh          # 4.1 IDF monitor
./scripts/plot_csi.sh             # Espressif CSI plotter
./scripts/set_csi_tcp_host.sh …   # flash C5 TCP → Mini
cd csi_pipeline_new && ./run_ingest.sh --listen-tcp 9055 …
cd csi_pipeline_new && ./run_csi_viz.sh …
```
