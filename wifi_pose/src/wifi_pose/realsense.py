"""Minimal librealsense C API wrapper for macOS (no pyrealsense2 wheel)."""

from __future__ import annotations

import ctypes
import sys
import time

import numpy as np

LIB_PATH = "/opt/homebrew/lib/librealsense2.dylib"
RS2_API_VERSION = 2 * 10000 + 59 * 100 + 1  # 2.59.1

RS2_STREAM_DEPTH = 1
RS2_STREAM_COLOR = 2
RS2_FORMAT_Z16 = 1
RS2_FORMAT_BGR8 = 6

STREAM_MODES = (
    (640, 480, 30),
    (848, 480, 30),
    (1280, 720, 30),
)


class Intrinsics(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("ppx", ctypes.c_float),
        ("ppy", ctypes.c_float),
        ("fx", ctypes.c_float),
        ("fy", ctypes.c_float),
        ("model", ctypes.c_int),
        ("coeffs", ctypes.c_float * 5),
    ]


class RealSense:
    def __init__(self, lib_path: str = LIB_PATH) -> None:
        self.lib = ctypes.CDLL(lib_path)
        self._bind()
        self.ctx = None
        self.pipe = None
        self.cfg = None
        self.profile = None
        self.align = None
        self.queue = None
        self.intrinsics: Intrinsics | None = None
        self.depth_scale = 0.001
        self.streaming = False
        self.mode: tuple[int, int, int] | None = None

    def _bind(self) -> None:
        lib = self.lib
        vp = ctypes.c_void_p
        ep = ctypes.POINTER(ctypes.c_void_p)
        lib.rs2_get_error_message.restype = ctypes.c_char_p
        lib.rs2_create_context.restype = vp
        lib.rs2_create_pipeline.restype = vp
        lib.rs2_create_config.restype = vp
        lib.rs2_pipeline_start_with_config.restype = vp
        lib.rs2_pipeline_wait_for_frames.restype = vp
        lib.rs2_create_align.restype = vp
        lib.rs2_create_frame_queue.restype = vp
        lib.rs2_wait_for_frame.restype = vp
        lib.rs2_extract_frame.restype = vp
        lib.rs2_get_frame_stream_profile.restype = vp
        lib.rs2_get_frame_data.restype = ctypes.c_void_p
        lib.rs2_depth_frame_get_distance.restype = ctypes.c_float
        lib.rs2_embedded_frames_count.restype = ctypes.c_int
        lib.rs2_get_frame_width.restype = ctypes.c_int
        lib.rs2_get_frame_height.restype = ctypes.c_int
        for name, args in (
            ("rs2_create_context", [ctypes.c_int, ep]),
            ("rs2_create_pipeline", [vp, ep]),
            ("rs2_create_config", [ep]),
            (
                "rs2_config_enable_stream",
                [
                    vp,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    ep,
                ],
            ),
            ("rs2_pipeline_start_with_config", [vp, vp, ep]),
            ("rs2_pipeline_wait_for_frames", [vp, ctypes.c_uint, ep]),
            ("rs2_create_align", [ctypes.c_int, ep]),
            ("rs2_create_frame_queue", [ctypes.c_int, ep]),
            ("rs2_start_processing_queue", [vp, vp, ep]),
            ("rs2_process_frame", [vp, vp, ep]),
            ("rs2_wait_for_frame", [vp, ctypes.c_uint, ep]),
            ("rs2_embedded_frames_count", [vp, ep]),
            ("rs2_extract_frame", [vp, ctypes.c_int, ep]),
            ("rs2_get_frame_stream_profile", [vp, ep]),
            (
                "rs2_get_stream_profile_data",
                [
                    vp,
                    ctypes.POINTER(ctypes.c_int),
                    ctypes.POINTER(ctypes.c_int),
                    ctypes.POINTER(ctypes.c_int),
                    ctypes.POINTER(ctypes.c_int),
                    ctypes.POINTER(ctypes.c_int),
                    ep,
                ],
            ),
            ("rs2_get_frame_width", [vp, ep]),
            ("rs2_get_frame_height", [vp, ep]),
            ("rs2_get_frame_data", [vp, ep]),
            ("rs2_get_video_stream_intrinsics", [vp, ctypes.POINTER(Intrinsics), ep]),
            ("rs2_depth_frame_get_distance", [vp, ctypes.c_int, ctypes.c_int, ep]),
            ("rs2_release_frame", [vp]),
            ("rs2_pipeline_stop", [vp, ep]),
            ("rs2_delete_context", [vp]),
            ("rs2_delete_pipeline", [vp]),
            ("rs2_delete_config", [vp]),
            ("rs2_delete_pipeline_profile", [vp]),
            ("rs2_delete_processing_block", [vp]),
            ("rs2_delete_frame_queue", [vp]),
        ):
            getattr(lib, name).argtypes = args

    def _err(self):
        return ctypes.c_void_p()

    def _check(self, err: ctypes.c_void_p) -> None:
        if not err.value:
            return
        msg = self.lib.rs2_get_error_message(err)
        text = msg.decode() if msg else "realsense error"
        self.lib.rs2_free_error(err)
        raise RuntimeError(text)

    def _drop(self) -> None:
        lib = self.lib
        if self.streaming and self.pipe:
            err = self._err()
            lib.rs2_pipeline_stop(self.pipe, ctypes.byref(err))
        self.streaming = False
        if self.align:
            lib.rs2_delete_processing_block(self.align)
        if self.queue:
            lib.rs2_delete_frame_queue(self.queue)
        if self.profile:
            lib.rs2_delete_pipeline_profile(self.profile)
        if self.cfg:
            lib.rs2_delete_config(self.cfg)
        if self.pipe:
            lib.rs2_delete_pipeline(self.pipe)
        if self.ctx:
            lib.rs2_delete_context(self.ctx)
        self.ctx = self.pipe = self.cfg = self.profile = self.align = self.queue = None
        self.intrinsics = None

    def start(self, wait_s: float = 45.0) -> None:
        deadline = time.time() + wait_s
        last = "No device connected"
        while True:
            try:
                self._start_once()
                return
            except RuntimeError as exc:
                last = str(exc)
                self._drop()
                transient = any(
                    key in last
                    for key in (
                        "No device",
                        "failed to set power state",
                        "Device or resource busy",
                        "Cannot open",
                        "Couldn't resolve",
                        "failed to open",
                    )
                )
                if not transient or time.time() >= deadline:
                    raise RuntimeError(last) from exc
                print(f"Waiting for the RealSense ({last})...", file=sys.stderr)
                time.sleep(1.0)

    def _start_once(self) -> None:
        lib = self.lib
        self.streaming = False
        self.intrinsics = None
        err = self._err()
        self.ctx = lib.rs2_create_context(RS2_API_VERSION, ctypes.byref(err))
        self._check(err)
        err = self._err()
        self.pipe = lib.rs2_create_pipeline(self.ctx, ctypes.byref(err))
        self._check(err)

        last = "no matching stream mode"
        for width, height, fps in STREAM_MODES:
            err = self._err()
            self.cfg = lib.rs2_create_config(ctypes.byref(err))
            self._check(err)
            try:
                for stream, fmt in (
                    (RS2_STREAM_DEPTH, RS2_FORMAT_Z16),
                    (RS2_STREAM_COLOR, RS2_FORMAT_BGR8),
                ):
                    err = self._err()
                    lib.rs2_config_enable_stream(
                        self.cfg, stream, -1, width, height, fmt, fps, ctypes.byref(err)
                    )
                    self._check(err)
                err = self._err()
                self.profile = lib.rs2_pipeline_start_with_config(
                    self.pipe, self.cfg, ctypes.byref(err)
                )
                self._check(err)
                self.streaming = True
                self.mode = (width, height, fps)
                break
            except RuntimeError as exc:
                last = str(exc)
                if self.cfg:
                    lib.rs2_delete_config(self.cfg)
                    self.cfg = None
                continue
        else:
            raise RuntimeError(last)

        err = self._err()
        self.align = lib.rs2_create_align(RS2_STREAM_COLOR, ctypes.byref(err))
        self._check(err)
        err = self._err()
        self.queue = lib.rs2_create_frame_queue(2, ctypes.byref(err))
        self._check(err)
        err = self._err()
        lib.rs2_start_processing_queue(self.align, self.queue, ctypes.byref(err))
        self._check(err)
        print(f"Streams {self.mode[0]}x{self.mode[1]}@{self.mode[2]} aligned to color")

    def stop(self) -> None:
        self._drop()

    def reopen(self) -> None:
        print("Reopening RealSense...", file=sys.stderr)
        self._drop()
        time.sleep(1.0)
        self.start(wait_s=30.0)

    def read(self) -> tuple[np.ndarray, np.ndarray]:
        if not self.streaming or not self.pipe:
            raise RuntimeError("wait_for_frames cannot be called before start()")
        lib = self.lib
        err = self._err()
        frames = lib.rs2_pipeline_wait_for_frames(self.pipe, 5000, ctypes.byref(err))
        self._check(err)
        err = self._err()
        try:
            lib.rs2_process_frame(self.align, frames, ctypes.byref(err))
            self._check(err)
            frames = None
        except Exception:
            if frames:
                lib.rs2_release_frame(frames)
            raise
        err = self._err()
        aligned = lib.rs2_wait_for_frame(self.queue, 5000, ctypes.byref(err))
        self._check(err)
        try:
            return self._split(aligned)
        finally:
            lib.rs2_release_frame(aligned)

    def _split(self, composite) -> tuple[np.ndarray, np.ndarray]:
        lib = self.lib
        err = self._err()
        count = lib.rs2_embedded_frames_count(composite, ctypes.byref(err))
        self._check(err)
        color = depth = None
        need_meta = self.intrinsics is None
        for index in range(count):
            err = self._err()
            frame = lib.rs2_extract_frame(composite, index, ctypes.byref(err))
            self._check(err)
            try:
                err = self._err()
                profile = lib.rs2_get_frame_stream_profile(frame, ctypes.byref(err))
                self._check(err)
                stream = ctypes.c_int()
                fmt = ctypes.c_int()
                sindex = ctypes.c_int()
                uid = ctypes.c_int()
                fps = ctypes.c_int()
                err = self._err()
                lib.rs2_get_stream_profile_data(
                    profile,
                    ctypes.byref(stream),
                    ctypes.byref(fmt),
                    ctypes.byref(sindex),
                    ctypes.byref(uid),
                    ctypes.byref(fps),
                    ctypes.byref(err),
                )
                self._check(err)
                err = self._err()
                width = lib.rs2_get_frame_width(frame, ctypes.byref(err))
                self._check(err)
                err = self._err()
                height = lib.rs2_get_frame_height(frame, ctypes.byref(err))
                self._check(err)
                err = self._err()
                data = lib.rs2_get_frame_data(frame, ctypes.byref(err))
                self._check(err)
                if stream.value == RS2_STREAM_DEPTH:
                    raw = ctypes.string_at(data, width * height * 2)
                    depth = np.frombuffer(raw, dtype=np.uint16).reshape(height, width).copy()
                    if need_meta:
                        self.depth_scale = self._scale_from_frame(frame, depth)
                elif stream.value == RS2_STREAM_COLOR:
                    raw = ctypes.string_at(data, width * height * 3)
                    color = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3).copy()
                    if need_meta:
                        intr = Intrinsics()
                        err = self._err()
                        lib.rs2_get_video_stream_intrinsics(
                            profile, ctypes.byref(intr), ctypes.byref(err)
                        )
                        self._check(err)
                        self.intrinsics = intr
            finally:
                lib.rs2_release_frame(frame)

        if color is None or depth is None:
            raise RuntimeError("aligned frameset is missing color or depth")
        if color.shape[:2] != depth.shape[:2]:
            raise RuntimeError(
                f"aligned size mismatch color={color.shape[:2]} depth={depth.shape[:2]}"
            )
        if need_meta:
            if self.intrinsics is None:
                raise RuntimeError("missing color profile for intrinsics")
            intr = self.intrinsics
            print(
                f"Intrinsics {intr.width}x{intr.height}  "
                f"fx={intr.fx:.1f} fy={intr.fy:.1f}  "
                f"pp=({intr.ppx:.1f},{intr.ppy:.1f})  "
                f"depth_scale={self.depth_scale:.6f}"
            )
        return color, depth

    def _scale_from_frame(self, frame, depth: np.ndarray) -> float:
        ys, xs = np.nonzero(depth)
        if len(xs) == 0:
            return 0.001
        h, w = depth.shape
        cy, cx = h // 2, w // 2
        order = np.argsort((ys - cy) ** 2 + (xs - cx) ** 2)
        for idx in order[:200]:
            x = int(xs[idx])
            y = int(ys[idx])
            err = self._err()
            dist = float(
                self.lib.rs2_depth_frame_get_distance(frame, x, y, ctypes.byref(err))
            )
            if err.value:
                self.lib.rs2_free_error(err)
                continue
            raw = int(depth[y, x])
            if raw > 0 and dist > 0:
                return dist / raw
        return 0.001
