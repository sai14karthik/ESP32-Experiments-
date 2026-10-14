#!/usr/bin/env python3
"""Probe Sense cam + CSI TCP / USB — are lab ESPs up?"""

from __future__ import annotations

import argparse
import glob
import os
import socket
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "configs" / "devices.toml"


def load_cfg() -> dict:
    with CFG.open("rb") as f:
        return tomllib.load(f)


def ping_ok(host: str, timeout_s: float = 2.0) -> bool:
    try:
        r = subprocess.run(
            ["ping", "-c", "1", "-W", "2000", host],
            capture_output=True,
            timeout=timeout_s + 1,
        )
        # macOS -W is ms; Linux -W is seconds — try both styles if needed
        if r.returncode == 0:
            return True
        r = subprocess.run(
            ["ping", "-c", "1", "-W", "2", host],
            capture_output=True,
            timeout=timeout_s + 2,
        )
        return r.returncode == 0
    except Exception:
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


def listening_local(port: int) -> bool:
    return tcp_open("127.0.0.1", port, timeout=0.5)


def tcp_clients(port: int) -> list[str]:
    """Peer IPs with ESTABLISHED connections to local :port (macOS/Linux)."""
    peers: list[str] = []
    try:
        r = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:ESTABLISHED"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        for line in r.stdout.splitlines()[1:]:
            # ... TCP 10.128.93.13:9055->10.128.93.29:61289 (ESTABLISHED)
            if "->" not in line:
                continue
            right = line.split("->", 1)[1].split()[0]
            ip = right.rsplit(":", 1)[0]
            if ip and ip not in peers:
                peers.append(ip)
    except Exception:
        pass
    return peers


def mark(ok: bool) -> str:
    return "OK " if ok else "FAIL"


def main() -> int:
    p = argparse.ArgumentParser(description="Check if lab ESP devices are reachable")
    p.add_argument("--config", type=Path, default=CFG)
    args = p.parse_args()
    cfg = tomllib.loads(args.config.read_text()) if args.config.exists() else load_cfg()

    print("=== ESP device check ===")
    print(f"config: {args.config}")
    print()

    fails = 0

    # --- Sense camera ---
    sense = cfg.get("sense", {})
    host = sense.get("host", "10.128.93.15")
    mjpeg = sense.get("mjpeg", f"http://{host}:81/stream")
    audio = sense.get("audio", f"http://{host}/audio")
    rtsp = sense.get("rtsp", "rtsp://127.0.0.1:8554/cam_sense")

    print("[Sense cam]")
    ok_ping = ping_ok(host)
    print(f"  {mark(ok_ping)} ping {host}")
    if not ok_ping:
        fails += 1

    ok81 = tcp_open(host, 81)
    print(f"  {mark(ok81)} tcp {host}:81")
    if not ok81:
        fails += 1

    ok_mj, detail_mj = http_ok(mjpeg)
    print(f"  {mark(ok_mj)} mjpeg {mjpeg}  ({detail_mj})")
    if not ok_mj:
        fails += 1

    ok_au, detail_au = http_ok(audio, timeout=3.0)
    print(f"  {mark(ok_au)} audio {audio}  ({detail_au})")
    # audio optional for CSI GT — warn only
    if not ok_au:
        print("       (audio optional for presence GT; needed for Sense A/V MediaMTX)")

    mtx_port = 8554
    ok_mtx = listening_local(mtx_port)
    print(f"  {mark(ok_mtx)} MediaMTX local :{mtx_port}  ({rtsp})")
    if ok_mtx and not ok_mj:
        print("       tip: use --camera rtsp://127.0.0.1:8554/cam_sense for collect GT")

    print()

    # --- CSI TCP / USB ---
    csi = cfg.get("csi", {})
    tcp_port = int(csi.get("tcp_port", 9055))
    print("[CSI C5]")
    ok_listen = listening_local(tcp_port)
    print(f"  {mark(ok_listen)} Mini listen :{tcp_port} (collect / ingest)")
    peers = tcp_clients(tcp_port) if ok_listen else []
    if ok_listen:
        if peers:
            print(f"  OK  active TCP clients ({len(peers)}): {', '.join(peers)}")
        else:
            print("  FAIL no ESTABLISHED clients on :9055 (boards off / wrong CSI_TCP_HOST?)")
            fails += 1
    else:
        print("       (start ./scripts/collect_session.sh to listen, or leave boards USB)")

    for h in csi.get("hosts", []) or []:
        ok = ping_ok(str(h))
        print(f"  {mark(ok)} ping C5 {h}")
        if not ok:
            fails += 1

    usb = cfg.get("usb", {})
    patterns = usb.get("patterns", ["/dev/cu.usbmodem*"])
    found: list[str] = []
    for pat in patterns:
        found.extend(sorted(glob.glob(os.path.expanduser(pat))))
    found = sorted(set(found))
    if found:
        print(f"  OK  USB serial ({len(found)}): {', '.join(found)}")
    else:
        print("  —   no USB serial (normal if wall-powered only)")

    print()
    if fails:
        print(f"RESULT: {fails} problem(s) — power-cycle Sense / check LabPSK / flash CSI_TCP_HOST")
        return 1
    print("RESULT: looks good")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
