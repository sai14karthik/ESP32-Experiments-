#!/usr/bin/env python3
"""Train empty vs object classifier — v4 features, leakage baselines, grouped CV.

Reporting hierarchy, strongest evidence first:

  A. session-grouped CV   — the only estimate that generalizes to a new capture.
                            Needs >=2 sessions per class; refuses to invent a
                            number when the dataset can't support one. When
                            available it also selects the model and threshold.
  B. time-block CV        — leave-one-time-chunk-out within sessions. Still
                            confounded whenever label and session coincide.
  C. temporal hold-out    — last N% of each session. Most optimistic; drives
                            selection only as a fallback when A is impossible.
  D. negative control     — empty vs. the same empty room later. 0.5 is clean.

Every run also prints what a single metadata feature (RSSI / AGC / FFT gain)
scores on its own. A CSI model that does not clear that line is not using the
channel.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    balanced_accuracy_score,
    classification_report,
    confusion_matrix,
    f1_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import (
    LeaveOneGroupOut,
    StratifiedGroupKFold,
    train_test_split,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

try:
    from xgboost import XGBClassifier
except ImportError:  # optional until uv sync --group csi
    XGBClassifier = None  # type: ignore[misc, assignment]

try:
    from lightgbm import LGBMClassifier
except ImportError:
    LGBMClassifier = None  # type: ignore[misc, assignment]

try:
    from catboost import CatBoostClassifier
except ImportError:
    CatBoostClassifier = None  # type: ignore[misc, assignment]

from csi_features import (
    FEATURE_VERSION,
    LABEL_EMPTY,
    LABEL_OBJECT,
    FeatureConfig,
    PacketRecord,
    WindowSpec,
    compute_baseline_phase_profile,
    compute_baseline_profile,
    feature_dim,
    iq_list_to_packet,
    parse_optional_float,
    window_is_contiguous,
    window_to_features,
)

MIN_LABEL_FRACTION = 0.9
META_NAMES = ("mean RSSI", "mean AGC gain", "mean FFT gain")


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------


def _parse_host_ts(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.strip()).timestamp()
    except ValueError:
        return None


def derive_session_keys(session_labels: list[str]) -> list[str]:
    """Assign a unique key per contiguous label run (when CSV has no session_id)."""
    keys: list[str] = []
    run = -1
    prev_label: str | None = None
    for label in session_labels:
        if label != prev_label:
            run += 1
            prev_label = label
        keys.append(f"{label}#{run}")
    return keys


def load_packets(
    csv_path: Path,
    *,
    config: FeatureConfig | None = None,
) -> tuple[list[PacketRecord], list[int], list[str], list[str]]:
    config = config or FeatureConfig()
    packets: list[PacketRecord] = []
    y: list[int] = []
    session_labels: list[str] = []
    session_keys: list[str] = []

    with csv_path.open(newline="") as f:
        reader = csv.DictReader(f)
        has_session_id = reader.fieldnames is not None and "session_id" in reader.fieldnames
        run = -1
        prev_label: str | None = None
        for row in reader:
            lab_raw = row["label"].strip()
            lab = lab_raw.lower()
            if "object" in lab:
                y.append(LABEL_OBJECT)
            elif "baseline" in lab or "empty" in lab:
                y.append(LABEL_EMPTY)
            else:
                print(f"skip unknown label: {row['label']!r}", file=sys.stderr)
                continue
            try:
                seq_raw = (row.get("seq") or "").strip()
                packets.append(
                    iq_list_to_packet(
                        [int(x) for x in row["iq"].strip("{}").split(",") if x.strip()],
                        rssi=parse_optional_float(row.get("rssi")),
                        agc_gain=parse_optional_float(row.get("agc_gain")),
                        fft_gain=parse_optional_float(row.get("fft_gain")),
                        seq=int(seq_raw) if seq_raw.lstrip("-").isdigit() else None,
                        host_ts=_parse_host_ts(row.get("host_ts")),
                        normalize_gain=config.normalize_gain,
                    )
                )
            except ValueError as exc:
                print(f"skip bad iq row: {exc}", file=sys.stderr)
                y.pop()
                continue
            session_labels.append(lab_raw)
            if has_session_id and row.get("session_id", "").strip():
                session_keys.append(row["session_id"].strip())
            else:
                if lab_raw != prev_label:
                    run += 1
                    prev_label = lab_raw
                session_keys.append(f"{lab_raw}#{run}")

    if not packets:
        sys.exit(f"No rows loaded from {csv_path}")
    return packets, y, session_labels, session_keys


# --------------------------------------------------------------------------
# windowing
# --------------------------------------------------------------------------


@dataclass
class WindowSet:
    X: np.ndarray
    y: np.ndarray
    groups: list[str]
    # Window-mean RSSI / AGC / FFT gain. Diagnostics only — these are what the
    # leakage baseline is computed from, and they must never enter X unless the
    # operator explicitly passes --use-meta.
    meta: np.ndarray
    # Median wall-clock span of accepted windows, and how many were rejected.
    dropped_discontiguous: int = 0
    dropped_mixed_label: int = 0
    median_span_s: float = float("nan")
    times: list[float] = field(default_factory=list)


def _contiguous_runs(keys: list[str]) -> list[tuple[int, int]]:
    if not keys:
        return []
    runs: list[tuple[int, int]] = []
    start = 0
    for i in range(1, len(keys)):
        if keys[i] != keys[i - 1]:
            runs.append((start, i))
            start = i
    runs.append((start, len(keys)))
    return runs


def _window_label(window_labs: list[int], *, min_fraction: float) -> int | None:
    n = len(window_labs)
    empty_frac = window_labs.count(LABEL_EMPTY) / n
    if empty_frac >= min_fraction:
        return LABEL_EMPTY
    if empty_frac <= 1.0 - min_fraction:
        return LABEL_OBJECT
    return None


def build_windows(
    packets: list[PacketRecord],
    labels: list[int],
    session_labels: list[str],
    spec: WindowSpec,
    baseline_profile: np.ndarray,
    baseline_phase: np.ndarray | None,
    *,
    config: FeatureConfig | None = None,
    session_keys: list[str] | None = None,
    min_label_fraction: float = MIN_LABEL_FRACTION,
) -> WindowSet:
    config = config or FeatureConfig()
    keys = session_keys if session_keys is not None else derive_session_keys(session_labels)
    fdim = feature_dim(baseline_profile, window_size=spec.size, config=config)

    X: list[np.ndarray] = []
    y: list[int] = []
    groups: list[str] = []
    meta: list[np.ndarray] = []
    times: list[float] = []
    spans: list[float] = []
    dropped_gap = 0
    dropped_mixed = 0

    for seg_start, seg_end in _contiguous_runs(keys):
        seg_packets = packets[seg_start:seg_end]
        seg_labels = labels[seg_start:seg_end]
        seg_keys = keys[seg_start:seg_end]
        if len(seg_packets) < spec.size:
            continue

        for start in range(0, len(seg_packets) - spec.size + 1, spec.stride):
            chunk = seg_packets[start : start + spec.size]
            label = _window_label(
                seg_labels[start : start + spec.size], min_fraction=min_label_fraction
            )
            if label is None:
                dropped_mixed += 1
                continue
            if not window_is_contiguous(chunk, spec):
                dropped_gap += 1
                continue

            X.append(
                window_to_features(
                    chunk,
                    config=config,
                    baseline_profile=baseline_profile,
                    baseline_phase=baseline_phase,
                )
            )
            y.append(label)
            groups.append(seg_keys[start])
            meta.append(
                np.array(
                    [
                        float(np.mean([p.rssi for p in chunk])),
                        float(np.mean([p.agc_gain for p in chunk])),
                        float(np.mean([p.fft_gain for p in chunk])),
                    ]
                )
            )
            stamps = [p.host_ts for p in chunk if p.host_ts is not None]
            times.append(stamps[0] if stamps else float("nan"))
            if len(stamps) >= 2:
                spans.append(max(stamps) - min(stamps))

    if not X:
        return WindowSet(
            X=np.empty((0, fdim)),
            y=np.empty(0, dtype=np.int32),
            groups=[],
            meta=np.empty((0, 3)),
            dropped_discontiguous=dropped_gap,
            dropped_mixed_label=dropped_mixed,
        )

    return WindowSet(
        X=np.asarray(X, dtype=np.float64),
        y=np.asarray(y, dtype=np.int32),
        groups=groups,
        meta=np.asarray(meta, dtype=np.float64),
        dropped_discontiguous=dropped_gap,
        dropped_mixed_label=dropped_mixed,
        median_span_s=float(np.median(spans)) if spans else float("nan"),
        times=times,
    )


def subdivide_groups(ws: WindowSet, n_blocks: int) -> list[str]:
    """Split each session into n contiguous time blocks, for leave-one-block-out.

    This measures temporal generalization *within* a recording. It does not
    break a label/session confound — if every session is single-class, holding
    out a block still trains and tests on the same two recordings.
    """
    if n_blocks <= 1:
        return list(ws.groups)
    out = list(ws.groups)
    for session in sorted(set(ws.groups)):
        idx = [i for i, g in enumerate(ws.groups) if g == session]
        if len(idx) < n_blocks:
            continue
        edges = np.linspace(0, len(idx), n_blocks + 1).astype(int)
        for b in range(n_blocks):
            for i in idx[edges[b] : edges[b + 1]]:
                out[i] = f"{session}/blk{b}"
    return out


# --------------------------------------------------------------------------
# leakage baseline
# --------------------------------------------------------------------------


def best_threshold_balanced_accuracy(v: np.ndarray, y: np.ndarray) -> tuple[float, float, float]:
    """Best single-threshold split of one scalar feature.

    Returns (balanced_accuracy, plain_accuracy, threshold). Sweeps every split
    point in sorted order and tries both polarities.
    """
    order = np.argsort(v, kind="stable")
    vs, ys = v[order], y[order]
    n = ys.size
    total_pos = int(ys.sum())
    total_neg = n - total_pos
    if total_pos == 0 or total_neg == 0:
        return 0.5, max(total_pos, total_neg) / n, float("nan")

    pos_below = np.concatenate([[0], np.cumsum(ys)])  # pos_below[i] = positives in vs[:i]
    best = (-1.0, 0.0, float("nan"))
    for i in range(n + 1):
        pb = int(pos_below[i])
        nb = i - pb
        # polarity +1: predict object when value >= vs[i]
        bal = 0.5 * ((total_pos - pb) / total_pos + nb / total_neg)
        acc = ((total_pos - pb) + nb) / n
        # polarity -1: predict object when value < vs[i]
        bal_n = 0.5 * (pb / total_pos + (total_neg - nb) / total_neg)
        acc_n = (pb + (total_neg - nb)) / n
        if bal_n > bal:
            bal, acc = bal_n, acc_n
        if bal > best[0]:
            thr = float(vs[i]) if i < n else float(vs[-1])
            best = (bal, acc, thr)
    return best


def leakage_baselines(ws: WindowSet) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for j, name in enumerate(META_NAMES):
        bal, acc, thr = best_threshold_balanced_accuracy(ws.meta[:, j], ws.y)
        out[name] = {"balanced_accuracy": bal, "accuracy": acc, "threshold": thr}
    return out


def holdout_balanced_accuracy(
    X: np.ndarray,
    y: np.ndarray,
    groups: list[str],
    *,
    test_fraction: float,
    seed: int,
    model: str = "hgb",
) -> float:
    """Fit on the temporal head, tune the threshold there, score the tail."""
    X_tr, X_te, y_tr, y_te = blocked_split(X, y, groups, test_fraction, seed)
    pipe = build_pipeline(model)
    pipe.fit(X_tr, y_tr)
    thr, _ = tune_threshold(y_tr, pipe.predict_proba(X_tr)[:, LABEL_OBJECT])
    proba = pipe.predict_proba(X_te)[:, LABEL_OBJECT]
    return float(balanced_accuracy_score(y_te, (proba >= thr).astype(np.int32)))


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------


def candidate_models() -> dict[str, object]:
    models: dict[str, object] = {
        "hgb": HistGradientBoostingClassifier(
            max_depth=10,
            learning_rate=0.06,
            max_iter=400,
            min_samples_leaf=8,
            l2_regularization=0.1,
            random_state=42,
        ),
        "rf": RandomForestClassifier(
            n_estimators=400,
            max_depth=16,
            min_samples_leaf=2,
            class_weight="balanced",
            random_state=42,
            n_jobs=-1,
        ),
        "logreg": LogisticRegression(max_iter=3000, class_weight="balanced", C=0.5),
    }
    if XGBClassifier is not None:
        models["xgb"] = XGBClassifier(
            n_estimators=400,
            max_depth=10,
            learning_rate=0.06,
            min_child_weight=8,
            reg_lambda=0.1,
            random_state=42,
            n_jobs=-1,
            eval_metric="logloss",
        )
    if LGBMClassifier is not None:
        models["lgbm"] = LGBMClassifier(
            n_estimators=400,
            max_depth=10,
            learning_rate=0.06,
            min_child_samples=8,
            reg_lambda=0.1,
            random_state=42,
            n_jobs=-1,
            verbose=-1,
        )
    if CatBoostClassifier is not None:
        models["catboost"] = CatBoostClassifier(
            iterations=400,
            depth=10,
            learning_rate=0.06,
            min_data_in_leaf=8,
            l2_leaf_reg=0.1,
            random_state=42,
            verbose=False,
        )
    return models


def build_pipeline(name: str) -> Pipeline:
    return Pipeline([("scaler", StandardScaler()), ("clf", candidate_models()[name])])


def tune_threshold(y_true: np.ndarray, proba: np.ndarray) -> tuple[float, float]:
    """Maximize worst-class recall (fair empty vs object)."""
    best_t, best_score = 0.5, -1.0
    for t in np.linspace(0.15, 0.85, 71):
        pred = (proba >= t).astype(np.int32)
        score = min(
            recall_score(y_true, pred, pos_label=LABEL_EMPTY, zero_division=0),
            recall_score(y_true, pred, pos_label=LABEL_OBJECT, zero_division=0),
        )
        if score > best_score:
            best_score, best_t = score, float(t)
    return best_t, best_score


def blocked_split(
    X: np.ndarray,
    y: np.ndarray,
    groups: list[str],
    test_fraction: float,
    seed: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Hold out the last test_fraction of each session, in time order.

    FALLBACK ONLY. Both sides of this split come from the same recordings, so
    it cannot detect a label/session confound — it is the split that made the
    original pipeline look accurate. ``main`` uses it for model selection and
    threshold tuning only when session-grouped CV is infeasible, and marks the
    resulting bundle ``evaluation_trustworthy=False``.
    """
    train_idx: list[int] = []
    test_idx: list[int] = []
    rng = np.random.default_rng(seed)

    for session in sorted(set(groups)):
        idx = [i for i, m in enumerate(groups) if m == session]
        if len(idx) < 4:
            train_idx.extend(idx)
            continue
        cut = int(len(idx) * (1.0 - test_fraction))
        train_idx.extend(idx[:cut])
        test_idx.extend(idx[cut:])

    if not test_idx:
        return train_test_split(X, y, test_size=test_fraction, random_state=seed, stratify=y)

    rng.shuffle(train_idx)
    return X[train_idx], X[test_idx], y[train_idx], y[test_idx]


