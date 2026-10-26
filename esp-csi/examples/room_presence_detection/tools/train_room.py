#!/usr/bin/env python3
"""Train empty/occupied from finished room_presence_csi sessions.

Run this after collection, on the Mini, with the collector stopped:

  ./tools/train_room.sh
  ./tools/train_room.sh --holdout occupied_02

Session tags named empty_* or occupied_* choose which runs to load.
pipeline_test and every other tag are ignored. The label on each row is the
camera value cv_occupied, not the session name.

Each window carries motion from the real time between packets, how much the
second varies, and how far the amplitude shape sits from that visit's empty
room. Packets that share one database timestamp are averaged first, then the
motion band is measured on that clock. The raw amplitude shape, raw phase,
and RSSI are not features. A visit's empty room is the empty windows in that
session, or the nearest session that has them. The score is
leave-one-session-out, weighted by how many windows each session has.
Results include unoccupied accuracy and occupied accuracy separately. A class
that was not in the held-out windows is reported as n/a. Live inference loads
tools/models/room_presence.joblib. A new fit replaces that file only when it
tested both labels, empty-versus-empty is near 0.5, occupied accuracy beats
the saved file by a clear margin, unoccupied accuracy stays high, and the
balanced accuracy is higher. A train that never held out occupied does not
replace the file.
"""

from __future__ import annotations

import argparse
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
DEFAULT_DB = "postgresql:///csi"
DEFAULT_MODEL = HERE / "models" / "room_presence.joblib"
N_SC = 56
IQ_LEN = N_SC * 2
# The stored 112 bytes are the valid HT-LTF only. The DC null is already
# removed, leaving 28 lower tones and 28 upper tones.
HALF = N_SC // 2
WINDOW_SEC = 1.0
HOP_SEC = 0.25
MIN_PACKETS = 30
MIN_SPAN_SEC = 0.5
# Rows inserted in one database transaction share one timestamp, then the next
# batch lands a fraction of a second later. Ignore that clump. A real hole
# inside the second is larger.
MAX_GAP_SEC = 0.75
NODE_IDS = (0, 1, 2)
# Version 4, kept so a saved model from that recipe still scores:
# amp mean, amp std, amp end-start, sanitized phase mean, sanitized phase std,
# then six scalars ending in a present flag.
_V4_SCALARS = 6
_V4_NODE_DIM = N_SC * 5 + _V4_SCALARS
# Version 5, per receiver, before the empty-room delta replaces the mean:
# amplitude mean (used only to build the delta), variation, motion,
# energy slope, and a present flag.
_V5_SCALARS = 4
_V5_NODE_DIM = N_SC + _V5_SCALARS
# Version 6 adds the real-time speed beside the motion band. The mean is
# still replaced by the empty-room delta. The last value is the present flag.
_V6_SCALARS = 5
_V6_NODE_DIM = N_SC + _V6_SCALARS
FEATURE_VERSION = 6
NODE_DIM = _V6_NODE_DIM
PRESENT_AT = NODE_DIM - 1
# An empty median needs this many windows before it can stand for the visit.
MIN_VISIT_EMPTY = 30
# Live updates a visit baseline from this many camera-empty packets per receiver.
MIN_LIVE_EMPTY_PACKETS = 30
# The saved file is replaced only when empty-versus-empty lands in this band
# and both class accuracies beat the file already on disk. These are the
# bars when that file has no stored class accuracies.
NEG_CONTROL_LO = 0.35
NEG_CONTROL_HI = 0.65
BAR_UNOCCUPIED = 0.705
BAR_OCCUPIED = 0.260
# The saved file already calls empty windows correctly almost every time.
# A new file may miss a few of those and still be the better presence test.
UNOCCUPIED_FLOOR = 0.90
# One extra occupied window is not a new model. 0.02 on this set is about
# 140 windows.
OCCUPIED_MARGIN = 0.02
BAR_BALANCED = 0.630
# Human motion and breathing, in hertz. A one-second window mainly resolves
# the upper part of this band. Slower change is the real-time slope.
MOTION_LO_HZ = 0.3
MOTION_HI_HZ = 3.0
_CLUMP_SEC = 1e-3
_EPS = 1e-6


def node_dim_for(version: int) -> int:
    ver = int(version)
    if ver >= 6:
        return _V6_NODE_DIM
    if ver >= 5:
        return _V5_NODE_DIM
    return _V4_NODE_DIM


def _hampel(x: np.ndarray, half: int = 3) -> np.ndarray:
    """Replace a tap that disagrees with its neighbors. A real shape is kept."""
    out = x.copy()
    n = x.size
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        neigh = x[lo:hi]
        med = float(np.median(neigh))
        mad = float(np.median(np.abs(neigh - med)))
        limit = _EPS if mad < _EPS else 3.0 * 1.4826 * mad
        if abs(float(x[i]) - med) > limit:
            out[i] = med
    return out


def _corr(a: np.ndarray, b: np.ndarray) -> float:
    if a.size < 4 or float(np.std(a)) < _EPS or float(np.std(b)) < _EPS:
        return 0.0
    value = float(np.corrcoef(a, b)[0, 1])
    if value != value:
        return 0.0
    return value


