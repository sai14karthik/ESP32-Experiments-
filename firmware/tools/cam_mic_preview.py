#!/usr/bin/env -S uv run python
"""Preview XIAO S3 Sense multimodal firmware (CameraWebServerWiFiSense).

Expects USB lines:
  - ``CSI_DATA,…`` (Wi‑Fi CSI)
  - ``rms:… peak:…`` (PDM mic)

Video (optional): ``http://<ip>:81/stream`` when reachable (often Mini only on LabPSK).

  uv run python firmware/tools/cam_mic_preview.py --port /dev/cu.usbmodem1101
  uv run python firmware/tools/cam_mic_preview.py --port /dev/cu.usbmodem1101 --no-video

For MediaMTX video-only, flash ``firmware/CameraWebServerWiFi`` instead of Sense.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

import cv2
import numpy as np
import serial

ROOT = Path(__file__).resolve().parents[2]
CSI_PIPE = ROOT / "csi_pipeline_new"
sys.path.insert(0, str(CSI_PIPE))
from csi_parse import iq_to_amplitudes, parse_csi_line  # noqa: E402

RMS_RE = re.compile(r"rms:?\s*=?\s*(-?\d+(?:\.\d+)?)", re.I)
PEAK_RE = re.compile(r"peak:?\s*=?\s*(-?\d+(?:\.\d+)?)", re.I)
DB_MIN, DB_MAX = -80.0, 0.0
UI_EMA = 0.55  # blend toward latest sample each frame (responsive + continuous)
UI_HZ = 50  # plot append rate (independent of OpenCV redraw)


def list_ports() -> list[str]:
    return sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/cu.usbserial*"))


def parse_mic_line(line: str) -> tuple[float, float] | None:
    rms_m = RMS_RE.search(line)
    if not rms_m:
        return None
    peak_m = PEAK_RE.search(line)
    rms = float(rms_m.group(1))
    peak = float(peak_m.group(1)) if peak_m else 0.0
    return rms, peak


def host_reachable(url: str, port: int, timeout: float = 1.0) -> bool:
    import socket

    host = urlparse(url).hostname
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class SharedState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rms = DB_MIN
        self.peak = 0.0
        self.mic_src = "—"
        self.mic_ok = False
        self.mic_n = 0
        self.mean_amp = 0.0
        self.amps: np.ndarray | None = None
        self.csi_rssi: int | None = None
        self.csi_fmt = "—"
        self.csi_n = 0
        self.csi_ok = False

    def set_mic(self, rms: float, peak: float, source: str) -> None:
        with self.lock:
            self.rms = rms
            self.peak = peak
            self.mic_src = source
            self.mic_ok = True
            self.mic_n += 1

    def set_csi(self, mean_amp: float, amps: np.ndarray, rssi: int | None, fmt: str) -> None:
        with self.lock:
            self.mean_amp = mean_amp
            self.amps = amps
            self.csi_rssi = rssi
            self.csi_fmt = fmt
            self.csi_n += 1
            self.csi_ok = True

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "rms": self.rms,
                "peak": self.peak,
                "mic_src": self.mic_src,
                "mic_ok": self.mic_ok,
                "mic_n": self.mic_n,
                "mean_amp": self.mean_amp,
                "amps": None if self.amps is None else self.amps.copy(),
                "csi_rssi": self.csi_rssi,
                "csi_fmt": self.csi_fmt,
                "csi_n": self.csi_n,
                "csi_ok": self.csi_ok,
            }


def serial_loop(port: str, baud: int, state: SharedState, stop: threading.Event) -> None:
    try:
        ser = serial.Serial(port, baud, timeout=0.05)
        ser.dtr = False
        ser.rts = False
        time.sleep(0.3)
        ser.reset_input_buffer()
    except serial.SerialException as exc:
        print(f"serial open failed: {exc}", file=sys.stderr)
        return
    buf = ""
    try:
        while not stop.is_set():
            n = ser.in_waiting
            chunk = ser.read(n if n else 4096)
            if not chunk:
                continue
            buf += chunk.decode("utf-8", errors="replace")
            # Cap buffer if USB floods (keep recent tail).
            if len(buf) > 512_000:
                buf = buf[-256_000:]
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                if "CSI_DATA" in line:
                    # Mic flood can leave junk before the token; parse from CSI_DATA.
                    idx = line.find("CSI_DATA")
                    sample = parse_csi_line(line[idx:])
                    if sample and sample.get("iq"):
                        amps = np.asarray(iq_to_amplitudes(sample["iq"]), dtype=np.float64)
                        if amps.size:
                            state.set_csi(
                                float(np.mean(amps)),
                                amps,
                                sample.get("rssi"),
                                str(sample.get("format") or "?"),
                            )
                    continue
                parsed = parse_mic_line(line)
                if parsed:
                    state.set_mic(parsed[0], parsed[1], f"usb:{port}")
    finally:
        ser.close()


def http_mic_loop(base_url: str, state: SharedState, stop: threading.Event) -> None:
    url = base_url.rstrip("/") + "/mic"
    while not stop.is_set():
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok"):
                state.set_mic(float(data["rms_db"]), float(data.get("peak", 0.0)), "http")
        except Exception:  # noqa: BLE001
            pass
        stop.wait(0.1)


def open_mjpeg(url: str) -> cv2.VideoCapture | None:
    cap = cv2.VideoCapture(url)
    return cap if cap.isOpened() else None


def fetch_capture_jpeg(base_url: str) -> np.ndarray | None:
    url = base_url.rstrip("/") + "/capture"
    try:
        with urllib.request.urlopen(url, timeout=3.0) as resp:
            buf = np.frombuffer(resp.read(), dtype=np.uint8)
        return cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except Exception:  # noqa: BLE001
        return None


def _maybe_append(smooth: list[float | None], target: float, history: deque[float], last_t: list[float]) -> float:
    """EMA toward target; append at UI_HZ so traces stay continuous without UI-rate stretch."""
    now = time.monotonic()
    if smooth[0] is None:
        smooth[0] = target
    else:
        smooth[0] = UI_EMA * target + (1.0 - UI_EMA) * float(smooth[0])
    if now - last_t[0] >= 1.0 / UI_HZ:
        last_t[0] = now
        history.append(float(smooth[0]))
    return float(smooth[0])


def _draw_mic_panel(
    img: np.ndarray,
    snap: dict,
    history: deque[float],
    smooth: list[float | None],
    last_t: list[float],
) -> None:
    h, w = img.shape[:2]
    rms = float(snap["rms"])
    peak = float(snap["peak"])
    if snap["mic_ok"]:
        plot_rms = _maybe_append(smooth, rms, history, last_t)
    else:
        plot_rms = rms

    bar_x, bar_y, bar_w, bar_h = 12, 12, w - 24, 22
    cv2.rectangle(img, (bar_x, bar_y), (bar_x + bar_w, bar_y + bar_h), (40, 40, 40), -1)
    frac = max(0.0, min(1.0, (plot_rms - DB_MIN) / (DB_MAX - DB_MIN)))
    fill = int(bar_w * frac)
    color = (40, 200, 40) if plot_rms < -25 else (0, 200, 255) if plot_rms < -10 else (0, 0, 220)
    if fill > 0:
        cv2.rectangle(img, (bar_x, bar_y), (bar_x + fill, bar_y + bar_h), color, -1)
    label = (
        f"mic {plot_rms:.1f} dBFS peak={peak:.3f} ({snap['mic_src']})"
        if snap["mic_ok"]
        else "mic: waiting for rms: (flash CameraWebServerWiFiSense)"
    )
    cv2.putText(img, label, (bar_x, bar_y + bar_h + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (230, 230, 230), 1)

    plot_y, plot_h = bar_y + bar_h + 28, max(40, h - (bar_y + bar_h + 40))
    cv2.rectangle(img, (bar_x, plot_y), (bar_x + bar_w, plot_y + plot_h), (28, 28, 28), -1)
    if len(history) >= 2:
        vals = np.asarray(history, dtype=np.float64)
        lo, hi = float(np.percentile(vals, 5)), float(np.percentile(vals, 95))
        pad = max(3.0, 0.2 * (hi - lo + 1e-6))
        lo -= pad
        hi += pad
        if hi <= lo:
            hi = lo + 1.0
        pts = []
        for i, v in enumerate(vals):
            x = bar_x + int(i * (bar_w - 1) / max(1, len(vals) - 1))
            yf = max(0.0, min(1.0, (v - lo) / (hi - lo)))
            y = plot_y + plot_h - 1 - int(yf * (plot_h - 1))
            pts.append((x, y))
        cv2.polylines(img, [np.array(pts, dtype=np.int32)], False, (200, 120, 255), 2)


def _draw_csi_panel(
    img: np.ndarray,
    snap: dict,
    mean_hist: deque[float],
    smooth: list[float | None],
    last_t: list[float],
) -> None:
    """Mean CSI amplitude vs time — EMA @ UI_HZ for a continuous trace."""
    h, w = img.shape[:2]
    if snap["csi_ok"]:
        plot = _maybe_append(smooth, float(snap["mean_amp"]), mean_hist, last_t)
        label = (
            f"CSI mean amp={plot:.1f}   "
            f"n={snap['csi_n']}   rssi={snap['csi_rssi']}"
        )
    else:
        label = "CSI: waiting for CSI_DATA (flash CameraWebServerWiFiSense)"
    cv2.putText(img, label, (12, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (220, 220, 220), 1)

    bar_x, plot_y = 12, 36
    bar_w, plot_h = w - 24, max(40, h - 48)
    cv2.rectangle(img, (bar_x, plot_y), (bar_x + bar_w, plot_y + plot_h), (28, 28, 28), -1)
    if len(mean_hist) >= 2:
        vals = np.asarray(mean_hist, dtype=np.float64)
        lo, hi = float(np.percentile(vals, 5)), float(np.percentile(vals, 95))
        pad = max(0.5, 0.2 * (hi - lo + 1e-6))
        lo -= pad
        hi += pad
        if hi <= lo:
            hi = lo + 1.0
        pts = []
        for i, v in enumerate(vals):
            x = bar_x + int(i * (bar_w - 1) / max(1, len(vals) - 1))
            yf = max(0.0, min(1.0, (v - lo) / (hi - lo)))
            y = plot_y + plot_h - 1 - int(yf * (plot_h - 1))
            pts.append((x, y))
        cv2.polylines(img, [np.array(pts, dtype=np.int32)], False, (94, 200, 255), 2)
        cv2.putText(
            img,
            f"{hi:.0f}",
            (bar_x + 4, plot_y + 14),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.35,
            (140, 140, 140),
            1,
        )
        cv2.putText(
            img,
            f"{lo:.0f}",
            (bar_x + 4, plot_y + plot_h - 6),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.35,
            (140, 140, 140),
            1,
        )


def compose_frame(
    video: np.ndarray | None,
    snap: dict,
    mic_hist: deque[float],
    mean_hist: deque[float],
    mic_smooth: list[float | None],
    csi_smooth: list[float | None],
    mic_t: list[float],
    csi_t: list[float],
    note: str,
) -> np.ndarray:
    vid_h, vid_w = 360, 640
    if video is None:
        video = np.zeros((vid_h, vid_w, 3), dtype=np.uint8)
        video[:] = (28, 28, 28)
        cv2.putText(
            video,
            "no video (LabPSK isolation? use Mini / MediaMTX)",
            (24, vid_h // 2),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (80, 80, 220),
            1,
        )
    else:
        video = cv2.resize(video, (vid_w, vid_h))

    mic_panel = np.zeros((140, vid_w, 3), dtype=np.uint8)
    mic_panel[:] = (22, 22, 22)
    _draw_mic_panel(mic_panel, snap, mic_hist, mic_smooth, mic_t)

    csi_panel = np.zeros((160, vid_w, 3), dtype=np.uint8)
    csi_panel[:] = (22, 22, 22)
    _draw_csi_panel(csi_panel, snap, mean_hist, csi_smooth, csi_t)

    out = np.vstack([video, mic_panel, csi_panel])
    cv2.putText(out, note, (12, out.shape[0] - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (160, 160, 160), 1)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default="http://10.128.93.25", help="Camera base URL")
    p.add_argument("--port", default=None, help="USB serial (default: first usbmodem)")
    p.add_argument("--baud", type=int, default=921600, help="Must match Sense Serial.begin")
    p.add_argument("--no-video", action="store_true")
    p.add_argument("--no-serial", action="store_true")
    p.add_argument("--no-http-mic", action="store_true")
    args = p.parse_args(argv)

    port = args.port
    if not args.no_serial and not port:
        ports = list_ports()
        port = ports[0] if ports else None

    u = urlparse(args.url)
    host = u.hostname or "10.128.93.25"
    scheme = u.scheme or "http"

    if not args.no_video:
        if not (
            host_reachable(args.url, 81, timeout=1.2) or host_reachable(args.url, 80, timeout=1.2)
        ):
            print(
                f"VIDEO UNREACHABLE → {host}:81/:80 (LabPSK isolation common).\n"
                "  Falling back to USB CSI+mic panels. Use Mini + MediaMTX for video.\n"
                "  Video-only flash: firmware/CameraWebServerWiFi"
            )
            args.no_video = True
            args.no_http_mic = True

    state = SharedState()
    stop = threading.Event()

    if port and not args.no_serial:
        threading.Thread(
            target=serial_loop, args=(port, args.baud, state, stop), daemon=True
        ).start()
        print(f"serial: {port} (CSI_DATA + rms:)")
    elif not args.no_serial:
        print("serial: no port found")

    if not args.no_http_mic:
        threading.Thread(target=http_mic_loop, args=(args.url, state, stop), daemon=True).start()
        print(f"mic http: {args.url.rstrip('/')}/mic")

    cap = None
    use_capture = False
    note = "q quit | Sense flash = CSI+mic USB; CameraWebServerWiFi = MediaMTX video-only"
    if not args.no_video:
        stream_url = f"{scheme}://{host}:81/stream"
        print(f"video: {stream_url}")
        cap = open_mjpeg(stream_url)
        if cap is None:
            use_capture = True
            note = "video: /capture fallback — q quit"
        else:
            note = "video: MJPEG — q quit"
    else:
        print("mode: USB CSI + mic only")

    # ~3 s window @ UI_HZ so the trace scrolls quickly.
    mic_hist: deque[float] = deque(maxlen=UI_HZ * 3)
    mean_hist: deque[float] = deque(maxlen=UI_HZ * 3)
    mic_smooth: list[float | None] = [None]
    csi_smooth: list[float | None] = [None]
    mic_t: list[float] = [0.0]
    csi_t: list[float] = [0.0]
    window = "S3 Sense: video + mic + CSI"

    try:
        while True:
            frame: np.ndarray | None = None
            if args.no_video:
                frame = None
            elif use_capture:
                frame = fetch_capture_jpeg(args.url)
            elif cap is not None:
                ok, frame = cap.read()
                if not ok:
                    frame = None

            snap = state.snapshot()
            vis = compose_frame(
                frame, snap, mic_hist, mean_hist, mic_smooth, csi_smooth, mic_t, csi_t, note
            )
            cv2.imshow(window, vis)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
    finally:
        stop.set()
        if cap is not None:
            cap.release()
        cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