def _group_splitter(y: np.ndarray, groups: list[str], max_folds: int):
    """Pick a group-aware splitter that actually yields scorable folds.

    Leave-one-group-out is the cleanest protocol, but it only works when every
    group contains both classes. Under the usual capture protocol each session
    holds one class, so leaving one out gives a single-class test set and every
    fold is discarded. In that case fold on stratified groups instead, with at
    most one fold per group of the rarer class.
    """
    by_group: dict[str, set[int]] = {}
    for lab, g in zip(y.tolist(), groups):
        by_group.setdefault(g, set()).add(int(lab))

    n_groups = len(by_group)
    if n_groups <= max_folds and all(len(s) > 1 for s in by_group.values()):
        return LeaveOneGroupOut()

    per_class: dict[int, int] = {}
    for present in by_group.values():
        for lab in present:
            per_class[lab] = per_class.get(lab, 0) + 1
    k = min(max_folds, n_groups, min(per_class.values(), default=n_groups))
    return StratifiedGroupKFold(n_splits=max(2, k), shuffle=True, random_state=0)


def grouped_oof_proba(
    name: str,
    X: np.ndarray,
    y: np.ndarray,
    groups: list[str],
    *,
    max_folds: int = 5,
) -> tuple[np.ndarray, int]:
    """Out-of-fold P(object) with every prediction made by a model that never
    saw that sample's group.

    Returns (proba, n_folds); entries are NaN where no usable fold covered the
    sample (a fold is skipped when train or test lacks both classes).
    """
    g = np.asarray(groups)
    splitter = _group_splitter(y, groups, max_folds)
    oof = np.full(y.shape[0], np.nan, dtype=np.float64)
    folds = 0

    for train_i, test_i in splitter.split(X, y, groups=g):
        if len(set(y[train_i].tolist())) < 2 or len(set(y[test_i].tolist())) < 2:
            continue
        pipe = build_pipeline(name)
        pipe.fit(X[train_i], y[train_i])
        oof[test_i] = pipe.predict_proba(X[test_i])[:, LABEL_OBJECT]
        folds += 1

    return oof, folds


