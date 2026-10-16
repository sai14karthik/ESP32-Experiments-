"""Shared TCP CSI → window features → P(occupied) loop."""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import numpy as np

from csi_presence.csi_parse import CsiSample
from csi_presence.data_collector.tcp_reader import CsiTcpListener
from csi_presence.features import FEATURE_VERSION, window_features
from csi_presence.model_io import PresenceDecision
from csi_presence.quality import window_ok


def _tcp_worker(port: int, out: queue.Queue, stop: threading.Event) -> None:
    try:
        with CsiTcpListener(port=port) as listener:
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


def predict_proba(bundle: dict[str, Any], feat: np.ndarray) -> float:
    """P(occupied) from feature vector."""
    scaler = bundle["scaler"]
    model = bundle["model"]
    dim = int(bundle.get("feat_dim") or feat.shape[0])
    x = np.zeros((1, dim), dtype=np.float64)
    n = min(len(feat), dim)
    x[0, :n] = feat[:n]
    xs = scaler.transform(x)
    if hasattr(model, "predict_proba"):
        proba = model.predict_proba(xs)[0]
        classes = list(getattr(model, "classes_", [0, 1]))
        if 1 in classes:
            return float(proba[classes.index(1)])
        return float(proba[-1])
    return float(model.predict(xs)[0])


def warn_feature_version(bundle: dict[str, Any]) -> None:
    bv = int(bundle.get("feature_version") or 1)
    if bv != FEATURE_VERSION:
        print(
            f"WARNING: model feature_version={bv} but code is v{FEATURE_VERSION} — "
            "rebuild windows + retrain, or live scores will be wrong"
        )


def run_score_loop(
    *,
    bundle: dict[str, Any],
    listen_tcp: int,
    seconds: float,
    calibration: dict[str, Any] | None = None,
    decision: PresenceDecision | None = None,
    on_score: Callable[[float, float, float, bool, float, int, str], None] | None = None,
    stop_event: threading.Event | None = None,
    stall_sec: float = 8.0,
) -> list[float]:
    """
    Listen for CSI, score every hop with EMA + hysteresis + debounce.

    on_score(elapsed, p_raw, p_ema, occupied, threshold, n_csi, status)
    status: OK | STALE | WAIT
    Returns list of raw P(occupied) scores (pre-EMA).
    """
    warn_feature_version(bundle)
    window_sec = float(bundle.get("window_sec") or 1.0)
    hop_sec = float(bundle.get("hop_sec") or 0.125)
    dec = decision or PresenceDecision.from_bundle(bundle, calibration)
    stop = stop_event or threading.Event()
    csi_q: queue.Queue = queue.Queue(maxsize=4000)
    worker = threading.Thread(
        target=_tcp_worker, args=(listen_tcp, csi_q, stop), daemon=True
    )
    worker.start()

    buf: deque[CsiSample] = deque()
    scores: list[float] = []
    t0 = time.time()
    next_hop = t0 + hop_sec
    n_lines = 0
    last_csi_wall = 0.0
    last_wait = 0.0

    try:
        while time.time() - t0 < seconds and not stop.is_set():
            now = time.time()
            while True:
                try:
                    item = csi_q.get_nowait()
                except queue.Empty:
                    break
                if isinstance(item, tuple) and item and item[0] == "__error__":
                    print(f"[csi] error: {item[1]}")
                    stop.set()
                    break
                sample: CsiSample = item  # type: ignore[assignment]
                buf.append(sample)
                n_lines += 1
                last_csi_wall = now

            stale = last_csi_wall > 0 and now - last_csi_wall > stall_sec
            if stale:
                if buf:
                    buf.clear()
                    dec.reset()
                    if now - last_wait > 5:
                        print(
                            f"[csi] stalled {now - last_csi_wall:.0f}s — "
                            "cleared buffer (waiting for CSI)"
                        )
                        last_wait = now

            cutoff = now - max(window_sec * 2, 2.0)
            while buf and buf[0].host_ts < cutoff:
                buf.popleft()

            if now < next_hop:
                time.sleep(0.005)
                continue
            next_hop = now + hop_sec

            if stale or (n_lines > 0 and last_csi_wall > 0 and now - last_csi_wall > stall_sec):
                if on_score is not None:
                    on_score(
                        now - t0, 0.0, 0.0, False, dec.threshold, 0, "STALE"
                    )
                continue

            win = [s for s in buf if s.host_ts >= now - window_sec]
            ok, _reason = window_ok(win, window_sec=window_sec)
            if not ok:
                if n_lines == 0 and now - t0 > 5 and now - last_wait > 5:
                    print(
                        "waiting for CSI TCP… power peer+sense; "
                        "stop collect if it owns :9055"
                    )
                    last_wait = now
                if on_score is not None and n_lines == 0:
                    on_score(
                        now - t0, 0.0, 0.0, False, dec.threshold, len(win), "WAIT"
                    )
                continue
            feats = window_features(win)
            if feats is None:
                continue

            p_raw = predict_proba(bundle, feats)
            occupied, p_ema = dec.update(p_raw)
            scores.append(p_raw)
            if on_score is not None:
                on_score(
                    now - t0, p_raw, p_ema, occupied, dec.threshold, len(win), "OK"
                )
    except KeyboardInterrupt:
        print("\nstopped by user")
    finally:
        stop.set()

    return scores
