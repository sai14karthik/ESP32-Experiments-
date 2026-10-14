"""Webcam YOLO person detect → occupied ground truth.

macOS: call poll() from the main thread when show_preview=True (cv2.imshow
cannot run on a worker thread).
"""

from __future__ import annotations

import argparse
import time
from collections import deque

import cv2


class PersonLabeler:
    def __init__(
        self,
        camera_index: int = 0,
        conf: float = 0.45,
        model_name: str = "yolo11n.pt",
        show_preview: bool = True,
    ):
        self.camera_index = camera_index
        self.conf = conf
        self.model_name = model_name
        self.show_preview = show_preview
        self._hits: deque[tuple[float, bool]] = deque(maxlen=300)
        self._model = None
        self._cap = None
        self._preview_ok = show_preview
        self._warned_preview = False
        self.last_occupied = False

    def start(self) -> None:
        from ultralytics import YOLO

        self._model = YOLO(self.model_name)
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
        results = self._model.predict(frame, verbose=False, conf=self.conf)
        occupied = False
        for r in results:
            if r.boxes is None:
                continue
            for cls in r.boxes.cls.tolist():
                if int(cls) == 0:
                    occupied = True
                    break
            if occupied:
                break
        self.last_occupied = occupied
        self._hits.append((time.time(), occupied))

        if self._preview_ok:
            tag = "OCCUPIED" if occupied else "EMPTY"
            color = (0, 0, 255) if occupied else (0, 180, 0)
            cv2.putText(frame, tag, (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.2, color, 2)
            try:
                cv2.imshow("csi_presence_c5 CV GT", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    raise KeyboardInterrupt
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                self._preview_ok = False
                if not self._warned_preview:
                    print(f"[cv] preview disabled ({exc}); labels still update from camera")
                    self._warned_preview = True
        return occupied


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Preview CV occupied/empty labels")
    p.add_argument("--camera", type=int, default=0)
    p.add_argument("--conf", type=float, default=0.45)
    p.add_argument("--seconds", type=float, default=30.0)
    args = p.parse_args(argv)
    lab = PersonLabeler(camera_index=args.camera, conf=args.conf, show_preview=True)
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