def select_best_model_grouped(
    X: np.ndarray,
    y: np.ndarray,
    groups: list[str],
    *,
    max_folds: int = 5,
) -> tuple[str, float, dict[str, float], dict[str, float], np.ndarray]:
    """Pick the model and threshold from out-of-fold, group-held-out predictions.

    This is the honest replacement for selecting on ``blocked_split``: no
    candidate is ever scored on a session it was trained on. Returns
    (best_name, threshold, metrics, per_model_balanced_accuracy, oof_proba);
    ``oof_proba`` is NaN where no fold covered the window.
    """
    scores: dict[str, float] = {}
    best = ("", -1.0, 0.5, {}, np.array([]))

    for name in candidate_models():
        oof, folds = grouped_oof_proba(name, X, y, groups, max_folds=max_folds)
        mask = ~np.isnan(oof)
        if folds == 0 or mask.sum() == 0 or len(set(y[mask].tolist())) < 2:
            continue
        yt, pt = y[mask], oof[mask]
        thr, _ = tune_threshold(yt, pt)
        pred = (pt >= thr).astype(np.int32)
        bal = float(balanced_accuracy_score(yt, pred))
        scores[name] = bal
        if bal > best[1]:
            best = (
                name,
                bal,
                thr,
                {
                    "accuracy": float(accuracy_score(yt, pred)),
                    "balanced_accuracy": bal,
                    "object_f1": float(
                        f1_score(yt, pred, pos_label=LABEL_OBJECT, zero_division=0)
                    ),
                    "empty_f1": float(
                        f1_score(yt, pred, pos_label=LABEL_EMPTY, zero_division=0)
                    ),
                    "roc_auc": float(roc_auc_score(yt, pt)),
                    "folds": float(folds),
                    "scored_windows": float(int(mask.sum())),
                    "protocol": "leave-one-group-out, out-of-fold",
                },
                oof,
            )

    return best[0], best[2], best[3], scores, best[4]


