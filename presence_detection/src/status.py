#!/usr/bin/env python3
"""Print presence model status (fusion, metrics, calibration)."""

from __future__ import annotations

import sys

import joblib

from presence_detection.src.paths import (
    CSI_PIPELINE,
    EXPORTS_DIR,
    MODELS_DIR,
    calibration_path,
    model_path,
)


def main() -> int:
    mp = model_path()
    cp = calibration_path()
    print(f"presence models dir: {MODELS_DIR}")
    print(f"csi pipeline:        {CSI_PIPELINE}")
    print(f"exports dir:         {EXPORTS_DIR}")
    print()
    if not mp.is_file():
        print("model: MISSING — run: ./run_presence.sh train")
        return 1
    b = joblib.load(mp)
    order = b.get("rx_sources_order") or []
    m = b.get("metrics") or {}
    g = b.get("grouped_metrics") or {}
    print(f"model: {mp}")
    print(
        f"  type={b.get('model_type')}  v{b.get('feature_version')}  "
        f"trained={b.get('trained_at')}"
    )
    print(f"  rx_fusion={b.get('rx_fusion')}  N={len(order)}  sources={order}")
    n_per = b.get("n_features_per_rx")
    if order and n_per:
        print(f"  feature width={int(n_per)}×{len(order)}={int(n_per) * len(order)}")
    print(
        f"  OOF bal_acc={m.get('balanced_accuracy', float('nan')):.3f}  "
        f"grouped={g.get('balanced_accuracy', float('nan')):.3f}"
    )
    ov = b.get("or_vote_metrics") or {}
    if ov:
        print(f"  OR-vote bal_acc={ov.get('balanced_accuracy', float('nan')):.3f}")
    print()
    if cp.is_file():
        cal = joblib.load(cp)
        print(f"calibration: {cp}")
        print(
            f"  thr={cal.get('threshold'):+.4f}  fpr={cal.get('fpr')}  "
            f"windows={cal.get('n_windows')}  source={cal.get('source')}"
        )
        if cal.get("model_trained_at") != b.get("trained_at"):
            print("  WARNING: calibration is for a different train — re-run calibrate")
    else:
        print("calibration: MISSING — run: ./run_presence.sh calibrate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
