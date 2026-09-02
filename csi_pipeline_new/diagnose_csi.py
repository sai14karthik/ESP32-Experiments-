#!/usr/bin/env python3
"""Diagnose CSI hardware link, serial ports, model, and live pipeline readiness."""

from __future__ import annotations

import argparse
import glob
import sys
import time
from pathlib import Path

import joblib
import serial

from csi_parse import DEFAULT_BAUD, parse_csi_line
from probe_recv_port import count_csi, list_ports


def sniff_port(port: str, seconds: float, baud: int) -> dict[str, object]:
    try:
        ser = serial.Serial(port=port, baudrate=baud, timeout=0.3)
        ser.dtr = False
        ser.rts = False
        time.sleep(0.15)
        ser.reset_input_buffer()
    except serial.SerialException as exc:
        return {"error": str(exc)}

    csi = 0
    other = 0
    bootish = 0
    sample_csi: str | None = None
    sample_other: str | None = None
    deadline = time.time() + seconds
    try:
        while time.time() < deadline:
            raw = ser.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            if line.startswith("CSI_DATA,"):
                csi += 1
                if sample_csi is None:
                    sample_csi = line[:90]
            else:
                other += 1
                low = line.lower()
                if any(k in low for k in ("esp_image", "rst:", "boot:", "cpu start")):
                    bootish += 1
                if sample_other is None:
                    sample_other = line[:90]
    finally:
        ser.close()

    return {
        "csi": csi,
        "other": other,
        "bootish": bootish,
        "sample_csi": sample_csi,
        "sample_other": sample_other,
    }


def check_model(model_path: Path) -> dict[str, object]:
    if not model_path.is_file():
        return {"ok": False, "error": "missing"}
    bundle = joblib.load(model_path)
    return {
        "ok": True,
        "version": bundle.get("feature_version"),
        "model_type": bundle.get("model_type"),
        "threshold": bundle.get("threshold"),
        "window": bundle.get("window_size"),
        "has_baseline_phase": bundle.get("baseline_phase") is not None,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--seconds", type=float, default=4.0, help="Listen time per port")
    p.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    p.add_argument(
        "--model",
        type=Path,
        default=Path(__file__).resolve().parent / "models" / "object_detector.joblib",
    )
    p.add_argument(
        "--all-ports",
        action="store_true",
        help="Also probe usbserial* (may reset sender)",
    )
    args = p.parse_args()

    root = Path(__file__).resolve().parent
    print("=== CSI pipeline diagnose ===\n")

    # Model
    print("Model:")
    m = check_model(args.model)
    if not m.get("ok"):
        print(f"  MISSING {args.model}")
        print("  Fix: ./run_detect.sh --train")
    else:
        print(
            f"  OK  v{m['version']}  {m['model_type']}  "
            f"window={m['window']}  thr={m['threshold']:.2f}"
        )
    print()

    # Ports
    ports = list_ports(recv_only=not args.all_ports)
    if not ports:
        print("Serial: NO PORTS — plug in recv (csi_recv) board via USB.")
        sys.exit(1)

    print(f"Serial ({args.seconds:.0f}s listen per port @ {args.baud}):")
    best_port: str | None = None
    best_csi = 0
    for port in ports:
        info = sniff_port(port, args.seconds, args.baud)
        if "error" in info:
            print(f"  {port}: ERROR {info['error']}")
            continue
        csi = int(info["csi"])
        other = int(info["other"])
        bootish = int(info["bootish"])
        rate = csi / args.seconds if args.seconds > 0 else 0.0

        if csi > 0:
            role = "recv (CSI_DATA) ✓"
        elif bootish > 0 or other > 10:
            role = "likely sender or boot log (not CSI)"
        elif other > 0:
            role = "activity but no CSI_DATA"
        else:
            role = "silent"

        print(f"  {port}: CSI_DATA={csi} ({rate:.1f}/s)  other_lines={other}  → {role}")
        if info.get("sample_csi"):
            print(f"    e.g. {info['sample_csi']}…")
        elif info.get("sample_other"):
            print(f"    e.g. {info['sample_other']}…")

        if csi > best_csi:
            best_csi = csi
            best_port = port

    print()
    if best_port and best_csi > 0:
        print(f"RECV PORT → {best_port}  ({best_csi} packets in {args.seconds:.0f}s)")
        print()
        print("Ready. Run:")
        print(f"  ./run_detect.sh --fast --quiet --port {best_port}")
        print("  ./run_ingest.sh --method 4.3 --channel 11 --label baseline_desk")
        sys.exit(0)

    print("PROBLEM: No CSI_DATA on any port.\n")
    print("Checklist:")
    print("  1. Recv board (csi_recv) → USB to Mac — must print CSI_DATA")
    print("  2. Send board (csi_send) → powered, within ~2 m, channel 11")
    print("  3. Do not run idf.py monitor on the recv port while detecting")
    print("  4. Reflash pair (send port first, recv second):")
    print("     cd .. && ./scripts/flash_csi_pair.sh /dev/cu.usbserial-10 /dev/cu.usbmodem2101")
    print("  5. Re-probe: ./run_detect.sh --probe --seconds 5")
    sys.exit(2)


if __name__ == "__main__":
    main()
