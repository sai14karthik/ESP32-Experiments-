"""ESP CameraWebServer MJPEG + RTSP frame readers (Ultralytics LoadStreams is flaky)."""
from __future__ import annotations

import os
import socket
import time
import urllib.error
import urllib.request
from typing import Iterator, Optional

import cv2
import numpy as np


def is_mjpeg_url(source) -> bool:
    if not isinstance(source, str):
        return False
    s = source.lower()
    if s.startswith("rtsp://"):
        return False
    if not (s.startswith("http://") or s.startswith("https://")):
        return False
    # ESP CameraWebServer paths
    return "/stream" in s or ":81" in s or "mjpeg" in s


def is_rtsp_url(source) -> bool:
    return isinstance(source, str) and source.lower().startswith("rtsp://")


def iter_rtsp(
    url: str,
    *,
    reconnect_s: float = 1.5,
    max_failures: int = 60,
    open_timeout_ms: int = 15000,
    read_timeout_ms: int = 15000,
) -> Iterator[tuple[np.ndarray, float]]:
    """
    Pull RTSP frames with OpenCV over TCP (MediaMTX / cam_sense).
    Avoids Ultralytics 'Waiting for stream 0' stalls.
    """
    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
    failures = 0
    while True:
        cap = cv2.VideoCapture()
        try:
            cap.open(
                url,
                cv2.CAP_FFMPEG,
                [
                    cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,
                    open_timeout_ms,
                    cv2.CAP_PROP_READ_TIMEOUT_MSEC,
                    read_timeout_ms,
                ],
            )
        except (TypeError, cv2.error):
            # Older OpenCV FFmpeg backends may not accept open-timeout params.
            cap.open(url, cv2.CAP_FFMPEG)
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        if not cap.isOpened():
            cap.release()
            failures += 1
            print(f"rtsp: open failed ({failures}); retry in {reconnect_s}s…", flush=True)
            time.sleep(reconnect_s)
            if failures >= max_failures:
                raise ConnectionError(f"RTSP open failed: {url}")
            continue
        print(f"rtsp: connected {url}", flush=True)
        failures = 0
        empty = 0
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                empty += 1
                if empty >= 2:
                    print("rtsp: no frames, reconnecting…", flush=True)
                    break
                time.sleep(0.05)
                continue
            empty = 0
            yield frame, time.monotonic()
        cap.release()
        time.sleep(reconnect_s)


def _decode_jpeg(buf: bytes) -> Optional[np.ndarray]:
    if not buf or len(buf) < 100:
        return None
    arr = np.frombuffer(buf, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return img


def iter_mjpeg(
    url: str,
    *,
    timeout: float = 8.0,
    reconnect_s: float = 1.5,
    max_reconnects: int = 0,
) -> Iterator[tuple[np.ndarray, float]]:
    """
    Yield BGR frames from multipart/x-mixed-replace MJPEG.
    max_reconnects=0 means reconnect forever.
    """
    boundary: Optional[bytes] = None
    attempts = 0
    while True:
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Connection": "keep-alive",
                    "Cache-Control": "no-cache",
                    "User-Agent": "healthcare-pose-mjpeg/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if "boundary=" in ctype:
                    boundary = ctype.split("boundary=", 1)[1].strip().encode()
                    if boundary.startswith(b'"') and boundary.endswith(b'"'):
                        boundary = boundary[1:-1]
                else:
                    # ESP often omits boundary in header; still JPEG SOI/EOI works
                    boundary = None

                buf = b""
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    buf += chunk
                    # Prefer JPEG markers (works with/without multipart boundary)
                    while True:
                        soi = buf.find(b"\xff\xd8")
                        if soi < 0:
                            buf = buf[-2:] if len(buf) > 2 else buf
                            break
                        eoi = buf.find(b"\xff\xd9", soi + 2)
                        if eoi < 0:
                            buf = buf[soi:]
                            break
                        jpeg = buf[soi : eoi + 2]
                        buf = buf[eoi + 2 :]
                        frame = _decode_jpeg(jpeg)
                        if frame is not None:
                            yield frame, time.monotonic()
            attempts += 1
            if max_reconnects and attempts >= max_reconnects:
                raise ConnectionError(f"MJPEG ended after {attempts} reconnects: {url}")
            print(f"mjpeg: stream ended, reconnecting in {reconnect_s}s…", flush=True)
            time.sleep(reconnect_s)
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as e:
            attempts += 1
            if max_reconnects and attempts >= max_reconnects:
                raise ConnectionError(f"MJPEG open failed: {url} ({e})") from e
            print(f"mjpeg: {e}; retry in {reconnect_s}s…", flush=True)
            time.sleep(reconnect_s)