def negative_control(
    packets: list[PacketRecord],
    labels: list[int],
    spec: WindowSpec,
    *,
    config: FeatureConfig,
    test_fraction: float,
    seed: int,
) -> dict[str, float]:
    """Score empty-vs-empty: the first half of the empty data against its own second half.

    No object is present in either half, so the only honest answer is 0.5.
    Anything above that is accuracy manufactured from time alone — drift,
    thermal state, gain wander — and it bounds how much of the real
    empty-vs-object score can be attributed to the object.
    """
    keep = [i for i, lab in enumerate(labels) if lab == LABEL_EMPTY]
    if len(keep) < 4 * spec.size:
        return {}

    half = len(keep) // 2
    sub_packets = [packets[i] for i in keep]
    sub_labels = [LABEL_EMPTY] * half + [LABEL_OBJECT] * (len(keep) - half)
    sub_sessions = ["empty_first"] * half + ["empty_second"] * (len(keep) - half)
    zeros = [LABEL_EMPTY] * len(sub_packets)

    baseline = compute_baseline_profile(sub_packets, zeros)
    baseline_phase = compute_baseline_phase_profile(sub_packets, zeros) if config.use_phase else None
    ws = build_windows(
        sub_packets, sub_labels, sub_sessions, spec, baseline, baseline_phase,
        config=config, session_keys=sub_sessions,
    )
    if ws.y.size == 0 or len(np.unique(ws.y)) < 2:
        return {}

    X_tr, X_te, y_tr, y_te = blocked_split(ws.X, ws.y, ws.groups, test_fraction, seed)
    if len(np.unique(y_te)) < 2:
        return {}
    pipe = build_pipeline("hgb")
    pipe.fit(X_tr, y_tr)
    thr, _ = tune_threshold(y_tr, pipe.predict_proba(X_tr)[:, LABEL_OBJECT])
    proba = pipe.predict_proba(X_te)[:, LABEL_OBJECT]
    return {
        "windows": float(ws.y.size),
        "balanced_accuracy": float(
            balanced_accuracy_score(y_te, (proba >= thr).astype(np.int32))
        ),
        "roc_auc": float(roc_auc_score(y_te, proba)),
    }


