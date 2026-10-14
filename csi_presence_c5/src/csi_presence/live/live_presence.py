"""Live empty/occupied from CSI serial (no camera required)."""

from __future__ import annotations

import argparse
import time
from collections import deque
from pathlib import Path

import joblib

from csi_presence.data_collector.serial_reader import CsiSerialReader
from csi_presence.features import window_features
from csi_presence.ports_util import ROOT, resolve_device

MODELS = ROOT / "models"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Live CSI presence")
    p.add_argument("--rx", default=None)
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--model", default=str(MODELS / "presence_rf.joblib"))
    p.add_argument("--window-sec", type=float, default=1.0)
    p.add_argument("--hop-sec", type=float, default=0.5)
    args = p.parse_args(argv)

    model_path = Path(args.model)
    if not model_path.exists():
        raise SystemExit(f"Missing model {model_path} — run ./scripts/train_presence.sh first")
    bundle = joblib.load(model_path)
    clf = bundle["model"]

    rx = args.rx or resolve_device("rx")
    buf: deque = deque()
    next_hop = time.time() + args.hop_sec
    print(f"live presence RX={rx} model={model_path}  (Ctrl+C quit)")

    with CsiSerialReader(rx, args.baud) as reader:
        try:
            for sample in reader.iter_samples():
                now = time.time()
                buf.append(sample)
                cutoff = now - max(args.window_sec * 2, 2.0)
                while buf and buf[0].host_ts < cutoff:
                    buf.popleft()
                if now < next_hop:
                    continue
                next_hop = now + args.hop_sec
                win = [s for s in buf if s.host_ts >= now - args.window_sec]
                feats = window_features(win)
                if feats is None:
                    print("waiting for CSI…")
                    continue
                pred = int(clf.predict(feats.reshape(1, -1))[0])
                proba = None
                if hasattr(clf, "predict_proba"):
                    proba = float(clf.predict_proba(feats.reshape(1, -1))[0][pred])
                label = "OCCUPIED" if pred == 1 else "EMPTY"
                conf = f" p={proba:.2f}" if proba is not None else ""
                print(f"{label}{conf}  n_csi={len(win)}")
        except KeyboardInterrupt:
            print("\nbye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
