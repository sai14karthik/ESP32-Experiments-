#!/usr/bin/env python3
"""Run trained empty/object classifier on CSI CSV."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import joblib

from csi_features import FEATURE_VERSION, LABEL_OBJECT, FeatureConfig, WindowSpec
from sklearn.metrics import accuracy_score, balanced_accuracy_score

from train_object_detector import build_windows, load_packets


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", type=Path, required=True)
    p.add_argument("--csv", type=Path, help="Score all windows in an export CSV")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--summary", action="store_true", help="Print accuracy on all windows")
    args = p.parse_args()

    bundle = joblib.load(args.model)
    model_version = bundle.get("feature_version")
    if model_version is not None and model_version != FEATURE_VERSION:
        sys.exit(
            f"Model feature v{model_version} != code v{FEATURE_VERSION}. Retrain the model."
        )

    pipe = bundle["pipeline"]
    threshold = float(bundle.get("threshold", 0.5))
    baseline = bundle["baseline_profile"]
    baseline_phase = bundle.get("baseline_phase")
    config = FeatureConfig.from_dict(bundle.get("feature_config"))
    spec = WindowSpec(
        size=bundle["window_size"],
        stride=bundle["stride"],
        max_span_s=bundle.get("max_span_s", 12.0),
        max_seq_gap=bundle.get("max_seq_gap", 256),
    )

    if not args.csv:
        sys.exit("Pass --csv path to score")

    packets, labels, session_labels, session_keys = load_packets(args.csv, config=config)
    ws = build_windows(
        packets,
        labels,
        session_labels,
        spec,
        baseline,
        baseline_phase,
        config=config,
        session_keys=session_keys,
    )
    X, y = ws.X, ws.y
    if len(y) == 0:
        sys.exit("No windows built from CSV (too few packets per session?)")

    proba = pipe.predict_proba(X)[:, LABEL_OBJECT]
    pred = (proba >= threshold).astype(int)

    print(
        f"windows={len(y)}  model={bundle['model_type']}  "
        f"v{bundle.get('feature_version', 1)}  threshold={threshold:.2f}  "
        f"[{config.describe()}]"
    )
    if args.summary:
        print(f"  accuracy={accuracy_score(y, pred):.3f}  "
              f"balanced={balanced_accuracy_score(y, pred):.3f}")
        trained_on = bundle.get("csv")
        if trained_on and Path(trained_on).resolve() == args.csv.resolve():
            print("  NOTE: this is the CSV the model was trained on — in-sample, not an estimate.")

    for i in range(min(args.limit, len(y))):
        truth = "object" if y[i] == LABEL_OBJECT else "empty"
        guess = "object" if pred[i] == LABEL_OBJECT else "empty"
        print(
            f"  [{i:4d}] session={ws.groups[i]!r}  P(object)={proba[i]:.3f}  "
            f"pred={guess}  true={truth}"
        )
    if len(y) > args.limit:
        print(f"  … ({len(y) - args.limit} more windows)")


if __name__ == "__main__":
    main()
