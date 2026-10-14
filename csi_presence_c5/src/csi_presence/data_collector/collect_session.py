"""Collect CSI + CV labels into a session folder.

Target: Mac Mini listens TCP :9055; wall-powered C5s forward CSI_DATA over LabPSK.
USB serial (--rx) remains optional for laptop debug.
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
from csi_presence.cv_ground_truth.camera_source import DEFAULT_ESP_CAM
from csi_presence.cv_ground_truth.person_labeler import PersonLabeler
from csi_presence.cv_ground_truth.yolo_pipeline import DEFAULT_QUALITY, QUALITY
from csi_presence.data_collector.serial_reader import CsiSerialReader
from csi_presence.data_collector.tcp_reader import CsiTcpListener
from csi_presence.features import window_features
from csi_presence.ports_util import ROOT, resolve_device

SESSIONS = ROOT / "data" / "sessions"
DEFAULT_TCP_PORT = 9055
COLLECT_TOML = ROOT / "configs" / "collect.toml"


def _load_collect_defaults() -> dict:
    """Optional denser defaults from configs/collect.toml."""
    out: dict = {
        "window_sec": 1.0,
        "hop_sec": 0.25,
        "seconds": 300.0,
    }
    if not COLLECT_TOML.exists():
        return out
    try:
        import tomllib

        with COLLECT_TOML.open("rb") as f:
            cfg = tomllib.load(f)
        sess = cfg.get("session") or {}
        for k in ("window_sec", "hop_sec", "seconds"):
            if k in sess:
                out[k] = float(sess[k])
    except Exception:
        pass
    return out


def _label_to_int(label: str) -> int:
    if label == "empty":
        return 0
    if label == "occupied":
        return 1
    raise ValueError(label)


def _csi_serial_worker(
    port: str, baud: int, out: queue.Queue, stop: threading.Event
) -> None:
    try:
        with CsiSerialReader(port, baud) as reader:
            for sample in reader.iter_samples():
                if stop.is_set():
                    break
                sample.source_id = port
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


def _csi_tcp_worker(listen_port: int, out: queue.Queue, stop: threading.Event) -> None:
    try:
        with CsiTcpListener(port=listen_port) as listener:
            for sample in listener.iter_samples():
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
    dflt = _load_collect_defaults()
    p = argparse.ArgumentParser(
        description="Collect CSI + CV presence GT (Mini TCP default)"
    )
    p.add_argument(
        "--listen-tcp",
        type=int,
        default=DEFAULT_TCP_PORT,
        help=f"Mac Mini: listen for wall-powered C5 TCP CSI (default {DEFAULT_TCP_PORT})",
    )
    p.add_argument(
        "--rx",
        default=None,
        help="USB serial RX (laptop debug). If set, TCP listen is disabled.",
    )
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument(
        "--camera",
        default=DEFAULT_ESP_CAM,
        help=f"CV GT source: ESP MJPEG/RTSP URL or webcam index (default {DEFAULT_ESP_CAM})",
    )
    p.add_argument(
        "--seconds",
        type=float,
        default=dflt["seconds"],
        help="Session length (default from collect.toml, denser runs ≈300s)",
    )
    p.add_argument(
        "--label",
        choices=("auto", "empty", "occupied"),
        default="auto",
        help="auto (default)=camera labels each window; empty/occupied=force (debug only)",
    )
    p.add_argument(
        "--window-sec",
        type=float,
        default=dflt["window_sec"],
        help="Feature window length (default 1.0s)",
    )
    p.add_argument(
        "--hop-sec",
        type=float,
        default=dflt["hop_sec"],
        help="Label/write every hop (default 0.25s ≈4 windows/s; was 0.5)",
    )
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

    # Default = TCP (Mini + wall power). --rx switches to USB serial debug.
    use_tcp = args.rx is None
    listen_port = args.listen_tcp if use_tcp else None
    rx = None if use_tcp else args.rx

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = args.name or args.label
    sess = SESSIONS / f"{stamp}_{suffix}"
    sess.mkdir(parents=True, exist_ok=True)
    raw_path = sess / "csi_raw.txt"
    windows_path = sess / "windows.jsonl"
    meta_path = sess / "meta.json"

    meta: dict = {
        "csi_transport": "tcp" if use_tcp else "serial",
        "listen_tcp": listen_port,
        "rx_port": rx,
        "baud": args.baud if not use_tcp else None,
        "camera": args.camera,
        "label_mode": args.label,
        "window_sec": args.window_sec,
        "hop_sec": args.hop_sec,
        "started_utc": stamp,
        "cv_pipeline": "healthcare_yolo26_pose",
        "host_role": "mac_mini_collect",
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")

    labeler = None
    if args.label == "auto":
        labeler = PersonLabeler(
            camera=args.camera,
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

    csi_q: queue.Queue = queue.Queue(maxsize=4000)
    stop = threading.Event()
    if use_tcp:
        assert listen_port is not None
        worker = threading.Thread(
            target=_csi_tcp_worker, args=(listen_port, csi_q, stop), daemon=True
        )
    else:
        assert rx is not None
        worker = threading.Thread(
            target=_csi_serial_worker, args=(rx, args.baud, csi_q, stop), daemon=True
        )
    worker.start()

    buf: deque[CsiSample] = deque()
    n_lines = 0
    n_windows = 0
    n_empty = 0
    n_occupied = 0
    sources: set[str] = set()
    t0 = time.time()
    next_hop = t0 + args.hop_sec

    print(f"session → {sess}")
    win_rate = 1.0 / max(args.hop_sec, 1e-3)
    if use_tcp:
        print(
            f"CSI=TCP :{listen_port} (C5s on wall power + LabPSK)  "
            f"CV camera={args.camera}  {args.seconds:.0f}s"
        )
    else:
        print(f"CSI=USB {rx}  CV camera={args.camera}  {args.seconds:.0f}s")
    print(
        f"density: window={args.window_sec:.2f}s hop={args.hop_sec:.2f}s "
        f"(≈{win_rate:.0f} labeled windows/s)"
    )
    if args.label == "auto":
        print("leave/enter frame in the SAME run — CV flips empty↔occupied")
    else:
        print(f"forced label={args.label}")

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
                        print(f"[csi] error: {item[1]}")
                        stop.set()
                        raise SystemExit(1)
                    sample: CsiSample = item  # type: ignore[assignment]
                    if sample.source_id:
                        sources.add(sample.source_id)
                        raw_f.write(f"{sample.source_id}\t{sample.raw_line}\n")
                    else:
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
                        if use_tcp:
                            print(
                                "waiting for CSI TCP… "
                                "flash boards with CSI_TCP_HOST=<this Mini IP>"
                            )
                        else:
                            print("waiting for CSI_DATA on serial…")
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
                    "sources": sorted({s.source_id for s in win if s.source_id}),
                    "feat": feats.tolist(),
                }
                win_f.write(json.dumps(rec) + "\n")
                n_windows += 1
                # Status ~ every 2.5s at hop=0.25 (was every 5s at hop=0.5)
                status_every = max(1, int(round(2.5 / max(args.hop_sec, 1e-3))))
                if n_windows % status_every == 0:
                    tag = "OCCUPIED" if occupied else "EMPTY"
                    elapsed = max(now - t0, 1e-3)
                    print(
                        f"  t={elapsed:.0f}s  now={tag}  "
                        f"empty={n_empty} occupied={n_occupied}  "
                        f"windows={n_windows} ({n_windows / elapsed:.1f}/s)  "
                        f"csi={n_lines}  clients={sorted(sources) or ['—']}"
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
        "source_ids": sorted(sources),
        "duration_sec": time.time() - t0,
    }
    meta_path.write_text(json.dumps(summary, indent=2) + "\n")
    print(
        f"done: {n_lines} CSI lines, {n_windows} windows "
        f"(empty={n_empty} occupied={n_occupied}) sources={sorted(sources)} → {sess}"
    )
    if args.label == "auto" and (n_empty == 0 or n_occupied == 0):
        print(
            "WARNING: need both empty and occupied — "
            "spend time both in and out of the camera frame"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
