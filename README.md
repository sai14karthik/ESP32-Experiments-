# ESP32 Experiments

GitHub tracks **ESP-type project trees only**. Lab host tooling (CSI ingest, presence ML, MediaMTX, Whisper, RPM, docs, scripts) stays local and is ignored via `.gitignore`.

## Tracked ESP folders

| Folder | What it is |
|---|---|
| [`firmware/`](firmware/) | Lab camera / BLE firmwares (CameraWebServer*, CameraRTSPWiFi, apple_ble_scan) |
| [`wifi_speaker/`](wifi_speaker/) | Waveshare ESP32-S3-AUDIO-Board → AirPlay Wi‑Fi speaker |
| [`speakers/`](speakers/) | Same board → Katie (XiaoZhi) voice image; flash helpers |
| [`esp-csi/`](esp-csi/) | Espressif CSI examples (incl. room presence RX/TX) |
| [`csi/`](csi/) | CSI firmware + shared collector bits |
| [`contactless_respiration/`](contactless_respiration/) | ESP32 CSI vitals / respiration |
| [`esp32-camera/`](esp32-camera/) | Espressif camera driver (submodule) |
| [`esp32cam-ready/`](esp32cam-ready/), [`esp32cam-rtsp/`](esp32cam-rtsp/) | ESP32-CAM RTSP refs (submodules) |
| [`ESP32-CSI-Tool/`](ESP32-CSI-Tool/) | Hernandez classic ESP32 CSI kit (submodule) |
| [`ESP32-RTSPServer/`](ESP32-RTSPServer/), [`ESP32_RTSP/`](ESP32_RTSP/), [`Micro-RTSP/`](Micro-RTSP/) | RTSP library refs |
| [`SeeedStudio-XIAO-ESP32S3-Sense-camera/`](SeeedStudio-XIAO-ESP32S3-Sense-camera/) | XIAO Sense camera examples |
| [`luma-esp32-ai-assistant/`](luma-esp32-ai-assistant/) | ESP32-S3 + local Whisper/Ollama ref |
| [`esphome-wifi-csi/`](esphome-wifi-csi/), [`espectre/`](espectre/), [`espectre-sense/`](espectre-sense/) | ESPHome / ESPectre CSI refs |

## Quick start (speakers)

```bash
# AirPlay Wi‑Fi speaker (clears saved Wi‑Fi; setup hotspot comes back)
./wifi_speaker/flash.sh

# Katie voice image (keeps saved Wi‑Fi)
./speakers/flash.sh katie
```

Details: [`wifi_speaker/README.md`](wifi_speaker/README.md) · [`speakers/README.md`](speakers/README.md)

## Camera firmware

Arduino / IDF projects under [`firmware/`](firmware/), e.g.:

- `CameraWebServerWiFi` — MJPEG HTTP (`:81/stream`)
- `CameraWebServerWiFiSense` — cam + PDM mic + CSI USB
- `CameraRTSPWiFi` — Micro-RTSP experiments
- `apple_ble_scan` — ESP-IDF BLE scan

## Policy

- **Commit / push:** only the ESP folders above (+ this README / `.gitignore` / `.gitmodules`)
- **Do not commit:** secrets (`*.env`, keys, `sdkconfig.defaults.local`), IDF `build/` trees, or ignored host pipelines
- **Submodules:** ESP-related only (see `.gitmodules`)
