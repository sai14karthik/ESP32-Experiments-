#!/usr/bin/env python3
"""Store the four-board room readings in Postgres on the Mac Mini.

The master pushes one JSON line at a time to this machine on TCP 9056.
The same row gets a camera ground-truth label: empty (0) or occupied (1),
from YOLO26-pose on the ESP camera.

  ./tools/collect_to_mini.sh --session pipeline_test

A second terminal can show the camera label next to the trained model
without stopping this collector:

  ./tools/live_room.sh
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
# MediaMTX already holds the ESP :81/stream (one client). Read the relay on the Mini.
DEFAULT_CAMERA = "rtsp://127.0.0.1:8554/cam_sense"
CSI_PORT = 9057


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


def camera_label(
    labeler,
    window_sec: float = 1.5,
    min_hits: int = 4,
) -> int | None:
    """1 occupied, 0 empty, None until the camera has a stable run of frames."""
    if labeler is None:
        return None
    now = time.time()
    recent = [hit for ts, hit in labeler._hits if now - ts <= window_sec]
    if len(recent) < min_hits:
        return None
    return 1 if labeler.recent_occupied(window_sec, min_hits=min_hits) else 0


# HT20 ESP-NOW from the sender: 112 int8 samples = 56 complex HT-LTF taps.
HT20_IQ_LEN = 112
TX_MAC = "1a:00:00:00:00:00"
# Walk-in and walk-out are mislabeled. On a real empty↔occupied change,
# drop the 5 minutes before the change. Hold the new label briefly so a
# one-frame camera flicker does not wipe the session.
PURGE_BEFORE_FLIP_SEC = 300.0
FLIP_HOLD_SEC = 2.0
# Receivers that must all be delivering. One silent board is a stall.
NODE_IDS = (0, 1, 2)
# No frame or CSI packet for this long means that device is stuck.
DEVICE_STALL_SEC = 8.0
# While a stall lasts, repeat the 5-minute delete about once a second.
PURGE_REPEAT_SEC = 1.0
# If any device, or all of them, stay down this long, stop the collector.
OUTAGE_STOP_SEC = 300.0


def parse_csi_line(line: str) -> dict | None:
    """CSI,<node>,<seq>,<mac>,<rssi>,<channel>,ht,<n>,i,q,..."""
    if not line.startswith("CSI,"):
        return None
    parts = line.split(",")
    if len(parts) < 9 or parts[6] != "ht":
        return None
    try:
        n = int(parts[7])
        iq = [int(x) for x in parts[8 : 8 + n]]
        node = int(parts[1])
        seq = int(parts[2])
        rssi = int(parts[4])
        channel = int(parts[5])
    except ValueError:
        return None
    if n != len(iq) or n < 8 or n % 2 != 0:
        return None
    if any(v < -32768 or v > 32767 for v in iq):
        return None
    return {
        "node": node,
        "seq": seq,
        "mac": parts[3].lower(),
        "rssi": rssi,
        "channel": channel,
        "iq": iq,
    }


def insert_csi(cur, session_id, sample: dict, source: str, cv_occupied: int | None) -> None:
    cur.execute(
        """
        INSERT INTO room_presence_csi (
            session_id, source_id, node_id, seq, mac, rssi, channel, iq, cv_occupied
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            session_id,
            source,
            sample["node"],
            sample["seq"],
            sample["mac"],
            sample["rssi"],
            sample["channel"],
            sample["iq"],
            cv_occupied,
        ),
    )


def _cv_word(cv_occupied: int | None) -> str:
    if cv_occupied is None:
        return "waiting"
    return "occupied" if cv_occupied else "empty"


class FlipGate:
    """Fire once when empty↔occupied holds for FLIP_HOLD_SEC.

    The first stable label does not delete anything. A flicker shorter than
    the hold, or a return to the previous label, cancels the pending delete.
    """

    def __init__(self) -> None:
        self.stable: int | None = None
        self.pending: int | None = None
        self.pending_since = 0.0

    def observe(self, cv: int | None, now: float) -> tuple[str, float]:
        if cv is None:
            # A camera stall is not a transition. Require the new label to
            # hold continuously, or a one-frame blip followed by waiting
            # would delete five minutes.
            self.pending = None
            return "ignore", 0.0
        if self.stable is None:
            self.stable = cv
            return "init", 0.0
        if cv == self.stable:
            self.pending = None
            return "same", 0.0
        if self.pending != cv:
            self.pending = cv
            self.pending_since = now
            return "wait", 0.0
        hold = now - self.pending_since
        if hold < FLIP_HOLD_SEC:
            return "wait", 0.0
        self.stable = cv
        self.pending = None
        return "purge", hold


