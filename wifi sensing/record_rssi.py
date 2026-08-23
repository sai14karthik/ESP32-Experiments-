#!/usr/bin/env python3
"""Save RSSI CSV lines from the C5 serial port into wifi sensing/data/."""

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
    out_path = os.path.join(DATA_DIR, time.strftime("rssi_%Y%m%d_%H%M%S.csv"))

    ser = serial.Serial()
    ser.port = port
    ser.baudrate = BAUD
    ser.timeout = 1
    ser.dtr = False
    ser.rts = False
    ser.open()
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
                if line.startswith("rssi:"):
                    line = line.split(":", 1)[1].strip()
                try:
                    rssi = int(line)
                except ValueError:
                    continue
                if not header_written:
                    out.write("ts_ms,ssid,rssi\n")
                    header_written = True
                ts_ms = int(time.time() * 1000)
                row = f"{ts_ms},SpectrumSetup-EB9C,{rssi}"
                out.write(row + "\n")
                out.flush()
                print(row)
        except KeyboardInterrupt:
            print(f"\nsaved {out_path}")


if __name__ == "__main__":
    main()
