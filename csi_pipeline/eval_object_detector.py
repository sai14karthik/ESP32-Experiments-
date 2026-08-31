#!/usr/bin/env python3
"""Print hold-out metrics saved at train time (honest; deploy model is fit on all data)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import joblib


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", type=Path, required=True)
    args = p.parse_args()

    bundle = joblib.load(args.model)
    metrics = bundle.get("metrics")
    if not metrics:
        sys.exit("No metrics in bundle — retrain with ./run_detect.sh --train")

    print(f"model={bundle.get('model_type')}  v{bundle.get('feature_version', 1)}  "
          f"threshold={bundle.get('threshold', 0.5):.2f}")
    print(f"features={bundle.get('window_size')}pkt window  stride={bundle.get('stride')}")
    print(f"trained from: {bundle.get('csv', '?')}")
    print()
    for k, v in metrics.items():
        print(f"  {k}: {v:.3f}" if isinstance(v, float) else f"  {k}: {v}")
    print()
    print("(Hold-out = last 20% of each session, evaluated before deploy retrain on all windows.)")


if __name__ == "__main__":
    main()
