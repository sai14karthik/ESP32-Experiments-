#!/usr/bin/env python3
"""Save CSI_DATA lines from the C5 into wifi sensing/data/."""

import glob
import os
import sys
import time

import serial

BAUD = 115200
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")


def find_port():
    ports = sorted(glob.glob("/dev/cu.usbmodem*"))
    if not ports:
        sys.exit("No /dev/cu.usbmodem* — plug in the C5.")
    return ports[0]


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else find_port()
    os.makedirs(DATA_DIR, exist_ok=True)
    out_path = os.path.join(DATA_DIR, time.strftime("csi_%Y%m%d_%H%M%S.csv"))

    ser = serial.Serial()
    ser.port = port
    ser.baudrate = BAUD
    ser.timeout = 1
    ser.dtr = False
    ser.rts = False
    try:
        ser.open()
    except serial.SerialException as exc:
        sys.exit(
            f"{exc}\n"
            "Port is busy. Close Arduino Serial Monitor and Serial Plotter, then run this again."
        )
    time.sleep(2.0)
    ser.reset_input_buffer()

    print(f"port {port}")
    print(f"writing {out_path}")
    print("Ctrl+C to stop")

    header_written = False
    with open(out_path, "w", encoding="utf-8") as out:
        try:
            while True:
                line = ser.readline().decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                print(line)
                if line.startswith("#"):
                    continue
                if line.startswith("type,"):
                    if header_written:
                        continue
                    header_written = True
                elif not line.startswith("CSI_DATA,"):
                    continue
                elif not header_written:
                    out.write(
                        "type,seq,mac,rssi,rate,noise_floor,fft_gain,agc_gain,"
                        "channel,local_timestamp,sig_len,rx_format,len,first_word,data\n"
                    )
                    header_written = True
                out.write(line + "\n")
                out.flush()
        except KeyboardInterrupt:
            print(f"\nsaved {out_path}")


if __name__ == "__main__":
    main()
