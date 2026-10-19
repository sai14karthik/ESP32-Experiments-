# Speakers

Waveshare ESP32-S3-AUDIO-Board. English XiaoZhi image, wake phrase baked into that speaker's firmware folder.

Katie is the image already running on the bench. Flash it again with:

```bash
./speakers/flash.sh katie
```

Another USB port:

```bash
./speakers/flash.sh katie /dev/cu.usbmodem1101
```

```text
speakers/firmware/katie/bootloader.bin
speakers/firmware/katie/partition-table.bin
speakers/firmware/katie/ota_data_initial.bin
speakers/firmware/katie/xiaozhi.bin          app, echo canceller off while listening for the wake phrase
speakers/firmware/katie/assets.bin           "Hello Katie", sensitivity 0.05
speakers/firmware/katie/flash_args
speakers/katie.env
speakers/flash.sh
```

Saved Wi-Fi on the board is not erased. The flash stops if the USB device is not an ESP32-S3 with 16 MB flash, so a different lab board is left alone.

## Add another speaker

1. Copy the example and edit the phrase.

```bash
cp speakers/speaker.env.example speakers/alex.env
```

2. Plug that board in over USB. If the port is not `/dev/cu.usbmodem101`, put the right port in the env file or pass it on the command line.

```bash
./speakers/flash_speaker.sh speakers/alex.env
```

That build writes `speakers/firmware/alex/`. Later flashes of the same image are `./speakers/flash.sh alex`.

3. On a new board, join its setup hotspot (`Xiaozhi-` plus the last four of the AP MAC), open `http://192.168.4.1`, and enter a 2.4 GHz network that can reach the internet. The phone hotspot `SaiPhone` works. LabHealthSecurePSK blocks the voice server port `8883`.

4. On [xiaozhi.me](https://xiaozhi.me), open the console and **+ Add Device** with the 6-digit code the board speaks. Point the device at an English agent (Skye en-US, reply in English).

A later flash of the same board does not erase saved Wi-Fi.

## This board

| Item | Value |
|---|---|
| Board | Waveshare ESP32-S3-AUDIO-Board |
| Chip | ESP32-S3, 16 MB flash, 8 MB PSRAM |
| MAC | `90:70:69:eb:1b:24` |
| Wake phrase | Hello Katie |
| Voice | Skye (en-US), agent Katie |
| Power | USB. Unplug to turn it off. BOOT only starts or stops a conversation. |

Say the wake phrase about a hand’s width from the board, then the question.
