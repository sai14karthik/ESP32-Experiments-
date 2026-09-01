#!/usr/bin/env python3
"""Quick software + optional hardware self-test for the CSI detect pipeline."""

from __future__ import annotations

import sys
from pathlib import Path

import joblib
import numpy as np

from csi_features import compute_baseline_phase_profile, compute_baseline_profile, feature_dim
from detect_live import LiveDetector
from probe_recv_port import count_csi, list_ports
from train_object_detector import WindowSpec, build_windows, load_packets


def main() -> int:
    root = Path(__file__).resolve().parent
    csv_path = root.parent / "sample data " / "csi_packets.csv"
    model_path = root / "models" / "object_detector.joblib"

    failures: list[str] = []

    def ok(msg: str) -> None:
        print(f"  OK  {msg}")

    def fail(msg: str) -> None:
        print(f"  FAIL {msg}")
        failures.append(msg)

    print("=== software ===")
    if not model_path.is_file():
        fail(f"model missing: {model_path}")
        return 1

    bundle = joblib.load(model_path)
    ok(f"model v{bundle.get('feature_version')} {bundle.get('model_type')}")

    if not csv_path.is_file():
        fail(f"sample csv missing: {csv_path}")
        return 1

    packets, labels, session_labels, session_keys = load_packets(csv_path)
    baseline = compute_baseline_profile(packets, labels)
    baseline_phase = compute_baseline_phase_profile(packets, labels)
    spec = WindowSpec(30, 15)
    X, y, meta = build_windows(
        packets,
        labels,
        session_labels,
        spec,
        baseline,
        baseline_phase,
        session_keys=session_keys,
    )
    if X.shape[1] != feature_dim(baseline, window_size=30):
        fail(f"feature dim mismatch: {X.shape[1]}")
    else:
        ok(f"features {X.shape[1]}  windows {len(y)}")

    det = LiveDetector(bundle, fast=False)
    import csv

    rows: list[dict] = []
    with csv_path.open() as f:
        for row in csv.DictReader(f):
            iq = [int(x) for x in row["iq"].strip("{}").split(",") if x.strip()]
            if len(iq) != 234:
                continue
            rows.append(row)
            if len(rows) >= 200:
                break

    max_diff = 0.0
    pred_n = 0
    for i, row in enumerate(rows):
        iq = [int(x) for x in row["iq"].strip("{}").split(",") if x.strip()]
        r = det.on_packet(
            iq,
            rssi=float(row["rssi"]),
            agc_gain=float(row["agc_gain"]),
            fft_gain=float(row["fft_gain"]),
        )
        if r and r.get("ready"):
            pred_n += 1
            start = i - 29
            if start % 15 != 0:
                continue
            from csi_features import window_to_features

            feat_buf = window_to_features(
                list(det.buf),
                baseline_profile=baseline,
                baseline_phase=baseline_phase,
            )
            feat_slice = window_to_features(
                packets[start : i + 1],
                baseline_profile=baseline,
                baseline_phase=baseline_phase,
            )
            max_diff = max(max_diff, float(np.max(np.abs(feat_buf - feat_slice))))

    if max_diff > 1e-9:
        fail(f"train/live parity max_diff={max_diff}")
    else:
        ok(f"train/live parity (max_diff=0)  live_preds={pred_n}")

    print("\n=== hardware (optional) ===")
    recv_ports = list_ports(recv_only=True)
    if not recv_ports:
        print("  skip  no usbmodem ports")
    else:
        port = recv_ports[0]
        n, sample = count_csi(port, 5.0, 115200)
        if n > 0:
            ok(f"{port} CSI_DATA={n}/5s")
            if sample:
                print(f"       {sample}…")
        else:
            print(f"  WARN {port} CSI_DATA=0 — reset boards or reflash pair")

    print()
    if failures:
        print(f"FAILED ({len(failures)})")
        return 1
    print("ALL SOFTWARE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
