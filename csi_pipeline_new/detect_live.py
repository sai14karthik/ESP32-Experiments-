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
import numpy as np
import serial

from csi_features import (
    FEATURE_VERSION,
    LABEL_OBJECT,
    FeatureConfig,
    iq_list_to_packet,
    window_to_features,
)
from csi_parse import DEFAULT_BAUD, parse_csi_line


def find_port() -> str:
    ports = sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/cu.usbserial*"))
    if not ports:
        sys.exit("No /dev/cu.usbmodem* — plug in the csi_recv board.")
    return ports[0]


# Probability space is bounded, so a threshold sitting at either end leaves no
# room for the hysteresis band. Log-odds space is unbounded and needs no such
# guard. See LiveDetector and score_windows.
PROBA_EPS = 1e-6


def score_windows(pipe, X: np.ndarray, *, kind: str | None = None) -> tuple[np.ndarray, str]:
    """Monotone score for "is this an object", preferring raw log-odds.

    predict_proba saturates. On real data here, empty windows sit at p=0.99974
    and object windows at p=0.99991 — cleanly separated (AUC 0.851), but with
    almost the whole gap crushed into the last four decimal places, and a more
    confident model would round both to exactly 1.0 and lose the ordering
    outright. Calibration sets its threshold at a quantile of this score, so
    the score has to keep its resolution where the quantile lands.
    decision_function does; probability does not. Fall back only for models
    that have no decision_function at all (random forest).

    Pass ``kind`` to force a space — an uncalibrated bundle carries a threshold
    tuned on predict_proba, and must keep being scored there.
    """
    if kind != "predict_proba":
        fn = getattr(pipe, "decision_function", None)
        if fn is not None:
            d = np.asarray(fn(X), dtype=np.float64)
            if d.ndim == 1:
                return d, "decision_function"
        if kind == "decision_function":
            raise RuntimeError(
                "calibration used decision_function but this model has none; recalibrate"
            )
    return pipe.predict_proba(X)[:, LABEL_OBJECT], "predict_proba"


def score_to_proba(score: float, kind: str) -> float:
    """Probability for display only. Exact for the binary models used here."""
    if kind == "predict_proba":
        return score
    return float(1.0 / (1.0 + np.exp(-np.clip(score, -700, 700))))


def check_calibration(cal: dict, bundle: dict) -> str | None:
    """Return why this calibration cannot be used with this bundle, or None.

    A threshold is a statement about one pipeline's score scale, and a baseline
    profile has to match the feature layout it was measured under. Neither
    survives a retrain, so a stale calibration must be refused rather than
    silently applied.
    """
    if cal.get("kind") != "site_calibration":
        return "not a site calibration file"
    if cal.get("feature_version") != bundle.get("feature_version"):
        return (
            f"calibrated against feature v{cal.get('feature_version')}, "
            f"model is v{bundle.get('feature_version')}"
        )
    if cal.get("feature_config") != bundle.get("feature_config"):
        return "feature config differs from the model's"
    if cal.get("model_trained_at") != bundle.get("trained_at"):
        return (
            f"calibrated against a model trained {cal.get('model_trained_at')}, "
            f"this one was trained {bundle.get('trained_at')}"
        )
    old = bundle.get("baseline_profile")
    if old is not None and np.shape(cal.get("baseline_profile")) != np.shape(old):
        return "baseline profile shape differs from the model's"
    return None


