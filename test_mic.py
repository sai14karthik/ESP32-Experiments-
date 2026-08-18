#!/usr/bin/env python3
import re
import sys
import time
from collections import deque

import cv2
import numpy as np
import serial

PORT = "/dev/cu.usbmodem101"
BAUD = 115200

RMS_RE = re.compile(r"rms:?\s*=?\s*(-?\d+(?:\.\d+)?)", re.I)
PEAK_RE = re.compile(r"peak:?\s*=?\s*(-?\d+(?:\.\d+)?)", re.I)


def parse_line(line):
    rms_m = RMS_RE.search(line)
    peak_m = PEAK_RE.search(line)
    if not rms_m:
        return None
    rms = float(rms_m.group(1))
    peak = float(peak_m.group(1)) if peak_m else 0.0
    return rms, peak


def draw_meter(rms, peak, history):
    w, h = 720, 360
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:] = (28, 28, 28)

    cv2.putText(img, "Microphone", (24, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (240, 240, 240), 2)
    cv2.putText(
        img,
        f"rms={rms:.1f} dBFS   peak={peak:.3f}",
        (24, 84),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        (80, 220, 120),
        2,
    )

    # Map dBFS [-90, 0] to bar width
    db_min, db_max = -90.0, 0.0
    frac = (rms - db_min) / (db_max - db_min)
    frac = max(0.0, min(1.0, frac))
    bar_x, bar_y, bar_w, bar_h = 24, 110, w - 48, 36
    cv2.rectangle(img, (bar_x, bar_y), (bar_x + bar_w, bar_y + bar_h), (60, 60, 60), 1)
    fill = int(bar_w * frac)
    color = (40, 200, 40) if rms < -20 else (0, 200, 255) if rms < -6 else (0, 0, 220)
    if fill > 0:
        cv2.rectangle(img, (bar_x, bar_y), (bar_x + fill, bar_y + bar_h), color, -1)

    # History plot
    plot_x, plot_y, plot_w, plot_h = 24, 170, w - 48, h - 194
    cv2.rectangle(img, (plot_x, plot_y), (plot_x + plot_w, plot_y + plot_h), (60, 60, 60), 1)
    if len(history) >= 2:
        pts = []
        for i, v in enumerate(history):
            x = plot_x + int(i * (plot_w - 1) / max(1, len(history) - 1))
            yf = (v - db_min) / (db_max - db_min)
            yf = max(0.0, min(1.0, yf))
            y = plot_y + plot_h - 1 - int(yf * (plot_h - 1))
            pts.append((x, y))
        cv2.polylines(img, [np.array(pts, dtype=np.int32)], False, (80, 180, 255), 2)

    cv2.putText(img, "Press q to quit", (24, h - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (160, 160, 160), 1)
    return img


def main():
    print(f"Opening {PORT}")
    print("Close Arduino Serial Monitor / Plotter first.")
    print("MicPlotter.ino must already be uploaded.")
    try:
        ser = serial.Serial()
        ser.port = PORT
        ser.baudrate = BAUD
        ser.timeout = 1
        ser.dtr = False
        ser.rts = False
        ser.open()
    except serial.SerialException as e:
        print(f"Could not open {PORT}: {e}")
        print("Close Serial Monitor / Plotter and the other preview script.")
        return 1
    time.sleep(2.0)
    ser.reset_input_buffer()

    print("Waiting for mic levels. Press q in the preview window to quit.")
    history = deque(maxlen=240)
    shown = False
    buf = ""

    while True:
        chunk = ser.read(256)
        if chunk:
            buf += chunk.decode("utf-8", errors="replace")
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            parsed = parse_line(line.strip())
            if not parsed:
                continue
            rms, peak = parsed
            history.append(rms)
            img = draw_meter(rms, peak, history)
            cv2.imshow("Microphone", img)
            shown = True
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    ser.close()
    if shown:
        cv2.destroyAllWindows()
    return 0 if shown else 1


if __name__ == "__main__":
    sys.exit(main())
