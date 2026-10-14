"""Presence GT from healthcare YOLO26-pose: someone in frame or not.

Only empty(0) / occupied(1). No activity / pose labels written to CSI sessions.
macOS: call poll() on the main thread when show_preview=True.
"""

from __future__ import annotations

import argparse
import time
from collections import deque
from typing import Any

import cv2

from csi_presence.cv_ground_truth.yolo_pipeline import (
    DEFAULT_QUALITY,
    QUALITY,
    frame_has_person,
    infer_kwargs,
    pick_device,
    resolve_profile,
)


class PersonLabeler:
    def __init__(
        self,
        camera_index: int = 0,
        quality: str = DEFAULT_QUALITY,
        model_name: str | None = None,
        conf: float | None = None,
        imgsz: int | None = None,
        device: str = "mps",
        show_preview: bool = True,
    ):
        self.camera_index = camera_index
        self.profile = resolve_profile(
            quality=quality, model=model_name, imgsz=imgsz, conf=conf
        )
        self.device = pick_device(device)
        self.show_preview = show_preview
        self._hits: deque[tuple[float, bool]] = deque(maxlen=300)
        self._model = None
        self._cap = None
        self._predict_kw: dict[str, Any] = {}
        self._preview_ok = show_preview
        self._warned_preview = False
        self.last_occupied = False

    @property
    def model_name(self) -> str:
        return str(self.profile["model"])

    def start(self) -> None:
        from ultralytics import YOLO

        print(
            f"[cv] presence GT from healthcare YOLO pipeline | "
            f"quality={self.profile['quality']} model={self.profile['model']} "
            f"imgsz={self.profile['imgsz']} conf={self.profile['conf']} "
            f"device={self.device}  (labels: empty|occupied only)",
            flush=True,
        )
        self._model = YOLO(self.profile["model"])
        if getattr(self._model, "task", None) != "pose":
            raise RuntimeError(
                f"Expected *-pose.pt (healthcare stack), got task={self._model.task!r} "
                f"for {self.profile['model']}"
            )
        try:
            self._model.fuse()
        except Exception as e:
            print(f"[cv] fuse skipped ({e})", flush=True)

        self._predict_kw = infer_kwargs(self.profile, self.device)
        try:
            import numpy as np

            dummy = np.zeros(
                (self.profile["imgsz"], self.profile["imgsz"], 3), dtype=np.uint8
            )
            _ = self._model.predict(dummy, **self._predict_kw)
        except Exception as e:
            print(f"[cv] warmup skipped ({e})", flush=True)
            self._predict_kw.pop("nms", None)

        self._cap = cv2.VideoCapture(self.camera_index)
        if not self._cap.isOpened():
            raise RuntimeError(f"camera {self.camera_index} failed to open")

    def stop(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        if self._preview_ok:
            try:
                cv2.destroyAllWindows()
            except Exception:
                pass

    def recent_occupied(self, window_sec: float, min_hits: int = 2) -> bool:
        now = time.time()
        recent = [h for t, h in self._hits if now - t <= window_sec and h]
        return len(recent) >= min_hits

    def poll(self) -> bool:
        """Grab one frame, update occupancy. Must run on main thread if previewing."""
        assert self._model is not None and self._cap is not None
        ok, frame = self._cap.read()
        if not ok:
            return self.last_occupied

        results = self._model.predict(frame, **self._predict_kw)
        occupied = False
        plot_frame = frame
        for r in results:
            if frame_has_person(r):
                occupied = True
            if self._preview_ok:
                try:
                    plot_frame = r.plot()
                except Exception:
                    plot_frame = frame
            break

        self.last_occupied = occupied
        self._hits.append((time.time(), occupied))

        if self._preview_ok:
            tag = "OCCUPIED" if occupied else "EMPTY"
            color = (0, 0, 255) if occupied else (0, 180, 0)
            cv2.putText(
                plot_frame, tag, (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.2, color, 2
            )
            try:
                cv2.imshow("csi_presence_c5 CV GT (YOLO26-pose)", plot_frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    raise KeyboardInterrupt
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                self._preview_ok = False
                if not self._warned_preview:
                    print(
                        f"[cv] preview disabled ({exc}); labels still update from camera"
                    )
                    self._warned_preview = True
        return occupied


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Preview YOLO26-pose occupied/empty GT (healthcare-aligned)"
    )
    p.add_argument("--camera", type=int, default=0)
    p.add_argument(
        "--quality",
        choices=tuple(QUALITY.keys()),
        default=DEFAULT_QUALITY,
        help="same presets as healthcare_pose_activity (default accurate=yolo26m-pose)",
    )
    p.add_argument("--model", default=None, help="override weights (or POSE_MODEL)")
    p.add_argument("--conf", type=float, default=None)
    p.add_argument("--seconds", type=float, default=30.0)
    args = p.parse_args(argv)
    lab = PersonLabeler(
        camera_index=args.camera,
        quality=args.quality,
        model_name=args.model,
        conf=args.conf,
        show_preview=True,
    )
    lab.start()
    print("CV preview — press q in window or wait for timeout")
    t0 = time.time()
    try:
        while time.time() - t0 < args.seconds:
            lab.poll()
            print(f"\r occupied={lab.last_occupied}   ", end="", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        lab.stop()
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
