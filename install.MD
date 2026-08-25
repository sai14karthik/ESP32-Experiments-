# ESP32 camera + Wi-Fi CSI toolkit

Prepared so a **fresh GitHub ZIP or clone** works on another laptop (office PC). Do not copy `.venv` between machines — recreate it with `uv`.

> **Important:** `csi/` and `esp-csi/` used to be empty Git *submodule pointers* (a ZIP from GitHub would not include their files). They are now normal folders in this working tree. **Commit and push them** before you download the repo at the office, or CSI will be missing.

## Office laptop — first-time setup

### A. Get the code

Prefer clone (keeps history):

```bash
git clone https://github.com/sai14karthik/ESP32-S3-.git camera_module
cd camera_module
```

Or unzip the GitHub ZIP and `cd` into the folder (name may be `ESP32-S3--main`).

### B. Python tools (plotter, USB preview, flasher helpers)

```bash
# Install uv once: https://docs.astral.sh/uv/
curl -LsSf https://astral.sh/uv/install.sh | sh          # macOS / Linux
# Windows PowerShell:  irm https://astral.sh/uv/install.ps1 | iex

./scripts/setup_python.sh    # creates .venv from pyproject.toml + uv.lock
```

Always run Python via:

```bash
uv run python …
./plot_csi.sh
```

### C. ESP-IDF (CSI on ESP32-C5)

Install **ESP-IDF 6.0.x** on that PC (Espressif IDE / `eim`). Wrappers auto-find:

`$HOME/.espressif/tools/activate_idf_v6.0.2.sh`

Override if needed:

```bash
export IDF_ACTIVATE="/path/to/activate_idf_v6.0.2.sh"
```

### D. Office Wi-Fi (required)

Home/lab SSIDs will not work at the office. Set credentials locally:

```bash
cd esp-csi/examples/get-started/csi_recv_router
cp sdkconfig.defaults.local.example sdkconfig.defaults.local
# edit SSID + password in sdkconfig.defaults.local
rm -f sdkconfig
idf.py set-target esp32c5
idf.py -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.local" build flash
```

Camera sketch: edit `ssid` / `password` in  
`firmware/CameraWebServerWiFi/CameraWebServerWiFi.ino`, then flash with Arduino.

Serial baud for CSI in this repo: **115200**.

### E. Monitor / plot CSI

```bash
# from repo root — only one of these at a time (same USB port)
./monitor_csi.sh
./plot_csi.sh

# if auto-detect fails, pass the port:
./monitor_csi.sh /dev/cu.usbserial-10   # macOS CH340 (common for C5)
./monitor_csi.sh /dev/ttyUSB0           # Linux
```

Quit monitor with `Ctrl+]` before starting the plotter.

## What a ZIP never includes (by design)

| Missing after unzip | Recreate on that PC |
|---------------------|---------------------|
| `.venv/` | `uv sync` / `./scripts/setup_python.sh` |
| ESP-IDF / Arduino toolchains | Install locally |
| `sdkconfig.defaults.local` | Copy from `.example`, set office Wi-Fi |
| `**/build/` | `idf.py build` / Arduino compile |

`uv.lock` **is** in the repo so Python package versions match.

## Smoke test

```bash
uv run python -c "import serial, numpy; print('python ok')"
ls /dev/cu.usb* /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
./monitor_csi.sh    # expect CSI_DATA lines after Wi-Fi joins
```

## Windows notes

- Use **Git Bash** or **WSL** for `./monitor_csi.sh` / `./plot_csi.sh`.
- Or: `uv sync` then `uv run python esp-csi/examples/get-started/tools/csi_data_read_parse.py -p COMx`.
- PyQt plotter needs a display (not a headless session).
