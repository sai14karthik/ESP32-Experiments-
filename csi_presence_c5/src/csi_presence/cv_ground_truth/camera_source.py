"""CV source for presence GT — ESP Sense cam (MJPEG or MediaMTX RTSP).

ESP CameraWebServer usually allows **one** MJPEG client. If MediaMTX already
holds :81/stream, direct MJPEG times out — use RTSP cam_sense instead.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np

_POSE_SRC = Path(__file__).resolve().parents[4] / "healthcare_pose_activity" / "src"
if str(_POSE_SRC) not in sys.path:
    sys.path.insert(0, str(_POSE_SRC))

from mjpeg import is_mjpeg_url, is_rtsp_url, iter_mjpeg, iter_rtsp  # noqa: E402

DEFAULT_ESP_CAM = "http://10.128.93.15:81/stream"
DEFAULT_MTX_CAM = "rtsp://127.0.0.1:8554/cam_sense"


def resolve_camera_source(camera: str | int | None) -> str | int:
    env = os.environ.get("CSI_GT_CAMERA") or os.environ.get("SENSE_CAM_URL")
    if camera is None or camera == "" or camera == "esp" or camera == "default":
        return env or DEFAULT_ESP_CAM
    if isinstance(camera, int):
        return camera
    s = str(camera).strip()
    if s.isdigit():
        return int(s)
    return s


def _http_reachable(url: str, timeout: float = 3.0) -> bool:
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "csi-gt/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # Read a little to prove the body moves (ESP may accept then stall)
            _ = resp.read(256)
            return True
    except Exception:
        return False


def _rtsp_port_open(host: str = "127.0.0.1", port: int = 8554, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def pick_gt_sources(preferred: str | int) -> list[str | int]:
    """Ordered candidates: preferred, then MediaMTX RTSP if MJPEG preferred."""
    if isinstance(preferred, int) or (isinstance(preferred, str) and str(preferred).isdigit()):
        return [int(preferred)]
    assert isinstance(preferred, str)
    out: list[str | int] = [preferred]
    # If user asked for direct ESP MJPEG, also try local MediaMTX (single-client workaround)
    if is_mjpeg_url(preferred):
        mtx = os.environ.get("CSI_GT_RTSP", DEFAULT_MTX_CAM)
        if mtx not in out:
            out.append(mtx)
    return out


class FrameSource:
    """Latest-frame grabber for webcam index, ESP MJPEG, or RTSP."""

    def __init__(self, source: str | int):
        self.source = source
        self._frame: np.ndarray | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cap = None
        self._mode = "webcam"
        self.frame_at: float | None = None
        self._frame_seq = 0

    def start(self) -> None:
        errors: list[str] = []
        for src in pick_gt_sources(self.source):
            try:
                self._start_one(src)
                self.source = src
                return
            except Exception as exc:
                errors.append(f"{src}: {exc}")
                self.stop()
        hint = (
            "ESP :81/stream often allows only ONE client. If MediaMTX is running, use:\n"
            "  ./scripts/collect_session.sh --camera rtsp://127.0.0.1:8554/cam_sense --seconds 120\n"
            "Or stop MediaMTX and use direct MJPEG. Check: curl -m 5 -I http://10.128.93.15:81/stream"
        )
        raise RuntimeError("no CV frames from any source:\n  " + "\n  ".join(errors) + "\n" + hint)

    def _start_one(self, src: str | int) -> None:
        if isinstance(src, int) or (isinstance(src, str) and str(src).isdigit()):
            import cv2

            idx = int(src)
            self._mode = "webcam"
            self._cap = cv2.VideoCapture(idx)
            if not self._cap.isOpened():
                raise RuntimeError(f"webcam index {idx} failed to open")
            print(f"[cv] camera source=webcam:{idx}", flush=True)
            return

        assert isinstance(src, str)
        wait_s = float(os.environ.get("CSI_GT_CAMERA_WAIT", "45"))

        if is_mjpeg_url(src):
            self._mode = "mjpeg"
            print(f"[cv] camera source=ESP MJPEG {src}", flush=True)
            if not _http_reachable(src, timeout=5.0):
                # Still try — MediaMTX may be the one holding the only slot
                print(
                    f"[cv] warning: quick probe failed on {src} "
                    "(board hung, wrong IP, or MediaMTX already holding /stream)",
                    flush=True,
                )
            self._thread = threading.Thread(
                target=self._loop_iter,
                args=(iter_mjpeg(src, timeout=10.0),),
                daemon=True,
            )
            self._thread.start()
            self._wait_first_frame(timeout=wait_s)
            return

        if is_rtsp_url(src):
            self._mode = "rtsp"
            print(f"[cv] camera source=RTSP {src}", flush=True)
            if "127.0.0.1:8554" in src or "localhost:8554" in src:
                if not _rtsp_port_open():
                    raise RuntimeError("MediaMTX :8554 not listening — start mediamtx_run.sh first")
            self._thread = threading.Thread(
                target=self._loop_iter, args=(iter_rtsp(src),), daemon=True
            )
            self._thread.start()
            self._wait_first_frame(timeout=wait_s)
            return

        raise RuntimeError(f"unsupported camera source: {src!r}")

    def _wait_first_frame(self, timeout: float = 45.0) -> None:
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self._stop.is_set():
                raise RuntimeError("camera start cancelled")
            if self.read() is not None:
                print(f"[cv] first frame OK from {self.source}", flush=True)
                return
            time.sleep(0.05)
        raise RuntimeError(f"no frames from {self.source} within {timeout}s")

    def _remember(self, frame: np.ndarray) -> None:
        with self._lock:
            self._frame = frame
            self._frame_seq += 1
            self.frame_at = time.time()

    def _loop_iter(self, it: Any) -> None:
        try:
            for frame, _ts in it:
                if self._stop.is_set():
                    break
                self._remember(frame)
        except Exception as exc:
            print(f"[cv] stream ended ({exc})", flush=True)

    @property
    def frame_seq(self) -> int:
        with self._lock:
            return self._frame_seq

    def read(self) -> np.ndarray | None:
        if self._mode == "webcam":
            assert self._cap is not None
            ok, frame = self._cap.read()
            if not ok or frame is None:
                return None
            self._remember(frame)
            return frame
        with self._lock:
            if self._frame is None:
                return None
            return self._frame.copy()

    def stop(self) -> None:
        self._stop.set()
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._stop = threading.Event()
        self._thread = None
        self._frame = None
