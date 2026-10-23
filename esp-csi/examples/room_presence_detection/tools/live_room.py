#!/usr/bin/env python3
"""Live camera label next to the trained room model.

Run this on the Mini. It does not bind the CSI port.
The Camera card reads the RTSP camera itself, so it updates when someone
walks in even if no collect is running. The Model card scores the open
collect, or the last second already stored.

  ./tools/live_room.sh

Open http://127.0.0.1:9070 on the Mini. The camera value is the same
cv_occupied stored with the CSI. The model is only
tools/models/room_presence.joblib, the latest file training saved.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

import train_room as recipe
from collect_to_mini import DEFAULT_CAMERA, camera_label, start_camera

HERE = Path(__file__).resolve().parent
DEFAULT_DB = recipe.DEFAULT_DB
DEFAULT_MODEL = recipe.DEFAULT_MODEL
PORT = 9070
LOOKBACK_SEC = 3.0
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
    if int(bundle.get("feature_version", 0)) != recipe.FEATURE_VERSION:
        raise RuntimeError(f"{path} is an older feature recipe. Train again.")
    if int(bundle.get("node_dim", 0)) != recipe.NODE_DIM:
        raise RuntimeError(f"{path} does not match this trainer.")
    bases = np.asarray(bundle["baselines"], dtype=np.float64)
    if bases.shape != (len(recipe.NODE_IDS), recipe.N_SC):
        raise RuntimeError(f"{path} baselines have shape {bases.shape}")
    return bundle["model"], bases, bool(bundle.get("evaluation_trustworthy"))


def score_packets(
    by_node: dict[int, list],
    model,
    bases: np.ndarray,
) -> dict | None:
    """Score the newest complete seconds. Majority of the last few windows."""
    x, y, _rssi = recipe.fuse_session(by_node)
    if x.shape[0] == 0:
        return None
    n = min(VOTE_WINDOWS, x.shape[0])
    xs = x[-n:]
    ys = y[-n:]
    scored = recipe.with_baseline(xs, bases)
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
        proba = model.predict_proba(scored[-1:])[0]
        confidence = float(np.max(proba))
    nodes = [
        node
        for node in recipe.NODE_IDS
        if x[-1, node * recipe.NODE_DIM + recipe.PRESENT_AT] > 0.5
    ]
    return {
        "model": held,
        "window_camera": int(ys[-1]),
        "confidence": confidence,
        "nodes": nodes,
        "windows": int(x.shape[0]),
    }


def _fetch(url: str, tag: str | None) -> dict:
    import psycopg

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        if tag:
            cur.execute(
                """
                SELECT id::text, tag
                FROM room_presence_sessions
                WHERE tag = %s
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (tag,),
            )
        else:
            cur.execute(
                """
                SELECT id::text, tag
                FROM room_presence_sessions
                WHERE tag ~ '^(empty|occupied)_'
                ORDER BY (ended_at IS NULL) DESC, COALESCE(ended_at, started_at) DESC
                LIMIT 1
                """
            )
        session = cur.fetchone()
        if session is None:
            return {"session": None}
        sid, found = session
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
            (sid, LOOKBACK_SEC, sid),
        )
        rows = cur.fetchall()
    return {"session": sid, "tag": found, "rows": rows}


def state_from_rows(tag: str | None, rows: list, model_pack) -> dict:
    """Build one dashboard snapshot from a few seconds of CSI rows."""
    latest_ts = None
    latest_seq = -1
    latest_cv = None
    by_node: dict[int, list] = {node: [] for node in recipe.NODE_IDS}
    for node, host_ts, rssi, iq, cv, seq in rows:
        ts = host_ts.timestamp()
        if latest_ts is None or ts > latest_ts or (ts == latest_ts and int(seq or 0) >= latest_seq):
            latest_ts = ts
            latest_seq = int(seq or 0)
            latest_cv = None if cv is None else int(cv)
        if node not in recipe.NODE_IDS or iq is None or cv is None:
            continue
        parsed = recipe._packet(iq)
        if parsed is None:
            continue
        amp, phase = parsed
        by_node[int(node)].append((ts, amp, phase, float(rssi or 0), int(cv)))

    age = None if latest_ts is None else max(0.0, time.time() - latest_ts)
    scored = None
    model_error = None
    if model_pack is not None and any(by_node.values()):
        try:
            scored = score_packets(by_node, model_pack[0], model_pack[1])
        except Exception as exc:
            model_error = str(exc)
    camera_now = _word(latest_cv) or "waiting"
    window_camera = _word(scored["window_camera"]) if scored else None
    model_word = _word(scored["model"]) if scored else None
    agree = None
    if scored is not None:
        agree = scored["model"] == scored["window_camera"]
    return {
        "tag": tag,
        "camera": camera_now,
        "window_camera": window_camera,
        "model": model_word,
        "agree": agree,
        "confidence": None if scored is None else scored["confidence"],
        "nodes": [] if scored is None else scored["nodes"],
        "age_sec": age,
        "rows": len(rows),
        "model_error": model_error,
        "stamp": None if latest_ts is None else round(latest_ts, 3),
    }


