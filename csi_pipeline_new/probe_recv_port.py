#!/usr/bin/env python3
"""Find which /dev/cu.usb* port is emitting CSI_DATA (the recv board)."""

from __future__ import annotations

import argparse
import glob
import sys
import time

import serial


def list_ports(*, recv_only: bool = False) -> list[str]:
    """Recv boards are usually usbmodem*; usbserial* is often the sender (do not open)."""
    modems = sorted(glob.glob("/dev/cu.usbmodem*"))
    if recv_only:
        return modems
    return modems + sorted(glob.glob("/dev/cu.usbserial*"))


def count_csi(port: str, seconds: float, baud: int) -> tuple[int, str | None]:
    try:
        ser = serial.Serial(port=port, baudrate=baud, timeout=0.3)
        ser.dtr = False
        ser.rts = False
        time.sleep(0.2)
        ser.reset_input_buffer()
    except serial.SerialException as exc:
        return -1, str(exc)

    n = 0
    sample = None
    deadline = time.time() + seconds
    try:
        while time.time() < deadline:
            raw = ser.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if line.startswith("CSI_DATA,"):
                n += 1
                if sample is None:
                    sample = line[:100]
    finally:
        ser.close()
    return n, sample


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--seconds", type=float, default=2.0)
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument(
        "--all-ports",
        action="store_true",
        help="Also probe usbserial* (may reset sender board; use for diagnose only)",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Print only the best recv port path (or nothing)",
    )
    args = p.parse_args()

    ports = list_ports(recv_only=not args.all_ports)
    if not ports:
        if not args.quiet:
            print("No /dev/cu.usbmodem* or usbserial* found.", file=sys.stderr)
        sys.exit(1)

    best_port = None
    best_n = 0
    for port in ports:
        n, sample = count_csi(port, args.seconds, args.baud)
        if args.quiet:
            if n > best_n:
                best_n = n
                best_port = port
            continue
        if n < 0:
            print(f"{port}: ERROR {sample}")
        else:
            print(f"{port}: CSI_DATA={n}" + (f"  e.g. {sample}…" if sample else ""))
        if n > best_n:
            best_n = n
            best_port = port

    if args.quiet:
        if best_port and best_n > 0:
            print(best_port)
            sys.exit(0)
        sys.exit(1)

    if best_port and best_n > 0:
        print(f"recv -> {best_port}")
    else:
        print("No CSI_DATA seen. Flash 4.3 csi_recv / keep sender powered.", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
