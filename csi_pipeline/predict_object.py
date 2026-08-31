#!/usr/bin/env python3
"""Run trained empty/object classifier on CSI CSV."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import joblib

from csi_features import LABEL_OBJECT, WindowSpec
from train_object_detector import build_windows, load_packets


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", type=Path, required=True)
    p.add_argument("--csv", type=Path, help="Score all windows in an export CSV")
    p.add_argument("--limit", type=int, default=20)
    args = p.parse_args()

    bundle = joblib.load(args.model)
    pipe = bundle["pipeline"]
    threshold = float(bundle.get("threshold", 0.5))
    baseline = bundle["baseline_profile"]
    spec = WindowSpec(size=bundle["window_size"], stride=bundle["stride"])

    if not args.csv:
        sys.exit("Pass --csv path to score")

    amps, labels, sessions = load_packets(args.csv)
    X, y, meta = build_windows(amps, labels, sessions, spec, baseline)
    proba = pipe.predict_proba(X)[:, LABEL_OBJECT]
    pred = (proba >= threshold).astype(int)

    print(
        f"windows={len(y)}  model={bundle['model_type']}  "
        f"v{bundle.get('feature_version', 1)}  threshold={threshold:.2f}"
    )
    for i in range(min(args.limit, len(y))):
        truth = "object" if y[i] == LABEL_OBJECT else "empty"
        guess = "object" if pred[i] == LABEL_OBJECT else "empty"
        print(
            f"  [{i:4d}] session={meta[i]!r}  P(object)={proba[i]:.3f}  "
            f"pred={guess}  true={truth}"
        )
    if len(y) > args.limit:
        print(f"  … ({len(y) - args.limit} more windows)")


if __name__ == "__main__":
    main()
