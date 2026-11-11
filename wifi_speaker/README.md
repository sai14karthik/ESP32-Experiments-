# Wi-Fi speaker (AirPlay)

Standalone package for the **Waveshare ESP32-S3-AUDIO-Board** as a Wi-Fi speaker.

This chip has Bluetooth LE only. It **cannot** be a normal Bluetooth music speaker.  
This package turns it into an **AirPlay** speaker (iPhone / Mac). Phone and board must be on the **same Wi-Fi**.

Katie (voice AI) lives in `../speakers/`. Flashing this package replaces Katie until you flash Katie again.

## What is in this folder

```text
wifi_speaker/
  README.md                 this file
  flash.sh                  flash the AirPlay image (clears Wi-Fi)
  reset_wifi.sh             clear Wi-Fi only; bring setup hotspot back
  firmware/
    bootloader.bin
    partition-table.bin
    ota_data_initial.bin
    firmware.bin            AirPlay app
    spiffs.bin              web setup UI
    flash_args
```

## Requirements

- Waveshare **ESP32-S3-AUDIO-Board** (16 MB flash)
- USB cable to this Mac
- ESP-IDF tools with `esptool` (default path used by the scripts):

```bash
ls "$HOME/.espressif/tools/python/v6.0.2/venv/bin/esptool"
```

- Or set your own tool:

```bash
export ESPTOOL=/path/to/esptool
```

Find the USB port:

```bash
ls /dev/cu.usbmodem*
```

## 1. Flash

From the repo root (`camera_module`):

```bash
./wifi_speaker/flash.sh
```

Other USB port:

```bash
./wifi_speaker/flash.sh /dev/cu.usbmodem1101
```

The script:

- refuses non–ESP32-S3 / non–16 MB boards
- erases saved Wi-Fi
- writes bootloader, partitions, app, and web UI
- leaves the board in setup mode

## 2. Join the setup hotspot

On a phone or Mac Wi-Fi list (must be **2.4 GHz**):

| Item | Value |
|---|---|
| Hotspot name | `ESP32-AirPlay-Setup` |
| Password | none (open) |
| Config page | http://192.168.4.1 |

Notes:

- A phone that is **sharing** a hotspot (for example SaiPhone) cannot also join `ESP32-AirPlay-Setup`. Use another phone, or this Mac’s Wi-Fi.
- After the board joins a real network, the setup hotspot **disappears**. That is normal.

## 3. Enter Wi-Fi

1. Open **http://192.168.4.1**
2. Enter a 2.4 GHz network your phone will also use  
   (example in lab: `LabHealthSecurePSK`, or phone hotspot `SaiPhone`)
3. Save / connect
4. Put the phone back on that **same** network

## 4. Play with AirPlay

1. Play music / video on iPhone or Mac
2. Control Center → AirPlay
3. Choose **ESP32 AirPlay**
4. Sound comes from the board speaker

Phone and board must stay on the same Wi-Fi while streaming.

## Reset Wi-Fi (setup hotspot again)

If you cannot find `ESP32-AirPlay-Setup` because the board already joined a network:

```bash
./wifi_speaker/reset_wifi.sh
# or
./wifi_speaker/reset_wifi.sh /dev/cu.usbmodem1101
```

Then repeat steps 2–4.

## Restore Katie (voice speaker)

```bash
./speakers/flash.sh katie
```

That puts the English “Hello Katie” XiaoZhi image back.

## This board (lab unit)

| Item | Value |
|---|---|
| Board | Waveshare ESP32-S3-AUDIO-Board |
| Chip | ESP32-S3, 16 MB flash, 8 MB PSRAM |
| STA MAC | `90:70:69:eb:1b:24` |
| AirPlay name | `ESP32 AirPlay` |
| Setup hotspot | `ESP32-AirPlay-Setup` |
| Power | USB (or battery). Unplug USB / switch battery off to power down |

## Flash map (reference)

| Offset | File |
|---|---|
| `0x0` | `bootloader.bin` |
| `0x8000` | `partition-table.bin` |
| `0x19000` | `ota_data_initial.bin` |
| `0x20000` | `firmware.bin` |
| `0x620000` | `spiffs.bin` |
| `0x9000` (64 KB) | NVS / saved Wi-Fi — erased by `flash.sh` and `reset_wifi.sh` |

## Rebuild from source (optional)

Only needed if you change pins or firmware. Known-good image is already in `firmware/`.

Source used for this image (gitignored under speakers):

```bash
# already cloned during bring-up:
# speakers/.src/airplay-esp32
# board: Waveshare ESP32-S3-AUDIO-Board (ES8311 + TCA9555 amp)

cd speakers/.src/airplay-esp32
~/.platformio/penv/bin/pio run -e waveshare-esp32-s3-audio
~/.platformio/penv/bin/pio run -e waveshare-esp32-s3-audio -t buildfs

# then copy into this package:
OUT=../../../wifi_speaker/firmware
BD=.pio/build/waveshare-esp32-s3-audio
cp "$BD/bootloader.bin" "$OUT/bootloader.bin"
cp "$BD/partitions.bin" "$OUT/partition-table.bin"
cp "$BD/ota_data_initial.bin" "$OUT/ota_data_initial.bin"
cp "$BD/firmware.bin" "$OUT/firmware.bin"
cp "$BD/spiffs.bin" "$OUT/spiffs.bin"
```

Upstream project: [rbouteiller/airplay-esp32](https://github.com/rbouteiller/airplay-esp32) (non-commercial license on that project).
