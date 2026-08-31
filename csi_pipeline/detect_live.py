#!/usr/bin/env python3
"""Real-time empty vs object detection from ESP32-C5 CSI serial stream."""

from __future__ import annotations

import argparse
import glob
import json
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import joblib
import serial

from csi_features import LABEL_OBJECT, iq_list_to_amplitudes, window_to_features
from csi_parse import DEFAULT_BAUD, parse_csi_line


def find_port() -> str:
    ports = sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/cu.usbserial*"))
    if not ports:
        sys.exit("No /dev/cu.usbmodem* — plug in the csi_recv board.")
    return ports[0]


class LiveDetector:
    def __init__(
        self,
        bundle: dict,
        *,
        threshold: float | None = None,
        ema_alpha: float | None = None,
        hysteresis: float | None = None,
    ) -> None:
        self.pipe = bundle["pipeline"]
        self.baseline_profile = bundle.get("baseline_profile")
        self.window_size: int = bundle["window_size"]
        self.stride: int = bundle["stride"]
        self.threshold = float(threshold if threshold is not None else bundle.get("threshold", 0.5))
        self.ema_alpha = float(
            ema_alpha if ema_alpha is not None else bundle.get("ema_alpha", 0.3)
        )
        self.hysteresis = float(
            hysteresis if hysteresis is not None else bundle.get("hysteresis", 0.06)
        )
        self.buf: deque = deque(maxlen=self.window_size)
        self._since_predict = 0
        self._ema_p: float | None = None
        self._state = "empty"

    def reset(self) -> None:
        self.buf.clear()
        self._since_predict = 0
        self._ema_p = None
        self._state = "empty"

    def on_iq(self, iq: list[int]) -> dict | None:
        try:
            self.buf.append(iq_list_to_amplitudes(iq))
        except ValueError:
            return None

        if len(self.buf) < self.window_size:
            return {
                "ready": False,
                "buffered": len(self.buf),
                "need": self.window_size,
            }

        self._since_predict += 1
        if self._since_predict < self.stride:
            return None
        self._since_predict = 0

        feat = window_to_features(
            list(self.buf),
            baseline_profile=self.baseline_profile,
        )
        proba = float(self.pipe.predict_proba(feat.reshape(1, -1))[0, LABEL_OBJECT])

        if self._ema_p is None:
            self._ema_p = proba
        else:
            self._ema_p = self.ema_alpha * proba + (1.0 - self.ema_alpha) * self._ema_p

        p = self._ema_p
        if self._state == "empty":
            if p >= self.threshold + self.hysteresis:
                self._state = "object"
        elif p <= self.threshold - self.hysteresis:
            self._state = "empty"

        return {
            "ready": True,
            "p_object": round(p, 4),
            "p_raw": round(proba, 4),
            "state": self._state,
            "threshold": self.threshold,
        }


def format_line(result: dict, *, seq: int | None = None, rssi: int | None = None) -> str:
    if not result.get("ready"):
        return (
            f"buffering {result['buffered']}/{result['need']} packets "
            f"(~{result['need'] * 0.2:.0f}s @ 5 pkt/s)…"
        )
    tag = result["state"].upper()
    extra = ""
    if seq is not None:
        extra += f" seq={seq}"
    if rssi is not None:
        extra += f" rssi={rssi}"
    return (
        f"{tag:6s}  P(object)={result['p_object']:.3f}  "
        f"raw={result['p_raw']:.3f}  thr={result['threshold']:.2f}{extra}"
    )


def main() -> None:
    root = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--model",
        type=Path,
        default=root / "models" / "object_detector.joblib",
    )
    p.add_argument("--port", help="Serial port (default: auto-detect recv)")
    p.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    p.add_argument("--from-file", type=Path, help="Replay CSI lines (no hardware)")
    p.add_argument("--threshold", type=float, help="Override saved threshold")
    p.add_argument("--json", action="store_true", help="One JSON object per prediction")
    p.add_argument("--quiet", action="store_true", help="Only print on state change")
    p.add_argument("--eval", action="store_true", help="Replay CSV and print accuracy summary")
    args = p.parse_args()

    if not args.model.is_file():
        sys.exit(f"Model not found: {args.model}\nTrain first: ./run_detect.sh --train")

    bundle = joblib.load(args.model)
    detector = LiveDetector(bundle, threshold=args.threshold)
    last_state: str | None = None

    metrics = bundle.get("metrics", {})
    print(
        f"model={bundle.get('model_type', '?')}  v{bundle.get('feature_version', 1)}  "
        f"window={detector.window_size}  stride={detector.stride}  "
        f"threshold={detector.threshold:.2f}",
        file=sys.stderr,
    )
    if metrics:
        print(
            f"trained metrics: bal_acc={metrics.get('balanced_accuracy', 0):.3f}  "
            f"acc={metrics.get('accuracy', 0):.3f}",
            file=sys.stderr,
        )
    print("Ctrl+C to stop. Do not run idf.py monitor on the same port.", file=sys.stderr)

    eval_true: list[str] = []
    eval_pred: list[str] = []

    def handle_packet(iq: list[int], meta: dict | None = None) -> None:
        nonlocal last_state
        meta = meta or {}
        result = detector.on_iq(iq)
        if result is None:
            return
        if not result.get("ready"):
            if not args.quiet and not args.eval:
                print(
                    format_line(result, seq=meta.get("seq"), rssi=meta.get("rssi")),
                    flush=True,
                )
            return

        state = result["state"]
        if args.eval and meta.get("true_label"):
            eval_true.append(meta["true_label"])
            eval_pred.append(state)

        if args.json:
            payload = {"ts": datetime.now(timezone.utc).isoformat(), **result, **meta}
            print(json.dumps(payload), flush=True)
            return

        if args.quiet and state == last_state:
            return
        last_state = state
        if not args.eval:
            print(
                format_line(result, seq=meta.get("seq"), rssi=meta.get("rssi")),
                flush=True,
            )

    if args.from_file:
        path = args.from_file
        with path.open() as f:
            for line in f:
                sample = parse_csi_line(line.rstrip("\n"))
                if not sample or not sample.get("iq"):
                    continue
                lab = (sample.get("label") or "").lower()
                true_label = None
                if args.eval:
                    # label not in serial lines — infer from replay context not available
                    pass
                handle_packet(
                    sample["iq"],
                    {"seq": sample.get("seq"), "rssi": sample.get("rssi"), "true_label": true_label},
                )
                if not args.eval:
                    time.sleep(0.05)

        if args.eval:
            print("Use: ./run_detect.sh --eval-csv for labeled replay accuracy.", file=sys.stderr)
        return

    port = args.port or find_port()
    print(f"serial: {port} @ {args.baud}", file=sys.stderr)

    with serial.Serial(port, args.baud, timeout=1.0) as ser:
        ser.reset_input_buffer()
        buf = ""
        while True:
            chunk = ser.read(ser.in_waiting or 1)
            if not chunk:
                continue
            buf += chunk.decode("utf-8", errors="replace")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                sample = parse_csi_line(line)
                if sample and sample.get("iq"):
                    handle_packet(
                        sample["iq"],
                        {"seq": sample.get("seq"), "rssi": sample.get("rssi")},
                    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)
