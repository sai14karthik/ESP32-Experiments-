#!/usr/bin/env python3
"""Derive a site-specific baseline profile and threshold from an empty room.

The model bundle ships two numbers that are properties of the *training site*,
not of the detector: the baseline amplitude profile every feature is measured
relative to, and the decision threshold. `cross_condition_eval.py` measured what
happens when they are carried into a new setup unchanged — balanced accuracy
falls to 0.549, which is chance. The representation survives the move (AUC 0.85
/ 0.95); the operating point does not.

So both have to come from the target site. This does that, using a recording of
the empty room only:

    baseline    median amplitude per active subcarrier over the recording
    threshold   the (1 - fpr) quantile of P(object) on those same windows

Neither step needs a labelled example of an object, which is what makes it an
install procedure rather than a second training run. You put the boards where
they will live, record a couple of minutes of nothing, and the detector adopts
that room as its definition of empty.

    ./calibrate_site.py --seconds 120                 # live, from the recv board
    ./calibrate_site.py --from-csv exports/pc.csv     # replay a capture's empty rows
    ./calibrate_site.py --fpr 0.05                    # stricter: fewer false alarms

Writes models/site_calibration.joblib, which detect_live.py picks up
automatically.
"""

from __future__ import annotations

import argparse
import glob
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np

from csi_features import (
    ACTIVE_IDX,
    FEATURE_VERSION,
    LABEL_EMPTY,
    LABEL_OBJECT,
    FeatureConfig,
    PacketRecord,
    WindowSpec,
    iq_list_to_packet,
)
from csi_parse import DEFAULT_BAUD, parse_csi_line
from detect_live import score_to_proba, score_windows
from train_object_detector import build_windows, load_packets

# Denser than the training stride. Windows are computed identically either way,
# so overlapping them only samples the empty distribution more finely, which is
# what a quantile estimate wants. It does not change what a window *is*.
CALIBRATION_STRIDE = 5

# The band the detector needs to cross to change its mind, as a fraction of the
# distance from the median empty window to the threshold. Expressed as a
# fraction so it lands in the score's own units, whatever those are.
HYSTERESIS_FRACTION = 0.25


def find_port() -> str:
    ports = sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/cu.usbserial*"))
    if not ports:
        sys.exit("No /dev/cu.usbmodem* — plug in the csi_recv board.")
    return ports[0]


# --------------------------------------------------------------------------
# collecting empty-room packets
# --------------------------------------------------------------------------


def collect_from_serial(
    port: str, baud: int, seconds: float, config: FeatureConfig
) -> list[PacketRecord]:
    import serial

    packets: list[PacketRecord] = []
    deadline = time.monotonic() + seconds
    last_report = 0.0
    print(f"serial: {port} @ {baud}", file=sys.stderr)
    print(f"Recording {seconds:.0f}s of the EMPTY room. Leave the area now.", file=sys.stderr)

    with serial.Serial(port, baud, timeout=1.0) as ser:
        ser.dtr = False
        ser.rts = False
        ser.reset_input_buffer()
        buf = ""
        while time.monotonic() < deadline:
            chunk = ser.read(ser.in_waiting or 1)
            if not chunk:
                continue
            buf += chunk.decode("utf-8", errors="replace")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                sample = parse_csi_line(line.strip())
                if not sample or not sample.get("iq"):
                    continue
                try:
                    packets.append(
                        iq_list_to_packet(
                            sample["iq"],
                            rssi=float(sample.get("rssi") or 0.0),
                            agc_gain=float(sample.get("agc_gain") or 0.0),
                            fft_gain=float(sample.get("fft_gain") or 0.0),
                            seq=sample.get("seq"),
                            host_ts=time.time(),
                            normalize_gain=config.normalize_gain,
                        )
                    )
                except ValueError:
                    continue
            now = time.monotonic()
            if now - last_report >= 5.0:
                last_report = now
                left = deadline - now
                print(
                    f"  {len(packets)} packets, {left:.0f}s left…",
                    end="\r",
                    file=sys.stderr,
                    flush=True,
                )
    print(" " * 50, end="\r", file=sys.stderr)
    return packets


def collect_from_lines(path: Path, config: FeatureConfig) -> list[PacketRecord]:
    """Replay a raw CSI_DATA serial log."""
    packets: list[PacketRecord] = []
    t = time.time()
    with path.open() as f:
        for line in f:
            sample = parse_csi_line(line.strip())
            if not sample or not sample.get("iq"):
                continue
            try:
                packets.append(
                    iq_list_to_packet(
                        sample["iq"],
                        rssi=float(sample.get("rssi") or 0.0),
                        agc_gain=float(sample.get("agc_gain") or 0.0),
                        fft_gain=float(sample.get("fft_gain") or 0.0),
                        seq=sample.get("seq"),
                        # Synthetic clock at the measured in-burst packet rate.
                        # Without timestamps every window looks discontiguous.
                        host_ts=t + len(packets) / 13.6,
                        normalize_gain=config.normalize_gain,
                    )
                )
            except ValueError:
                continue
    return packets