class LiveDetector:
    def __init__(
        self,
        bundle: dict,
        *,
        threshold: float | None = None,
        ema_alpha: float | None = None,
        hysteresis: float | None = None,
        live_stride: int | None = None,
        fast: bool = False,
        calibration: dict | None = None,
    ) -> None:
        self.pipe = bundle["pipeline"]
        self.baseline_profile = bundle.get("baseline_profile")
        self.baseline_phase = bundle.get("baseline_phase")
        # A site calibration replaces both site-specific quantities at once.
        # They are a matched pair: the threshold was measured against
        # probabilities produced under this exact baseline, so taking one
        # without the other would put the operating point on a distribution
        # that was never observed.
        self.calibration = calibration
        if calibration is not None:
            self.baseline_profile = calibration["baseline_profile"]
            self.baseline_phase = calibration.get("baseline_phase")
        # Replay the exact feature layout the model was fitted with. Without
        # this, a bundle trained with --use-meta or --no-normalize-gain would
        # be fed a differently-shaped (or differently-scaled) vector at
        # inference time.
        self.config = FeatureConfig.from_dict(bundle.get("feature_config"))
        self.window_size: int = bundle["window_size"]
        self.stride: int = bundle["stride"]
        # A window is only meaningful if its packets are close together in
        # time; a link stall makes the temporal features nonsense.
        self.max_span_s: float | None = bundle.get("max_span_s", 12.0)
        self.fast = fast
        self.live_stride = int(live_stride if live_stride is not None else (1 if fast else self.stride))
        # The bundle's own threshold was tuned on predict_proba, so that stays
        # the space when running uncalibrated. A calibration carries the space
        # it measured its threshold in.
        self.score_kind = (
            calibration.get("score_kind", "predict_proba")
            if calibration is not None
            else "predict_proba"
        )
        # Explicit --threshold wins; then the site calibration, which measured
        # one here; then whatever training happened to leave in the bundle.
        if threshold is None and calibration is not None:
            threshold = calibration["threshold"]
        self.threshold = float(threshold if threshold is not None else bundle.get("threshold", 0.5))
        if self.score_kind == "predict_proba":
            self.threshold = float(min(max(self.threshold, PROBA_EPS), 1.0 - PROBA_EPS))
        if fast and ema_alpha is None:
            ema_alpha = 1.0
        if fast and hysteresis is None:
            hysteresis = 0.03 if self.score_kind == "predict_proba" else None
        self.ema_alpha = float(
            ema_alpha if ema_alpha is not None else bundle.get("ema_alpha", 0.3)
        )
        # A fixed 0.06 band is meaningful on a 0-1 probability but arbitrary on
        # log-odds, which here span roughly -7 to +9. Calibration measures a
        # band in its own units from the spread of the empty room; use that
        # when it is available.
        if hysteresis is None and calibration is not None:
            hysteresis = calibration.get("hysteresis")
        self.hysteresis = float(
            hysteresis if hysteresis is not None else bundle.get("hysteresis", 0.06)
        )
        self._enter_at = self.threshold + self.hysteresis
        self._exit_at = self.threshold - self.hysteresis
        if self.score_kind == "predict_proba":
            # A calibrated threshold can land near 0 or 1, where a fixed band
            # runs off the end of the scale: with threshold 0.01 the exit point
            # is -0.05, which no probability is ever below, so the state
            # machine latches on OBJECT forever. Shrink the band to fit.
            self._enter_at = min(self._enter_at, 1.0 - PROBA_EPS)
            self._exit_at = max(self._exit_at, PROBA_EPS)
        self.buf: deque = deque(maxlen=self.window_size)
        self._packet_idx = -1
        self._ema_p: float | None = None
        self._state = "empty"
        self._last_arrival: float | None = None
        self.stalls_dropped = 0

    def reset(self) -> None:
        self.buf.clear()
        self._packet_idx = -1
        self._ema_p = None
        self._state = "empty"
        self._last_arrival = None

    def on_packet(
        self,
        iq: list[int],
        *,
        rssi: float = 0.0,
        agc_gain: float = 0.0,
        fft_gain: float = 0.0,
        seq: int | None = None,
        arrival: float | None = None,
    ) -> dict | None:
        # A gap longer than the whole training window span means the buffered
        # packets describe a channel from minutes ago. Start over rather than
        # predict across the seam.
        now = arrival if arrival is not None else time.monotonic()
        if (
            self.max_span_s
            and self._last_arrival is not None
            and now - self._last_arrival > self.max_span_s
            and self.buf
        ):
            self.stalls_dropped += 1
            self.buf.clear()
            self._packet_idx = -1
        self._last_arrival = now

        try:
            packet = iq_list_to_packet(
                iq,
                rssi=rssi,
                agc_gain=agc_gain,
                fft_gain=fft_gain,
                seq=seq,
                host_ts=now,
                normalize_gain=self.config.normalize_gain,
            )
            self.buf.append(packet)
        except ValueError:
            return None

        self._packet_idx += 1

        if len(self.buf) < self.window_size:
            return {
                "ready": False,
                "buffered": len(self.buf),
                "need": self.window_size,
            }

        # Training stride (15) or live_stride=1 in --fast mode (predict every packet once full).
        if (self._packet_idx - (self.window_size - 1)) % self.live_stride != 0:
            return None

        feat = window_to_features(
            list(self.buf),
            config=self.config,
            baseline_profile=self.baseline_profile,
            baseline_phase=self.baseline_phase,
        )
        scores, kind = score_windows(self.pipe, feat.reshape(1, -1), kind=self.score_kind)
        if kind != self.score_kind:
            # Only reachable if the bundle's estimator changed under a
            # calibration that passed the trained_at check, which it cannot.
            raise RuntimeError(
                f"score space {kind} != calibrated {self.score_kind}; recalibrate"
            )
        raw = float(scores[0])

        # Smooth in score space, because that is the space the threshold was
        # quantiled in. Smoothing a probability and thresholding a log-odds
        # would compare two different quantities.
        if self._ema_p is None:
            self._ema_p = raw
        else:
            self._ema_p = self.ema_alpha * raw + (1.0 - self.ema_alpha) * self._ema_p

        s = self._ema_p
        if self._state == "empty":
            if s >= self._enter_at:
                self._state = "object"
        elif s <= self._exit_at:
            self._state = "empty"

        return {
            "ready": True,
            "p_object": round(score_to_proba(s, self.score_kind), 4),
            "p_raw": round(score_to_proba(raw, self.score_kind), 4),
            "score": round(s, 4),
            "score_kind": self.score_kind,
            "state": self._state,
            "threshold": self.threshold,
        }

    def on_iq(self, iq: list[int], **meta: float) -> dict | None:
        """Backward-compatible wrapper — pass rssi/agc_gain/fft_gain via meta."""
        return self.on_packet(
            iq,
            rssi=float(meta.get("rssi", 0.0)),
            agc_gain=float(meta.get("agc_gain", 0.0)),
            fft_gain=float(meta.get("fft_gain", 0.0)),
        )