def select_best_model(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
) -> tuple[str, Pipeline, float, dict[str, float]]:
    scores: dict[str, float] = {}
    best_name, best_pipe, best_threshold, best_bal = "hgb", None, 0.5, -1.0

    for name in candidate_models():
        pipe = build_pipeline(name)
        pipe.fit(X_train, y_train)
        proba = pipe.predict_proba(X_val)[:, LABEL_OBJECT]
        thr, min_rec = tune_threshold(y_val, proba)
        bal = balanced_accuracy_score(y_val, (proba >= thr).astype(np.int32))
        scores[name] = bal
        print(
            f"  candidate {name}: min_recall={min_rec:.3f}  bal_acc={bal:.3f}  thr={thr:.2f}",
            file=sys.stderr,
        )
        if bal > best_bal:
            best_bal, best_name, best_pipe, best_threshold = bal, name, pipe, thr

    assert best_pipe is not None
    return best_name, best_pipe, best_threshold, scores


# --------------------------------------------------------------------------
# grouped cross-validation
# --------------------------------------------------------------------------


def group_cv_feasibility(y: np.ndarray, groups: list[str]) -> tuple[bool, str]:
    """Can leave-one-group-out produce a meaningful score on this dataset?"""
    by_group: dict[str, set[int]] = {}
    for lab, g in zip(y.tolist(), groups):
        by_group.setdefault(g, set()).add(int(lab))

    n_groups = len(by_group)
    if n_groups < 2:
        return False, f"only {n_groups} group(s)"

    groups_per_class: dict[int, int] = {LABEL_EMPTY: 0, LABEL_OBJECT: 0}
    for present in by_group.values():
        for lab in present:
            groups_per_class[lab] += 1

    if min(groups_per_class.values()) < 2:
        pure = sum(1 for s in by_group.values() if len(s) == 1)
        return False, (
            f"{n_groups} groups, {pure} of them single-class; "
            f"empty appears in {groups_per_class[LABEL_EMPTY]} group(s), "
            f"object in {groups_per_class[LABEL_OBJECT]}. "
            "Holding out a group removes an entire class from training."
        )
    return True, ""


def grouped_cv(
    name: str,
    X: np.ndarray,
    y: np.ndarray,
    groups: list[str],
    *,
    max_folds: int = 5,
) -> dict[str, float]:
    """Leave-one-group-out (or GroupKFold) with the threshold tuned on train only."""
    g = np.asarray(groups)
    splitter = _group_splitter(y, groups, max_folds)

    bals: list[float] = []
    aucs: list[float] = []
    for train_i, test_i in splitter.split(X, y, groups=g):
        if len(set(y[train_i].tolist())) < 2 or len(set(y[test_i].tolist())) < 2:
            continue
        pipe = build_pipeline(name)
        pipe.fit(X[train_i], y[train_i])
        thr, _ = tune_threshold(y[train_i], pipe.predict_proba(X[train_i])[:, LABEL_OBJECT])
        proba = pipe.predict_proba(X[test_i])[:, LABEL_OBJECT]
        bals.append(balanced_accuracy_score(y[test_i], (proba >= thr).astype(np.int32)))
        aucs.append(roc_auc_score(y[test_i], proba))

    if not bals:
        return {"folds": 0}
    return {
        "folds": len(bals),
        "balanced_accuracy": float(np.mean(bals)),
        "balanced_accuracy_std": float(np.std(bals)),
        "roc_auc": float(np.mean(aucs)),
    }


# --------------------------------------------------------------------------
# bundle
# --------------------------------------------------------------------------


