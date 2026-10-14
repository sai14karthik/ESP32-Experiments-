#!/usr/bin/env python3
"""Check Sense ESP cam + CSI C5 boards (collect / powered / connected)."""

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


def port_listening(port: int) -> bool:
    """True if something is LISTENing on port — does not connect (avoids fake CSI clients)."""
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


def collect_process_running() -> bool:
    """True if a collect / csi-collect process looks active."""
    try:
        r = subprocess.run(
            ["pgrep", "-fl", "csi-collect|collect_session"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


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
    known_hosts = [str(h) for h in (csi.get("hosts") or [])]

    print("=== Lab ESP check (Sense cam + CSI C5) ===\n")

    # ----- Sense -----
    host = sense.get("host", "10.128.93.15")
    mjpeg = sense.get("mjpeg", f"http://{host}:81/stream")
    audio = sense.get("audio", f"http://{host}/audio")
    rtsp = sense.get("rtsp", "rtsp://127.0.0.1:8554/cam_sense")

    print("[1] Sense ESP cam (CV GT)")
    ok_ping = ping_ok(host)
    print(f"  {mark(ok_ping)} ping {host}")

    ok81 = tcp_open(host, 81)
    print(f"  {mark(ok81)} tcp :81")

    ok_mj, d_mj = http_ok(mjpeg)
    if ok_mj:
        print(f"  OK  mjpeg {mjpeg}  ({d_mj})")
    else:
        print(f"  WARN mjpeg {mjpeg}  ({d_mj})")
        print("       (normal if MediaMTX already holds :81/stream — use RTSP for GT)")

    ok_au, d_au = http_ok(audio, timeout=3.0)
    if ok_au:
        print(f"  OK  audio {audio}  ({d_au})")
    else:
        print(f"  WARN audio {audio}  ({d_au})")
        print("       (optional for presence GT)")

    ok_mtx = tcp_open("127.0.0.1", 8554, timeout=0.4)
    print(f"  {mark(ok_mtx)} MediaMTX :8554  ({rtsp})")
    if ok_mtx:
        print("       GT path: --camera rtsp://127.0.0.1:8554/cam_sense")

    sense_ok = ok_ping and ok81 and (ok_mj or ok_mtx)
    sense_status = "POWERED" if (ok_ping and ok81) else "OFF / unreachable"
    gt_path = "MediaMTX RTSP" if (ok_mtx and not ok_mj) else ("MJPEG" if ok_mj else "none")

    print()

    # ----- CSI C5 -----
    print("[2] CSI ESP32-C5")
    patterns = usb_cfg.get("patterns") or [
        "/dev/cu.usbmodem*",
        "/dev/cu.usbserial*",
        "/dev/cu.wchusbserial*",
    ]
    usb_ports = list_usb(patterns)
    listening = port_listening(tcp_port)
    proc = collect_process_running()
    # Drop localhost probes from peer list (legacy check_esp used to connect)
    peers = [p for p in (tcp_peers(tcp_port) if listening else []) if p not in ("127.0.0.1", "::1")]

    # Collect status
    if listening or proc:
        collect_status = "COLLECTING"
        how = []
        if listening:
            how.append(f":{tcp_port} open")
        if proc:
            how.append("process running")
        print(f"  OK  collect: {collect_status}  ({', '.join(how)})")
    else:
        collect_status = "NOT COLLECTING"
        print(f"  FAIL collect: {collect_status}")
        print(
            "       start: ./scripts/collect_session.sh "
            "--camera rtsp://127.0.0.1:8554/cam_sense --seconds 120"
        )

    # USB boards
    usb_live: list[str] = []
    usb_dead: list[str] = []
    usb_plugged_only = False
    if usb_ports:
        print(f"  OK  USB plugged: {len(usb_ports)}  ({', '.join(usb_ports)})")
        if args.no_sniff:
            usb_plugged_only = True
            for p in usb_ports:
                print(f"  —   {p}  POWERED (USB; CSI sniff skipped)")
        else:
            for p in usb_ports:
                n, note = sniff_csi(p, baud=baud, seconds=args.sniff_sec)
                if n > 0:
                    print(f"  OK  {p}  POWERED + CSI_DATA≈{n}")
                    usb_live.append(p)
                elif n < 0:
                    print(f"  FAIL {p}  {note}")
                    usb_dead.append(p)
                else:
                    print(f"  FAIL {p}  plugged but no CSI_DATA ({args.sniff_sec:.1f}s)")
                    usb_dead.append(p)
    else:
        print("  —   USB: none (expected for wall power)")

    # TCP clients = wall boards powered + forwarding
    if listening:
        if peers:
            print(f"  OK  wall TCP clients: {len(peers)}")
            for ip in peers:
                print(f"  OK  C5 {ip}  POWERED + CONNECTED (sending CSI)")
        else:
            print("  WARN wall TCP clients: 0")
            print("       collect is up — boards not connected yet")
    else:
        print("  —   wall TCP: unknown until collect opens :9055")

    # Optional known STA IPs
    ping_up: list[str] = []
    ping_down: list[str] = []
    for h in known_hosts:
        if h in peers:
            continue  # already shown as CONNECTED
        ok = ping_ok(h)
        if ok:
            ping_up.append(h)
            if listening:
                print(f"  WARN C5 {h}  POWERED (ping) but NOT CONNECTED to :{tcp_port}")
            else:
                print(f"  OK  C5 {h}  POWERED (ping) — start collect to confirm CSI")
        else:
            ping_down.append(h)
            print(f"  FAIL C5 {h}  OFF / not on LabPSK")

    connected = len(peers) + len(usb_live)
    powered_usb = len(usb_ports) if usb_plugged_only else 0
    powered_extra = len(ping_up)
    csi_ok = connected > 0
    # USB with live CSI counts as ready even without TCP collect
    csi_ready = csi_ok or (powered_usb > 0)

    print()
    print("--- status ---")
    print(f"Sense cam:  {'UP' if sense_ok else 'DOWN'}  "
          f"[{sense_status}; GT={gt_path}]")
    print(f"Collect:    {collect_status}")
    if connected:
        board_line = f"POWERED + SENDING  ({connected} source(s)"
        if peers:
            board_line += f"; TCP {', '.join(peers)}"
        if usb_live:
            board_line += f"; USB {len(usb_live)}"
        board_line += ")"
    elif powered_usb:
        board_line = f"POWERED (USB ×{powered_usb}; sniff skipped)"
    elif powered_extra:
        board_line = f"POWERED but NOT SENDING  (ping {', '.join(ping_up)})"
    elif known_hosts and ping_down and not listening:
        board_line = "OFF / unreachable  (known hosts down)"
    elif not listening and not usb_ports:
        board_line = "UNKNOWN  (start collect to see wall boards)"
    else:
        board_line = "OFF or not connected  (collect up, 0 TCP clients)"
    print(f"CSI C5:     {board_line}")

    if sense_ok and csi_ok and collect_status == "COLLECTING":
        print("RESULT: Sense UP + collect COLLECTING + C5 sending")
        return 0
    if sense_ok and csi_ok and collect_status != "COLLECTING":
        print("RESULT: Sense UP + C5 sending (USB) — optional: start collect for wall TCP")
        return 0
    if sense_ok and collect_status == "COLLECTING" and not csi_ok:
        print("RESULT: Sense UP + collect COLLECTING — power / wait for wall C5s")
        return 1
    if sense_ok and collect_status != "COLLECTING":
        print("RESULT: Sense UP — start collect, then power wall C5s")
        return 1
    print("RESULT: fix DOWN side")
    return 1 if not (sense_ok and csi_ready) else 0


if __name__ == "__main__":
    raise SystemExit(main())
