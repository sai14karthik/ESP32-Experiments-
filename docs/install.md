# Install — ESP32 camera + Wi‑Fi CSI toolkit

Use this on a **fresh machine** after a GitHub **clone** or **ZIP** download.  
Do **not** copy `.venv/` or ESP-IDF between PCs — recreate them locally.

| Part | Hardware | Toolchain |
|------|----------|-----------|
| **Camera** | Seeed XIAO ESP32S3 Sense | Arduino IDE |
| **CSI** | ESP32-C5 (e.g. C5-KITC-A) | ESP-IDF **6.0.x** |

CSI methods (4.1 / 4.2 / 4.3): **[CSI_METHODS.md](CSI_METHODS.md)**

---

## 1. Get the code

**Clone (preferred):**

```bash
git clone https://github.com/sai14karthik/ESP32-S3-.git camera_module
cd camera_module
```

**Or ZIP:** Download from GitHub → unzip → `cd` into the folder (often named `ESP32-S3--main`).

Check that these folders are **not empty**:

```bash
ls esp-csi/examples/get-started/
ls firmware/CameraWebServerWiFi/
```

If `esp-csi/` is empty, the clone/ZIP is missing tree contents (old submodule issue). Use a commit where `esp-csi/` is a normal folder, or ask the maintainer to push the full tree.

---

## 2. Python tools (required for CSI plotter / helpers)

macOS / Linux:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# then open a new terminal (or source your shell profile)
./scripts/setup_python.sh
```

Windows (PowerShell):

```powershell
irm https://astral.sh/uv/install.ps1 | iex
# Git Bash or WSL for ./scripts/… ; or from repo root:
uv sync
```

Smoke test:

```bash
uv run python -c "import serial, numpy; print('python ok')"
```

Use `./plot_csi.sh` or `uv run python …` — not a system-wide random Python.

---

## 3. Camera (XIAO ESP32S3 Sense)

### 3.1 Install Arduino IDE

1. Install [Arduino IDE 2.x](https://www.arduino.cc/en/software).
2. **Boards Manager** → add Espressif ESP32 boards (URL if needed):  
   `https://espressif.github.io/arduino-esp32/package_esp32_index.json`
3. Install **esp32** by Espressif Systems.
4. Board: **XIAO_ESP32S3** (or “XIAO ESP32S3”).
5. Enable **PSRAM** / use a partition scheme with enough APP space (see comments in `board_config.h`).
6. USB CDC / upload port: pick the XIAO serial port (`cu.usbmodem…` on macOS).

### 3.2 Sketch + Wi‑Fi

Main sketch:

`firmware/CameraWebServerWiFi/CameraWebServerWiFi.ino`

1. Open that `.ino` in Arduino IDE.
2. Confirm `board_config.h` has `#define CAMERA_MODEL_XIAO_ESP32S3`.
3. Set your network (do not commit real passwords if you push):

```cpp
const char *ssid = "YourSSID";
const char *password = "YourPassword";
```

4. **Upload**. Open Serial Monitor at **115200**.
5. Note the printed IP, then open `http://<ip>` in a browser for the camera stream.

SoftAP-only sketch (no home Wi‑Fi): `firmware/CameraWebServer/` → typically `http://192.168.4.1`.

Other sketches under `firmware/` (serial preview, remote upload, mic) — same board settings.

### 3.3 Camera smoke test

```bash
ls /dev/cu.usb* /dev/ttyACM* /dev/ttyUSB* 2>/dev/null   # macOS / Linux
# Serial Monitor: Wi‑Fi connected + IP
# Browser: live MJPEG / web UI
```

---

## 4. CSI (ESP32-C5)

### 4.1 Install ESP-IDF 6.0.x

