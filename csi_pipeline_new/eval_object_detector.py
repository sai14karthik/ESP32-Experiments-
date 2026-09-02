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
    config = bundle.get("feature_config")
    if config:
        on = [k for k, v in config.items() if v]
        print(f"feature blocks: {', '.join(on) if on else 'amplitude only'}")
    print(f"trained from: {bundle.get('csv', '?')}")
    sessions = bundle.get("sessions")
    if sessions:
        print(f"sessions: {', '.join(sessions)}")
    if bundle.get("packet_count"):
        print(f"packets: {bundle['packet_count']}")
    if bundle.get("trained_at"):
        print(f"trained_at: {bundle['trained_at']}")
    print()
    for k, v in metrics.items():
        print(f"  {k}: {v:.3f}" if isinstance(v, float) else f"  {k}: {v}")

    grouped = bundle.get("grouped_metrics")
    if grouped and "balanced_accuracy" in grouped:
        print(
            f"\ngrouped CV: balanced_accuracy={grouped['balanced_accuracy']:.3f} "
            f"over {grouped.get('folds', '?')} folds"
        )

    nc = bundle.get("negative_control")
    if nc and "balanced_accuracy" in nc:
        print(
            f"\nnegative control (empty vs. the same empty room later): "
            f"balanced_accuracy={nc['balanced_accuracy']:.3f} "
            f"over {int(nc.get('windows', 0))} windows — 0.5 is clean, and whatever\n"
            f"  it scores is the share of the headline number owed to time, not the object."
        )

    leakage = bundle.get("leakage_baseline")
    if leakage:
        best = max(leakage.items(), key=lambda kv: kv[1]["balanced_accuracy"])
        print(
            f"\nmetadata-only baseline (in-sample upper bound): "
            f"{best[0]} alone reaches {best[1]['balanced_accuracy']:.3f}"
        )

    print()
    print("(Hold-out = last 20% of each session, evaluated before deploy retrain on all windows.)")

    if bundle.get("evaluation_trustworthy") is False:
        print()
        print("WARNING — these numbers are confounded:")
        print(f"  {bundle.get('evaluation_note', '')}")
        print("  Label and session coincide, so the score cannot distinguish")
        print("  'detects the object' from 'detects which recording this is'.")
        print("  Re-capture interleaved (A/B/A/B, ~2 min blocks) and retrain.")


if __name__ == "__main__":
    main()