def purge_before_flip(cur, session_id, *, hold_sec: float) -> tuple[int, int]:
    """Delete this session's rows in the 5 minutes before the label flipped.

    ``hold_sec`` is how long the new label has already been true, so rows
    collected after the flip stay. Live inference reads only that kept tail,
    so this delete does not remove a row the live page is showing.
    """
    cur.execute(
        """
        DELETE FROM room_presence_csi
        WHERE session_id = %s
          AND host_ts >= now() - make_interval(secs => %s)
          AND host_ts < now() - make_interval(secs => %s)
        """,
        (session_id, PURGE_BEFORE_FLIP_SEC + hold_sec, hold_sec),
    )
    csi_deleted = cur.rowcount
    cur.execute(
        """
        DELETE FROM room_presence_samples
        WHERE session_id = %s
          AND host_ts >= now() - make_interval(secs => %s)
          AND host_ts < now() - make_interval(secs => %s)
        """,
        (session_id, PURGE_BEFORE_FLIP_SEC + hold_sec, hold_sec),
    )
    return csi_deleted, cur.rowcount


def purge_while_down(cur, session_id) -> tuple[int, int]:
    """Delete this session's rows from the last 5 minutes.

    Called for as long as the camera or any receiver is stuck, so partial
    rows from the outage do not stay, and neither do the 5 minutes before it.
    """
    cur.execute(
        """
        DELETE FROM room_presence_csi
        WHERE session_id = %s
          AND host_ts >= now() - make_interval(secs => %s)
        """,
        (session_id, PURGE_BEFORE_FLIP_SEC),
    )
    csi_deleted = cur.rowcount
    cur.execute(
        """
        DELETE FROM room_presence_samples
        WHERE session_id = %s
          AND host_ts >= now() - make_interval(secs => %s)
        """,
        (session_id, PURGE_BEFORE_FLIP_SEC),
    )
    return csi_deleted, cur.rowcount


class DeviceWatch:
    """Camera plus every receiver. Storage waits until all of them are live."""

    def __init__(self, *, camera_on: bool) -> None:
        self.camera_on = camera_on
        self.node_at: dict[int, float | None] = {node: None for node in NODE_IDS}
        self.camera_at: float | None = None
        self.ready = False
        self._lock = threading.Lock()

    def note_node(self, node: int, now: float) -> None:
        with self._lock:
            if node in self.node_at:
                self.node_at[node] = now

    def note_camera(self, frame_at: float | None) -> None:
        if frame_at is None:
            return
        with self._lock:
            self.camera_at = frame_at

    def missing(self, now: float) -> list[str]:
        with self._lock:
            node_at = dict(self.node_at)
            camera_at = self.camera_at
        missing = []
        for node in NODE_IDS:
            seen = node_at[node]
            if seen is None or now - seen > DEVICE_STALL_SEC:
                missing.append(f"node {node}")
        if self.camera_on and (camera_at is None or now - camera_at > DEVICE_STALL_SEC):
            missing.append("camera")
        return missing

    def update(self, now: float) -> list[str]:
        missing = self.missing(now)
        if not missing:
            self.ready = True
        return missing


def _camera_frame_at(labeler) -> float | None:
    """Newest proof the camera delivered a frame.

    Frame time is when the picture arrived. The hit time is when YOLO finished
    that picture. A slow inference must not look like a dead camera.
    """
    if labeler is None:
        return None
    frames = getattr(labeler, "_frames", None)
    frame_at = getattr(frames, "frame_at", None) if frames is not None else None
    hits = getattr(labeler, "_hits", None)
    hit_at = hits[-1][0] if hits else None
    times = [stamp for stamp in (frame_at, hit_at) if stamp is not None]
    if not times:
        return None
    return max(times)


