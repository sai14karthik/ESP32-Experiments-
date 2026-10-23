#!/usr/bin/env python3
"""Train empty/occupied from finished room_presence_csi sessions.

Run this after collection, on the Mini, with the collector stopped:

  ./tools/train_room.sh
  ./tools/train_room.sh --holdout occupied_02

Session tags named empty_* or occupied_* choose which runs to load.
pipeline_test and every other tag are ignored. The label on each row is the
camera value cv_occupied, not the session name.

Each window carries gain-normalized amplitude, sanitized phase, how far the
amplitude shape sits from the camera-empty median, and the short correlation
and slope terms from csi_pipeline_new version 4. Raw phase and RSSI are not
features. The score is leave-one-session-out. Every finished fit is written to
tools/models/room_presence.joblib and that file is what live inference loads.
The trustworthy flag is only a warning. A later train replaces the file.
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
# Per receiver, before the empty-room delta is appended:
# amp mean, amp std, amp end-start, sanitized phase mean, sanitized phase std,
# then energy slope, motion, adjacent-tap correlation, frame correlation,
# center-band ratio, and a present flag.
_SCALARS = 6
NODE_DIM = N_SC * 5 + _SCALARS
PRESENT_AT = NODE_DIM - 1
FEATURE_VERSION = 4
_EPS = 1e-6


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


def node_features(amps: np.ndarray, phases: np.ndarray) -> np.ndarray:
    """One receiver, one second. Baseline delta is added later, from empty windows only."""
    mean = amps.mean(axis=0)
    std = amps.std(axis=0)
    delta = amps[-1] - amps[0]
    energy = amps.sum(axis=1)
    t = np.arange(amps.shape[0], dtype=np.float64)
    if amps.shape[0] > 1 and float(np.std(t)) > _EPS:
        energy_slope = float(np.polyfit(t, energy, 1)[0])
        motion = float(np.abs(np.diff(amps, axis=0)).mean())
    else:
        energy_slope = 0.0
        motion = 0.0
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
    assert feat.shape == (NODE_DIM,)
    return feat


def empty_baselines(x: np.ndarray, empty: np.ndarray) -> np.ndarray:
    """Median amplitude shape per receiver, from camera-empty windows only."""
    bases = []
    for node in NODE_IDS:
        start = node * NODE_DIM
        present = x[:, start + PRESENT_AT] > 0.5
        use = present & empty
        if not np.any(use):
            bases.append(np.ones(N_SC, dtype=np.float64))
        else:
            bases.append(np.median(x[use, start : start + N_SC], axis=0))
    return np.stack(bases)


def with_baseline(x: np.ndarray, bases: np.ndarray) -> np.ndarray:
    """Append how far each receiver's amplitude shape sits from the empty room."""
    deltas = []
    for node in NODE_IDS:
        start = node * NODE_DIM
        mean = x[:, start : start + N_SC]
        present = x[:, start + PRESENT_AT] > 0.5
        delta = (mean - bases[node]) / (bases[node] + _EPS)
        delta[~present] = 0.0
        deltas.append(delta)
    return np.hstack([x, *deltas])


