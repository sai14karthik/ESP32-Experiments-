"""ESP CameraWebServer MJPEG reader (OpenCV/ffmpeg often fails on :81/stream)."""
from __future__ import annotations

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
) -> Iterator[np.ndarray]:
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
                            yield frame
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
