#!/usr/bin/env python3
"""Store the four-board room readings in Postgres on the Mac Mini.

The master pushes one JSON line at a time to this machine on TCP 9056.
The same row gets a camera ground-truth label: empty (0) or occupied (1),
from YOLO26-pose on the ESP camera.

  ./tools/collect_to_mini.sh --session pipeline_test
"""

from __future__ import annotations

import argparse
import json
import queue
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = Path(__file__).resolve().parent / "schema.sql"
DEFAULT_MASTER = "http://192.168.4.1"
DEFAULT_DB = "postgresql:///csi"
DEFAULT_CAMERA = "http://10.128.93.15:81/stream"


def fetch_status(master: str, timeout: float = 0.8) -> dict:
    url = master.rstrip("/") + "/api/status"
    req = urllib.request.Request(url, headers={"User-Agent": "room-mini-collect/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def link_fields(links: list, index: int) -> tuple:
    if index >= len(links) or not isinstance(links[index], dict):
        return (None, None, None, None, None)
    link = links[index]
    return (
        int(link.get("active", 0)),
        int(link.get("room", 0)),
        int(link.get("move", 0)),
        float(link.get("wander", 0.0)),
        float(link.get("jitter", 0.0)),
    )


def apply_schema(conn) -> None:
    lines = []
    for line in SCHEMA.read_text().splitlines():
        if line.strip().startswith("--"):
            continue
        lines.append(line)
    with conn.cursor() as cur:
        for stmt in "\n".join(lines).split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
    conn.commit()


def insert_status(cur, session_id, status: dict, cv_occupied: int | None) -> None:
    links = status.get("links") or []
    l1, l2, l3 = (link_fields(links, i) for i in range(3))
    cur.execute(
        """
        INSERT INTO room_presence_samples (
            session_id, room, moving, calibrating, wander_th, jitter_th,
            l1_active, l1_room, l1_move, l1_wander, l1_jitter,
            l2_active, l2_room, l2_move, l2_wander, l2_jitter,
            l3_active, l3_room, l3_move, l3_wander, l3_jitter,
            cv_occupied
        ) VALUES (
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s
        )
        """,
        (
            session_id,
            int(status.get("room", 0)),
            int(status.get("moving", 0)),
            int(status.get("calibrating", 0)),
            float(status.get("wander_th", 0.0)),
            float(status.get("jitter_th", 0.0)),
            *l1,
            *l2,
            *l3,
            cv_occupied,
        ),
    )


def start_camera(camera: str, preview: bool):
    from csi_presence.cv_ground_truth.person_labeler import PersonLabeler

    labeler = PersonLabeler(camera=camera, show_preview=preview)
    labeler.start()
    return labeler


def camera_label(labeler, window_sec: float = 1.5) -> int | None:
    """1 occupied, 0 empty, None until the camera has a stable run of frames."""
    if labeler is None:
        return None
    now = time.time()
    recent = [hit for ts, hit in labeler._hits if now - ts <= window_sec]
    if len(recent) < 4:
        return None
    return 1 if labeler.recent_occupied(window_sec) else 0


def _cv_word(cv_occupied: int | None) -> str:
    if cv_occupied is None:
        return "waiting"
    return "occupied" if cv_occupied else "empty"


def listen_push(port: int, database_url: str, tag: str, camera: str, preview: bool) -> int:
    import psycopg

    labeler = None if camera == "off" else start_camera(camera, preview)
    samples: queue.Queue = queue.Queue()
    stop = threading.Event()

    def accept_loop() -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(2)
        print(f"listening 0.0.0.0:{port}  postgres {database_url}  tag={tag}", flush=True)
        print("waiting for the master. Ctrl-C to stop.", flush=True)
        try:
            while not stop.is_set():
                srv.settimeout(1.0)
                try:
                    client, addr = srv.accept()
                except socket.timeout:
                    continue
                print(f"master connected from {addr[0]}:{addr[1]}", flush=True)
                client.settimeout(30.0)
                buf = b""
                try:
                    while not stop.is_set():
                        try:
                            chunk = client.recv(2048)
                        except socket.timeout:
                            continue
                        if not chunk:
                            break
                        buf += chunk
                        while b"\n" in buf:
                            raw, buf = buf.split(b"\n", 1)
                            raw = raw.strip()
                            if not raw:
                                continue
                            samples.put((json.loads(raw.decode("utf-8")), addr[0]))
                except (json.JSONDecodeError, OSError, KeyError, TypeError, ValueError) as exc:
                    print(f"link dropped ({exc})", flush=True)
                finally:
                    client.close()
                    print("master disconnected; still listening", flush=True)
        finally:
            srv.close()

    threading.Thread(target=accept_loop, daemon=True).start()
    n = 0
    session_id = None
    try:
        with psycopg.connect(database_url, autocommit=False) as conn:
            apply_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO room_presence_sessions (tag, master_url)
                    VALUES (%s, %s)
                    RETURNING id
                    """,
                    (tag, f"tcp:{port}"),
                )
                session_id = cur.fetchone()[0]
            conn.commit()
            while True:
                if labeler is not None:
                    labeler.poll()
                cv_occupied = camera_label(labeler)
                drained = False
                while True:
                    try:
                        status, src = samples.get_nowait()
                    except queue.Empty:
                        break
                    drained = True
                    with conn.cursor() as cur:
                        insert_status(cur, session_id, status, cv_occupied)
                    n += 1
                    if n % 20 == 0:
                        print(
                            f"rows={n}  room={status.get('room')} moving={status.get('moving')}  "
                            f"cv={_cv_word(cv_occupied)}  from {src}",
                            flush=True,
                        )
                if drained:
                    conn.commit()
                else:
                    time.sleep(0.02)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        stop.set()
        if labeler is not None:
            labeler.stop()
        if session_id is not None:
            try:
                with psycopg.connect(database_url, autocommit=True) as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE room_presence_sessions SET ended_at = now() WHERE id = %s",
                            (session_id,),
                        )
            except Exception as exc:
                print(f"could not mark session ended: {exc}")
    print(f"stored {n} rows  tag={tag}")
    return 0 if n else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Room presence → local Postgres")
    p.add_argument("--master", default=DEFAULT_MASTER)
    p.add_argument("--database-url", default=DEFAULT_DB)
    p.add_argument("--session", default="", help="tag stored with the session")
    p.add_argument("--hz", type=float, default=5.0, help="poll rate when using --master")
    p.add_argument("--seconds", type=float, default=0.0, help="0 = until Ctrl-C")
    p.add_argument(
        "--listen",
        type=int,
        default=0,
        help="TCP port the master pushes to (9056). Mini stays on Ethernet.",
    )
    p.add_argument(
        "--camera",
        default=DEFAULT_CAMERA,
        help="ESP camera MJPEG/RTSP for ground truth, or 'off'",
    )
    p.add_argument("--no-preview", action="store_true", help="skip the camera window")
    args = p.parse_args(argv)
    tag = args.session or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if args.listen:
        return listen_push(
            args.listen, args.database_url, tag, args.camera, not args.no_preview
        )

    try:
        probe = fetch_status(args.master, timeout=2.0)
    except Exception as exc:
        print(
            f"Cannot read {args.master}/api/status ({exc}).\n"
            "This fallback reads http://192.168.4.1. The normal path is --listen 9056\n"
            "on the Mini, with the master pushing over lab Wi-Fi."
        )
        return 1

    import psycopg

    period = 1.0 / max(args.hz, 0.2)
    tag = args.session or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    n = 0
    t0 = time.time()
    session_id = None

    try:
        with psycopg.connect(args.database_url, autocommit=False) as conn:
            apply_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO room_presence_sessions (tag, master_url)
                    VALUES (%s, %s)
                    RETURNING id
                    """,
                    (tag, args.master),
                )
                session_id = cur.fetchone()[0]
            conn.commit()
            print(
                f"postgres {args.database_url}  session={session_id}  tag={tag}\n"
                f"master room={probe.get('room')} moving={probe.get('moving')}  "
                f"links={len(probe.get('links') or [])}"
            )
            print("recording. Ctrl-C to stop.")
            last_print = 0.0
            while args.seconds <= 0 or (time.time() - t0) < args.seconds:
                loop_t = time.time()
                try:
                    status = fetch_status(args.master)
                except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                    print(f"status miss: {exc}")
                    time.sleep(period)
                    continue
                links = status.get("links") or []
                l1, l2, l3 = (link_fields(links, i) for i in range(3))
                with conn.cursor() as cur:
                    insert_status(cur, session_id, status, None)
                n += 1
                if n % 20 == 0:
                    conn.commit()
                now = time.time()
                if now - last_print >= 1.0:
                    last_print = now
                    print(
                        f"rows={n}  room={status.get('room')} moving={status.get('moving')}  "
                        f"L1={l1[3]:.4f}/{l1[4]:.4f}  L2={l2[3]:.4f}/{l2[4]:.4f}  "
                        f"L3={l3[3]:.4f}/{l3[4]:.4f}",
                        flush=True,
                    )
                remain = period - (time.time() - loop_t)
                if remain > 0:
                    time.sleep(remain)
            conn.commit()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        if session_id is not None:
            try:
                with psycopg.connect(args.database_url, autocommit=True) as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE room_presence_sessions SET ended_at = now() WHERE id = %s",
                            (session_id,),
                        )
            except Exception as exc:
                print(f"could not mark session ended: {exc}")

    print(f"stored {n} rows  tag={tag}")
    return 0 if n else 1


if __name__ == "__main__":
    raise SystemExit(main())
