#!/usr/bin/env python3
"""Live camera label next to the trained room model.

Run this on the Mini. The two cards always share one source and one moment.
The camera word is only the camera. The model word is only the CSI score.
A new pair is shown when both are ready for that moment. Until then the
previous pair stays, so one card cannot move ahead of the other.

While a collect is open, both cards read that collect: the Camera card is the
cv_occupied on the newest row, and the Model card scores the CSI that ends at
that same row. Recipe 7 uses the last four seconds ending there. This process
lets go of port 9057 so the collector can take the boards.

With no collect open, neither card reads stored rows. The Camera card is the
RTSP lens for that same second and the Model card scores CSI arriving now
on port 9057.

  ./tools/live_room.sh

Open http://127.0.0.1:9070 on the Mini. The same camera and model pair
is printed in this terminal. The model is only
tools/models/room_presence.joblib, the latest file training saved.
"""

from __future__ import annotations

import argparse
import json
import socket
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

import train_room as recipe
from collect_to_mini import (
    CSI_PORT,
    DEFAULT_CAMERA,
    FLIP_HOLD_SEC,
    camera_label,
    parse_csi_line,
    start_camera,
)

HERE = Path(__file__).resolve().parent
DEFAULT_DB = recipe.DEFAULT_DB
DEFAULT_MODEL = recipe.DEFAULT_MODEL
PORT = 9070
# The label-flip delete keeps the last FLIP_HOLD_SEC seconds and removes the
# 5 minutes before that. Live reads only inside the kept tail, so that delete
# cannot remove a row this page is showing.
LOOKBACK_SEC = FLIP_HOLD_SEC - 0.1


def _lookback(version: int) -> float:
    """How many seconds of CSI a score needs.

    The saved one-second model stays inside the short tail. Recipe 7 needs the
    whole four-second window plus the votes that follow it.
    """
    need = recipe.window_sec_for(version) + (VOTE_WINDOWS - 1) * recipe.HOP_SEC
    return max(LOOKBACK_SEC, need)


# A collect that has not stored a row for this long is in a device stall.
# The page must not keep showing the last label from before the stall.
COLLECT_STALE_SEC = 15.0
# YOLO on this Mini is often slower than two frames a second. A one-second
# window then has no word, and both cards go blank. Two frames in four
# seconds still means the camera is alive.
LENS_WINDOW_SEC = 4.0
LENS_MIN_HITS = 2
VOTE_WINDOWS = 4
HISTORY = 40


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _word(label: int | None) -> str | None:
    if label is None:
        return None
    return "occupied" if int(label) else "empty"


def load_model(path: Path) -> tuple[object, np.ndarray, bool] | None:
    """Load the one saved best model. Any other file is refused."""
    if path.resolve() != recipe.DEFAULT_MODEL.resolve():
        raise RuntimeError("live inference loads only tools/models/room_presence.joblib")
    if not path.is_file():
        return None
    import joblib

    bundle = joblib.load(path)
    if bundle.get("role") != "best" or "model" not in bundle:
        raise RuntimeError("room_presence.joblib is not the saved model")
    version = int(bundle.get("feature_version", 0))
    if version not in (4, 5, 6, 7):
        raise RuntimeError(f"{path} is an older feature recipe. Train again.")
    if int(bundle.get("node_dim", 0)) != recipe.node_dim_for(version):
        raise RuntimeError(f"{path} does not match this trainer.")
    bases = np.asarray(bundle["baselines"], dtype=np.float64)
    if bases.shape != (len(recipe.NODE_IDS), recipe.N_SC):
        raise RuntimeError(f"{path} baselines have shape {bases.shape}")
    return (
        bundle["model"],
        bases,
        bool(bundle.get("evaluation_trustworthy")),
        {
            "role": "best",
            "score": bundle.get("best_score"),
            "trained_at": bundle.get("trained_at"),
            "feature_version": version,
        },
    )


def _feature_version(model_pack) -> int:
    if not model_pack or len(model_pack) < 4:
        return recipe.FEATURE_VERSION
    try:
        return int(model_pack[3].get("feature_version", recipe.FEATURE_VERSION))
    except (TypeError, ValueError):
        return recipe.FEATURE_VERSION


