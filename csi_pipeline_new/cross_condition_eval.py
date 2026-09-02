#!/usr/bin/env python3
"""Train on one capture condition, test on a completely different one.

Session-grouped CV answers "does this generalize to an unseen recording of the
same scene". It does not answer "does this generalize to an object it has never
seen, somewhere it has never been" — every fold still contains the same object
at the same spot. This script answers that one, by holding out a whole
condition: different object, different position, different day if you like.

Two transfer modes, because they correspond to different deployments:

  strict      the model carries its training baseline into the new condition.
              What happens if you ship the bundle and never recalibrate.

  calibrated  the new condition's own empty blocks supply the baseline, and
              features are built relative to it. What happens if deployment
              starts with "record 60 s of the empty room". Realistic, and the
              baseline is the one thing that is genuinely site-specific.

  ./cross_condition_eval.py --a exports/pc_control.csv --b exports/pc2_control.csv \
      --a-name "water @ midpoint" --b-name "backpack @ one-third"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

from csi_features import (
    FeatureConfig,
    WindowSpec,
    compute_baseline_phase_profile,
    compute_baseline_profile,
)
from sklearn.metrics import balanced_accuracy_score, roc_auc_score

from train_object_detector import (
    build_pipeline,
    build_windows,
    load_packets,
    tune_threshold,
)


def prepare(csv: Path, spec: WindowSpec, config: FeatureConfig, baseline=None):
    """Window a capture. Pass `baseline` to reuse another condition's profile."""
    packets, labels, session_labels, session_keys = load_packets(csv, config=config)
    if baseline is None:
        prof = compute_baseline_profile(packets, labels)
        phase = compute_baseline_phase_profile(packets, labels) if config.use_phase else None
    else:
        prof, phase = baseline
    ws = build_windows(
        packets, labels, session_labels, spec, prof, phase,
        config=config, session_keys=session_keys,
    )
    return ws, (prof, phase)


def transfer(train_ws, test_ws, model: str, *, fpr: float = 0.10) -> dict[str, float]:
    pipe = build_pipeline(model)
    pipe.fit(train_ws.X, train_ws.y)
    # Threshold is tuned on training data only — the test condition is unseen
    # by construction, so peeking at it here would defeat the whole exercise.
    thr, _ = tune_threshold(train_ws.y, pipe.predict_proba(train_ws.X)[:, 1])
    proba = pipe.predict_proba(test_ws.X)[:, 1]
    pred = (proba >= thr).astype(np.int32)
    out = {
        "carried": float(balanced_accuracy_score(test_ws.y, pred)),
        "threshold": float(thr),
        "windows": float(test_ws.y.size),
    }
    if len(np.unique(test_ws.y)) > 1:
        out["roc_auc"] = float(roc_auc_score(test_ws.y, proba))

        # Carrying the source threshold conflates two separate failures: the
        # features not transferring, and the probability distribution shifting
        # under them. AUC isolates the first. This isolates the second, and is
        # what you would actually deploy: at install you record the empty room,
        # set the threshold at a chosen false-positive rate on those windows,
        # and never need a labelled example of the object at the new site.
        empty_proba = proba[test_ws.y == 0]
        t = float(np.quantile(empty_proba, 1.0 - fpr)) - 1e-9
        cal = (proba >= t).astype(np.int32)
        out["calibrated"] = float(balanced_accuracy_score(test_ws.y, cal))
        out["cal_threshold"] = t
        out["recall_object"] = float(
            ((cal == 1) & (test_ws.y == 1)).sum() / max((test_ws.y == 1).sum(), 1)
        )
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--a", type=Path, required=True)
    p.add_argument("--b", type=Path, required=True)
    p.add_argument("--a-name", default="condition A")
    p.add_argument("--b-name", default="condition B")
    p.add_argument("--model", default="hgb")
    p.add_argument(
        "--fpr",
        type=float,
        default=0.10,
        help="False-positive rate to set the calibrated threshold at (default 0.10)",
    )
    p.add_argument("--window", type=int, default=30)
    p.add_argument("--stride", type=int, default=15)
    args = p.parse_args()

    for f in (args.a, args.b):
        if not f.is_file():
            sys.exit(f"not found: {f}")

    spec = WindowSpec(size=args.window, stride=args.stride)
    config = FeatureConfig()

    a, a_base = prepare(args.a, spec, config)
    b, b_base = prepare(args.b, spec, config)
    # Strict mode: the other condition's windows rebuilt against this one's baseline.
    b_strict, _ = prepare(args.b, spec, config, baseline=a_base)
    a_strict, _ = prepare(args.a, spec, config, baseline=b_base)

    print(f"A = {args.a_name}: {a.y.size} windows  ({args.a.name})")
    print(f"B = {args.b_name}: {b.y.size} windows  ({args.b.name})")
    print(f"model={args.model}  window={args.window} stride={args.stride}\n")

    runs = [
        ("A -> B  src-baseline", a, b_strict),
        ("A -> B  own-baseline", a, b),
        ("B -> A  src-baseline", b, a_strict),
        ("B -> A  own-baseline", b, a),
    ]

    hdr = (f"{'transfer':22s} {'auc':>7s} {'carried':>8s} {'calib':>7s} "
           f"{'rec.obj':>8s} {'n':>6s}")
    print(hdr)
    print("-" * len(hdr))
    results = {}
    for name, tr, te in runs:
        r = transfer(tr, te, args.model, fpr=args.fpr)
        results[name] = r
        print(
            f"{name:22s} {r.get('roc_auc', float('nan')):7.3f} {r['carried']:8.3f} "
            f"{r.get('calibrated', float('nan')):7.3f} "
            f"{r.get('recall_object', float('nan')):8.3f} {int(r['windows']):6d}"
        )

    print()
    print("  auc      ranking quality — does the representation transfer at all")
    print(f"  carried  source threshold reused as-is (no recalibration)")
    print(f"  calib    threshold set from the target's EMPTY windows at {args.fpr:.0%} FPR")

    # Verdict is judged on the own-baseline rows only. src-baseline is the
    # ship-it-blind reference: no recalibration of any kind at the new site.
    # own-baseline is the deployable path — record the empty room at install,
    # which supplies both the baseline profile and the threshold, and needs no
    # labelled object data.
    dep = [r for k, r in results.items() if "own-baseline" in k]
    worst_cal = min(r["calibrated"] for r in dep if "calibrated" in r)
    worst_auc = min(r["roc_auc"] for r in dep if "roc_auc" in r)

    print()
    print("=" * 68)
    if worst_cal >= 0.80:
        print(f"Transfers. Worst direction is {worst_cal:.3f} balanced accuracy on an")
        print("object and position never trained on, once the threshold is set")
        print("from the target's empty room. That calibration needs no labelled")
        print("object data, so it is something you can actually do at install.")
    elif worst_auc >= 0.80:
        print(f"The representation transfers (worst AUC {worst_auc:.3f}) but the")
        print(f"operating point does not (calibrated {worst_cal:.3f}). Recalibrating")
        print("on the target's empty room is required; shipping a fixed threshold")
        print("will not work across conditions.")
    else:
        print(f"Does not transfer (worst AUC {worst_auc:.3f}). What the model learned")
        print("is specific to the object and position it saw. Training on more")
        print("conditions is the fix, not more data per condition.")
    print("=" * 68)
    print("\n0.5 is chance. Compare against the within-condition grouped-CV score;")
    print("the gap between them is the cost of changing object and position.")


if __name__ == "__main__":
    main()