def _packet(iq) -> tuple[np.ndarray, np.ndarray] | None:
    """Gain-normalized amplitude and phase with the linear clock trend removed.

    Firmware order is imaginary, real. Raw phase is not returned.
    """
    vals = np.asarray(iq, dtype=np.float64)
    if vals.size != IQ_LEN:
        return None
    im = vals[0::2]
    re = vals[1::2]
    amp = np.hypot(im, re)
    if amp.shape != (N_SC,):
        return None
    cleaned = amp.copy()
    # The removed DC bin sits between the two halves. A spike filter must
    # not treat those neighbors as adjacent.
    cleaned[:HALF] = _hampel(amp[:HALF])
    cleaned[HALF:] = _hampel(amp[HALF:])
    scale = float(cleaned.mean())
    if scale < _EPS:
        return None
    amp = cleaned / scale

    raw = np.arctan2(im, re)
    # Each half is continuous. Join them across the missing DC bin so one
    # clock line fits both.
    y_lo = np.unwrap(raw[:HALF])
    y_hi = np.unwrap(raw[HALF:])
    step = float(y_lo[-1] - y_lo[-2]) if y_lo.size >= 2 else 0.0
    expected = float(y_lo[-1] + 2.0 * step)
    y_hi = y_hi + (2.0 * math.pi * round((expected - float(y_hi[0])) / (2.0 * math.pi)))
    y = np.concatenate([y_lo, y_hi])
    x = np.concatenate(
        [np.arange(HALF, dtype=np.float64), np.arange(HALF + 1, N_SC + 1, dtype=np.float64)]
    )
    phase = np.zeros(N_SC, dtype=np.float64)
    if float(np.std(y)) >= _EPS:
        trend = np.polyval(np.polyfit(x, y, 1), x)
        delta = y - trend
        phase = np.arctan2(np.sin(delta), np.cos(delta))
    return amp, phase


def _motion_terms(amps: np.ndarray) -> tuple[float, float, float]:
    """Variation across the second, motion, and the slope of total energy."""
    energy = amps.sum(axis=1)
    t = np.arange(amps.shape[0], dtype=np.float64)
    if amps.shape[0] > 1 and float(np.std(t)) > _EPS:
        energy_slope = float(np.polyfit(t, energy, 1)[0])
        motion = float(np.abs(np.diff(amps, axis=0)).mean())
    else:
        energy_slope = 0.0
        motion = 0.0
    variation = float(amps.std(axis=0).mean())
    return variation, motion, energy_slope