def score_packets(
    by_node: dict[int, list],
    model,
    bases: np.ndarray,
    *,
    min_nodes: int = 2,
    feature_version: int = recipe.FEATURE_VERSION,
) -> dict | None:
    """Score the newest complete seconds. Majority of the last few windows."""
    x, y, _rssi = recipe.fuse_session(
        by_node, min_nodes=min_nodes, feature_version=feature_version
    )
    if x.shape[0] == 0:
        return None
    n = min(VOTE_WINDOWS, x.shape[0])
    xs = x[-n:]
    ys = y[-n:]
    scored = recipe.with_baseline(xs, bases, version=feature_version)
    pred = model.predict(scored).astype(int)
    newest = int(pred[-1])
    ones = int(np.sum(pred == 1))
    zeros = int(pred.size - ones)
    if ones == zeros:
        held = newest
    else:
        held = 1 if ones > zeros else 0
    confidence = None
    if hasattr(model, "predict_proba"):
        proba = model.predict_proba(scored)
        classes = [int(c) for c in model.classes_]
        if held in classes:
            confidence = float(np.mean(proba[:, classes.index(held)]))
    width = recipe.node_dim_for(feature_version)
    nodes = [
        node
        for node in recipe.NODE_IDS
        if x[-1, node * width + width - 1] > 0.5
    ]
    return {
        "model": held,
        "window_camera": int(ys[-1]),
        "confidence": confidence,
        "nodes": nodes,
        "windows": int(x.shape[0]),
    }


