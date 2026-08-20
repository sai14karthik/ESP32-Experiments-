#!/usr/bin/env python3
import glob
import struct
import sys
import time

import cv2
import numpy as np
import serial

BAUD = 921600
MAGIC = b"CAM0"


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
        chunk = ser.read(256)
        if not chunk:
            continue
        window += chunk
        while True:
            idx = window.find(MAGIC)
            if idx < 0:
                window = window[-3:]
                break
            window = window[idx + 4 :]
            while len(window) < 4:
                extra = ser.read(4 - len(window))
                if not extra:
                    return None
                window += extra
            (length,) = struct.unpack("<I", window[:4])
            window = window[4:]
            if not (1000 < length < 200_000):
                continue
            payload = bytes(window[:length])
            window = window[length:]
            missing = length - len(payload)
            if missing:
                rest = read_exact(ser, missing)
                if not rest:
                    return None
                payload += rest
            if payload.startswith(b"\xff\xd8"):
                return payload
    return None


def find_port():
    ports = sorted(glob.glob("/dev/cu.usbmodem*"))
    if not ports:
        print("No /dev/cu.usbmodem* — plug in the XIAO USB cable.")
        return None
    return ports[0]


def main():
    port = find_port()
    if not port:
        return 1
    print(f"Opening {port}")
    print("Close Arduino Serial Monitor first. CameraSerial.ino must be uploaded.")
    try:
        ser = serial.Serial()
        ser.port = port
        ser.baudrate = BAUD
        ser.timeout = 1
        ser.dtr = False
        ser.rts = False
        ser.open()
    except serial.SerialException as e:
        print(f"Could not open {port}: {e}")
        print("Close Serial Monitor / Plotter and the other preview script.")
        return 1
    time.sleep(2.0)
    ser.reset_input_buffer()

    print("Waiting for camera frames. Press q in the preview window to quit.")
    shown = False
    while True:
        jpeg = sync_frame(ser)
        if not jpeg:
            print("No frame yet, retrying (is CameraSerial.ino uploaded, and Serial Monitor closed?)")
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
            continue
        img = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            continue
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
