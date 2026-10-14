#!/usr/bin/env python3
"""Check Sense ESP cam + CSI C5 boards (USB / TCP)."""

from __future__ import annotations

import argparse
import glob
import os
import socket
import subprocess
import time
import tomllib
import urllib.error
import urllib.request
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


def ping_ok(host: str) -> bool:
    for args in (
        ["ping", "-c", "1", "-W", "2000", host],
        ["ping", "-c", "1", "-W", "2", host],
    ):
        try:
            if subprocess.run(args, capture_output=True, timeout=3).returncode == 0:
                return True
        except Exception:
            continue
    return False


def tcp_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def http_ok(url: str, timeout: float = 4.0, read_bytes: int = 128) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "csi-presence-check-esp/1.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            _ = resp.read(read_bytes)
            return True, f"HTTP {code}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, str(e).split("\n")[0][:80]


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


def sniff_csi(port: str, baud: int = 115200, seconds: float = 2.0) -> tuple[int, str]:
    try:
        import serial
    except ImportError:
        return -1, "pyserial missing"
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
    return n, sample or "(no CSI_DATA)"


def list_usb(patterns: list[str]) -> list[str]:
    found: list[str] = []
    for pat in patterns:
        found.extend(glob.glob(os.path.expanduser(pat)))
    return sorted(set(found))


def main() -> int:
    ap = argparse.ArgumentParser(description="Check Sense cam + CSI C5 boards")
    ap.add_argument("--sniff-sec", type=float, default=2.0)
    ap.add_argument("--no-sniff", action="store_true")
    args = ap.parse_args()

    cfg = load_toml(DEVICES)
    ports_cfg = load_toml(PORTS)
    sense = cfg.get("sense", {})
    csi = cfg.get("csi", {})
    usb_cfg = cfg.get("usb", {})
    baud = int(ports_cfg.get("serial", {}).get("baud", 115200))
    tcp_port = int(csi.get("tcp_port", 9055))

    print("=== Lab ESP check (Sense cam + CSI C5) ===\n")
    fails = 0

    # ----- Sense -----
    host = sense.get("host", "10.128.93.15")
    mjpeg = sense.get("mjpeg", f"http://{host}:81/stream")
    audio = sense.get("audio", f"http://{host}/audio")
    rtsp = sense.get("rtsp", "rtsp://127.0.0.1:8554/cam_sense")

    print("[1] Sense ESP cam (CV GT)")
    ok_ping = ping_ok(host)
    print(f"  {mark(ok_ping)} ping {host}")
    if not ok_ping:
        fails += 1

    ok81 = tcp_open(host, 81)
    print(f"  {mark(ok81)} tcp :81")
    if not ok81:
        fails += 1

    ok_mj, d_mj = http_ok(mjpeg)
    print(f"  {mark(ok_mj)} mjpeg {mjpeg}  ({d_mj})")
    if not ok_mj:
        fails += 1
        print("       power-cycle Sense / check LabPSK; or use MediaMTX RTSP for GT")

    ok_au, d_au = http_ok(audio, timeout=3.0)
    print(f"  {mark(ok_au)} audio {audio}  ({d_au})")
    if not ok_au:
        print("       (optional for presence GT; required for Sense A/V MediaMTX)")

    ok_mtx = tcp_open("127.0.0.1", 8554, timeout=0.4)
    print(f"  {mark(ok_mtx)} MediaMTX :8554  ({rtsp})")
    if ok_mtx and not ok_mj:
        print("       tip: collect --camera rtsp://127.0.0.1:8554/cam_sense")

    print()

    # ----- CSI C5 -----
    print("[2] CSI ESP32-C5")
    patterns = usb_cfg.get("patterns") or [
        "/dev/cu.usbmodem*",
        "/dev/cu.usbserial*",
        "/dev/cu.wchusbserial*",
    ]
    usb_ports = list_usb(patterns)
    csi_live = 0

    if usb_ports:
        print(f"  OK  USB {len(usb_ports)} port(s): {', '.join(usb_ports)}")
        if not args.no_sniff:
            for p in usb_ports:
                n, note = sniff_csi(p, baud=baud, seconds=args.sniff_sec)
                if n < 0:
                    print(f"  FAIL {p}  {note}")
                    fails += 1
                elif n == 0:
                    print(f"  FAIL {p}  no CSI_DATA in {args.sniff_sec:.1f}s")
                    fails += 1
                else:
                    print(f"  OK  {p}  CSI_DATA≈{n}  {note}…")
                    csi_live += 1
    else:
        print("  —   no USB (normal if wall-powered)")

    if tcp_open("127.0.0.1", tcp_port, timeout=0.4):
        print(f"  OK  Mini listen :{tcp_port}")
        peers = tcp_peers(tcp_port)
        if peers:
            print(f"  OK  TCP clients ({len(peers)}): {', '.join(peers)}")
            csi_live += len(peers)
        else:
            print("  FAIL :9055 listening but 0 clients")
            fails += 1
    else:
        print(f"  —   nothing on :{tcp_port} (start collect to accept wall boards)")
        if not usb_ports:
            fails += 1
            print("  FAIL no USB and no TCP — no CSI path visible")

    for h in csi.get("hosts") or []:
        ok = ping_ok(str(h))
        print(f"  {mark(ok)} ping C5 {h}")
        if not ok:
            fails += 1
        else:
            csi_live += 1

    if csi_live == 0 and (usb_ports or tcp_open("127.0.0.1", tcp_port, timeout=0.2)):
        # USB present but no CSI_DATA counted as fails already
        pass

    print()
    sense_ok = ok_ping and ok81 and ok_mj
    csi_ok = csi_live > 0
    print(f"Sense cam: {'UP' if sense_ok else 'DOWN'}")
    print(f"CSI C5:    {'UP' if csi_ok else 'DOWN'}  (sources≈{csi_live})")
    if sense_ok and csi_ok:
        print("RESULT: both Sense + CSI look active")
        return 0
    print("RESULT: fix DOWN side before collect")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