def _fetch(url: str, tag: str | None, lookback: float) -> dict:
    """The open collect, if one is running. A finished session is not the live page."""
    import psycopg

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        session = None
        if tag:
            cur.execute(
                """
                SELECT id::text, tag,
                       EXTRACT(EPOCH FROM (now() - started_at))
                FROM room_presence_sessions
                WHERE ended_at IS NULL AND tag = %s
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (tag,),
            )
            session = cur.fetchone()
        if session is None:
            cur.execute(
                """
                SELECT id::text, tag,
                       EXTRACT(EPOCH FROM (now() - started_at))
                FROM room_presence_sessions
                WHERE ended_at IS NULL
                ORDER BY started_at DESC
                LIMIT 1
                """
            )
            session = cur.fetchone()
        if session is None:
            return {"session": None, "open": False}
        sid, found, started_age = session
        cur.execute(
            """
            SELECT node_id, host_ts, rssi, iq, cv_occupied, seq
            FROM room_presence_csi
            WHERE session_id = %s
              AND host_ts >= (
                    SELECT max(host_ts) - make_interval(secs => %s)
                    FROM room_presence_csi
                    WHERE session_id = %s
                  )
            ORDER BY node_id, host_ts, seq
            """,
            (sid, lookback, sid),
        )
        rows = cur.fetchall()
    return {
        "session": sid,
        "tag": found,
        "rows": rows,
        "open": True,
        "started_age_sec": None if started_age is None else float(started_age),
    }


def state_from_rows(
    tag: str | None, rows: list, model_pack, visit_bases: dict | None = None
) -> dict:
    """Build one dashboard snapshot from a few seconds of CSI rows."""
    latest_ts = None
    latest_seq = -1
    latest_cv = None
    kept: list[tuple] = []
    for node, host_ts, rssi, iq, cv, seq in rows:
        ts = host_ts.timestamp()
        cv_i = None if cv is None else int(cv)
        if latest_ts is None or ts > latest_ts or (ts == latest_ts and int(seq or 0) >= latest_seq):
            latest_ts = ts
            latest_seq = int(seq or 0)
            latest_cv = cv_i
        if node not in recipe.NODE_IDS or iq is None:
            continue
        parsed = recipe._packet(iq)
        if parsed is None:
            continue
        amp, phase = parsed
        kept.append((ts, int(node), amp, phase, float(rssi or 0), cv_i))
    # Same end time as the camera row. The label is not a model input.
    # Recipe 7 reaches four seconds back from that row.
    version = _feature_version(model_pack)
    if latest_ts is not None:
        start = latest_ts - _lookback(version)
        kept = [row for row in kept if row[0] >= start]
    by_node: dict[int, list] = {node: [] for node in recipe.NODE_IDS}
    for ts, node, amp, phase, rssi, _cv_i in kept:
        by_node[node].append((ts, amp, phase, rssi, 0))

    age = None if latest_ts is None else max(0.0, time.time() - latest_ts)
    scored = None
    model_error = None
    bases = None if model_pack is None else model_pack[1]
    if (
        version >= 5
        and bases is not None
        and visit_bases is not None
    ):
        bases = recipe.refresh_visit_bases(rows, bases, visit_bases)
    if model_pack is not None and any(by_node.values()):
        try:
            scored = score_packets(
                by_node,
                model_pack[0],
                bases,
                min_nodes=1,
                feature_version=version,
            )
        except Exception as exc:
            model_error = str(exc)
    camera_now = _word(latest_cv) or "waiting"
    model_word = _word(scored["model"]) if scored else None
    return {
        "tag": tag,
        "camera": camera_now,
        "window_camera": None,
        "model": model_word,
        "agree": None,
        "confidence": None if scored is None else scored["confidence"],
        "nodes": [] if scored is None else scored["nodes"],
        "age_sec": age,
        "rows": len(rows),
        "model_error": model_error,
        "stamp": None if latest_ts is None else round(latest_ts, 3),
    }


class CsiEar:
    """CSI from the boards when collect is not listening on 9057."""

    def __init__(self) -> None:
        self._buf: deque = deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._srv: socket.socket | None = None
        self.running = False
        self.clients = 0
        self.lines = 0
        self._bad_logged = False

    def start(self) -> bool:
        if self.running:
            return True
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("0.0.0.0", CSI_PORT))
        except OSError:
            srv.close()
            return False
        srv.listen(8)
        self._srv = srv
        self._stop = threading.Event()
        self.running = True
        self.clients = 0
        self.lines = 0
        self._bad_logged = False
        threading.Thread(target=self._accept, daemon=True).start()
        print(f"csi live  0.0.0.0:{CSI_PORT}", flush=True)
        return True

    def stop(self) -> None:
        self._stop.set()
        srv = self._srv
        self._srv = None
        self.running = False
        with self._lock:
            self._buf.clear()
        if srv is not None:
            try:
                srv.close()
            except OSError:
                pass

    def since(self, after: float) -> list[tuple[float, int, object]]:
        """Packets newer than ``after``. The caller decides which ones are empty."""
        with self._lock:
            return [
                (ts, node, iq)
                for ts, node, iq, _rssi in self._buf
                if ts > after
            ]

    def recent(self, lookback: float) -> dict[int, list]:
        cutoff = time.time() - lookback
        by_node: dict[int, list] = {node: [] for node in recipe.NODE_IDS}
        with self._lock:
            rows = [row for row in self._buf if row[0] >= cutoff]
        for ts, node, iq, rssi in rows:
            parsed = recipe._packet(iq)
            if parsed is None:
                continue
            amp, phase = parsed
            by_node[node].append((ts, amp, phase, rssi, 0))
        return by_node

    def _add(self, sample: dict) -> None:
        node = int(sample["node"])
        if node not in recipe.NODE_IDS:
            return
        now = time.time()
        with self._lock:
            self.lines += 1
            first = self.lines == 1
            self._buf.append((now, node, sample["iq"], float(sample["rssi"])))
            cutoff = now - _lookback(recipe.FEATURE_VERSION)
            while self._buf and self._buf[0][0] < cutoff:
                self._buf.popleft()
        if first:
            print(f"csi line  node={node} iq={len(sample['iq'])}", flush=True)

    def _accept(self) -> None:
        srv = self._srv
        if srv is None:
            return

        def read_client(client: socket.socket) -> None:
            client.settimeout(30.0)
            try:
                client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            buf = b""
            try:
                while not self._stop.is_set():
                    try:
                        chunk = client.recv(65536)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        text = raw.decode("utf-8", "replace").strip()
                        sample = parse_csi_line(text)
                        if sample is None:
                            if text and not self._bad_logged:
                                self._bad_logged = True
                                print(f"csi ignored  {text[:80]}", flush=True)
                            continue
                        self._add(sample)
                    if len(buf) > 1_000_000:
                        buf = b""
            except OSError:
                pass
            finally:
                client.close()
                with self._lock:
                    self.clients = max(0, self.clients - 1)

        try:
            while not self._stop.is_set():
                srv.settimeout(1.0)
                try:
                    client, _addr = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                with self._lock:
                    self.clients += 1
                    shown = self.clients
                print(f"csi connected  boards={shown}", flush=True)
                threading.Thread(target=read_client, args=(client,), daemon=True).start()
        finally:
            self.running = False


class Live:
    def __init__(self, url: str, model_path: Path, tag: str | None, camera: str) -> None:
        self.url = url
        self.model_path = model_path
        self.tag = tag
        self.camera_url = camera
        self.camera_labeler = None
        self.camera_error = None
        self._camera_thread_on = False
        self._camera_stop = False
        self.ear = CsiEar()
        self._ear_counts: dict[int, int] = {}
        self._last_camera: str | None = None
        self._last_model: str | None = None
        self._last_confidence: float | None = None
        self._source: str | None = None
        self._score_error: str | None = None
        self.lock = threading.Lock()
        self.model_pack = None
        self.model_mtime = None
        self.model_note = "no trained model yet"
        self.trustworthy = None
        self.history: deque[dict] = deque(maxlen=HISTORY)
        self.last_stamp = None
        self._printed_key = None
        self._printed_at = 0.0
        self._last_pair_at = 0.0
        self._visit_bases: dict[int, np.ndarray] = {}
        self._empty_bucket: dict[int, list] = {}
        self._empty_after = 0.0
        self._empty_locked = False
        self.state: dict = {
            "tag": tag,
            "camera": "waiting",
            "window_camera": None,
            "model": None,
            "agree": None,
            "confidence": None,
            "nodes": [],
            "age_sec": None,
            "rows": 0,
            "model_error": None,
            "note": self.model_note,
            "trustworthy": None,
            "history": [],
            "error": None,
        }

    def refresh_model(self) -> None:
        if not self.model_path.is_file():
            note = "no trained model yet. Camera still updates."
            if self.model_note != note:
                print(note, flush=True)
            self.model_pack = None
            self.model_mtime = None
            self.model_note = note
            self.trustworthy = None
            self._forget_model_score()
            self._publish_note()
            return
        mtime = self.model_path.stat().st_mtime
        if self.model_mtime == mtime:
            return
        try:
            loaded = load_model(self.model_path)
        except Exception as exc:
            self.model_pack = None
            self.model_mtime = mtime
            self.model_note = str(exc)
            self.trustworthy = None
            self._forget_model_score()
            print(self.model_note, flush=True)
            self._publish_note()
            return
        if not loaded:
            self.model_pack = None
            self.model_mtime = mtime
            self.model_note = "no trained model yet. Camera still updates."
            self.trustworthy = None
            self._forget_model_score()
            print(self.model_note, flush=True)
            self._publish_note()
            return
        self.model_pack = loaded
        self.model_mtime = mtime
        self.trustworthy = loaded[2]
        self._forget_model_score()
        self.model_note = self._model_line()
        print(self.model_note, flush=True)
        self._publish_note()

    def _forget_model_score(self) -> None:
        """Drop a cached pair so the next words come from the file just loaded."""
        self._last_camera = None
        self._last_model = None
        self._last_confidence = None
        self._last_pair_at = 0.0
        self._visit_bases = {}
        self._empty_bucket = {}
        self._empty_after = 0.0
        self._empty_locked = False

    def _note_empty_room(self, camera: str) -> None:
        """Lock this visit's empty room from packets the camera calls empty.

        The saved file is the median of older sessions. Scoring the live room
        against that median looks like a person. Once each receiver has enough
        empty packets, the median stays put. Later packets do not move it.
        """
        if _feature_version(self.model_pack) < 5 or self._empty_locked:
            rows = self.ear.since(self._empty_after)
            if rows:
                self._empty_after = max(row[0] for row in rows)
            return
        rows = self.ear.since(self._empty_after)
        if rows:
            self._empty_after = max(row[0] for row in rows)
        if camera != "empty":
            return
        for _ts, node, iq in rows:
            if node not in recipe.NODE_IDS or node in self._visit_bases:
                continue
            parsed = recipe._packet(iq)
            if parsed is None:
                continue
            bucket = self._empty_bucket.setdefault(node, [])
            bucket.append(parsed[0])
            if len(bucket) < recipe.MIN_LIVE_EMPTY_PACKETS:
                continue
            self._visit_bases[node] = np.median(np.stack(bucket[-recipe.MIN_LIVE_EMPTY_PACKETS :]), axis=0)
            self._empty_bucket.pop(node, None)
        if all(node in self._visit_bases for node in recipe.NODE_IDS):
            self._empty_locked = True
            print("empty room locked for this visit", flush=True)

    def _show_pair(self, snap: dict, camera, model, confidence) -> bool:
        """Publish both cards from one moment.

        A camera word without a model score from that same moment stays off
        the page, and the reverse. The last complete pair stays until the next
        one is ready, and only while it is still this moment.
        """
        now = time.time()
        if camera in ("empty", "occupied") and model in ("empty", "occupied"):
            self._last_camera = camera
            self._last_model = model
            self._last_confidence = (
                float(confidence) if isinstance(confidence, (int, float)) else None
            )
            self._last_pair_at = now
            snap["camera"] = camera
            snap["model"] = model
            snap["confidence"] = self._last_confidence
            snap["agree"] = camera == model
            return True
        held = (
            self._last_camera in ("empty", "occupied")
            and self._last_model in ("empty", "occupied")
            and now - self._last_pair_at <= COLLECT_STALE_SEC
        )
        if held:
            snap["camera"] = self._last_camera
            snap["model"] = self._last_model
            snap["confidence"] = self._last_confidence
            snap["agree"] = self._last_camera == self._last_model
            return True
        self._last_camera = None
        self._last_model = None
        self._last_confidence = None
        self._last_pair_at = 0.0
        snap["camera"] = "waiting"
        snap["model"] = None
        snap["confidence"] = None
        snap["agree"] = None
        return False

    def _lens_word(self) -> str:
        if self.camera_error or self.camera_labeler is None:
            return "waiting"
        return (
            _word(
                camera_label(
                    self.camera_labeler,
                    window_sec=LENS_WINDOW_SEC,
                    min_hits=LENS_MIN_HITS,
                )
            )
            or "waiting"
        )

    def _publish_note(self) -> None:
        with self.lock:
            self.state["note"] = self.model_note
            self.state["trustworthy"] = self.trustworthy

    def _model_line(self) -> str:
        """Identity of the file every live score uses. Training writes this one file."""
        pack = self.model_pack
        if not pack or len(pack) < 4:
            return self.model_note
        info = pack[3]
        score = info.get("score")
        if isinstance(score, (int, float)) and score == score:
            shown = f"{float(score):.3f}"
        else:
            shown = "n/a"
        when = info.get("trained_at") or ""
        return f"best model  {self.model_path.name}  score={shown}  {when}".strip()

    def start_camera(self) -> None:
        """Open the RTSP camera once. Reloading YOLO blanks the card."""
        if self._camera_thread_on or not self.camera_url or self.camera_url == "off":
            return
        if self.camera_error:
            return
        self._camera_thread_on = True
        self._camera_stop = False

        def run() -> None:
            labeler = None
            try:
                labeler = start_camera(self.camera_url, False)
                if self._camera_stop:
                    return
                self.camera_labeler = labeler
                labeler = None
                print(f"camera live  {self.camera_url}", flush=True)
                while not self._camera_stop:
                    self.camera_labeler.poll()
                    time.sleep(0.05)
            except Exception as exc:
                self.camera_error = str(exc)
                print(f"camera failed  {exc}", flush=True)
            finally:
                self.camera_labeler = None
                if labeler is not None:
                    labeler.stop()
                self._camera_thread_on = False

        threading.Thread(target=run, daemon=True).start()

    def stop_camera(self) -> None:
        """Stop the lens. Used on shutdown, and once when a collect takes over.

        A poll must not call this on every pass. The thread clears
        ``_camera_thread_on`` when it exits, so the next room period can
        start one new load.
        """
        self._camera_stop = True
        labeler = self.camera_labeler
        self.camera_labeler = None
        if labeler is not None:
            labeler.stop()

    def stop_csi(self) -> None:
        self.ear.stop()

    def _session_is_live(self, snap: dict) -> bool:
        """An open collect owns both cards until it sets ended_at.

        The 5-minute delete pauses new rows. That pause must not switch the
        page onto the lens or onto port 9057.
        """
        return bool(snap.get("open"))

    def _use_source(self, collect: bool) -> None:
        """Switch camera and model together. A cached word stays in its source."""
        source = "collect" if collect else "room"
        if source == self._source:
            return
        self._source = source
        self._last_camera = None
        self._last_model = None
        self._last_confidence = None
        self.last_stamp = None
        if source == "collect":
            self._visit_bases = {}
            self._empty_bucket = {}
            self._empty_after = 0.0
            self._empty_locked = False
            self.stop_csi()
            self.stop_camera()
            print("live follows the collect", flush=True)
        else:
            print("live follows the room", flush=True)

    def _score_ear(self) -> tuple[str, float | None] | None:
        if self.model_pack is None or not self.ear.running:
            return None
        version = _feature_version(self.model_pack)
        by_node = self.ear.recent(_lookback(version))
        self._ear_counts = {node: len(rows) for node, rows in by_node.items()}
        bases = self.model_pack[1]
        if version >= 5 and self._visit_bases:
            bases = np.array(bases, dtype=np.float64, copy=True)
            for node, median in self._visit_bases.items():
                bases[node] = median
        try:
            scored = score_packets(
                by_node,
                self.model_pack[0],
                bases,
                min_nodes=1,
                feature_version=version,
            )
        except Exception as exc:
            self._score_error = str(exc)
            return None
        if scored is None:
            return None
        self._score_error = None
        return _word(scored["model"]), scored["confidence"]

    def _apply_sources(self, snap: dict) -> None:
        """One source for both cards. Collect rows, or the room right now."""
        collect = self._session_is_live(snap)
        self._use_source(collect)
        if collect:
            self.stop_csi()
            age = snap.get("age_sec")
            if isinstance(age, (int, float)) and age > COLLECT_STALE_SEC:
                self._last_camera = None
                self._last_model = None
                self._last_confidence = None
                snap["camera"] = "waiting"
                snap["model"] = None
                snap["confidence"] = None
                snap["agree"] = None
                snap["error"] = "collect is waiting. A device is not sending."
                return
            self._show_pair(
                snap,
                snap.get("camera"),
                snap.get("model"),
                snap.get("confidence"),
            )
            return
        snap["camera"] = "waiting"
        snap["model"] = None
        snap["confidence"] = None
        snap["agree"] = None
        snap["window_camera"] = None
        self.ear.start()
        self.start_camera()
        camera = self._lens_word()
        self._note_empty_room(camera)
        heard = self._score_ear()
        model = heard[0] if heard else None
        confidence = heard[1] if heard else None
        if self._show_pair(snap, camera, model, confidence):
            return
        if self.camera_error:
            snap["error"] = self.camera_error
        elif self.model_pack is None:
            snap["error"] = self.model_note
        elif not self.ear.running:
            snap["error"] = "model is waiting. Port 9057 is busy."
        elif self.ear.lines == 0 and self.ear.clients == 0:
            snap["error"] = "model is waiting for CSI from the boards."
        elif self.ear.lines == 0:
            snap["error"] = "boards are connected. Waiting for a CSI line."
        elif camera not in ("empty", "occupied"):
            snap["error"] = "camera is catching up."
        elif model not in ("empty", "occupied"):
            snap["error"] = "model is waiting for a full CSI window."
        else:
            snap["error"] = "waiting for the camera and the model from the same second."

    def poll_once(self) -> None:
        self._score_error = None
        self.refresh_model()
        try:
            fetched = _fetch(
                self.url, self.tag, _lookback(_feature_version(self.model_pack))
            )
        except Exception as exc:
            with self.lock:
                self.state["error"] = str(exc)
            return
        if not fetched.get("open"):
            snap = {
                "error": None,
                "tag": self.tag,
                "camera": "waiting",
                "model": None,
                "agree": None,
                "confidence": None,
                "open": False,
                "rows": 0,
                "history": list(self.history),
            }
        else:
            snap = state_from_rows(
                fetched["tag"], fetched["rows"], self.model_pack, self._visit_bases
            )
            snap["open"] = True
            snap["started_age_sec"] = fetched.get("started_age_sec")
            snap["error"] = None
            snap["history"] = list(self.history)
        self._apply_sources(snap)
        if snap.get("model_error") and not snap.get("error"):
            snap["error"] = snap["model_error"]
        if self._score_error and not snap.get("error"):
            snap["error"] = self._score_error
        where = "with the collect" if snap.get("open") else "live room"
        snap["note"] = f"{self._model_line()}  ·  {where}"
        snap["trustworthy"] = self.trustworthy
        cam = snap.get("camera")
        model = snap.get("model")
        stamp = snap.get("stamp")
        if (
            stamp is not None
            and stamp != self.last_stamp
            and cam in ("empty", "occupied")
            and model in ("empty", "occupied")
        ):
            self.history.append(
                {"camera": cam, "model": model, "agree": snap.get("agree")}
            )
            self.last_stamp = stamp
            snap["history"] = list(self.history)
        with self.lock:
            self.state = snap
        self._print_live(snap)

    def _print_live(self, snap: dict) -> None:
        """Print the same pair the page shows. Repeat a steady pair once a second."""
        camera = snap.get("camera") or "waiting"
        model = snap.get("model")
        if model in ("empty", "occupied"):
            conf = _percent(snap.get("confidence"))
            model_col = f"{model}  {conf}".rstrip()
        else:
            model = "—"
            model_col = "—"
            conf = ""
        agree = snap.get("agree")
        if agree is True:
            match = "match"
        elif agree is False:
            match = "disagree"
        else:
            match = ""
        where = "with the collect" if snap.get("open") else "live room"
        err = snap.get("error") or ""
        key = (camera, model, conf, match, where, err)
        now = time.time()
        if key == self._printed_key and now - self._printed_at < 1.0:
            return
        self._printed_key = key
        self._printed_at = now
        line = f"camera={camera}  model={model_col}"
        if match:
            line += f"  {match}"
        line += f"  {where}"
        if err:
            line += f"  {err}"
        print(line, flush=True)

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.state)


def _percent(confidence) -> str:
    if not isinstance(confidence, (int, float)) or confidence != confidence:
        return ""
    return f"{round(100 * float(confidence))}%"


def _page(state: dict) -> str:
    camera = state.get("camera") or "waiting"
    model = state.get("model") or "—"
    confidence = _percent(state.get("confidence"))
    agree = state.get("agree")
    if agree is True:
        match = "model matches the camera"
    elif agree is False:
        match = "model disagrees with the camera"
    else:
        match = "waiting for a full second"
    error = state.get("error") or ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mock patient room</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin: 0; font-family: ui-sans-serif, system-ui, sans-serif; background: #12141a; color: #e8e6e3; }}
  main {{ max-width: 880px; margin: 0 auto; padding: 28px 20px 48px; }}
  h1 {{ font-size: 28px; font-weight: 650; letter-spacing: 0; text-transform: none; color: #e8e6e3; margin: 0 0 22px; }}
  .cards {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }}
  .card {{ border-radius: 16px; padding: 18px 20px 16px; background: #1c1f27; }}
  .card .conf {{ margin-top: 10px; font-size: 22px; font-weight: 600; color: #d5d1ca; min-height: 1.2em; }}
  .card .v {{ font-size: 44px; font-weight: 680; line-height: 1.05; margin-top: 8px; text-transform: lowercase; animation: liveblink 0.9s step-end infinite; }}
  @keyframes liveblink {{
    0% {{ opacity: 1; }}
    50% {{ opacity: 0.15; }}
    100% {{ opacity: 1; }}
  }}
  .empty {{ color: #8fd49a; }}
  .occupied {{ color: #f0c36a; }}
  .waiting, .none {{ color: #8b909a; }}
  .match {{ margin: 18px 0 0; font-size: 18px; }}
  .match.bad {{ color: #f0a0a0; }}
  .match.ok {{ color: #8fd49a; }}
  .meta {{ color: #9a968f; font-size: 14px; line-height: 1.5; min-height: 1.2em; }}
  @media (max-width: 640px) {{
    .cards {{ grid-template-columns: 1fr; }}
    .card .v {{ font-size: 42px; }}
  }}
</style>
</head>
<body>
<main>
  <h1>Mock patient room</h1>
  <div class="cards">
    <section class="card">
      <div class="k">Camera</div>
      <div class="v {camera}">{camera}</div>
    </section>
    <section class="card">
      <div class="k">Model</div>
      <div class="v {model if model in ('empty', 'occupied') else 'none'}">{model}</div>
      <div class="conf">{confidence}</div>
    </section>
  </div>
  <p class="match {('ok' if agree else 'bad') if agree is not None else ''}">{match}</p>
  <p class="meta">{_esc(state.get("note") or error)}</p>
</main>
<script>
async function tick() {{
  try {{
    const res = await fetch('/api/live', {{ cache: 'no-store' }});
    const data = await res.json();
    const cam = data.camera || 'waiting';
    const model = data.model || '—';
    const cards = document.querySelectorAll('.card .v');
    cards[0].textContent = cam;
    cards[0].className = 'v ' + cam;
    cards[1].textContent = model;
    cards[1].className = 'v ' + (model === 'empty' || model === 'occupied' ? model : 'none');
    const conf = document.querySelector('.conf');
    conf.textContent = (model === 'empty' || model === 'occupied') && typeof data.confidence === 'number'
      ? Math.round(data.confidence * 100) + '%'
      : '';
    const match = document.querySelector('.match');
    if (data.agree === true) {{
      match.textContent = 'model matches the camera';
      match.className = 'match ok';
    }} else if (data.agree === false) {{
      match.textContent = 'model disagrees with the camera';
      match.className = 'match bad';
    }} else {{
      match.textContent = 'waiting for a full second';
      match.className = 'match';
    }}
    const bits = [data.note, data.error].filter(Boolean);
    document.querySelector('.meta').textContent = bits.join('  ');
  }} catch (err) {{
    document.querySelector('.match').textContent = 'dashboard lost the update';
  }}
}}
tick();
setInterval(tick, 400);
</script>
</body>
</html>
"""


def make_handler(live: Live):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            return

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            try:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/api/live":
                body = json.dumps(live.snapshot()).encode()
                self._send(200, body, "application/json")
                return
            if path in ("/", "/index.html"):
                self._send(200, _page(live.snapshot()).encode(), "text/html; charset=utf-8")
                return
            self._send(404, b"not found", "text/plain")

    return Handler


def serve(live: Live, host: str, port: int) -> None:
    def loop() -> None:
        while True:
            try:
                live.poll_once()
            except Exception as exc:
                print(f"live poll error: {exc}", flush=True)
            time.sleep(0.4)

    threading.Thread(target=loop, daemon=True).start()
    httpd = ThreadingHTTPServer((host, port), make_handler(live))
    print(f"room live  http://{host}:{port}", flush=True)
    print(
        "both cards follow a running collect. With collect stopped, both follow the room now.",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        live.stop_camera()
        live.stop_csi()
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Live camera vs room model")
    p.add_argument("--database-url", default=DEFAULT_DB)
    p.add_argument(
        "--session",
        default=None,
        help="Prefer this open collect. With no collect running, the page reads the room directly.",
    )
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=PORT)
    p.add_argument(
        "--camera",
        default=DEFAULT_CAMERA,
        help="RTSP camera for the Camera card. 'off' leaves that card on the stored label.",
    )
    args = p.parse_args(argv)
    live = Live(args.database_url, recipe.DEFAULT_MODEL, args.session, args.camera)
    live.refresh_model()
    serve(live, args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
