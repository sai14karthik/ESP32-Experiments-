#!/usr/bin/env python3
"""Train empty vs object classifier — auto model pick, baseline features, deploy bundle."""

from __future__ import annotations

import argparse
import csv
import sys
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
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from csi_features import (
    FEATURE_VERSION,
    LABEL_EMPTY,
    LABEL_OBJECT,
    WindowSpec,
    compute_baseline_profile,
    feature_dim,
    iq_field_to_amplitudes,
    window_to_features,
)


def load_packets(csv_path: Path) -> tuple[list[np.ndarray], list[int], list[str]]:
    amps: list[np.ndarray] = []
    y: list[int] = []
    sessions: list[str] = []

    with csv_path.open(newline="") as f:
        for row in csv.DictReader(f):
            lab = row["label"].strip().lower()
            if "object" in lab:
                y.append(LABEL_OBJECT)
            elif "baseline" in lab or "empty" in lab:
                y.append(LABEL_EMPTY)
            else:
                print(f"skip unknown label: {row['label']!r}", file=sys.stderr)
                continue
            try:
                amps.append(iq_field_to_amplitudes(row["iq"]))
            except ValueError as exc:
                print(f"skip bad iq row: {exc}", file=sys.stderr)
                continue
            sessions.append(row["label"])

    if not amps:
        sys.exit(f"No rows loaded from {csv_path}")
    return amps, y, sessions


