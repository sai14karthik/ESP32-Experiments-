#!/usr/bin/env python3
"""Live respiration rate from an ESP32-C5 on USB.

Reads CSI_DATA lines from csi_recv_router (method 4.1: CSI on the AP's
replies) and estimates 8–36 breaths/min from amplitude. The first number
needs about 35 seconds of a person sitting still. A peak that is not
shared across subcarriers is not shown.
"""
import argparse
import re
import sys
import time
from collections import deque
from pathlib import Path

import numpy as np
import serial

TOOLS = Path(__file__).resolve().parent / "esp32-csi-vitals" / "tools"
sys.path.insert(0, str(TOOLS))
import analyze  # noqa: E402

DATA = re.compile(r"CSI_DATA,.*?\"\[(.*?)\]\"")


def amplitudes(payload):
    vals = np.fromstring(payload, sep=",")
    if vals.size < 8 or vals.size % 2:
        return None
    iq = vals.reshape(-1, 2)
    return np.hypot(iq[:, 0], iq[:, 1]).astype(np.float32)


def estimate(samples):
    """samples: list of (t, amp). Dominant subcarrier count only."""
    widths = [a.size for _, a in samples]
    dom = int(np.bincount(widths).argmax())
    kept = [(t, a) for t, a in samples if a.size == dom]
    if len(kept) < 40:
        return None, dom, 0.0
    ts = np.array([t for t, _ in kept], dtype=np.float64)
    ts -= ts[0]
    span = float(ts[-1] - ts[0])
    if span < 8:
        return None, dom, 0.0
    rows = np.stack([a for _, a in kept])
    fs = len(ts) / span
    result = analyze.score(ts, rows, fs)
    return result, dom, fs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/cu.usbserial-210")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--window", type=float, default=45.0,
                    help="seconds of CSI used for each estimate")
    ap.add_argument("--min-seconds", type=float, default=35.0,
                    help="wait this long before the first estimate")
    args = ap.parse_args()

    ser = serial.Serial()
    ser.port = args.port
    ser.baudrate = args.baud
    ser.timeout = 0.4
    ser.dtr = False
    ser.rts = False
    ser.open()
    ser.reset_input_buffer()

    print(f"listening on {args.port} at {args.baud}", flush=True)
    print("Sit still near the board and breathe normally. "
          f"First estimate after ~{args.min_seconds:.0f}s of CSI.", flush=True)

    buf = b""
    samples = deque()
    n = 0
    t_first = None
    last_report = 0.0
    last_bpm = None

    while True:
        chunk = ser.read(16384)
        now = time.time()
        if chunk:
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("utf-8", "replace").strip()
                m = DATA.search(line)
                if not m:
                    continue
                amp = amplitudes(m.group(1))
                if amp is None:
                    continue
                if t_first is None:
                    t_first = now
                    print("CSI started", flush=True)
                samples.append((now, amp))
                n += 1
                cutoff = now - args.window
                while samples and samples[0][0] < cutoff:
                    samples.popleft()

        if now - last_report < 5 or t_first is None:
            continue
        last_report = now
        span = now - samples[0][0] if samples else 0.0
        hz = n / max(now - t_first, 0.1)
        if span < args.min_seconds:
            print(f"collecting  {hz:4.1f} Hz   {span:4.0f}s / {args.min_seconds:.0f}s",
                  flush=True)
            continue
        result, nsub, fs = estimate(list(samples))
        if not result or not result.get("bpm") or result["consensus"] < 0.45:
            strong = result["strong"] if result else 0
            cons = result["consensus"] if result else 0.0
            last_bpm = None
            print(f"no stable rate   {hz:4.1f} pkt/s   {nsub} subcarriers   "
                  f"consensus {cons:.2f}   agreeing {strong}",
                  flush=True)
            continue
        bpm = result["bpm"]
        # Chest motion is not a sine; the 2nd harmonic is often the tall peak.
        if last_bpm and bpm > last_bpm * 1.6 and abs(bpm / 2 - last_bpm) < 4:
            bpm = bpm / 2
        last_bpm = bpm
        print(f"{bpm:4.1f} breaths/min   consensus {result['consensus']:.2f}   "
              f"{result['strong']} subcarriers   {fs:.1f} Hz",
              flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
        sys.exit(0)