def _windows_for_node(
    packets: list[tuple[float, np.ndarray, np.ndarray, float, int]],
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
        out[b] = (node_features(amps, phases), next(iter(labels)), rssi)
    return out


def fuse_session(
    by_node: dict[int, list[tuple[float, np.ndarray, np.ndarray, float, int]]],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Concatenate receivers that share a 1-second bin. Need at least two."""
    per_node = {node: _windows_for_node(rows) for node, rows in by_node.items()}
    bins: set[int] = set()
    for windows in per_node.values():
        bins.update(windows)
    xs: list[np.ndarray] = []
    ys: list[int] = []
    rssi: list[float] = []
    missing = np.zeros(NODE_DIM, dtype=np.float64)
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
        if present < 2 or len(labels) != 1:
            continue
        xs.append(np.concatenate(parts))
        ys.append(next(iter(labels)))
        rssi.append(float(np.mean(rssis)))
    if not xs:
        return (
            np.zeros((0, NODE_DIM * len(NODE_IDS))),
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


def load_sessions(url: str, tags: list[str] | None) -> list[tuple[str, dict]]:
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
        loaded: list[tuple[str, datetime, dict]] = []
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
        unique: list[tuple[str, dict]] = []
        for name, _started, rows in loaded:
            seen[name] += 1
            if counts[name] > 1:
                name = f"{name}#{seen[name]}"
            unique.append((name, rows))
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
        bases = empty_baselines(x_tr, y_tr == 0)
        model = _fit(with_baseline(x_tr, bases), y_tr, seed)
        pred = model.predict(with_baseline(x_te, bases))
        score = _cv_score(y_te, pred)
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
            f"score={score:.3f}  rssi_only={rssi_score:.3f}"
        )
        reports.append(
            {
                "tag": tag,
                "skipped": False,
                "labels": labels,
                "windows": int(y_te.size),
                "recall": score,
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
    xtr = np.vstack(x_tr)
    bases = empty_baselines(xtr, np.ones(xtr.shape[0], dtype=bool))
    model = _fit(with_baseline(xtr, bases), ytr, seed)
    return _balanced_accuracy(yte, model.predict(with_baseline(np.vstack(x_te), bases)))


def _save_best(path: Path, bundle: dict, score: float, trustworthy: bool) -> None:
    """Write the one file live inference loads. The latest fit replaces it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    import joblib

    joblib.dump(bundle, path)
    shown = f"{score:.3f}" if score == score else "n/a"
    print(f"saved {path}  score={shown}  evaluation_trustworthy={trustworthy}")
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

    blocks: list[Block] = []
    print("sessions")
    for tag, by_node in loaded:
        x, y, rssi = fuse_session(by_node)
        n0 = int(np.sum(y == 0)) if y.size else 0
        n1 = int(np.sum(y == 1)) if y.size else 0
        print(f"  {tag}  windows={y.size}  empty={n0}  occupied={n1}")
        if y.size:
            blocks.append((tag, x, y, rssi))

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
    model_recalls = [r["recall"] for r in scored]
    rssi_recalls = [r["rssi_recall"] for r in scored if r["rssi_recall"] == r["rssi_recall"]]
    mean_recall = float(np.mean(model_recalls)) if model_recalls else float("nan")
    mean_rssi = float(np.mean(rssi_recalls)) if rssi_recalls else float("nan")
    if model_recalls:
        print(f"loo_mean_recall={mean_recall:.3f}  rssi_only={mean_rssi:.3f}")
    confusion = None
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
        confusion = confusion_matrix(y_true, y_pred, labels=[0, 1]).tolist()
        print(
            f"accuracy={accuracy_score(y_true, y_pred):.3f}  "
            f"balanced_accuracy={balanced_accuracy_score(y_true, y_pred):.3f}"
        )
        print("confusion matrix  rows=true camera label, cols=predicted  [empty, occupied]")
        print(f"  {confusion}")
        print(
            classification_report(
                y_true,
                y_pred,
                labels=[0, 1],
                target_names=["empty", "occupied"],
                zero_division=0,
            )
        )
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
    bases = empty_baselines(x_fit, y_fit == 0)
    model = _fit(with_baseline(x_fit, bases), y_fit, seed)

    exam_recall = None
    if exam is not None:
        exam_pred = model.predict(with_baseline(exam[1], bases))
        exam_recall = _cv_score(exam[2], exam_pred)
        shown = "+".join(
            "occupied" if v == 1 else "empty"
            for v in sorted({int(v) for v in exam[2].tolist()})
        )
        print(f"holdout {exam[0]}  cv={shown}  windows={exam[2].size}  score={exam_recall:.3f}")

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
        "baselines": bases.tolist(),
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
        "rssi_only": mean_rssi,
        "negative_control": neg,
        "holdout_recall": exam_recall,
        "n_empty": n_empty,
        "n_occupied": n_occ,
    }
    _save_best(DEFAULT_MODEL, bundle, mean_recall, trustworthy)
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
