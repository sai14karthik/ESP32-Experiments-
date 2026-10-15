"""Collect CSI + CV labels into a session folder.

Target: Mac Mini listens TCP :9055; wall-powered C5s forward CSI_DATA over LabPSK.
USB serial (--rx) remains optional for laptop debug.
"""

from __future__ import annotations

import argparse
import json
import queue
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone

from csi_presence.csi_parse import PEER_MAC_4_2, CsiSample
from csi_presence.cv_ground_truth.camera_source import DEFAULT_ESP_CAM
from csi_presence.cv_ground_truth.person_labeler import PersonLabeler
from csi_presence.cv_ground_truth.yolo_pipeline import DEFAULT_QUALITY, QUALITY
from csi_presence.data_collector.serial_reader import CsiSerialReader
from csi_presence.data_collector.tcp_reader import CsiTcpListener
from csi_presence.db import PresenceDb, database_url
from csi_presence.features import window_features
from csi_presence.ports_util import ROOT, resolve_device

SESSIONS = ROOT / "data" / "sessions"
DEFAULT_TCP_PORT = 9055
COLLECT_TOML = ROOT / "configs" / "collect.toml"


def _load_collect_defaults() -> dict:
    """Optional denser defaults from configs/collect.toml."""
    out: dict = {
        "window_sec": 1.0,
        "hop_sec": 0.125,
        "seconds": 300.0,
        "postgres": True,
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
        pg = cfg.get("postgres") or {}
        if "enable" in pg:
            out["postgres"] = bool(pg["enable"])
    except Exception:
        pass
    return out


def _label_to_int(label: str) -> int:
    if label == "empty":
        return 0
    if label == "occupied":
        return 1
    raise ValueError(label)


def _safe_session_tag(raw: str) -> str:
    """Filesystem-safe session tag (e.g. session1, mock_hospital_runA)."""
    tag = re.sub(r"[^\w.\-]+", "_", raw.strip())
    tag = tag.strip("._-")
    return tag or "auto"


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
        help="Label/write every hop (default 0.125s ≈8 windows/s)",
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
    p.add_argument(
        "--no-preview",
        action="store_true",
        help="disable OpenCV live GT window (EMPTY/OCCUPIED overlay)",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="less console status (still prints every ~5s)",
    )
    p.add_argument(
        "--live-hz",
        type=float,
        default=4.0,
        help="console live status rate (default 4 lines/s; use 1 for quieter)",
    )
    p.add_argument(
        "--session",
        "-s",
        "--name",
        dest="session",
        default=None,
        metavar="TAG",
        help="run tag for folder name, e.g. --session session1 → …_session1",
    )
    p.add_argument(
        "--method",
        choices=("auto", "4.1", "4.2", "4.3"),
        default="auto",
        help="CSI method tag in meta (auto=detect from CSI MAC; 4.2 peer=1a:00:00:00:00:0a)",
    )
    pg_grp = p.add_mutually_exclusive_group()
    pg_grp.add_argument(
        "--postgres",
        dest="postgres",
        action="store_true",
        default=dflt["postgres"],
        help="store CSI + labeled windows in PostgreSQL (default on)",
    )
    pg_grp.add_argument(
        "--no-postgres",
        dest="postgres",
        action="store_false",
        help="file-only session (skip PostgreSQL)",
    )
    p.add_argument(
        "--database-url",
        default=None,
        help="Postgres URL (default $DATABASE_URL or postgresql:///csi)",
    )
    args = p.parse_args(argv)

    # Default = TCP (Mini + wall power). --rx switches to USB serial debug.
    use_tcp = args.rx is None
    listen_port = args.listen_tcp if use_tcp else None
    rx = None if use_tcp else args.rx

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    session_tag = _safe_session_tag(args.session or args.label)
    sess = SESSIONS / f"{stamp}_{session_tag}"
    sess.mkdir(parents=True, exist_ok=True)
    raw_path = sess / "csi_raw.txt"
    windows_path = sess / "windows.jsonl"
    meta_path = sess / "meta.json"

    method = args.method
    meta: dict = {
        "csi_transport": "tcp" if use_tcp else "serial",
        "listen_tcp": listen_port,
        "rx_port": rx,
        "baud": args.baud if not use_tcp else None,
        "camera": args.camera,
        "session_tag": session_tag,
        "method": method,
        "postgres": bool(args.postgres),
        "database_url": database_url(args.database_url) if args.postgres else None,
        "method_note": (
            "4.2=CSI from peer STA; 4.1=CSI from AP; auto detects 4.2 via "
            f"MAC {PEER_MAC_4_2}"
        ),
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
    macs: set[str] = set()
    t0 = time.time()
    next_hop = t0 + args.hop_sec
    last_csi_wall = 0.0
    last_wait_print = 0.0
    last_cv_poll = 0.0
    # YOLO every loop (~100 Hz) starves CSI drain on Mini — ~6–8 Hz is enough for GT
    cv_period = min(0.2, max(0.1, args.hop_sec))
    method_locked = method != "auto"

    print(f"session → {sess}")
    win_rate = 1.0 / max(args.hop_sec, 1e-3)
    if use_tcp:
        print(
            f"CSI=TCP :{listen_port} (wall C5 → Mini)  "
            f"method={method}  CV={args.camera}  {args.seconds:.0f}s"
        )
        print(
            "  §4.2: expect 1 client (sense); CSI MAC "
            f"{PEER_MAC_4_2}  |  §4.1: N clients, AP BSSID MAC"
        )
    else:
        print(f"CSI=USB {rx}  method={method}  CV={args.camera}  {args.seconds:.0f}s")
    print(
        f"density: window={args.window_sec:.2f}s hop={args.hop_sec:.2f}s "
        f"(≈{win_rate:.0f} labeled windows/s; need ≥3 CSI/window)"
    )
    if args.label == "auto":
        print("leave/enter frame in the SAME run — CV flips empty↔occupied")
        if not args.no_preview:
            print("live: OpenCV window 'csi_presence_c5 CV GT' + console status")
        else:
            print("live: console status only (--no-preview)")
    else:
        print(f"forced label={args.label}")

    db: PresenceDb | None = None
    if args.postgres:
        try:
            db = PresenceDb(args.database_url)
            db.ensure_schema()
            db_id = db.start_session(
                method=method if method != "auto" else "auto",
                label=session_tag,
                recv_port=(
                    f"tcp:{listen_port}" if use_tcp else (rx or "usb")
                ),
                baud=None if use_tcp else args.baud,
                session_tag=session_tag,
            )
            meta["db_session_id"] = str(db_id)
            meta_path.write_text(json.dumps(meta, indent=2) + "\n")
            print(f"postgres → {database_url(args.database_url)}  session_id={db_id}")
        except Exception as exc:
            if db is not None:
                db.close()
                db = None
            print(f"ERROR: postgres connect/schema failed: {exc}")
            print("  fix DB or re-run with --no-postgres (file-only)")
            stop.set()
            if labeler is not None:
                labeler.stop()
            return 1

    live_period = 1.0 / max(args.live_hz, 0.2)
    last_live_print = 0.0
    last_full_print = 0.0

    try:
        with raw_path.open("a") as raw_f, windows_path.open("a") as win_f:
            while time.time() - t0 < args.seconds:
                now = time.time()
                # Drain CSI first (don't block behind YOLO)
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
                    if sample.mac:
                        macs.add(sample.mac)
                    if not method_locked:
                        if sample.is_between_devices:
                            method = "4.2"
                            method_locked = True
                            meta["method"] = method
                            meta_path.write_text(json.dumps(meta, indent=2) + "\n")
                            print(
                                f"[csi] detected method=4.2 (peer MAC {PEER_MAC_4_2})"
                            )
                            if db is not None and db.session_id is not None:
                                with db.conn.cursor() as cur:
                                    cur.execute(
                                        "UPDATE csi_sessions SET method = %s WHERE id = %s",
                                        (method, db.session_id),
                                    )
                                db.conn.commit()
                        elif sample.mac:
                            method = "4.1"
                            method_locked = True
                            meta["method"] = method
                            meta_path.write_text(json.dumps(meta, indent=2) + "\n")
                            print(f"[csi] detected method=4.1 (MAC {sample.mac})")
                            if db is not None and db.session_id is not None:
                                with db.conn.cursor() as cur:
                                    cur.execute(
                                        "UPDATE csi_sessions SET method = %s WHERE id = %s",
                                        (method, db.session_id),
                                    )
                                db.conn.commit()
                    n_lines += 1
                    last_csi_wall = now
                    buf.append(sample)
                    if db is not None:
                        db.enqueue_sample(sample)

                # CV at capped rate (fills PersonLabeler hit history)
                if labeler is not None and (now - last_cv_poll) >= cv_period:
                    labeler.poll()
                    last_cv_poll = now

                cutoff = now - max(args.window_sec * 2, 2.0)
                while buf and buf[0].host_ts < cutoff:
                    buf.popleft()

                # Stall: TCP client(s) but no CSI_DATA (common §4.2 band mismatch)
                if (
                    use_tcp
                    and sources
                    and n_lines == 0
                    and now - t0 > 8
                    and now - last_wait_print > 5
                ):
                    print(
                        "[csi] TCP client(s) up but 0 CSI_DATA — "
                        "power peer+sense; both must be 2.4 GHz same AP "
                        f"(§4.2 peer MAC {PEER_MAC_4_2})"
                    )
                    last_wait_print = now
                elif (
                    n_lines > 0
                    and last_csi_wall > 0
                    and now - last_csi_wall > 8
                    and now - last_wait_print > 5
                ):
                    print(
                        f"[csi] CSI stalled {now - last_csi_wall:.0f}s "
                        f"(last clients={sorted(sources)})"
                    )
                    last_wait_print = now

                if now < next_hop:
                    time.sleep(0.005)
                    continue
                next_hop = now + args.hop_sec
                win = [s for s in buf if s.host_ts >= now - args.window_sec]
                feats = window_features(win)
                if feats is None:
                    if n_lines == 0 and now - t0 > 5 and now - last_wait_print > 5:
                        if use_tcp:
                            print(
                                "waiting for CSI TCP… "
                                "start boards (CSI_TCP_HOST=this Mini) / check LabPSK"
                            )
                        else:
                            print("waiting for CSI_DATA on serial…")
                        last_wait_print = now
                    continue

                if args.label == "auto":
                    assert labeler is not None
                    # One fresh frame at hop boundary for label decision
                    labeler.poll()
                    last_cv_poll = time.time()
                    occupied = 1 if labeler.recent_occupied(args.window_sec) else 0
                    label_src = "cv"
                else:
                    occupied = _label_to_int(args.label)
                    label_src = "forced"

                if occupied:
                    n_occupied += 1
                else:
                    n_empty += 1

                win_sources = sorted({s.source_id for s in win if s.source_id})
                win_macs = sorted({s.mac for s in win if s.mac})
                rec = {
                    "t": now,
                    "label": occupied,
                    "label_src": label_src,
                    "method": method,
                    "n_csi": len(win),
                    "sources": win_sources,
                    "macs": win_macs,
                    "feat": feats.tolist(),
                }
                win_f.write(json.dumps(rec) + "\n")
                if db is not None:
                    db.insert_window(
                        host_ts=now,
                        label=occupied,
                        label_src=label_src,
                        method=method,
                        n_csi=len(win),
                        sources=win_sources,
                        macs=win_macs,
                        feat=feats.tolist(),
                    )
                n_windows += 1
                elapsed = max(now - t0, 1e-3)
                tag = "OCCUPIED" if occupied else "EMPTY"
                line = (
                    f"t={elapsed:5.1f}s  {tag:8s}  method={method}  "
                    f"empty={n_empty} occupied={n_occupied}  "
                    f"win={n_windows} ({n_windows / elapsed:.1f}/s)  "
                    f"csi={n_lines} ({n_lines / elapsed:.1f}/s)  "
                    f"n={len(win)}  clients={sorted(sources) or ['—']}"
                )
                # Live console: same-line update, full newline periodically
                if not args.quiet and (now - last_live_print) >= live_period:
                    print(f"\r  {line}    ", end="", flush=True)
                    last_live_print = now
                if (now - last_full_print) >= (5.0 if args.quiet else 2.5):
                    raw_f.flush()
                    win_f.flush()
                    print(f"\n  {line}")
                    last_full_print = now
    except KeyboardInterrupt:
        print("\nstopped by user")
    finally:
        stop.set()
        if labeler is not None:
            labeler.stop()
        if db is not None:
            try:
                db.end_session()
            except Exception as exc:
                print(f"[postgres] end_session warning: {exc}")

    db_samples = db.n_samples if db is not None else 0
    db_windows = db.n_windows if db is not None else 0
    if db is not None:
        try:
            db.close()
        except Exception:
            pass

    duration = time.time() - t0
    summary = {
        **meta,
        "method": method,
        "ended_utc": datetime.now(timezone.utc).isoformat(),
        "csi_lines": n_lines,
        "csi_hz": round(n_lines / duration, 2) if duration > 0 else 0,
        "windows": n_windows,
        "windows_empty": n_empty,
        "windows_occupied": n_occupied,
        "source_ids": sorted(sources),
        "csi_macs": sorted(macs),
        "duration_sec": duration,
        "db_samples": db_samples,
        "db_windows": db_windows,
    }
    meta_path.write_text(json.dumps(summary, indent=2) + "\n")
    print(
        f"done: method={method}  {n_lines} CSI ({summary['csi_hz']}/s), "
        f"{n_windows} windows (empty={n_empty} occupied={n_occupied})  "
        f"sources={sorted(sources)} macs={sorted(macs)} → {sess}"
    )
    if db is not None and summary.get("db_session_id"):
        print(
            f"postgres: session_id={summary['db_session_id']}  "
            f"samples={summary['db_samples']} windows={summary['db_windows']}  "
            f"(./scripts/db_dist.sh)"
        )
    if n_lines == 0:
        print(
            "WARNING: 0 CSI — for §4.2 power peer+sense on same 2.4 GHz AP; "
            "Mini must listen :9055; CSI_TCP_HOST must be Mini IP"
        )
    if args.label == "auto" and n_windows > 0 and (n_empty == 0 or n_occupied == 0):
        print(
            "WARNING: need both empty and occupied — "
            "spend time both in and out of the camera frame"
        )
    return 0 if n_lines > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
