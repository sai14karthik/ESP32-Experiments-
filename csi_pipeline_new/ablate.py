#!/usr/bin/env python3
"""Ablation: which feature blocks actually carry the object signal?

Prints, for each configuration, the balanced accuracy under whichever
cross-validation the dataset can support, next to the strongest
metadata-only baseline. If a configuration cannot beat one threshold on one
scalar of receiver state, it is not detecting the object.

  ./run_detect.sh --ablate
  uv run --group csi python ablate.py --csv ../sample_data/csi_packets.csv
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
from train_object_detector import (
    build_windows,
    blocked_split,
    group_cv_feasibility,
    grouped_cv,
    leakage_baselines,
    load_packets,
    negative_control,
    subdivide_groups,
    tune_threshold,
)
from sklearn.metrics import balanced_accuracy_score, roc_auc_score

from train_object_detector import build_pipeline

VARIANTS: list[tuple[str, FeatureConfig, str]] = [
    (
        "v3-style (raw gain + meta + seq)",
        FeatureConfig(normalize_gain=False, use_phase=True, use_meta=True, use_sequence=True),
        "what the pipeline did before v4",
    ),
    (
        "v4 default",
        FeatureConfig(),
        "gain-normalized amplitude + phase, no meta, no sequence",
    ),
    (
        "v4 amplitude-only",
        FeatureConfig(use_phase=False),
        "isolates how much the phase block contributes",
    ),
    (
        "v4 + meta",
        FeatureConfig(use_meta=True),
        "v4 with the leaky block deliberately re-enabled",
    ),
]


def score(
    csv_path: Path,
    config: FeatureConfig,
    spec: WindowSpec,
    *,
    time_blocks: int,
    test_fraction: float,
    seed: int,
    meta_only: bool = False,
) -> dict[str, float]:
    packets, labels, session_labels, session_keys = load_packets(csv_path, config=config)
    baseline = compute_baseline_profile(packets, labels)
    baseline_phase = compute_baseline_phase_profile(packets, labels) if config.use_phase else None
    ws = build_windows(
        packets, labels, session_labels, spec, baseline, baseline_phase,
        config=config, session_keys=session_keys,
    )
    if ws.y.size == 0:
        return {}

    # The true meta-only control: three scalars of receiver state, nothing
    # from the channel response at all.
    X = ws.meta if meta_only else ws.X
    out: dict[str, float] = {"dims": float(X.shape[1]), "windows": float(ws.y.size)}

    feasible, _ = group_cv_feasibility(ws.y, ws.groups)
    if feasible:
        g = grouped_cv("hgb", X, ws.y, ws.groups)
        out["session_cv"] = g.get("balanced_accuracy", float("nan"))

    block_groups = subdivide_groups(ws, time_blocks)
    ok, _ = group_cv_feasibility(ws.y, block_groups)
    if ok:
        g = grouped_cv("hgb", X, ws.y, block_groups)
        out["block_cv"] = g.get("balanced_accuracy", float("nan"))
        out["block_auc"] = g.get("roc_auc", float("nan"))

    X_tr, X_te, y_tr, y_te = blocked_split(
        X, ws.y, ws.groups, test_fraction=test_fraction, seed=seed
    )
    pipe = build_pipeline("hgb")
    pipe.fit(X_tr, y_tr)
    thr, _ = tune_threshold(y_tr, pipe.predict_proba(X_tr)[:, 1])
    proba = pipe.predict_proba(X_te)[:, 1]
    out["holdout"] = float(balanced_accuracy_score(y_te, (proba >= thr).astype(np.int32)))
    if len(np.unique(y_te)) > 1:
        out["holdout_auc"] = float(roc_auc_score(y_te, proba))

    # The leakage baseline is a property of the windows, not of the feature
    # set, but window boundaries shift slightly with the continuity guard, so
    # recompute it per variant rather than assuming it is constant.
    out["best_meta_baseline"] = max(
        d["balanced_accuracy"] for d in leakage_baselines(ws).values()
    )
    return out


def main() -> None:
    root = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--csv", type=Path, default=root.parent / "sample_data" / "csi_packets.csv")
    p.add_argument("--window", type=int, default=30)
    p.add_argument("--stride", type=int, default=15)
    p.add_argument("--time-blocks", type=int, default=4)
    p.add_argument("--test-fraction", type=float, default=0.2)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()

    if not args.csv.is_file():
        sys.exit(f"CSV not found: {args.csv}")

    spec = WindowSpec(size=args.window, stride=args.stride)
    print(f"csv: {args.csv}")
    print(f"window={args.window} stride={args.stride} time_blocks={args.time_blocks}\n")

    header = f"{'variant':34s} {'dims':>6s} {'sessCV':>8s} {'blockCV':>8s} {'holdout':>8s} {'meta':>8s}"
    print(header)
    print("-" * len(header))

    results: dict[str, dict[str, float]] = {}
    rows: list[tuple[str, FeatureConfig, str, bool]] = [
        ("meta-only (3 scalars, no CSI)", FeatureConfig(), "receiver state alone — the control", True)
    ]
    rows += [(n, c, note, False) for n, c, note in VARIANTS]

    for name, config, note, meta_only in rows:
        r = score(
            args.csv, config, spec,
            time_blocks=args.time_blocks,
            test_fraction=args.test_fraction,
            seed=args.seed,
            meta_only=meta_only,
        )
        results[name] = r
        if not r:
            print(f"{name:34s} {'—':>6s}  no windows")
            continue

        def fmt(key: str, _r: dict[str, float] = r) -> str:
            v = _r.get(key)
            return "n/a" if v is None else f"{v:.3f}"

        print(
            f"{name:34s} {int(r['dims']):6d} {fmt('session_cv'):>8s} "
            f"{fmt('block_cv'):>8s} {fmt('holdout'):>8s} {fmt('best_meta_baseline'):>8s}"
        )

    print()
    for name, _, note, _mo in rows:
        print(f"  {name:34s} {note}")

    # ---- negative control --------------------------------------------------
    # Same function the trainer runs as tier [D], so the two reports cannot drift.
    print("\nNegative control — empty room vs. the same empty room, later:")
    nc_config = FeatureConfig()
    nc_packets, nc_labels, _, _ = load_packets(args.csv, config=nc_config)
    nc = negative_control(
        nc_packets, nc_labels, spec,
        config=nc_config,
        test_fraction=args.test_fraction,
        seed=args.seed,
    )
    if not nc:
        print("  skipped (not enough empty packets)")
    else:
        print(
            f"  v4 default separates first-half-empty from second-half-empty at\n"
            f"  bal_acc={nc['balanced_accuracy']:.3f}  auc={nc['roc_auc']:.3f}  "
            f"over {int(nc['windows'])} windows."
        )
        v4 = results.get("v4 default", {}).get("holdout")
        if v4 is not None:
            print(
                f"  Honest empty-vs-object hold-out was {v4:.3f}. Anything the\n"
                f"  control also achieves ({nc['balanced_accuracy']:.3f}) is time, not object."
            )

    print()
    if all("session_cv" not in r for r in results.values() if r):
        print(
            "sessCV is n/a for every variant: this capture has one session per\n"
            "class, so no split holds out a session without removing a class.\n"
            "blockCV and holdout below it cannot separate 'detects the object'\n"
            "from 'detects which recording this is'. Re-capture interleaved\n"
            "(A/B/A/B, ~2 min blocks) and these columns become meaningful."
        )


if __name__ == "__main__":
    main()
