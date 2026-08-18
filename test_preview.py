#!/usr/bin/env python3
import struct
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import serial

PORT = "/dev/cu.usbmodem101"
BAUD = 921600
MAGIC = b"CAM0"
# OUT = Path(__file__).resolve().parent / "preview.jpg"


def read_exact(ser, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = ser.read(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def sync_frame(ser):
    window = bytearray()
    deadline = time.time() + 8
    while time.time() < deadline:
        b = ser.read(1)
        if not b:
            continue
        window += b
        if len(window) > 4:
            window = window[-4:]
        if bytes(window) == MAGIC:
            header = read_exact(ser, 4)
            if not header:
                return None
            (length,) = struct.unpack("<I", header)
            if 1000 < length < 200_000:
                return read_exact(ser, length)
    return None


def main():
    print(f"Opening {PORT}...")
    ser = serial.Serial(PORT, BAUD, timeout=1)
    time.sleep(2.5)
    leftover = ser.read(2048)
    if leftover:
        text = leftover.decode("utf-8", errors="replace")
        if "Camera init failed" in text:
            print(text)
            print("Camera failed to start. Re-upload CameraSerial.ino with PSRAM = OPI PSRAM.")
            ser.close()
            return 1
        if b"CAM0" not in leftover:
            print("Board said:")
            print(text[:400])

    print("Waiting for camera frames. Press q in the preview window to quit.")
    shown = False
    # saved = False
    while True:
        jpeg = sync_frame(ser)
        if not jpeg:
            print("No frame yet, retrying... (is CameraSerial.ino uploaded, and Serial Monitor closed?)")
            continue
        img = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            continue
        # if not saved:
        #     OUT.write_bytes(jpeg)
        #     saved = True
        #     print(f"Saved first frame to {OUT}")
        cv2.imshow("Camera module", img)
        shown = True
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    ser.close()
    if shown:
        cv2.destroyAllWindows()
    return 0 if shown else 1


if __name__ == "__main__":
    sys.exit(main())