Install via [Espressif IDE](https://dl.espressif.com/dl/esp-idf/) / `eim`, or the official ESP-IDF install docs. Target version: **6.0.x** (wrappers look for `activate_idf_v6.0.2.sh`).

Wrappers auto-find:

```text
$HOME/.espressif/tools/activate_idf_v6.0.2.sh
```

Override if needed:

```bash
export IDF_ACTIVATE="/path/to/activate_idf_v6.0.2.sh"
```

**Do not** `source` the repo `.venv` in the same shell you use for `idf.py` (toolchain PATH conflicts). Use a clean terminal + IDF activate, or only the repo scripts (they activate IDF themselves).

### 4.2 Serial ports + baud

```bash
ls /dev/cu.usb*          # macOS — usbmodem (USB-JTAG) or usbserial (CH340)
ls /dev/ttyUSB* /dev/ttyACM*   # Linux
```

CSI console / plotter baud in this repo: **115200**.

### 4.3 Pick a method and flash

Full commands and diagrams: **[CSI_METHODS.md](CSI_METHODS.md)**.

| Method | Boards | Needs Wi‑Fi AP? | From repo root |
|--------|--------|-----------------|----------------|
| **4.1** Router CSI | 1× C5 | Yes | `./scripts/set_csi_wifi.sh 'SSID' 'PASSWORD' [port]` |
| **4.2** Between devices | 2× C5 | Yes | `./scripts/flash_csi_between.sh 'SSID' 'PASSWORD' [peer] [sense]` |
| **4.3** ESP‑NOW pair | 2× C5 | No | `./scripts/flash_csi_pair.sh [send] [recv]` |

Examples:

```bash
# 4.1 — one board + hotspot/router
./scripts/set_csi_wifi.sh 'SaiPhone' '123456789' /dev/cu.usbmodem101
./monitor_csi.sh /dev/cu.usbmodem101          # quit with Ctrl+]
./plot_csi.sh /dev/cu.usbmodem101

# 4.2 — two boards on same AP (plot sense port)
./scripts/flash_csi_between.sh 'SaiPhone' '123456789' \
  /dev/cu.usbmodem101 /dev/cu.usbmodem2101
./plot_csi.sh /dev/cu.usbmodem2101

# 4.3 — two boards, no AP (plot recv port)
./scripts/flash_csi_pair.sh /dev/cu.usbmodem101 /dev/cu.usbmodem2101
./plot_csi.sh /dev/cu.usbmodem2101
```

Wi‑Fi secrets for 4.1/4.2 are written to gitignored `sdkconfig.defaults.local` (never commit passwords).

Manual IDF path (4.1 only), if you prefer:

```bash
cd esp-csi/examples/get-started/csi_recv_router
cp sdkconfig.defaults.local.example sdkconfig.defaults.local
# edit SSID + password
# source your IDF activate script first
idf.py set-target esp32c5
idf.py -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.local" build flash -p /dev/cu.usbmodem101
```

### 4.4 CSI smoke test

```bash
uv run python -c "import serial, numpy; print('python ok')"
ls /dev/cu.usb* /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
# After flash + AP on (for 4.1/4.2): expect CSI_DATA on monitor/plotter
```

---

## 5. What a ZIP/clone never includes (recreate locally)

| Missing | Recreate |
|---------|----------|
| `.venv/` | `./scripts/setup_python.sh` or `uv sync` |
| ESP-IDF / Arduino | Install on that PC |
| `sdkconfig.defaults.local` | Copy from `.example` or use `set_csi_wifi.sh` |
| `**/build/` · `build-peer/` · `build-sense/` | Built by `idf.py` / flash scripts |
| Real Wi‑Fi passwords in git | Set locally |

`uv.lock` **is** in the repo so Python package versions match.

---

## 6. Windows notes

- Prefer **Git Bash** or **WSL** for `./scripts/*.sh`, `./monitor_csi.sh`, `./plot_csi.sh`.
- Or: `uv sync` then  
  `uv run python esp-csi/examples/get-started/tools/csi_data_read_parse.py -p COMx`
- PyQt plotter needs a display (not headless).
- Arduino camera upload: select the correct COM port in Arduino IDE.

---

## 7. Quick checklist

- [ ] `git clone` or unzip; `esp-csi/` and `firmware/` present  
- [ ] `uv` + `./scripts/setup_python.sh` → `python ok`  
- [ ] **Camera:** Arduino + XIAO_ESP32S3 + Wi‑Fi in sketch + upload → browser stream  
- [ ] **CSI:** ESP-IDF 6.0.x + C5 plugged in → flash via helper → `CSI_DATA` / plot  
- [ ] Method details: [CSI_METHODS.md](CSI_METHODS.md)  