def save_bundle(
    path: Path,
    *,
    pipe: Pipeline,
    spec: WindowSpec,
    config: FeatureConfig,
    baseline_profile: np.ndarray,
    baseline_phase: np.ndarray | None,
    threshold: float,
    model_type: str,
    csv_path: Path,
    metrics: dict[str, float],
    deploy_metrics: dict[str, float] | None = None,
    grouped_metrics: dict[str, float] | None = None,
    negative_control_metrics: dict[str, float] | None = None,
    evaluation_trustworthy: bool = False,
    evaluation_note: str = "",
    leakage: dict[str, dict[str, float]] | None = None,
    session_keys: list[str] | None = None,
    packet_count: int = 0,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {
            "pipeline": pipe,
            "feature_version": FEATURE_VERSION,
            "feature_config": config.to_dict(),
            "window_size": spec.size,
            "stride": spec.stride,
            "max_span_s": spec.max_span_s,
            "max_seq_gap": spec.max_seq_gap,
            "threshold": threshold,
            "hysteresis": 0.06,
            "ema_alpha": 0.3,
            "baseline_profile": baseline_profile,
            "baseline_phase": baseline_phase,
            "labels": {"empty": LABEL_EMPTY, "object": LABEL_OBJECT},
            "model_type": model_type,
            # Held-out metrics from before any deploy refit. These are the ones
            # to quote.
            "metrics": metrics,
            # Metrics after refitting on everything — optimistic by
            # construction, kept only for traceability.
            "deploy_metrics": deploy_metrics,
            "grouped_metrics": grouped_metrics,
            "negative_control": negative_control_metrics,
            "evaluation_trustworthy": evaluation_trustworthy,
            "evaluation_note": evaluation_note,
            "leakage_baseline": leakage,
            "csv": str(csv_path),
            "sessions": sorted(set(session_keys or [])),
            "packet_count": packet_count,
            "trained_at": datetime.now(timezone.utc).isoformat(),
        },
        path,
    )


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--csv", type=Path, default=root.parent / "sample_data" / "csi_packets.csv")
    p.add_argument("--window", type=int, default=30)
    p.add_argument("--stride", type=int, default=15)
    p.add_argument("--test-fraction", type=float, default=0.2)
    p.add_argument("--out", type=Path, default=root / "models" / "object_detector.joblib")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--deploy", action="store_true", help="Refit best model on all windows")
    p.add_argument(
        "--time-blocks",
        type=int,
        default=4,
        help="Split each session into N time blocks for leave-one-block-out CV (1 disables)",
    )
    p.add_argument(
        "--max-span-s",
        type=float,
        default=12.0,
        help="Reject windows spanning longer than this (0 disables)",
    )
    g = p.add_argument_group("feature layout (stored in the model bundle)")
    g.add_argument(
        "--use-meta",
        action="store_true",
        help="Include RSSI/AGC/FFT gain features. Leaky on single-session captures.",
    )
    g.add_argument("--use-sequence", action="store_true", help="Include flattened window sequence")
    g.add_argument("--no-phase", action="store_true", help="Drop the phase block")
    g.add_argument(
        "--no-normalize-gain",
        action="store_true",
        help="Keep raw amplitude scale (v3 behaviour; lets receiver gain leak in)",
    )
    return p