class Live:
    def __init__(self, url: str, model_path: Path, tag: str | None, camera: str) -> None:
        self.url = url
        self.model_path = model_path
        self.tag = tag
        self.camera_url = camera
        self.camera_labeler = None
        self.camera_error = None
        self.lock = threading.Lock()
        self.model_pack = None
        self.model_mtime = None
        self.model_note = "no trained model yet"
        self.trustworthy = None
        self.history: deque[dict] = deque(maxlen=HISTORY)
        self.last_stamp = None
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
            self.model_pack = None
            self.model_mtime = None
            self.model_note = "no trained model yet. Camera still updates."
            self.trustworthy = None
            return
        mtime = self.model_path.stat().st_mtime
        if self.model_mtime == mtime and self.model_pack is not None:
            return
        try:
            loaded = load_model(self.model_path)
        except Exception as exc:
            self.model_pack = None
            self.model_note = str(exc)
            self.trustworthy = None
            return
        self.model_pack = loaded
        self.model_mtime = mtime
        self.trustworthy = loaded[2]
        self.model_note = "loaded best model"

    def start_camera(self) -> None:
        """Open the RTSP camera in the background. The page comes up while YOLO loads."""
        if not self.camera_url or self.camera_url == "off":
            return

        def run() -> None:
            try:
                self.camera_labeler = start_camera(self.camera_url, False)
                print(f"camera live  {self.camera_url}", flush=True)
            except Exception as exc:
                self.camera_error = str(exc)
                print(f"camera failed  {exc}", flush=True)

        threading.Thread(target=run, daemon=True).start()

    def stop_camera(self) -> None:
        labeler = self.camera_labeler
        self.camera_labeler = None
        if labeler is not None:
            labeler.stop()

    def _apply_live_camera(self, snap: dict) -> None:
        """Camera card follows the lens. Model card stays the CSI score."""
        if self.camera_error:
            snap["error"] = snap.get("error") or self.camera_error
            return
        if self.camera_url and self.camera_url != "off" and self.camera_labeler is None:
            snap["camera"] = "waiting"
            snap["agree"] = None
            return
        if self.camera_labeler is None:
            return
        word = _word(camera_label(self.camera_labeler))
        if word is None:
            snap["camera"] = "waiting"
            snap["agree"] = None
            return
        snap["camera"] = word
        model = snap.get("model")
        snap["agree"] = word == model if model in ("empty", "occupied") else None

    def poll_once(self) -> None:
        self.refresh_model()
        try:
            fetched = _fetch(self.url, self.tag)
        except Exception as exc:
            with self.lock:
                self.state["error"] = str(exc)
            return
        if fetched.get("session") is None:
            snap = {
                "error": None,
                "tag": self.tag,
                "camera": "waiting",
                "model": None,
                "agree": None,
                "note": "no stored session",
                "rows": 0,
                "history": list(self.history),
            }
            self._apply_live_camera(snap)
            with self.lock:
                self.state = {**self.state, **snap}
            return
        snap = state_from_rows(fetched["tag"], fetched["rows"], self.model_pack)
        stamp = snap["stamp"]
        if stamp is not None and stamp != self.last_stamp and snap["model"] is not None:
            self.history.append(
                {
                    "camera": snap["window_camera"],
                    "model": snap["model"],
                    "agree": snap["agree"],
                }
            )
            self.last_stamp = stamp
        snap["note"] = snap["model_error"] or self.model_note
        snap["trustworthy"] = self.trustworthy
        snap["history"] = list(self.history)
        snap["error"] = None
        self._apply_live_camera(snap)
        with self.lock:
            self.state = snap

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.state)


def _page(state: dict) -> str:
    camera = state.get("camera") or "waiting"
    model = state.get("model") or "—"
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
  .card .k {{ font-size: 13px; color: #9a968f; text-transform: uppercase; letter-spacing: 0.06em; }}
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
    </section>
  </div>
  <p class="match {('ok' if agree else 'bad') if agree is not None else ''}">{match}</p>
  <p class="meta">{_esc(error)}</p>
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
    document.querySelector('.meta').textContent = data.error || '';
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
            live.poll_once()
            time.sleep(0.4)

    threading.Thread(target=loop, daemon=True).start()
    httpd = ThreadingHTTPServer((host, port), make_handler(live))
    print(f"room live  http://{host}:{port}", flush=True)
    print("camera follows the lens. model is the last full second of CSI.", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        live.stop_camera()
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Live camera vs room model")
    p.add_argument("--database-url", default=DEFAULT_DB)
    p.add_argument(
        "--session",
        default=None,
        help="tag to score. Default: the open collect, or the latest stored session.",
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
    live.start_camera()
    serve(live, args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