def node_features_v4(amps: np.ndarray, phases: np.ndarray) -> np.ndarray:
    """Version 4 window. The raw shape and sanitized phase stay in the vector."""
    mean = amps.mean(axis=0)
    std = amps.std(axis=0)
    delta = amps[-1] - amps[0]
    _variation, motion, energy_slope = _motion_terms(amps)
    adjacent = float(
        np.mean(
            [
                _corr(mean[:HALF][:-1], mean[:HALF][1:]),
                _corr(mean[HALF:][:-1], mean[HALF:][1:]),
            ]
        )
    )
    step = max(1, amps.shape[0] // 40)
    pairs = [
        _corr(amps[i], amps[i + step])
        for i in range(0, amps.shape[0] - step, step)
    ]
    temporal = float(np.mean(pairs)) if pairs else 0.0
    lo, hi = max(0, HALF - 8), min(N_SC, HALF + 8)
    edges = np.concatenate([mean[:8], mean[-8:]])
    band_ratio = float(mean[lo:hi].mean() / (float(edges.mean()) + _EPS))
    feat = np.concatenate(
        [
            mean,
            std,
            delta,
            phases.mean(axis=0),
            phases.std(axis=0),
            np.array(
                [energy_slope, motion, adjacent, temporal, band_ratio, 1.0],
                dtype=np.float64,
            ),
        ]
    )
    assert feat.shape == (_V4_NODE_DIM,)
    return feat


def node_features_v5(amps: np.ndarray, phases: np.ndarray) -> np.ndarray:
    """Version 5 window. Motion is the jump from one packet to the next."""
    del phases
    mean = amps.mean(axis=0)
    variation, motion, energy_slope = _motion_terms(amps)
    feat = np.concatenate(
        [
            mean,
            np.array([variation, motion, energy_slope, 1.0], dtype=np.float64),
        ]
    )
    assert feat.shape == (_V5_NODE_DIM,)
    return feat


def _collapse_clumps(
    times: np.ndarray, amps: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Average packets that share one commit timestamp.

    Postgres stamps every row in a transaction with the same time, so a burst
    is not a 100 Hz clock.
    """
    t = np.asarray(times, dtype=np.float64)
    a = np.asarray(amps, dtype=np.float64)
    if t.size == 0:
        return t, a
    order = np.argsort(t, kind="mergesort")
    t = t[order]
    a = a[order]
    kept_t: list[float] = []
    kept_a: list[np.ndarray] = []
    start = 0
    for i in range(1, int(t.size) + 1):
        if i == t.size or float(t[i] - t[start]) > _CLUMP_SEC:
            kept_t.append(float(np.mean(t[start:i])))
            kept_a.append(np.mean(a[start:i], axis=0))
            start = i
    return np.asarray(kept_t, dtype=np.float64), np.stack(kept_a, axis=0)


def _timed_motion(times: np.ndarray, amps: np.ndarray) -> tuple[float, float, float]:
    """Band power, speed, and energy slope on the real packet clock.

    The series is placed on a grid at the median gap. The band is 0.3–3 Hz.
    Speed is the average size of the energy change divided by the real gap.
    """
    if times.size < 8 or float(times[-1] - times[0]) < MIN_SPAN_SEC:
        return 0.0, 0.0, 0.0
    energy = amps.sum(axis=1)
    gaps = np.diff(times)
    gaps = gaps[gaps > _CLUMP_SEC]
    if gaps.size == 0:
        return 0.0, 0.0, 0.0
    dt = float(np.median(gaps))
    if dt <= _EPS:
        return 0.0, 0.0, 0.0
    speed = float(np.mean(np.abs(np.diff(energy)) / np.maximum(np.diff(times), _EPS)))
    slope = 0.0
    t0 = times - float(times[0])
    if float(np.std(t0)) > _EPS:
        slope = float(np.polyfit(t0, energy, 1)[0])
    span = float(times[-1] - times[0])
    n = int(math.floor(span / dt)) + 1
    if n < 8:
        return 0.0, speed, slope
    grid = float(times[0]) + np.arange(n, dtype=np.float64) * dt
    grid = grid[grid <= float(times[-1]) + 1e-9]
    if grid.size < 8:
        return 0.0, speed, slope
    series = np.interp(grid, times, energy)
    series = series - float(series.mean())
    spec = np.fft.rfft(series)
    freqs = np.fft.rfftfreq(series.size, d=dt)
    band = (freqs >= MOTION_LO_HZ) & (freqs <= MOTION_HI_HZ)
    if not np.any(band):
        return 0.0, speed, slope
    power = float(np.sum(np.abs(spec[band]) ** 2) / series.size)
    return power, speed, slope


def node_features(
    amps: np.ndarray, phases: np.ndarray, times: np.ndarray
) -> np.ndarray:
    """Version 6 window. Motion uses the real time between packets."""
    del phases
    collapsed_t, collapsed = _collapse_clumps(times, amps)
    mean = collapsed.mean(axis=0)
    variation = float(collapsed.std(axis=0).mean()) if collapsed.shape[0] else 0.0
    band, speed, slope = _timed_motion(collapsed_t, collapsed)
    feat = np.concatenate(
        [
            mean,
            np.array([variation, band, speed, slope, 1.0], dtype=np.float64),
        ]
    )
    assert feat.shape == (_V6_NODE_DIM,)
    return feat


def empty_baselines(
    x: np.ndarray, empty: np.ndarray, *, version: int = FEATURE_VERSION
) -> np.ndarray:
    """Median amplitude shape per receiver, from camera-empty windows only."""
    width = node_dim_for(version)
    present_at = width - 1
    bases = []
    for node in NODE_IDS:
        start = node * width
        present = x[:, start + present_at] > 0.5
        use = present & empty
        if not np.any(use):
            bases.append(np.ones(N_SC, dtype=np.float64))
        else:
            bases.append(np.median(x[use, start : start + N_SC], axis=0))
    return np.stack(bases)


def with_baseline(
    x: np.ndarray, bases: np.ndarray, *, version: int = FEATURE_VERSION
) -> np.ndarray:
    """Turn each receiver's amplitude mean into its change from the empty room.

    Version 5 replaces the mean, so the raw shape is not a model input.
    Version 4 keeps the old vector and appends the change.
    """
    width = node_dim_for(version)
    present_at = width - 1
    if int(version) >= 5:
        out = np.array(x, dtype=np.float64, copy=True)
    else:
        out = None
    deltas = []
    for node in NODE_IDS:
        start = node * width
        mean = x[:, start : start + N_SC]
        present = x[:, start + present_at] > 0.5
        delta = (mean - bases[node]) / (bases[node] + _EPS)
        delta[~present] = 0.0
        if out is not None:
            out[:, start : start + N_SC] = delta
        else:
            deltas.append(delta)
    if out is not None:
        return out
    return np.hstack([x, *deltas])


def refresh_visit_bases(rows, fallback, cache: dict[int, np.ndarray]) -> np.ndarray:
    """Remember the empty room from camera-empty packets and reuse it later.

    Occupied packets do not move the memory. A receiver with too few empty
    packets keeps the saved median.
    """
    buckets: dict[int, list[np.ndarray]] = {node: [] for node in NODE_IDS}
    for node, _ts, _rssi, iq, cv, _seq in rows:
        if cv is None or int(cv) != 0 or int(node) not in NODE_IDS:
            continue
        parsed = _packet(iq)
        if parsed is None:
            continue
        buckets[int(node)].append(parsed[0])
    for node, amps in buckets.items():
        if len(amps) >= MIN_LIVE_EMPTY_PACKETS:
            cache[node] = np.median(np.stack(amps, axis=0), axis=0)
    bases = np.array(fallback, dtype=np.float64, copy=True)
    for node, median in cache.items():
        bases[node] = median
    return bases


def _windows_for_node(
    packets: list[tuple[float, np.ndarray, np.ndarray, float, int]],
    *,
    feature_version: int = FEATURE_VERSION,
) -> dict[int, tuple[np.ndarray, int, float]]:
    """Map hop-bin -> (features, camera label, mean rssi). Packets are time-sorted."""
    if not packets:
        return {}
    t_min = packets[0][0]
    t_max = packets[-1][0]
    first = math.floor(t_min / HOP_SEC)
    last = math.floor((t_max - WINDOW_SEC) / HOP_SEC)
    out: dict[int, tuple[np.ndarray, int, float]] = {}
    i = 0
    j = 0
    n = len(packets)
    for b in range(first, last + 1):
        start = b * HOP_SEC
        end = start + WINDOW_SEC
        while i < n and packets[i][0] < start:
            i += 1
        if j < i:
            j = i
        while j < n and packets[j][0] < end:
            j += 1
        chunk = packets[i:j]
        if len(chunk) < MIN_PACKETS:
            continue
        times = [row[0] for row in chunk]
        span = times[-1] - times[0]
        if span < MIN_SPAN_SEC:
            continue
        gaps = [
            times[k + 1] - times[k]
            for k in range(len(times) - 1)
            if times[k + 1] - times[k] > 1e-3
        ]
        if gaps and max(gaps) > MAX_GAP_SEC:
            continue
        labels = {row[4] for row in chunk}
        if labels != {0} and labels != {1}:
            continue
        amps = np.stack([row[1] for row in chunk], axis=0)
        phases = np.stack([row[2] for row in chunk], axis=0)
        rssi = float(np.mean([row[3] for row in chunk]))
        ver = int(feature_version)
        if ver >= 6:
            feat = node_features(amps, phases, np.asarray(times, dtype=np.float64))
        elif ver >= 5:
            feat = node_features_v5(amps, phases)
        else:
            feat = node_features_v4(amps, phases)
        out[b] = (feat, next(iter(labels)), rssi)
    return out


def fuse_session(
    by_node: dict[int, list[tuple[float, np.ndarray, np.ndarray, float, int]]],
    *,
    min_nodes: int = 2,
    feature_version: int = FEATURE_VERSION,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Concatenate receivers that share a 1-second bin. Training needs two."""
    width = node_dim_for(feature_version)
    per_node = {
        node: _windows_for_node(rows, feature_version=feature_version)
        for node, rows in by_node.items()
    }
    bins: set[int] = set()
    for windows in per_node.values():
        bins.update(windows)
    xs: list[np.ndarray] = []
    ys: list[int] = []
    rssi: list[float] = []
    missing = np.zeros(width, dtype=np.float64)
    for b in sorted(bins):
        parts: list[np.ndarray] = []
        labels: set[int] = set()
        rssis: list[float] = []
        present = 0
        for node in NODE_IDS:
            hit = per_node.get(node, {}).get(b)
            if hit is None:
                parts.append(missing)
                continue
            feat, label, mean_rssi = hit
            parts.append(feat)
            labels.add(label)
            rssis.append(mean_rssi)
            present += 1
        if present < min_nodes or len(labels) != 1:
            continue
        xs.append(np.concatenate(parts))
        ys.append(next(iter(labels)))
        rssi.append(float(np.mean(rssis)))
    if not xs:
        return (
            np.zeros((0, width * len(NODE_IDS))),
            np.zeros(0, dtype=np.int64),
            np.zeros(0, dtype=np.float64),
        )
    return (
        np.vstack(xs),
        np.asarray(ys, dtype=np.int64),
        np.asarray(rssi, dtype=np.float64),
    )


def train_tag(tag: str | None) -> bool:
    """True when this run's name is empty_* or occupied_*. The name is not the label."""
    if tag is None:
        return False
    return tag.startswith("empty_") or tag.startswith("occupied_")


def load_sessions(
    url: str, tags: list[str] | None
) -> list[tuple[str, datetime, dict]]:
    import psycopg

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        if tags:
            skipped = [tag for tag in tags if not train_tag(tag)]
            for tag in skipped:
                print(f"skip {tag}  (name must be empty_* or occupied_*)")
            wanted = [tag for tag in tags if train_tag(tag)]
            if not wanted:
                return []
            cur.execute(
                """
                SELECT id, tag, started_at
                FROM room_presence_sessions
                WHERE tag = ANY(%s)
                  AND tag ~ '^(empty|occupied)_'
                ORDER BY started_at
                """,
                (wanted,),
            )
        else:
            cur.execute(
                """
                SELECT id, tag, started_at
                FROM room_presence_sessions
                WHERE tag ~ '^(empty|occupied)_'
                ORDER BY started_at
                """
            )
        sessions = cur.fetchall()
        loaded: list[tuple[str, datetime, dict]] = []  # name, started, packets
        for sid, tag, started in sessions:
            if not train_tag(tag):
                continue
            cur.execute(
                """
                SELECT node_id, host_ts, rssi, iq, cv_occupied
                FROM room_presence_csi
                WHERE session_id = %s AND cv_occupied IN (0, 1)
                ORDER BY node_id, host_ts, seq
                """,
                (sid,),
            )
            by_node: dict[int, list] = defaultdict(list)
            for node, host_ts, rssi, iq, cv in cur:
                if node not in NODE_IDS or iq is None:
                    continue
                parsed = _packet(iq)
                if parsed is None:
                    continue
                amp, phase = parsed
                by_node[int(node)].append(
                    (host_ts.timestamp(), amp, phase, float(rssi or 0), int(cv))
                )
            name = tag or started.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            loaded.append((name, started, by_node))
        counts: dict[str, int] = defaultdict(int)
        for name, _started, _rows in loaded:
            counts[name] += 1
        seen: dict[str, int] = defaultdict(int)
        unique: list[tuple[str, datetime, dict]] = []
        for name, started, rows in loaded:
            seen[name] += 1
            if counts[name] > 1:
                name = f"{name}#{seen[name]}"
            unique.append((name, started, rows))
        return unique


def _rssi_predict(rssi: np.ndarray, *, higher_is_occupied: bool, threshold: float) -> np.ndarray:
    if higher_is_occupied:
        return (rssi >= threshold).astype(np.int64)
    return (rssi <= threshold).astype(np.int64)


def _rssi_rule(rssi: np.ndarray, y: np.ndarray) -> tuple[bool, float] | None:
    """Threshold on window-mean RSSI fit to the training windows. RSSI stays out of the model."""
    if len(set(y.tolist())) < 2 or rssi.size < 10:
        return None
    best_bal = -1.0
    best: tuple[bool, float] | None = None
    for thr in np.quantile(rssi, np.linspace(0.05, 0.95, 19)):
        for higher in (True, False):
            pred = _rssi_predict(rssi, higher_is_occupied=higher, threshold=float(thr))
            bal = _balanced_accuracy(y, pred)
            if bal > best_bal:
                best_bal = bal
                best = (higher, float(thr))
    return best


def _balanced_accuracy(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    scores = []
    for label in (0, 1):
        mask = y_true == label
        if not np.any(mask):
            continue
        scores.append(float(np.mean(y_pred[mask] == label)))
    if not scores:
        return float("nan")
    return float(np.mean(scores))


def _fit(X: np.ndarray, y: np.ndarray, seed: int):
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    pipe = Pipeline(
        [
            ("scaler", StandardScaler()),
            (
                "clf",
                HistGradientBoostingClassifier(
                    max_depth=6,
                    learning_rate=0.08,
                    max_iter=200,
                    class_weight="balanced",
                    random_state=seed,
                ),
            ),
        ]
    )
    pipe.fit(X, y)
    return pipe


Block = tuple[str, np.ndarray, np.ndarray, np.ndarray]


def _recall(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    if y_true.size == 0:
        return float("nan")
    return float(np.mean(y_pred == y_true))


def _class_accuracy(y_true: np.ndarray, y_pred: np.ndarray, label: int) -> float:
    """Share of windows of this camera label that were predicted as that label."""
    mask = y_true == label
    if y_true.size == 0 or not np.any(mask):
        return float("nan")
    return float(np.mean(y_pred[mask] == label))


def _fmt_acc(value: float) -> str:
    if value != value:
        return "n/a"
    return f"{value:.3f}"


def _stored_acc(value: float) -> float | None:
    if value != value:
        return None
    return float(value)


def _cv_score(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """Camera-label score. Balanced accuracy when the session has both labels."""
    if len(set(y_true.tolist())) > 1:
        return _balanced_accuracy(y_true, y_pred)
    return _recall(y_true, y_pred)


def _leave_one_session_out(blocks: list[Block], seed: int) -> list[dict]:
    """Score each session with a model that has never seen it.

    y is cv_occupied. A held-out session can contain one camera label or both.
    """
    reports: list[dict] = []
    for i, (tag, x_te, y_te, rssi_te) in enumerate(blocks):
        labels = sorted({int(v) for v in y_te.tolist()})
        rest = blocks[:i] + blocks[i + 1 :]
        y_tr = np.concatenate([b[2] for b in rest])
        if len(set(y_tr.tolist())) < 2:
            print(f"loo {tag}  skipped  (the other sessions are one class)")
            reports.append({"tag": tag, "skipped": True, "labels": labels})
            continue
        x_tr = np.vstack([b[1] for b in rest])
        model = _fit(x_tr, y_tr, seed)
        pred = model.predict(x_te)
        score = _cv_score(y_te, pred)
        empty_acc = _class_accuracy(y_te, pred, 0)
        occupied_acc = _class_accuracy(y_te, pred, 1)
        rule = _rssi_rule(
            np.concatenate([b[3] for b in rest]),
            y_tr,
        )
        rssi_score = float("nan")
        if rule is not None:
            higher, thr = rule
            rssi_score = _cv_score(
                y_te,
                _rssi_predict(rssi_te, higher_is_occupied=higher, threshold=thr),
            )
        shown = "+".join("occupied" if v == 1 else "empty" for v in labels)
        print(
            f"loo {tag}  cv={shown}  windows={y_te.size}  "
            f"unoccupied={_fmt_acc(empty_acc)}  occupied={_fmt_acc(occupied_acc)}  "
            f"score={score:.3f}  rssi_only={rssi_score:.3f}"
        )
        reports.append(
            {
                "tag": tag,
                "skipped": False,
                "labels": labels,
                "windows": int(y_te.size),
                "recall": score,
                "unoccupied_accuracy": _stored_acc(empty_acc),
                "occupied_accuracy": _stored_acc(occupied_acc),
                "rssi_recall": rssi_score,
                "y_true": y_te,
                "y_pred": pred,
            }
        )
    return reports


def _negative_control(blocks: list[Block], seed: int) -> float | None:
    """Can the model tell two camera-empty stretches apart? A clean score is near 0.5."""
    empty = [b for b in blocks if len(set(b[2].tolist())) == 1 and int(b[2][0]) == 0]
    if len(empty) < 2:
        return None
    x_tr: list[np.ndarray] = []
    y_tr: list[np.ndarray] = []
    x_te: list[np.ndarray] = []
    y_te: list[np.ndarray] = []
    for i, (_tag, x, _y, _rssi) in enumerate(empty):
        cut = int(x.shape[0] * 0.7)
        if cut < 20 or x.shape[0] - cut < 10:
            continue
        pseudo = np.full(x.shape[0], i % 2, dtype=np.int64)
        x_tr.append(x[:cut])
        y_tr.append(pseudo[:cut])
        x_te.append(x[cut:])
        y_te.append(pseudo[cut:])
    if len(x_tr) < 2:
        return None
    ytr = np.concatenate(y_tr)
    yte = np.concatenate(y_te)
    if len(set(ytr.tolist())) < 2 or len(set(yte.tolist())) < 2:
        return None
    model = _fit(np.vstack(x_tr), ytr, seed)
    return _balanced_accuracy(yte, model.predict(np.vstack(x_te)))


def _tested_both(loo: object) -> bool:
    """True when some held-out fold contained empty and some contained occupied."""
    if not isinstance(loo, list):
        return False
    labels: set[int] = set()
    for row in loo:
        if not isinstance(row, dict) or row.get("skipped"):
            continue
        for lab in row.get("labels") or []:
            try:
                labels.add(int(lab))
            except (TypeError, ValueError):
                continue
    return 0 in labels and 1 in labels


def _finite(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _previous(path: Path) -> dict | None:
    """The file live is using, including a recipe this code no longer trains."""
    if not path.is_file():
        return None
    import joblib

    try:
        previous = joblib.load(path)
    except Exception:
        return None
    if not isinstance(previous, dict) or previous.get("role") != "best":
        return None
    score = _finite(previous.get("best_score"))
    if score is None:
        score = _finite(previous.get("loo_mean_recall"))
    return {
        "score": score,
        "tested_both": _tested_both(previous.get("loo")),
        "unoccupied": _finite(previous.get("unoccupied_accuracy")),
        "occupied": _finite(previous.get("occupied_accuracy")),
        "balanced": _finite(previous.get("balanced_accuracy")),
    }


def _save_best(
    path: Path,
    bundle: dict,
    score: float,
    trustworthy: bool,
    tested_both: bool,
    unoccupied: float,
    occupied: float,
    balanced: float,
    neg: float | None,
) -> None:
    """Replace the live file only when this train is the better presence test.

    Both labels must have been held out. Empty recordings must not be separable
    from each other. Occupied accuracy must beat the saved file by a clear
    margin, unoccupied accuracy must stay high, and balanced accuracy must rise.
    """
    previous = _previous(path)
    shown = f"{score:.3f}" if score == score else "n/a"
    reasons: list[str] = []
    if not tested_both:
        reasons.append("this train did not test both labels")
    neg_value = _finite(neg)
    if neg_value is None or not (NEG_CONTROL_LO <= neg_value <= NEG_CONTROL_HI):
        shown_neg = "n/a" if neg_value is None else f"{neg_value:.3f}"
        reasons.append(f"negative_control={shown_neg}  want about 0.5")
    bar_occupied = BAR_OCCUPIED
    bar_balanced = BAR_BALANCED
    if previous is not None and previous["occupied"] is not None:
        bar_occupied = previous["occupied"]
    if previous is not None and previous["balanced"] is not None:
        bar_balanced = previous["balanced"]
    empty_now = _finite(unoccupied)
    occupied_now = _finite(occupied)
    balanced_now = _finite(balanced)
    occupied_need = bar_occupied + OCCUPIED_MARGIN
    if empty_now is None or empty_now < UNOCCUPIED_FLOOR:
        reasons.append(
            f"unoccupied={_fmt_acc(unoccupied)} fell below {UNOCCUPIED_FLOOR:.2f}"
        )
    if occupied_now is None or occupied_now <= occupied_need:
        reasons.append(
            f"occupied={_fmt_acc(occupied)} did not beat {occupied_need:.3f}"
        )
    if balanced_now is None or balanced_now <= bar_balanced:
        reasons.append(
            f"balanced={_fmt_acc(balanced)} did not beat {bar_balanced:.3f}"
        )
    if reasons:
        print("kept previous best  " + "  ".join(reasons))
        if path.is_file():
            print(f"live inference still uses {path}")
        else:
            print("no model saved")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    import joblib

    joblib.dump(bundle, path)
    if previous is None or previous["score"] is None:
        print(f"saved {path}  score={shown}  evaluation_trustworthy={trustworthy}")
    else:
        print(
            f"saved {path}  score={shown}  previous={previous['score']:.3f}  "
            f"evaluation_trustworthy={trustworthy}"
        )
    print("live inference uses this file")


def train(
    url: str,
    holdout: str | None,
    seed: int,
    tags: list[str] | None,
) -> int:
    loaded = load_sessions(url, tags)
    if not loaded:
        print("no empty_* or occupied_* sessions in postgres")
        return 1

    print(
        "feature recipe 6  motion from real packet time, variation, "
        "delta from this visit's empty room"
    )
    raw: list[tuple[str, datetime | None, np.ndarray, np.ndarray, np.ndarray]] = []
    print("sessions")
    for tag, started, by_node in loaded:
        x, y, rssi = fuse_session(by_node)
        n0 = int(np.sum(y == 0)) if y.size else 0
        n1 = int(np.sum(y == 1)) if y.size else 0
        print(f"  {tag}  windows={y.size}  empty={n0}  occupied={n1}")
        if y.size:
            raw.append((tag, started, x, y, rssi))

    if not raw:
        print("no usable 1-second windows (need cv label 0 or 1 and two receivers)")
        return 1

    donors: list[tuple[float, str, np.ndarray]] = []
    for tag, started, x, y, _rssi in raw:
        if int(np.sum(y == 0)) < MIN_VISIT_EMPTY:
            continue
        when = started.timestamp() if started is not None else 0.0
        donors.append((when, tag, empty_baselines(x, y == 0)))
    blocks = []
    for tag, started, x, y, rssi in raw:
        own = int(np.sum(y == 0)) >= MIN_VISIT_EMPTY
        if own:
            bases = empty_baselines(x, y == 0)
        elif donors:
            when = started.timestamp() if started is not None else 0.0
            _stamp, donor, bases = min(donors, key=lambda item: abs(item[0] - when))
            print(f"  {tag}  empty room from {donor}")
        else:
            bases = empty_baselines(x, y == 0)
        blocks.append((tag, with_baseline(x, bases), y, rssi))
    saved_bases = empty_baselines(
        np.vstack([item[2] for item in raw]),
        np.concatenate([item[3] for item in raw]) == 0,
    )

    if not blocks:
        print("no usable 1-second windows (need cv label 0 or 1 and two receivers)")
        return 1

    y_all = np.concatenate([b[2] for b in blocks])
    n_empty = int(np.sum(y_all == 0))
    n_occ = int(np.sum(y_all == 1))
    if n_empty == 0 or n_occ == 0:
        have = "empty" if n_occ == 0 else "occupied"
        print(f"only {have} windows so far. Collect the other class, then run this again.")
        return 1
    empty_share = n_empty / max(n_empty + n_occ, 1)
    if empty_share < 0.3 or empty_share > 0.7:
        print(f"class balance empty_share={empty_share:.2f}  (aim about 0.4–0.6)")

    exam: Block | None = None
    fit_blocks = blocks
    if holdout is not None:
        matched = [b for b in blocks if b[0] == holdout]
        if not matched:
            print(f"holdout {holdout!r} not in {[b[0] for b in blocks]}")
            return 1
        exam = matched[0]
        fit_blocks = [b for b in blocks if b[0] != holdout]
        y_fit = np.concatenate([b[2] for b in fit_blocks])
        if len(set(y_fit.tolist())) < 2:
            print(
                f"training sessions have one class after holding out {holdout}. "
                "Hold out a session only when other empty_ and occupied_ sessions remain."
            )
            return 1

    reports = _leave_one_session_out(fit_blocks, seed)
    scored = [r for r in reports if not r.get("skipped")]
    tested_empty = any(0 in r["labels"] for r in scored)
    tested_occupied = any(1 in r["labels"] for r in scored)
    model_recalls: list[float] = []
    weights: list[int] = []
    rssi_pairs: list[tuple[float, int]] = []
    for row in scored:
        weight = int(row.get("windows") or 0)
        recall = row["recall"]
        if weight <= 0 or recall != recall:
            continue
        model_recalls.append(recall)
        weights.append(weight)
        rssi_recall = row["rssi_recall"]
        if rssi_recall == rssi_recall:
            rssi_pairs.append((rssi_recall, weight))
    mean_recall = (
        float(np.average(model_recalls, weights=weights)) if weights else float("nan")
    )
    mean_rssi = (
        float(np.average([pair[0] for pair in rssi_pairs], weights=[pair[1] for pair in rssi_pairs]))
        if rssi_pairs
        else float("nan")
    )
    rssi_recalls = [pair[0] for pair in rssi_pairs]
    if model_recalls:
        print(f"loo_mean_recall={mean_recall:.3f}  rssi_only={mean_rssi:.3f}  (weighted by windows)")
    confusion = None
    unoccupied_accuracy = float("nan")
    occupied_accuracy = float("nan")
    balanced_accuracy = float("nan")
    pooled_true = [r["y_true"] for r in scored if "y_true" in r]
    if pooled_true:
        from sklearn.metrics import (
            accuracy_score,
            balanced_accuracy_score,
            classification_report,
            confusion_matrix,
        )

        y_true = np.concatenate(pooled_true)
        y_pred = np.concatenate([r["y_pred"] for r in scored if "y_pred" in r])
        unoccupied_accuracy = _class_accuracy(y_true, y_pred, 0)
        occupied_accuracy = _class_accuracy(y_true, y_pred, 1)
        balanced_accuracy = float(balanced_accuracy_score(y_true, y_pred))
        confusion = confusion_matrix(y_true, y_pred, labels=[0, 1]).tolist()
        print(
            f"unoccupied_accuracy={_fmt_acc(unoccupied_accuracy)}  "
            f"windows={int(np.sum(y_true == 0))}  "
            f"occupied_accuracy={_fmt_acc(occupied_accuracy)}  "
            f"windows={int(np.sum(y_true == 1))}"
        )
        print(
            f"accuracy={accuracy_score(y_true, y_pred):.3f}  "
            f"balanced_accuracy={balanced_accuracy:.3f}"
        )
        print("confusion matrix  rows=true camera label, cols=predicted  [unoccupied, occupied]")
        print(f"  {confusion}")
        print(
            classification_report(
                y_true,
                y_pred,
                labels=[0, 1],
                target_names=["unoccupied", "occupied"],
                zero_division=0,
            )
        )
    else:
        print("unoccupied_accuracy=n/a  windows=0  occupied_accuracy=n/a  windows=0")
    if not tested_empty or not tested_occupied:
        print(
            "leave-one-session-out has not seen both camera labels. "
            "Collect another session that contains the missing label."
        )

    neg = _negative_control(fit_blocks, seed)
    if neg is not None:
        print(f"negative_control={neg:.3f}  (empty versus empty; want about 0.5)")

    x_fit = np.vstack([b[1] for b in fit_blocks])
    y_fit = np.concatenate([b[2] for b in fit_blocks])
    model = _fit(x_fit, y_fit, seed)

    exam_recall = None
    if exam is not None:
        exam_pred = model.predict(exam[1])
        exam_recall = _cv_score(exam[2], exam_pred)
        exam_empty = _class_accuracy(exam[2], exam_pred, 0)
        exam_occupied = _class_accuracy(exam[2], exam_pred, 1)
        shown = "+".join(
            "occupied" if v == 1 else "unoccupied"
            for v in sorted({int(v) for v in exam[2].tolist()})
        )
        print(
            f"holdout {exam[0]}  cv={shown}  windows={exam[2].size}  "
            f"unoccupied={_fmt_acc(exam_empty)}  occupied={_fmt_acc(exam_occupied)}  "
            f"score={exam_recall:.3f}"
        )

    beats_rssi = (not rssi_recalls) or mean_recall > mean_rssi + 1e-6
    trustworthy = bool(tested_empty and tested_occupied and beats_rssi and (neg is None or neg < 0.7))
    if neg is not None and neg >= 0.7:
        print("negative control is high: empty sessions differ from each other")
    if rssi_recalls and not beats_rssi:
        print("model did not beat RSSI alone")
    print(f"evaluation_trustworthy={trustworthy}")
    bundle = {
        "model": model,
        "role": "best",
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "holdout": holdout,
        "train_tags": [b[0] for b in fit_blocks],
        "window_sec": WINDOW_SEC,
        "hop_sec": HOP_SEC,
        "node_ids": NODE_IDS,
        "node_dim": NODE_DIM,
        "feature_version": FEATURE_VERSION,
        "baselines": saved_bases.tolist(),
        "iq_len": IQ_LEN,
        "min_packets": MIN_PACKETS,
        "max_gap_sec": MAX_GAP_SEC,
        "evaluation_trustworthy": trustworthy,
        "loo": [
            {k: v for k, v in r.items() if k not in ("y_true", "y_pred")}
            for r in reports
        ],
        "confusion_matrix": confusion,
        "loo_mean_recall": mean_recall,
        "best_score": mean_recall,
        "unoccupied_accuracy": _stored_acc(unoccupied_accuracy),
        "occupied_accuracy": _stored_acc(occupied_accuracy),
        "balanced_accuracy": _stored_acc(balanced_accuracy),
        "tested_both": bool(tested_empty and tested_occupied),
        "rssi_only": mean_rssi,
        "negative_control": neg,
        "holdout_recall": exam_recall,
        "n_empty": n_empty,
        "n_occupied": n_occ,
    }
    _save_best(
        DEFAULT_MODEL,
        bundle,
        mean_recall,
        trustworthy,
        bool(tested_empty and tested_occupied),
        unoccupied_accuracy,
        occupied_accuracy,
        balanced_accuracy,
        neg,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Train room empty/occupied offline")
    p.add_argument("--database-url", default=DEFAULT_DB)
    p.add_argument(
        "--session",
        action="append",
        default=None,
        help="empty_* or occupied_* tag to include (repeat for each). Default: all of those.",
    )
    p.add_argument(
        "--holdout",
        default=None,
        help="session tag kept out of the saved model and scored once",
    )
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args(argv)
    return train(args.database_url, args.holdout, args.seed, args.session)


if __name__ == "__main__":
    raise SystemExit(main())