def listen_push(port: int, database_url: str, tag: str, camera: str, preview: bool) -> int:
    import psycopg

    labeler = None if camera == "off" else start_camera(camera, preview)
    samples: queue.Queue = queue.Queue()
    csi_q: queue.Queue = queue.Queue(maxsize=8000)
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

    def accept_csi() -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        announced = False
        while not stop.is_set():
            try:
                srv.bind(("0.0.0.0", CSI_PORT))
                break
            except OSError as exc:
                if not announced:
                    print(f"waiting for port {CSI_PORT} ({exc})", flush=True)
                    announced = True
                time.sleep(0.5)
        else:
            srv.close()
            return
        srv.listen(8)
        print(f"listening 0.0.0.0:{CSI_PORT} for CSI", flush=True)

        def read_client(client: socket.socket, addr) -> None:
            client.settimeout(30.0)
            buf = b""
            try:
                while not stop.is_set():
                    try:
                        chunk = client.recv(8192)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        sample = parse_csi_line(raw.decode("utf-8", "replace").strip())
                        if sample is None:
                            continue
                        # Stamp liveness here. The main loop may be busy in
                        # YOLO, and a full queue must not look like a dead board.
                        watch.note_node(int(sample["node"]), time.time())
                        try:
                            csi_q.put_nowait((sample, addr[0]))
                        except queue.Full:
                            pass
                    if len(buf) > 65536:
                        buf = b""
            except OSError as exc:
                print(f"csi link {addr[0]} dropped ({exc})", flush=True)
            finally:
                client.close()

        try:
            while not stop.is_set():
                srv.settimeout(1.0)
                try:
                    client, addr = srv.accept()
                except socket.timeout:
                    continue
                print(f"csi connected from {addr[0]}:{addr[1]}", flush=True)
                threading.Thread(target=read_client, args=(client, addr), daemon=True).start()
        finally:
            srv.close()

    n = 0
    csi_n = 0
    session_id = None
    gate = FlipGate()
    watch = DeviceWatch(camera_on=labeler is not None)
    outage = False
    outage_since = 0.0
    stopped_down = False
    announced_live = False
    wait_note: tuple[str, ...] | None = None
    last_purge_at = 0.0
    last_outage_note = 0.0
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
            # Session is open first, so a running live page drops port 9057
            # and this bind can take the boards.
            threading.Thread(target=accept_loop, daemon=True).start()
            threading.Thread(target=accept_csi, daemon=True).start()
            while True:
                # Inference holds this thread, so a reader cannot stamp a packet
                # that arrived during the frame. Drain those packets first.
                # A board that sent nothing stays stale and can still be down.
                if labeler is not None:
                    labeler.poll()
                for _ in range(8):
                    time.sleep(0)
                time.sleep(0.05)
                watch.note_camera(_camera_frame_at(labeler))
                cv_occupied = camera_label(labeler)
                action, hold = gate.observe(cv_occupied, time.time())
                statuses: list[tuple] = []
                while True:
                    try:
                        statuses.append(samples.get_nowait())
                    except queue.Empty:
                        break
                csi_batch: list[tuple] = []
                while True:
                    try:
                        sample, src = csi_q.get_nowait()
                    except queue.Empty:
                        break
                    csi_batch.append((sample, src))
                now = time.time()
                missing = watch.update(now)
                if missing:
                    if not watch.ready:
                        noted = tuple(missing)
                        if noted != wait_note:
                            print(
                                f"waiting for {', '.join(missing)}",
                                flush=True,
                            )
                            wait_note = noted
                    else:
                        if not outage:
                            outage = True
                            outage_since = now
                            print(
                                f"devices down  {', '.join(missing)}  "
                                "deleting last 5 min until all are back  "
                                f"stopping in {int(OUTAGE_STOP_SEC)}s if they stay down",
                                flush=True,
                            )
                            last_outage_note = now
                        elif now - last_outage_note >= 30.0:
                            left = max(0, int(OUTAGE_STOP_SEC - (now - outage_since)))
                            print(
                                f"still down  {', '.join(missing)}  "
                                f"deleting last 5 min  stopping in {left}s",
                                flush=True,
                            )
                            last_outage_note = now
                        if now - last_purge_at >= PURGE_REPEAT_SEC:
                            with conn.cursor() as cur:
                                csi_deleted, status_deleted = purge_while_down(
                                    cur, session_id
                                )
                            csi_n = max(0, csi_n - csi_deleted)
                            n = max(0, n - status_deleted)
                            conn.commit()
                            last_purge_at = now
                            if csi_deleted or status_deleted:
                                print(
                                    f"deleted last 5 min  csi={csi_deleted}  "
                                    f"status={status_deleted}",
                                    flush=True,
                                )
                        if now - outage_since >= OUTAGE_STOP_SEC:
                            stopped_down = True
                            print(
                                f"devices still down  {', '.join(missing)}  stopping",
                                flush=True,
                            )
                            break
                    if not statuses and not csi_batch:
                        time.sleep(0.02)
                    continue
                if outage:
                    outage = False
                    print("all devices back", flush=True)
                    # Queued lines arrived while a device was down. Do not store them.
                    statuses.clear()
                    csi_batch.clear()
                if not announced_live:
                    announced_live = True
                    print("all devices live", flush=True)
                wait_note = None
                purged = False
                if action == "init":
                    print(f"cv stable {_cv_word(cv_occupied)}", flush=True)
                elif action == "purge":
                    with conn.cursor() as cur:
                        csi_deleted, status_deleted = purge_before_flip(
                            cur, session_id, hold_sec=hold
                        )
                    csi_n = max(0, csi_n - csi_deleted)
                    n = max(0, n - status_deleted)
                    print(
                        f"cv {_cv_word(1 - cv_occupied)} → {_cv_word(cv_occupied)}  "
                        f"deleted last 5 min  csi={csi_deleted}  status={status_deleted}",
                        flush=True,
                    )
                    purged = True
                drained = False
                for status, src in statuses:
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
                got_csi = False
                for sample, src in csi_batch:
                    got_csi = True
                    with conn.cursor() as cur:
                        insert_csi(cur, session_id, sample, src, cv_occupied)
                    csi_n += 1
                    if csi_n % 100 == 0:
                        good = (
                            len(sample["iq"]) == HT20_IQ_LEN
                            and sample["mac"] == TX_MAC
                        )
                        print(
                            f"csi={csi_n}  node={sample['node']}  iq={len(sample['iq'])}  "
                            f"mac={sample['mac']}  ch={sample['channel']}  "
                            f"cv={_cv_word(cv_occupied)}  {'ok' if good else 'WRONG'}  from {src}",
                            flush=True,
                        )
                if drained or got_csi or purged:
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
    print(f"stored {n} status rows  {csi_n} csi rows  tag={tag}")
    if stopped_down:
        return 2
    return 0 if (n or csi_n) else 1


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
