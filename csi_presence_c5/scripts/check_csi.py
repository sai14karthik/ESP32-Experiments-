#!/usr/bin/env python3
"""Check CSI ESP32-C5 boards: USB serial + CSI_DATA, or TCP clients on :9055."""

from __future__ import annotations

import argparse
import glob
import os
import socket
import subprocess
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEVICES = ROOT / "configs" / "devices.toml"
PORTS = ROOT / "configs" / "ports.toml"


def load_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def mark(ok: bool) -> str:
    return "OK  " if ok else "FAIL"


def listening(port: int) -> bool:
    """LISTENing check only — do not connect (would look like a CSI client)."""
    try:
        r = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def tcp_peers(port: int) -> list[str]:
    peers: list[str] = []
    try:
        r = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:ESTABLISHED"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        for line in r.stdout.splitlines()[1:]:
            if "->" not in line:
                continue
            right = line.split("->", 1)[1].split()[0]
            ip = right.rsplit(":", 1)[0]
            if ip and ip not in peers:
                peers.append(ip)
    except Exception:
        pass
    return peers


def ping_ok(host: str) -> bool:
    for args in (
        ["ping", "-c", "1", "-W", "2000", host],
        ["ping", "-c", "1", "-W", "2", host],
    ):
        try:
            r = subprocess.run(args, capture_output=True, timeout=3)
            if r.returncode == 0:
                return True
        except Exception:
            continue
    return False


def sniff_csi(port: str, baud: int = 115200, seconds: float = 2.5) -> tuple[int, str]:
    """Count CSI_DATA lines on USB serial. Returns (count, note)."""
    try:
        import serial
    except ImportError:
        return -1, "pyserial missing (uv sync)"
    try:
        ser = serial.Serial(port, baud, timeout=0.2)
    except Exception as e:
        return -1, str(e)[:60]
    t0 = time.time()
    n = 0
    buf = ""
    sample = ""
    try:
        while time.time() - t0 < seconds:
            chunk = ser.read(4096)
            if not chunk:
                continue
            buf += chunk.decode("utf-8", errors="ignore")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                if line.startswith("CSI_DATA"):
                    n += 1
                    if not sample:
                        sample = line[:72]
    finally:
        ser.close()
    return n, sample or "(no CSI_DATA in window)"


def list_usb(patterns: list[str]) -> list[str]:
    found: list[str] = []
    for pat in patterns:
        found.extend(glob.glob(os.path.expanduser(pat)))
    return sorted(set(found))


def main() -> int:
    ap = argparse.ArgumentParser(description="Check CSI C5 boards (USB / TCP)")
    ap.add_argument("--sniff-sec", type=float, default=2.5, help="USB CSI sniff seconds")
    ap.add_argument("--no-sniff", action="store_true", help="Skip serial CSI_DATA sniff")
    args = ap.parse_args()

    dev = load_toml(DEVICES)
    ports_cfg = load_toml(PORTS)
    csi = dev.get("csi", {})
    usb_cfg = dev.get("usb", {})
    tcp_port = int(csi.get("tcp_port", 9055))
    baud = int(ports_cfg.get("serial", {}).get("baud", 115200))
    patterns = usb_cfg.get("patterns") or [
        "/dev/cu.usbmodem*",
        "/dev/cu.usbserial*",
        "/dev/cu.wchusbserial*",
    ]

    print("=== CSI C5 device check ===\n")
    fails = 0
    live = 0

    # USB
    usb_ports = list_usb(patterns)
    print("[USB]")
    if not usb_ports:
        print("  —   no USB serial (OK if boards are on wall power only)")
    else:
        print(f"  OK  {len(usb_ports)} port(s): {', '.join(usb_ports)}")
        if not args.no_sniff:
            for p in usb_ports:
                n, note = sniff_csi(p, baud=baud, seconds=args.sniff_sec)
                if n < 0:
                    print(f"  FAIL {p}  open/sniff: {note}")
                    fails += 1
                elif n == 0:
                    print(f"  FAIL {p}  no CSI_DATA in {args.sniff_sec:.1f}s")
                    fails += 1
                else:
                    rate = n / max(args.sniff_sec, 0.1)
                    print(f"  OK  {p}  CSI_DATA≈{n} (~{rate:.0f}/s)  {note}…")
                    live += 1

    print()

    # TCP fan-in (Mini wall-power path)
    print(f"[TCP :{tcp_port}]")
    if listening(tcp_port):
        print(f"  OK  something listening on 127.0.0.1:{tcp_port}")
        peers = tcp_peers(tcp_port)
        if peers:
            print(f"  OK  {len(peers)} board(s) connected: {', '.join(peers)}")
            live += len(peers)
        else:
            print("  FAIL listen up but 0 ESTABLISHED clients")
            print("       (wall boards off, wrong Wi‑Fi, or CSI_TCP_HOST ≠ this Mini)")
            fails += 1
    else:
        print(f"  —   nothing listening on :{tcp_port}")
        print("       start collect to accept boards: ./scripts/collect_session.sh …")

    # Optional known STA IPs
    hosts = csi.get("hosts") or []
    if hosts:
        print("\n[CSI STA ping]")
        for h in hosts:
            ok = ping_ok(str(h))
            print(f"  {mark(ok)} {h}")
            if ok:
                live += 1
            else:
                fails += 1

    print()
    if live > 0 and fails == 0:
        print(f"RESULT: {live} CSI source(s) active")
        return 0
    if live > 0:
        print(f"RESULT: {live} active, but {fails} issue(s)")
        return 1
    print("RESULT: no active CSI boards seen")
    print("  USB: plug boards or sniff failed → reflash / check baud")
    print("  TCP: wall power + LabPSK + CSI_TCP_HOST=<Mini IP>, then collect listening")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
