"""Collect CSI + CV labels into a session folder.

CSI reads on a background thread; CV + preview run on the main thread (macOS
requires cv2.imshow on main).
"""

from __future__ import annotations

import argparse
import json
import queue
import threading
import time
from collections import deque
from datetime import datetime, timezone

from csi_presence.csi_parse import CsiSample
from csi_presence.cv_ground_truth.person_labeler import PersonLabeler
from csi_presence.cv_ground_truth.yolo_pipeline import DEFAULT_QUALITY, QUALITY
from csi_presence.data_collector.serial_reader import CsiSerialReader
from csi_presence.features import window_features
from csi_presence.ports_util import ROOT, resolve_device

SESSIONS = ROOT / "data" / "sessions"


def _label_to_int(label: str) -> int:
    if label == "empty":
        return 0
    if label == "occupied":
        return 1
    raise ValueError(label)


def _csi_worker(port: str, baud: int, out: queue.Queue, stop: threading.Event) -> None:
    try:
        with CsiSerialReader(port, baud) as reader:
            for sample in reader.iter_samples():
                if stop.is_set():
                    break
                try:
                    out.put_nowait(sample)
                except queue.Full:
                    try:
                        out.get_nowait()
                    except queue.Empty:
                        pass
                    out.put_nowait(sample)
    except Exception as exc:
        out.put(("__error__", str(exc)))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Collect CSI + presence labels")
    p.add_argument("--rx", default=None, help="RX serial port (default from configs/ports.toml)")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--camera", type=int, default=0)
    p.add_argument(
        "--seconds",
        type=float,
        default=120.0,
        help="One session: walk in/out of camera; CV flips empty↔occupied automatically",
    )
    p.add_argument(
        "--label",
        choices=("auto", "empty", "occupied"),
        default="auto",
        help="auto (default)=camera labels each window; empty/occupied=force (debug only)",
    )
    p.add_argument("--window-sec", type=float, default=1.0)
    p.add_argument("--hop-sec", type=float, default=0.5)
    p.add_argument(
        "--quality",
        choices=tuple(QUALITY.keys()),
        default=DEFAULT_QUALITY,
        help="YOLO26-pose preset (same as healthcare_pose_activity; default accurate)",
    )
    p.add_argument(
        "--model",
        default=None,
        help="override pose weights (else from --quality / POSE_MODEL)",
    )
    p.add_argument("--person-conf", type=float, default=None, help="override pose conf")
    p.add_argument("--imgsz", type=int, default=None, help="override pose imgsz")
    p.add_argument("--no-preview", action="store_true")
    p.add_argument("--name", default=None, help="session folder name suffix")
    args = p.parse_args(argv)

    rx = args.rx or resolve_device("rx")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = args.name or args.label
    sess = SESSIONS / f"{stamp}_{suffix}"
    sess.mkdir(parents=True, exist_ok=True)
    raw_path = sess / "csi_raw.txt"
    windows_path = sess / "windows.jsonl"
    meta_path = sess / "meta.json"

    meta: dict = {
        "rx_port": rx,
        "baud": args.baud,
        "camera": args.camera,
        "label_mode": args.label,
        "window_sec": args.window_sec,
        "hop_sec": args.hop_sec,
        "started_utc": stamp,
        "cv_pipeline": "healthcare_yolo26_pose",
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")

    labeler = None
    if args.label == "auto":
        labeler = PersonLabeler(
            camera_index=args.camera,
            quality=args.quality,
            model_name=args.model,
            conf=args.person_conf,
            imgsz=args.imgsz,
            show_preview=not args.no_preview,
        )
        labeler.start()
        meta.update(
            {
                "pose_quality": labeler.profile["quality"],
                "pose_model": labeler.profile["model"],
                "pose_imgsz": labeler.profile["imgsz"],
                "pose_conf": labeler.profile["conf"],
                "pose_map": labeler.profile["map"],
            }
        )
        meta_path.write_text(json.dumps(meta, indent=2) + "\n")

    csi_q: queue.Queue = queue.Queue(maxsize=2000)
    stop = threading.Event()
    worker = threading.Thread(
        target=_csi_worker, args=(rx, args.baud, csi_q, stop), daemon=True
    )
    worker.start()

    buf: deque[CsiSample] = deque()
    n_lines = 0
    n_windows = 0
    n_empty = 0
    n_occupied = 0
    t0 = time.time()
    next_hop = t0 + args.hop_sec

    print(f"session → {sess}")
    if args.label == "auto":
        print(
            f"RX={rx}  CV auto-labels for {args.seconds:.0f}s — "
            "leave the frame (empty) and walk in (occupied) in the SAME run"
        )
        print("Preview on main thread; press q in window or Ctrl+C to stop")
    else:
        print(f"RX={rx} forced label={args.label} for {args.seconds}s  (Ctrl+C to stop)")

    try:
        with raw_path.open("a") as raw_f, windows_path.open("a") as win_f:
            while time.time() - t0 < args.seconds:
                if labeler is not None:
                    labeler.poll()

                while True:
                    try:
                        item = csi_q.get_nowait()
                    except queue.Empty:
                        break
                    if isinstance(item, tuple) and item and item[0] == "__error__":
                        print(f"[csi] serial error: {item[1]}")
                        stop.set()
                        raise SystemExit(1)
                    sample: CsiSample = item  # type: ignore[assignment]
                    raw_f.write(sample.raw_line + "\n")
                    n_lines += 1
                    buf.append(sample)

                now = time.time()
                cutoff = now - max(args.window_sec * 2, 2.0)
                while buf and buf[0].host_ts < cutoff:
                    buf.popleft()

                if now < next_hop:
                    time.sleep(0.01)
                    continue
                next_hop = now + args.hop_sec
                win = [s for s in buf if s.host_ts >= now - args.window_sec]
                feats = window_features(win)
                if feats is None:
                    if n_lines == 0 and now - t0 > 5:
                        print("waiting for CSI_DATA on serial… (is RX flashed?)")
                    continue

                if args.label == "auto":
                    assert labeler is not None
                    occupied = 1 if labeler.recent_occupied(args.window_sec) else 0
                    label_src = "cv"
                else:
                    occupied = _label_to_int(args.label)
                    label_src = "forced"

                if occupied:
                    n_occupied += 1
                else:
                    n_empty += 1

                rec = {
                    "t": now,
                    "label": occupied,
                    "label_src": label_src,
                    "n_csi": len(win),
                    "feat": feats.tolist(),
                }
                win_f.write(json.dumps(rec) + "\n")
                n_windows += 1
                if n_windows % 10 == 0:
                    tag = "OCCUPIED" if occupied else "EMPTY"
                    print(
                        f"  t={now - t0:.0f}s  now={tag}  "
                        f"empty={n_empty} occupied={n_occupied}  csi={n_lines}"
                    )
    except KeyboardInterrupt:
        print("\nstopped by user")
    finally:
        stop.set()
        if labeler is not None:
            labeler.stop()

    summary = {
        **meta,
        "ended_utc": datetime.now(timezone.utc).isoformat(),
        "csi_lines": n_lines,
        "windows": n_windows,
        "windows_empty": n_empty,
        "windows_occupied": n_occupied,
        "duration_sec": time.time() - t0,
    }
    meta_path.write_text(json.dumps(summary, indent=2) + "\n")
    print(
        f"done: {n_lines} CSI lines, {n_windows} windows "
        f"(empty={n_empty} occupied={n_occupied}) → {sess}"
    )
    if args.label == "auto" and (n_empty == 0 or n_occupied == 0):
        print(
            "WARNING: need both empty and occupied in this session — "
            "run again and spend time both in and out of the camera frame"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