def main() -> None:
    args = build_parser().parse_args()

    if not args.csv.is_file():
        sys.exit(f"CSV not found: {args.csv}")

    config = FeatureConfig(
        normalize_gain=not args.no_normalize_gain,
        use_phase=not args.no_phase,
        use_meta=args.use_meta,
        use_sequence=args.use_sequence,
    )
    spec = WindowSpec(
        size=args.window,
        stride=args.stride,
        max_span_s=args.max_span_s if args.max_span_s > 0 else None,
    )

    print(f"Loading {args.csv} …")
    packets, labels, session_labels, session_keys = load_packets(args.csv, config=config)
    print(
        f"  packets: {len(packets)}  empty={labels.count(LABEL_EMPTY)}  "
        f"object={labels.count(LABEL_OBJECT)}"
    )
    print(f"  sessions: {len(set(session_keys))}  ({', '.join(sorted(set(session_labels)))})")
    print(f"  features: v{FEATURE_VERSION}  [{config.describe()}]")

    baseline_profile = compute_baseline_profile(packets, labels)
    baseline_phase = compute_baseline_phase_profile(packets, labels) if config.use_phase else None

    ws = build_windows(
        packets, labels, session_labels, spec, baseline_profile, baseline_phase,
        config=config, session_keys=session_keys,
    )
    if ws.y.size == 0:
        sys.exit("No windows built — too few packets per session, or all rejected as discontiguous.")
    print(
        f"  windows: {len(ws.y)}  dims={ws.X.shape[1]}  "
        f"median span={ws.median_span_s:.1f}s  "
        f"(dropped {ws.dropped_discontiguous} discontiguous, "
        f"{ws.dropped_mixed_label} mixed-label)"
    )

    # ---- leakage baselines -------------------------------------------------
    leakage = leakage_baselines(ws)
    best_leak = max(leakage.values(), key=lambda d: d["balanced_accuracy"])
    print("\nMetadata-only baselines — one scalar, best threshold, in-sample:")
    for name, d in leakage.items():
        print(
            f"  {name:14s} bal_acc={d['balanced_accuracy']:.3f}  "
            f"acc={d['accuracy']:.3f}  at {d['threshold']:.2f}"
        )
    if not config.use_meta:
        print("  (excluded from features — --use-meta to include)")
    else:
        print("  WARNING: --use-meta puts these in the feature vector.")
    print(
        "  These are an upper bound, not a rival score. Each tier below also\n"
        "  reports a meta-only model fitted under that tier's own protocol,\n"
        "  which is the comparison that matters."
    )

    # ---- A. session-grouped CV --------------------------------------------
    print("\n[A] Session-grouped CV (generalization to an unseen capture):")
    feasible, why = group_cv_feasibility(ws.y, ws.groups)
    grouped: dict[str, float] | None = None
    grouped_meta: dict[str, float] | None = None
    if feasible:
        grouped_by_model: dict[str, dict[str, float]] = {}
        for name in candidate_models():
            grouped_by_model[name] = grouped_cv(name, ws.X, ws.y, ws.groups)
        grouped = grouped_by_model.get("hgb") or next(iter(grouped_by_model.values()))
        grouped_meta = grouped_cv("hgb", ws.meta, ws.y, ws.groups)
        for name, g in grouped_by_model.items():
            if g.get("folds", 0) == 0:
                continue
            print(
                f"  csi/{name:6s}: bal_acc={g['balanced_accuracy']:.3f} "
                f"±{g['balanced_accuracy_std']:.3f}  "
                f"roc_auc={g['roc_auc']:.3f}  ({int(g['folds'])} folds)"
            )
        print(f"  meta/hgb : bal_acc={grouped_meta.get('balanced_accuracy', float('nan')):.3f}")
    else:
        print(f"  NOT POSSIBLE — {why}")
        print("  Fix: capture interleaved blocks (A/B/A/B, ~2 min each) so that")
        print("  each class appears in several sessions. Until then no number")
        print("  here separates 'detects the object' from 'detects the session'.")

    # ---- B. time-block CV --------------------------------------------------
    block_groups = subdivide_groups(ws, args.time_blocks)
    grouped_blocks: dict[str, float] | None = None
    blocks_meta: dict[str, float] | None = None
    if args.time_blocks > 1:
        print(f"\n[B] Time-block CV ({args.time_blocks} blocks/session, leave-one-block-out):")
        ok_b, why_b = group_cv_feasibility(ws.y, block_groups)
        if ok_b:
            grouped_blocks = grouped_cv("hgb", ws.X, ws.y, block_groups)
            blocks_meta = grouped_cv("hgb", ws.meta, ws.y, block_groups)
            print(
                f"  csi : bal_acc={grouped_blocks['balanced_accuracy']:.3f} "
                f"±{grouped_blocks['balanced_accuracy_std']:.3f}  "
                f"roc_auc={grouped_blocks['roc_auc']:.3f}  ({grouped_blocks['folds']} folds)"
            )
            print(f"  meta: bal_acc={blocks_meta.get('balanced_accuracy', float('nan')):.3f}")
            if not feasible:
                print("  Confounded: label and session coincide, so this measures")
                print("  temporal generalization only, not object detection.")
        else:
            print(f"  NOT POSSIBLE — {why_b}")

    # ---- C. temporal hold-out ---------------------------------------------
    print(f"\n[C] Temporal hold-out (last {args.test_fraction:.0%} of each session):")
    X_train, X_test, y_train, y_test = blocked_split(
        ws.X, ws.y, ws.groups, args.test_fraction, args.seed
    )
    print(f"  train={len(y_train)}  hold-out={len(y_test)}")
    holdout_meta = holdout_balanced_accuracy(
        ws.meta, ws.y, ws.groups, test_fraction=args.test_fraction, seed=args.seed
    )
    print(f"  meta-only model on the same split: bal_acc={holdout_meta:.3f}")

    # ---- model + threshold selection ---------------------------------------
    # When grouped CV is available it drives everything: which model, which
    # threshold, and the metrics written to the bundle. blocked_split is used
    # for selection ONLY as a fallback, because both sides of it come from the
    # same recordings and it cannot see a label/session confound.
    if feasible:
        print("\nSelection protocol: session-grouped out-of-fold (leave-one-session-out).")
        model_name, threshold, metrics, model_scores, oof = select_best_model_grouped(
            ws.X, ws.y, ws.groups
        )
        if not model_name:
            sys.exit("Grouped selection produced no usable fold — rerun with --time-blocks 1.")
        scored = ~np.isnan(oof)
        y_eval = ws.y[scored]
        pred_eval = (oof[scored] >= threshold).astype(np.int32)
        # The saved model is fit on every window; the metrics above estimate how
        # that procedure generalizes to an unseen session.
        eval_pipe = build_pipeline(model_name)
        eval_pipe.fit(ws.X, ws.y)
    else:
        print("\nSelection protocol: temporal hold-out (FALLBACK — grouped CV impossible).")
        model_name, eval_pipe, threshold, model_scores = select_best_model(
            X_train, y_train, X_test, y_test
        )
        proba_test = eval_pipe.predict_proba(X_test)[:, LABEL_OBJECT]
        y_eval = y_test
        pred_eval = (proba_test >= threshold).astype(np.int32)
        metrics = {
            "accuracy": float(accuracy_score(y_eval, pred_eval)),
            "balanced_accuracy": float(balanced_accuracy_score(y_eval, pred_eval)),
            "object_f1": float(
                f1_score(y_eval, pred_eval, pos_label=LABEL_OBJECT, zero_division=0)
            ),
            "empty_f1": float(
                f1_score(y_eval, pred_eval, pos_label=LABEL_EMPTY, zero_division=0)
            ),
            "protocol": "temporal hold-out (confounded)",
        }
        if len(np.unique(y_eval)) > 1:
            metrics["roc_auc"] = float(roc_auc_score(y_eval, proba_test))

    print("  model selection (balanced accuracy):")
    for name, sc in sorted(model_scores.items(), key=lambda kv: -kv[1]):
        print(f"    {name:6s}  {sc:.3f}" + ("  <-" if name == model_name else ""))

    print(f"\n  model={model_name}  threshold={threshold:.2f}")
    print(f"  accuracy={metrics['accuracy']:.3f}  balanced={metrics['balanced_accuracy']:.3f}")
    print("  confusion [empty, object] rows=true:")
    print(confusion_matrix(y_eval, pred_eval, labels=[LABEL_EMPTY, LABEL_OBJECT]))
    print(classification_report(y_eval, pred_eval, target_names=["empty", "object"]))

    # ---- D. negative control ------------------------------------------------
    # Empty vs. the same empty room later. No object in either half, so the only
    # honest answer is 0.5. Whatever it scores is accuracy manufactured from
    # time alone, and it bounds how much of the headline number is the object.
    print("\n[D] Negative control (empty first half vs. empty second half):")
    nc = negative_control(
        packets, labels, spec, config=config,
        test_fraction=args.test_fraction, seed=args.seed,
    )
    if nc:
        print(
            f"  bal_acc={nc['balanced_accuracy']:.3f}  auc={nc['roc_auc']:.3f}  "
            f"over {int(nc['windows'])} windows   (0.5 = clean)"
        )
    else:
        print("  not enough empty windows to run it")
    nc_bad = bool(nc) and nc["balanced_accuracy"] >= 0.60

    # ---- verdict -----------------------------------------------------------
    # Report the strongest available tier, and compare it against the
    # meta-only model measured under that same tier — not against the
    # in-sample threshold sweep, which flatters the baseline.
    if grouped is not None:
        tier, csi_score = "A session-grouped CV", grouped["balanced_accuracy"]
        meta_score = (grouped_meta or {}).get("balanced_accuracy", float("nan"))
    elif grouped_blocks is not None:
        tier, csi_score = "B time-block CV", grouped_blocks["balanced_accuracy"]
        meta_score = (blocks_meta or {}).get("balanced_accuracy", float("nan"))
    else:
        tier, csi_score = "C temporal hold-out", metrics["balanced_accuracy"]
        meta_score = holdout_meta

    print("=" * 68)
    print(f"Tier [{tier}]")
    print(f"  CSI features   {csi_score:.3f}")
    print(f"  meta-only      {meta_score:.3f}   (same protocol)")
    print(f"  meta in-sample {best_leak['balanced_accuracy']:.3f}   (upper bound)")
    if nc:
        print(f"  empty-vs-empty {nc['balanced_accuracy']:.3f}   (negative control, want 0.5)")
    if not np.isnan(meta_score) and csi_score <= meta_score + 0.01:
        print("VERDICT: CSI adds nothing over receiver state. Not object detection.")
    elif not feasible:
        print("VERDICT: UNVALIDATED — every tier here is confounded (see [A]).")
        print("         The score is real; what it measures is not established.")
    elif nc_bad:
        print("VERDICT: UNVALIDATED — the negative control also scores high, so an")
        print("         empty room reproduces the signal. Time, not the object.")
    else:
        print("VERDICT: CSI features beat the metadata baseline under grouped CV.")
    print("=" * 68)

    # ---- deploy + save -----------------------------------------------------
    deploy_metrics = None
    pipe = eval_pipe
    if args.deploy and feasible:
        print("\nDeploy: model already fit on all windows (grouped selection); nothing to refit.")
    elif args.deploy:
        print(f"\nDeploy: refitting {model_name} on all {len(ws.y)} windows …")
        pipe = build_pipeline(model_name)
        pipe.fit(ws.X, ws.y)
        proba_all = pipe.predict_proba(X_test)[:, LABEL_OBJECT]
        threshold, min_rec = tune_threshold(y_test, proba_all)
        pred_all = (proba_all >= threshold).astype(np.int32)
        deploy_metrics = {
            "accuracy": float(accuracy_score(y_test, pred_all)),
            "balanced_accuracy": float(balanced_accuracy_score(y_test, pred_all)),
            "note": "in-sample: X_test was part of the refit; not an honest estimate",
        }
        print(f"  threshold={threshold:.2f}  min_recall={min_rec:.3f} (in-sample)")

    if not feasible:
        note = why
    elif nc_bad:
        note = (
            f"empty-vs-empty negative control scores "
            f"{nc['balanced_accuracy']:.3f}; an object-free room reproduces the signal"
        )
    else:
        note = ""

    save_bundle(
        args.out,
        pipe=pipe,
        spec=spec,
        config=config,
        baseline_profile=baseline_profile,
        baseline_phase=baseline_phase,
        threshold=threshold,
        model_type=model_name,
        csv_path=args.csv,
        metrics=metrics,
        deploy_metrics=deploy_metrics,
        grouped_metrics=grouped or grouped_blocks,
        negative_control_metrics=nc or None,
        evaluation_trustworthy=feasible and not nc_bad,
        evaluation_note=note,
        leakage=leakage,
        session_keys=session_keys,
        packet_count=len(packets),
    )
    print(f"\nSaved → {args.out}")
    print("Live:  ./run_detect.sh --fast --quiet")


if __name__ == "__main__":
    main()