def collect_from_csv(path: Path, config: FeatureConfig) -> list[PacketRecord]:
    """Take only the EMPTY rows of a training CSV — object rows are ignored."""
    packets, labels, _, _ = load_packets(path, config=config)
    empty = [p for p, lab in zip(packets, labels) if lab == LABEL_EMPTY]
    if not empty:
        sys.exit(f"No empty/baseline rows in {path}")
    return empty


# --------------------------------------------------------------------------
# calibration
# --------------------------------------------------------------------------


def smooth(proba: np.ndarray, alpha: float) -> np.ndarray:
    """Replay the detector's EMA over the window sequence.

    The threshold is applied live to a smoothed probability, not a raw one.
    Quantiling the raw values would set the operating point on a distribution
    the detector never sees — EMA shrinks the spread, so the real false-positive
    rate would land below the requested one by an unknown margin.
    """
    if alpha >= 1.0:
        return proba
    out = np.empty_like(proba)
    acc = proba[0]
    for i, p in enumerate(proba):
        acc = alpha * p + (1.0 - alpha) * acc
        out[i] = acc
    return out


def calibrate(
    bundle: dict,
    packets: list[PacketRecord],
    *,
    fpr: float,
    stride: int,
    fast: bool,
) -> dict:
    config = FeatureConfig.from_dict(bundle.get("feature_config"))
    n = len(packets)
    labels = [LABEL_EMPTY] * n

    profile = np.median(np.stack([p.amp[ACTIVE_IDX] for p in packets], axis=0), axis=0)
    phase = None
    if config.use_phase:
        phase = np.median(np.stack([p.phase[ACTIVE_IDX] for p in packets], axis=0), axis=0)

    spec = WindowSpec(
        size=int(bundle["window_size"]),
        stride=stride,
        max_span_s=bundle.get("max_span_s", 12.0),
        max_seq_gap=bundle.get("max_seq_gap", 256),
    )
    ws = build_windows(
        packets,
        labels,
        ["calibration"] * n,
        spec,
        profile,
        phase,
        config=config,
        session_keys=["calibration"] * n,
    )
    if ws.X.shape[0] == 0:
        sys.exit(
            f"No usable windows from {n} packets "
            f"({ws.dropped_discontiguous} dropped for time/seq gaps).\n"
            "The link is stalling. Check that csi_send is powered and in range."
        )

    raw, score_kind = score_windows(bundle["pipeline"], ws.X)
    alpha = 1.0 if fast else float(bundle.get("ema_alpha", 0.3))
    scores = smooth(raw, alpha)

    # The empty room defines the null distribution. Everything above its
    # (1 - fpr) quantile is, by construction, as surprising as the top fpr of
    # normal variation — no labelled object needed to place that line.
    threshold = float(np.quantile(scores, 1.0 - fpr))
    median = float(np.median(scores))
    # Half a band below the threshold still leaves the exit point well above
    # the bulk of the empty room, so the detector does not chatter, but it
    # cannot latch either.
    hysteresis = abs(threshold - median) * HYSTERESIS_FRACTION

    rssi = np.array([p.rssi for p in packets], dtype=np.float64)
    return {
        "kind": "site_calibration",
        "baseline_profile": profile,
        "baseline_phase": phase,
        "threshold": threshold,
        "hysteresis": hysteresis,
        "score_kind": score_kind,
        "fpr": fpr,
        "ema_alpha": alpha,
        "fast": fast,
        # Identity of the model this threshold was measured against. A
        # threshold is a statement about one pipeline's score scale and is
        # meaningless attached to another.
        "feature_version": bundle.get("feature_version"),
        "feature_config": bundle.get("feature_config"),
        "model_trained_at": bundle.get("trained_at"),
        "model_type": bundle.get("model_type"),
        "n_packets": n,
        "n_windows": int(ws.X.shape[0]),
        "dropped_discontiguous": int(ws.dropped_discontiguous),
        "empty_score": {
            "median": median,
            "p90": float(np.quantile(scores, 0.90)),
            "p99": float(np.quantile(scores, 0.99)),
            "max": float(scores.max()),
            "min": float(scores.min()),
        },
        "rssi_mean": float(rssi.mean()),
        "rssi_sd": float(rssi.std()),
        "calibrated_at": datetime.now(timezone.utc).isoformat(),
    }


