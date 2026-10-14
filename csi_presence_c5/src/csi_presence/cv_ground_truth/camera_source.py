"""CV source for presence GT — default ESP cam .15 MJPEG (not local webcam)."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

# Reuse healthcare pose MJPEG/RTSP readers (same ESP CameraWebServer path)
_POSE_SRC = Path(__file__).resolve().parents[4] / "healthcare_pose_activity" / "src"
if str(_POSE_SRC) not in sys.path:
    sys.path.insert(0, str(_POSE_SRC))

from mjpeg import is_mjpeg_url, is_rtsp_url, iter_mjpeg, iter_rtsp  # noqa: E402

# Lab Sense / CameraWebServerWiFi on LabPSK
DEFAULT_ESP_CAM = "http://10.128.93.15:81/stream"


def resolve_camera_source(camera: str | int | None) -> str | int:
    """
    --camera 0        → local webcam index
    --camera <url>    → MJPEG or RTSP
    default / env     → ESP .15 MJPEG
    """
    env = os.environ.get("CSI_GT_CAMERA") or os.environ.get("SENSE_CAM_URL")
    if camera is None or camera == "" or camera == "esp" or camera == "default":
        return env or DEFAULT_ESP_CAM
    if isinstance(camera, int):
        return camera
    s = str(camera).strip()
    if s.isdigit():
        return int(s)
    return s


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

    def start(self) -> None:
        src = self.source
        if isinstance(src, int) or (isinstance(src, str) and src.isdigit()):
            import cv2

            idx = int(src)
            self._mode = "webcam"
            self._cap = cv2.VideoCapture(idx)
            if not self._cap.isOpened():
                raise RuntimeError(f"webcam index {idx} failed to open")
            print(f"[cv] camera source=webcam:{idx}", flush=True)
            return

        assert isinstance(src, str)
        if is_mjpeg_url(src):
            self._mode = "mjpeg"
            print(f"[cv] camera source=ESP MJPEG {src}", flush=True)
            self._thread = threading.Thread(
                target=self._loop_iter, args=(iter_mjpeg(src),), daemon=True
            )
            self._thread.start()
            self._wait_first_frame()
            return
        if is_rtsp_url(src):
            self._mode = "rtsp"
            print(f"[cv] camera source=RTSP {src}", flush=True)
            self._thread = threading.Thread(
                target=self._loop_iter, args=(iter_rtsp(src),), daemon=True
            )
            self._thread.start()
            self._wait_first_frame()
            return

        raise RuntimeError(f"unsupported camera source: {src!r}")

    def _wait_first_frame(self, timeout: float = 20.0) -> None:
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.read() is not None:
                return
            time.sleep(0.05)
        raise RuntimeError(f"no frames from {self.source} within {timeout}s")

    def _loop_iter(self, it: Any) -> None:
        try:
            for frame, _ts in it:
                if self._stop.is_set():
                    break
                with self._lock:
                    self._frame = frame
        except Exception as exc:
            print(f"[cv] stream ended ({exc})", flush=True)

    def read(self) -> np.ndarray | None:
        if self._mode == "webcam":
            assert self._cap is not None
            ok, frame = self._cap.read()
            return frame if ok else None
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
