#!/usr/bin/env python3
"""Train empty/occupied from finished room_presence_csi sessions.

Run this after collection, on the Mini, with the collector stopped:

  ./tools/train_room.sh
  ./tools/train_room.sh --holdout empty_02

Rows are already labeled by the camera (cv_occupied 0 or 1). This script
groups them into 1-second windows, concatenates the three receivers, and
fits one model. It does not listen on :9057 and it does not update itself
while a session is still being recorded.
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
DEAD_BIN = N_SC // 2
WINDOW_SEC = 1.0
HOP_SEC = 0.25
MIN_PACKETS = 30
NODE_IDS = (0, 1, 2)
# mean[56] + std[56] + motion + present flag
NODE_DIM = N_SC + N_SC + 2


def _amp(iq) -> np.ndarray | None:
    vals = np.asarray(iq, dtype=np.float64)
    if vals.size != IQ_LEN:
        return None
    amp = np.hypot(vals[0::2], vals[1::2])
    if amp.shape != (N_SC,):
        return None
    active = np.ones(N_SC, dtype=bool)
    active[DEAD_BIN] = False
    scale = float(amp[active].mean())
    if scale < 1e-9:
        return None
    amp = amp / scale
    amp[~active] = 0.0
    return amp


def node_features(amps: np.ndarray) -> np.ndarray:
    """One receiver, one second. amps shape (packets, 56)."""
    mean = amps.mean(axis=0)
    std = amps.std(axis=0)
    motion = float(np.abs(np.diff(amps, axis=0)).mean()) if amps.shape[0] > 1 else 0.0
    feat = np.concatenate(
        [mean, std, np.array([motion, 1.0], dtype=np.float64)]
    )
    assert feat.shape == (NODE_DIM,)
    return feat


def _windows_for_node(
    packets: list[tuple[float, np.ndarray, float, int]],
) -> dict[int, tuple[np.ndarray, int, float]]:
    """Map hop-bin -> (features, label, mean rssi). Packets are time-sorted."""
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
        labels = {row[3] for row in chunk}
        if labels != {0} and labels != {1}:
            continue
        amps = np.stack([row[1] for row in chunk], axis=0)
        rssi = float(np.mean([row[2] for row in chunk]))
        out[b] = (node_features(amps), next(iter(labels)), rssi)
    return out


def fuse_session(
    by_node: dict[int, list[tuple[float, np.ndarray, float, int]]],
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


def load_sessions(url: str, tags: list[str] | None) -> list[tuple[str, dict]]:
    import psycopg

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        if tags:
            cur.execute(
                """
                SELECT id, tag, started_at
                FROM room_presence_sessions
                WHERE tag = ANY(%s)
                ORDER BY started_at
                """,
                (tags,),
            )
        else:
            cur.execute(
                """
                SELECT id, tag, started_at
                FROM room_presence_sessions
                ORDER BY started_at
                """
            )
        sessions = cur.fetchall()
        loaded: list[tuple[str, dict]] = []
        for sid, tag, started in sessions:
            cur.execute(
                """
                SELECT node_id, host_ts, rssi, iq, cv_occupied
                FROM room_presence_csi
                WHERE session_id = %s AND cv_occupied IS NOT NULL
                ORDER BY node_id, host_ts
                """,
                (sid,),
            )
            by_node: dict[int, list] = defaultdict(list)
            for node, host_ts, rssi, iq, cv in cur:
                if node not in NODE_IDS or iq is None:
                    continue
                amp = _amp(iq)
                if amp is None:
                    continue
                by_node[int(node)].append(
                    (host_ts.timestamp(), amp, float(rssi or 0), int(cv))
                )
            name = tag or started.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            loaded.append((name, by_node))
    return loaded


def _rssi_baseline(rssi: np.ndarray, y: np.ndarray) -> float:
    """Balanced accuracy of a single threshold on window-mean RSSI."""
    if len(set(y.tolist())) < 2 or rssi.size < 10:
        return float("nan")
    best = 0.5
    for thr in np.quantile(rssi, np.linspace(0.05, 0.95, 19)):
        pred = (rssi >= thr).astype(np.int64)
        best = max(best, _balanced_accuracy(y, pred))
        pred = (rssi <= thr).astype(np.int64)
        best = max(best, _balanced_accuracy(y, pred))
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


def train(
    url: str,
    model_path: Path,
    holdout: str | None,
    seed: int,
    tags: list[str] | None,
) -> int:
    loaded = load_sessions(url, tags)
    if not loaded:
        print("no room_presence sessions in postgres")
        return 1

    blocks: list[tuple[str, np.ndarray, np.ndarray, np.ndarray]] = []
    print("sessions")
    for tag, by_node in loaded:
        X, y, rssi = fuse_session(by_node)
        n0 = int(np.sum(y == 0)) if y.size else 0
        n1 = int(np.sum(y == 1)) if y.size else 0
        print(f"  {tag}  windows={y.size}  empty={n0}  occupied={n1}")
        if y.size:
            blocks.append((tag, X, y, rssi))

    if not blocks:
        print("no usable 1-second windows (need cv label 0 or 1 and two receivers)")
        return 1

    y_all = np.concatenate([b[2] for b in blocks])
    if len(set(y_all.tolist())) < 2:
        have = "empty" if int(y_all[0]) == 0 else "occupied"
        print(
            f"only {have} windows so far. "
            "Collect the other class, then run this again."
        )
        return 1
    if len(blocks) < 2:
        print("only one session. Collect a second session before training.")
        return 1

    tags = [b[0] for b in blocks]
    if holdout is None:
        holdout = tags[-1]
    if holdout not in tags:
        print(f"holdout {holdout!r} not in {tags}")
        return 1

    train_blocks = [b for b in blocks if b[0] != holdout]
    test_blocks = [b for b in blocks if b[0] == holdout]
    Xtr = np.vstack([b[1] for b in train_blocks])
    ytr = np.concatenate([b[2] for b in train_blocks])
    Xte = np.vstack([b[1] for b in test_blocks])
    yte = np.concatenate([b[2] for b in test_blocks])
    if len(set(ytr.tolist())) < 2:
        print(
            f"training sessions have one class after holding out {holdout}. "
            "Pick another --holdout, or collect both classes in the training sessions."
        )
        return 1

    model = _fit(Xtr, ytr, seed)
    pred = model.predict(Xte)
    bal = _balanced_accuracy(yte, pred)
    rssi_te = np.concatenate([b[3] for b in test_blocks])
    rssi_score = _rssi_baseline(rssi_te, yte)
    print(
        f"holdout {holdout}  windows={yte.size}  "
        f"balanced_accuracy={bal:.3f}  rssi_only={rssi_score:.3f}"
    )
    if yte.min() == yte.max():
        print(f"holdout {holdout} is only one class, so this score is incomplete")
    elif bal <= rssi_score + 1e-6:
        print("model did not beat RSSI alone on the holdout")

    model_path.parent.mkdir(parents=True, exist_ok=True)
    import joblib

    joblib.dump(
        {
            "model": model,
            "trained_at": datetime.now(timezone.utc).isoformat(),
            "holdout": holdout,
            "train_tags": [b[0] for b in train_blocks],
            "window_sec": WINDOW_SEC,
            "hop_sec": HOP_SEC,
            "node_ids": NODE_IDS,
            "node_dim": NODE_DIM,
            "iq_len": IQ_LEN,
            "holdout_balanced_accuracy": bal,
            "holdout_rssi_only": rssi_score,
        },
        model_path,
    )
    print(f"saved {model_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Train room empty/occupied offline")
    p.add_argument("--database-url", default=DEFAULT_DB)
    p.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    p.add_argument(
        "--session",
        action="append",
        default=None,
        help="session tag to include (repeat for each). Default: every stored session.",
    )
    p.add_argument(
        "--holdout",
        default=None,
        help="session tag left out of training (default: the latest session)",
    )
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args(argv)
    return train(args.database_url, args.model, args.holdout, args.seed, args.session)


if __name__ == "__main__":
    raise SystemExit(main())