def format_line(result: dict, *, seq: int | None = None, rssi: int | None = None) -> str:
    if not result.get("ready"):
        # ~13.6 pkt/s measured in-burst on the 4.3 ESP-NOW pair.
        return (
            f"buffering {result['buffered']}/{result['need']} packets "
            f"(~{result['need'] / 13.6:.0f}s)…"
        )
    tag = result["state"].upper()
    extra = ""
    if seq is not None:
        extra += f" seq={seq}"
    if rssi is not None:
        extra += f" rssi={rssi}"
    if result.get("score_kind") == "decision_function":
        # Probability is uninformative once the model saturates, so lead with
        # the score the decision is actually made on.
        return (
            f"{tag:6s}  score={result['score']:+7.3f}  thr={result['threshold']:+.3f}  "
            f"p={result['p_object']:.4f}{extra}"
        )
    return (
        f"{tag:6s}  P(object)={result['p_object']:.3f}  "
        f"raw={result['p_raw']:.3f}  thr={result['threshold']:.3f}{extra}"
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
    p.add_argument(
        "--calibration",
        type=Path,
        help="Site calibration (default: models/site_calibration.joblib if present)",
    )
    p.add_argument(
        "--no-calibration",
        action="store_true",
        help="Ignore any site calibration and use the baked-in training baseline",
    )
    p.add_argument("--json", action="store_true", help="One JSON object per prediction")
    p.add_argument("--quiet", action="store_true", help="Only print on state change")
    p.add_argument(
        "--fast",
        action="store_true",
        help="Low-latency live mode: predict every packet (stride=1), no EMA, lower hysteresis",
    )
    p.add_argument("--eval", action="store_true", help="Replay CSV and print accuracy summary")
    args = p.parse_args()

    if not args.model.is_file():
        sys.exit(f"Model not found: {args.model}\nTrain first: ./run_detect.sh --train")

    bundle = joblib.load(args.model)
    model_version = bundle.get("feature_version")
    if model_version is not None and model_version != FEATURE_VERSION:
        sys.exit(
            f"Model feature v{model_version} != code v{FEATURE_VERSION}.\n"
            f"Retrain: cd csi_pipeline && ./run_detect.sh --train"
        )
    calibration = None
    cal_path = args.calibration or (args.model.parent / "site_calibration.joblib")
    if not args.no_calibration and cal_path.is_file():
        loaded = joblib.load(cal_path)
        why = check_calibration(loaded, bundle)
        if why:
            # An explicitly requested calibration that cannot be honoured is an
            # error; a stale one found by autodiscovery is only a warning, since
            # the operator did not ask for it.
            msg = f"calibration {cal_path.name} unusable: {why}"
            if args.calibration:
                sys.exit(f"{msg}\nRecalibrate: ./run_detect.sh --calibrate")
            print(f"WARNING: ignoring {msg}", file=sys.stderr)
            print("         Recalibrate: ./run_detect.sh --calibrate", file=sys.stderr)
        else:
            calibration = loaded
    elif args.calibration:
        sys.exit(f"Calibration not found: {cal_path}")

    detector = LiveDetector(
        bundle, threshold=args.threshold, fast=args.fast, calibration=calibration
    )
    last_state: str | None = None
    metrics = bundle.get("metrics", {})
    mode = "fast" if args.fast else "normal"
    print(
        f"model={bundle.get('model_type', '?')}  v{bundle.get('feature_version', 1)}  "
        f"mode={mode}  window={detector.window_size}  "
        f"live_stride={detector.live_stride}  threshold={detector.threshold:+.3f}"
        f" ({detector.score_kind})",
        file=sys.stderr,
    )
    print(f"features: {detector.config.describe()}", file=sys.stderr)
    if calibration is not None:
        print(
            f"calibrated: {cal_path.name}  {calibration['n_windows']} empty windows"
            f" @ {calibration['fpr']:.0%} FPR  ({calibration.get('source', '?')},"
            f" {calibration['calibrated_at'][:19]})",
            file=sys.stderr,
        )
        if calibration.get("fast", False) != args.fast:
            print(
                "WARNING: calibrated for "
                f"{'--fast' if calibration.get('fast') else 'normal'} mode but running "
                f"{'--fast' if args.fast else 'normal'}. The EMA differs, so the\n"
                "         false-positive rate will not be the one you asked for.",
                file=sys.stderr,
            )
    else:
        print(
            "NOT calibrated: using the training site's baseline and threshold.\n"
            "         Measured transfer to an uncalibrated new setup is ~0.55 balanced\n"
            "         accuracy (chance). Run ./run_detect.sh --calibrate first.",
            file=sys.stderr,
        )
    if args.fast:
        print(
            "fast: predict every packet after buffer fills (~2s warmup, then per-packet updates).",
            file=sys.stderr,
        )
    if metrics:
        print(
            f"trained metrics: bal_acc={metrics.get('balanced_accuracy', 0):.3f}  "
            f"acc={metrics.get('accuracy', 0):.3f}",
            file=sys.stderr,
        )
    if bundle.get("evaluation_trustworthy") is False:
        print(
            f"WARNING: those metrics are confounded — {bundle.get('evaluation_note', '')}\n"
            "         Treat live output as unvalidated until sessions are interleaved.",
            file=sys.stderr,
        )
    print("Ctrl+C to stop. Do not run idf.py monitor on the same port.", file=sys.stderr)

    eval_true: list[str] = []
    eval_pred: list[str] = []

    def handle_packet(iq: list[int], meta: dict | None = None) -> None:
        nonlocal last_state
        meta = meta or {}
        result = detector.on_packet(
            iq,
            rssi=float(meta.get("rssi") or 0.0),
            agc_gain=float(meta.get("agc_gain") or 0.0),
            fft_gain=float(meta.get("fft_gain") or 0.0),
            seq=meta.get("seq"),
        )
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
                    {
                        "seq": sample.get("seq"),
                        "rssi": sample.get("rssi"),
                        "agc_gain": sample.get("agc_gain"),
                        "fft_gain": sample.get("fft_gain"),
                        "true_label": true_label,
                    },
                )
                if not args.eval:
                    time.sleep(0.05)

        if args.eval:
            print("Use: ./run_detect.sh --eval-csv for labeled replay accuracy.", file=sys.stderr)
        return

    port = args.port or find_port()
    print(f"serial: {port} @ {args.baud}", file=sys.stderr)

    last_csi_at = time.monotonic()
    last_warn_at = 0.0
    csi_count = 0
    line_count = 0

    with serial.Serial(port, args.baud, timeout=1.0) as ser:
        ser.dtr = False
        ser.rts = False
        ser.reset_input_buffer()
        buf = ""
        while True:
            chunk = ser.read(ser.in_waiting or 1)
            if not chunk:
                now = time.monotonic()
                if now - last_csi_at > 5.0 and now - last_warn_at > 5.0:
                    last_warn_at = now
                    print(
                        f"waiting for CSI_DATA… ({csi_count} packets so far, "
                        f"{line_count} serial lines; is csi_send powered?)",
                        file=sys.stderr,
                    )
                continue
            buf += chunk.decode("utf-8", errors="replace")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                line_count += 1
                sample = parse_csi_line(line)
                if sample and sample.get("iq"):
                    csi_count += 1
                    if csi_count == 1:
                        print(
                            f"CSI stream OK (seq={sample.get('seq')} rssi={sample.get('rssi')})",
                            file=sys.stderr,
                        )
                    last_csi_at = time.monotonic()
                    handle_packet(
                        sample["iq"],
                        {
                            "seq": sample.get("seq"),
                            "rssi": sample.get("rssi"),
                            "agc_gain": sample.get("agc_gain"),
                            "fft_gain": sample.get("fft_gain"),
                        },
                    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)