def build_windows(
    amps: list[np.ndarray],
    labels: list[int],
    session_labels: list[str],
    spec: WindowSpec,
    baseline_profile: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    X: list[np.ndarray] = []
    y: list[int] = []
    meta: list[str] = []

    if len(amps) < spec.size:
        return np.empty((0, feature_dim(baseline_profile))), np.empty(0, dtype=np.int32), []

    for start in range(0, len(amps) - spec.size + 1, spec.stride):
        chunk = amps[start : start + spec.size]
        feat = window_to_features(chunk, baseline_profile=baseline_profile)
        window_labs = labels[start : start + spec.size]
        label = int(round(sum(window_labs) / len(window_labs)))
        X.append(feat)
        y.append(label)
        meta.append(session_labels[start])

    return np.asarray(X, dtype=np.float64), np.asarray(y, dtype=np.int32), meta


def blocked_split(
    X: np.ndarray,
    y: np.ndarray,
    meta: list[str],
    test_fraction: float,
    seed: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    train_idx: list[int] = []
    test_idx: list[int] = []
    rng = np.random.default_rng(seed)

    for session in sorted(set(meta)):
        idx = [i for i, m in enumerate(meta) if m == session]
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


def tune_threshold(y_true: np.ndarray, proba: np.ndarray) -> tuple[float, float]:
    """Maximize worst-class recall (fair empty vs object)."""
    best_t = 0.5
    best_score = -1.0
    for t in np.linspace(0.15, 0.85, 71):
        pred = (proba >= t).astype(np.int32)
        empty_rec = recall_score(y_true, pred, pos_label=LABEL_EMPTY, zero_division=0)
        obj_rec = recall_score(y_true, pred, pos_label=LABEL_OBJECT, zero_division=0)
        score = min(empty_rec, obj_rec)
        if score > best_score:
            best_score = score
            best_t = float(t)
    return best_t, best_score


def candidate_models() -> dict[str, object]:
    return {
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


def build_pipeline(name: str) -> Pipeline:
    clf = candidate_models()[name]
    # HGB is scale-invariant; scaler still helps RF/logreg and is harmless for HGB.
    return Pipeline([("scaler", StandardScaler()), ("clf", clf)])


def select_best_model(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
) -> tuple[str, Pipeline, float, dict[str, float]]:
    scores: dict[str, float] = {}
    best_name = "hgb"
    best_pipe: Pipeline | None = None
    best_threshold = 0.5
    best_bal = -1.0

    for name in candidate_models():
        pipe = build_pipeline(name)
        pipe.fit(X_train, y_train)
        proba = pipe.predict_proba(X_val)[:, LABEL_OBJECT]
        thr, min_rec = tune_threshold(y_val, proba)
        pred = (proba >= thr).astype(np.int32)
        bal = balanced_accuracy_score(y_val, pred)
        scores[name] = bal
        print(f"  candidate {name}: min_recall={min_rec:.3f}  bal_acc={bal:.3f}  thr={thr:.2f}", file=sys.stderr)
        if bal > best_bal:
            best_bal = bal
            best_name = name
            best_pipe = pipe
            best_threshold = thr

    assert best_pipe is not None
    return best_name, best_pipe, best_threshold, scores


def save_bundle(
    path: Path,
    *,
    pipe: Pipeline,
    spec: WindowSpec,
    baseline_profile: np.ndarray,
    threshold: float,
    model_type: str,
    csv_path: Path,
    metrics: dict[str, float],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {
            "pipeline": pipe,
            "feature_version": FEATURE_VERSION,
            "window_size": spec.size,
            "stride": spec.stride,
            "threshold": threshold,
            "hysteresis": 0.06,
            "ema_alpha": 0.3,
            "baseline_profile": baseline_profile,
            "labels": {"empty": LABEL_EMPTY, "object": LABEL_OBJECT},
            "model_type": model_type,
            "metrics": metrics,
            "csv": str(csv_path),
        },
        path,
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--csv",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "sample data " / "csi_packets.csv",
    )
    p.add_argument("--window", type=int, default=30)
    p.add_argument("--stride", type=int, default=15)
    p.add_argument("--test-fraction", type=float, default=0.2)
    p.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "models" / "object_detector.joblib")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--deploy",
        action="store_true",
        help="Retrain best model type on all windows after validation",
    )
    args = p.parse_args()

    if not args.csv.is_file():
        sys.exit(f"CSV not found: {args.csv}")

    print(f"Loading {args.csv} …")
    amps, labels, sessions = load_packets(args.csv)
    print(f"  packets: {len(amps)}  empty={labels.count(LABEL_EMPTY)}  object={labels.count(LABEL_OBJECT)}")

    baseline_profile = compute_baseline_profile(amps, labels)
    print(f"  baseline profile: {baseline_profile.shape[0]} active subcarriers")

    spec = WindowSpec(size=args.window, stride=args.stride)
    X, y, meta = build_windows(amps, labels, sessions, spec, baseline_profile)
    print(f"  windows: {len(y)}  features={X.shape[1]}  (v{FEATURE_VERSION})")

    X_train, X_test, y_train, y_test = blocked_split(
        X, y, meta, test_fraction=args.test_fraction, seed=args.seed
    )
    print(f"  train={len(y_train)}  hold-out={len(y_test)}")

    model_name, eval_pipe, threshold, model_scores = select_best_model(
        X_train, y_train, X_test, y_test
    )
    print("\nModel selection (balanced accuracy on hold-out):")
    for name, sc in sorted(model_scores.items(), key=lambda kv: -kv[1]):
        mark = " ←" if name == model_name else ""
        print(f"  {name:6s}  {sc:.3f}{mark}")

    proba_test = eval_pipe.predict_proba(X_test)[:, LABEL_OBJECT]
    pred_test = (proba_test >= threshold).astype(np.int32)

    metrics = {
        "accuracy": float(accuracy_score(y_test, pred_test)),
        "balanced_accuracy": float(balanced_accuracy_score(y_test, pred_test)),
        "object_f1": float(f1_score(y_test, pred_test, pos_label=LABEL_OBJECT, zero_division=0)),
        "empty_f1": float(f1_score(y_test, pred_test, pos_label=LABEL_EMPTY, zero_division=0)),
    }
    if len(np.unique(y_test)) > 1:
        metrics["roc_auc"] = float(roc_auc_score(y_test, proba_test))

    print(f"\nHold-out  model={model_name}  threshold={threshold:.2f}")
    print(f"  accuracy:          {metrics['accuracy']:.3f}")
    print(f"  balanced accuracy: {metrics['balanced_accuracy']:.3f}")
    if "roc_auc" in metrics:
        print(f"  ROC-AUC:           {metrics['roc_auc']:.3f}")
    print("  confusion [empty, object] rows=true:")
    print(confusion_matrix(y_test, pred_test, labels=[LABEL_EMPTY, LABEL_OBJECT]))
    print(classification_report(y_test, pred_test, target_names=["empty", "object"]))

    if args.deploy:
        print(f"\nDeploy: retraining {model_name} on all {len(y)} windows …")
        pipe = build_pipeline(model_name)
        pipe.fit(X, y)
    else:
        pipe = eval_pipe

    save_bundle(
        args.out,
        pipe=pipe,
        spec=spec,
        baseline_profile=baseline_profile,
        threshold=threshold,
        model_type=model_name,
        csv_path=args.csv,
        metrics=metrics,
    )
    print(f"\nSaved → {args.out}")
    print("Live:  cd csi_pipeline && ./run_detect.sh")
    print("Setup: cd .. && uv sync --group csi")


if __name__ == "__main__":
    main()
