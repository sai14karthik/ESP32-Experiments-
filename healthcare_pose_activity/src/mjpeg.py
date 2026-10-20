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


VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"}


def is_video_file(source) -> bool:
    """A local clip. The live RTSP and webcam sources stay on their own path."""
    from pathlib import Path

    if not isinstance(source, str) or is_rtsp_url(source) or is_mjpeg_url(source):
        return False
    path = Path(source)
    return path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES


def source_mode(source) -> str:
    """Which reader to use. Live camera, RTSP, and a looping file stay separate."""
    if is_mjpeg_url(source):
        return "mjpeg"
    if is_rtsp_url(source):
        return "rtsp"
    if is_video_file(source):
        return "video"
    return "live"


def iter_video_loop(
    path: str,
    *,
    pace: bool = True,
    on_media_time=None,
    on_loop=None,
    sync_to=None,
) -> Iterator[tuple[np.ndarray, float]]:
    """
    Yield frames from a file, then rewind and repeat.
    Playback follows the file frame rate when inference keeps up.
    ``sync_to`` returns how many seconds of the sound have played; frames
    already behind that clock are dropped, and early frames wait.
    """
    loop = 0
    while True:
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            raise ConnectionError(f"video open failed: {path}")
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        if fps < 1.0 or fps > 240.0:
            fps = 30.0
        period = 1.0 / fps
        counted = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        loop += 1
        print(
            f"video: loop {loop} {path} fps={fps:.1f} frames={counted or '?'}",
            flush=True,
        )
        if on_loop is not None:
            on_loop()
        frames = 0
        deadline = time.monotonic()
        try:
            while True:
                ok, frame = cap.read()
                if not ok or frame is None:
                    break
                frames += 1
                media_t = frames / fps
                if sync_to is not None:
                    target = sync_to()
                    if target is not None and media_t + 0.04 < target:
                        continue
                    if target is not None and media_t > target:
                        time.sleep(min(media_t - target, 0.5))
                elif pace:
                    delay = deadline - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
                    deadline += period
                    if time.monotonic() - deadline > period:
                        deadline = time.monotonic()
                if on_media_time is not None:
                    on_media_time(media_t)
                yield frame, time.monotonic()
        finally:
            cap.release()
        if frames == 0:
            raise ConnectionError(f"video has no frames: {path}")


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