def report(cal: dict, bundle: dict) -> int:
    """Print the calibration and return the number of problems found."""
    problems = 0
    es = cal["empty_score"]
    kind = cal["score_kind"]

    print(f"\npackets {cal['n_packets']}  windows {cal['n_windows']}"
          f"  dropped {cal['dropped_discontiguous']}")
    print(f"RSSI    {cal['rssi_mean']:.1f} dBm  sd {cal['rssi_sd']:.2f}")

    old = bundle.get("baseline_profile")
    if old is not None and np.shape(old) == np.shape(cal["baseline_profile"]):
        r = float(np.corrcoef(np.asarray(old), cal["baseline_profile"])[0, 1])
        rel = float(
            np.sqrt(np.mean((cal["baseline_profile"] - old) ** 2)) / max(np.mean(old), 1e-9)
        )
        print(f"baseline vs trained:  r={r:+.3f}  rms diff {rel:.1%}")

    print(f"\nempty-room score ({kind}), EMA alpha={cal['ema_alpha']:.2f}:")
    print(f"  min {es['min']:+.3f}   median {es['median']:+.3f}   p90 {es['p90']:+.3f}"
          f"   p99 {es['p99']:+.3f}   max {es['max']:+.3f}")
    print(f"\nthreshold {cal['threshold']:+.4f}  at {cal['fpr']:.0%} false-positive rate")
    print(f"hysteresis {cal['hysteresis']:.4f}  "
          f"(enter {cal['threshold'] + cal['hysteresis']:+.3f}, "
          f"exit {cal['threshold'] - cal['hysteresis']:+.3f})")
    if kind == "decision_function":
        print(f"  equivalent probability {score_to_proba(cal['threshold'], kind):.6f} — "
              "saturated, which is why the threshold is set on log-odds")

    if cal["n_windows"] < 50:
        problems += 1
        print(
            f"\nWARNING: {cal['n_windows']} windows is too few to place a "
            f"{cal['fpr']:.0%} quantile.\n"
            "         Record longer — 120 s is the intended minimum."
        )
    if score_to_proba(es["median"], kind) >= 0.5:
        problems += 1
        print(
            "\nNOTE: the model calls the median empty window an OBJECT in absolute\n"
            "      terms. That is expected at a site it was not trained on, and is\n"
            "      exactly what this calibration corrects: the threshold below is\n"
            "      set from this room's own distribution, not from training."
        )
    if cal["rssi_sd"] > 3.0:
        problems += 1
        print(
            f"\nWARNING: RSSI sd {cal['rssi_sd']:.2f} dB — the channel moved during\n"
            "         calibration. Something was in the room. Re-record."
        )
    if es["max"] - es["min"] < 1e-9:
        problems += 1
        print(
            "\nWARNING: every empty window scored identically. The threshold is\n"
            "         meaningless. Check that CSI is actually varying."
        )
    return problems


def main() -> None:
    root = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--model", type=Path, default=root / "models" / "object_detector.joblib")
    p.add_argument("--out", type=Path, default=root / "models" / "site_calibration.joblib")
    p.add_argument("--port")
    p.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    p.add_argument("--seconds", type=float, default=120.0, help="Empty-room recording length")
    p.add_argument(
        "--fpr",
        type=float,
        default=0.10,
        help="Tolerated false-positive rate on the empty room (default 0.10)",
    )
    p.add_argument("--stride", type=int, default=CALIBRATION_STRIDE)
    p.add_argument("--from-file", type=Path, help="Replay a raw CSI_DATA serial log")
    p.add_argument("--from-csv", type=Path, help="Use the empty rows of a training CSV")
    p.add_argument(
        "--fast",
        action="store_true",
        help="Calibrate for --fast detection (no EMA). Must match how you run detect.",
    )
    p.add_argument("--dry-run", action="store_true", help="Report but do not write")
    args = p.parse_args()

    if not 0.0 < args.fpr < 1.0:
        sys.exit("--fpr must be strictly between 0 and 1")
    if not args.model.is_file():
        sys.exit(f"Model not found: {args.model}\nTrain first: ./run_detect.sh --train")

    bundle = joblib.load(args.model)
    if bundle.get("feature_version") != FEATURE_VERSION:
        sys.exit(
            f"Model feature v{bundle.get('feature_version')} != code v{FEATURE_VERSION}.\n"
            "Retrain before calibrating: ./run_detect.sh --train"
        )
    config = FeatureConfig.from_dict(bundle.get("feature_config"))

    print(f"model {args.model.name}  {bundle.get('model_type', '?')}"
          f"  trained {bundle.get('trained_at', '?')}")
    print(f"features: {config.describe()}")

    if args.from_csv:
        packets = collect_from_csv(args.from_csv, config)
        source = f"csv:{args.from_csv.name}"
    elif args.from_file:
        packets = collect_from_lines(args.from_file, config)
        source = f"file:{args.from_file.name}"
    else:
        port = args.port or find_port()
        packets = collect_from_serial(port, args.baud, args.seconds, config)
        source = f"serial:{port}"

    if len(packets) < bundle["window_size"]:
        sys.exit(
            f"Only {len(packets)} packets — need at least {bundle['window_size']} "
            "for one window. Is csi_send powered?"
        )

    cal = calibrate(bundle, packets, fpr=args.fpr, stride=args.stride, fast=args.fast)
    cal["source"] = source
    problems = report(cal, bundle)

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return

    args.out.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(cal, args.out)
    print(f"\nwrote {args.out}")
    if problems:
        print(f"{problems} warning(s) above — the calibration was saved anyway.")
    print("detect_live.py will use it automatically. Verify with a real object:")
    print("  ./run_detect.sh --quiet" + (" --fast" if args.fast else ""))


if __name__ == "__main__":
    main()
