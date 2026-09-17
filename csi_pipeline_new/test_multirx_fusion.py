#!/usr/bin/env python3
"""End-to-end checks for N-board multi-RX windowing + feature concat fusion."""

from __future__ import annotations

import csv
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import joblib
import numpy as np

from csi_features import FeatureConfig, LABEL_EMPTY, LABEL_OBJECT, configure_from_iq_len
from train_object_detector import (
    WindowSpec,
    build_windows,
    fuse_multirx_windows,
    load_packets,
    vote_or_session_score,
)

ROOT = Path(__file__).resolve().parent
N_SC = 52  # smaller than C5 default — faster synthetic tests
IQ_LEN = N_SC * 2


def _iq(seed: int, occupied: bool) -> str:
    rng = np.random.default_rng(seed)
    # Occupied: higher amplitude on a mid-band slice so fusion has a real cue.
    amp = 8.0 + (6.0 if occupied else 0.0)
    noise = rng.normal(0.0, 1.5, size=IQ_LEN)
    vals = (noise + amp).astype(np.int32)
    if occupied:
        vals[20:40] += 12
    return ",".join(str(int(v)) for v in vals)


def write_synthetic_csv(
    path: Path,
    *,
    n_rx: int,
    sessions_per_class: int = 3,
    packets_per_stream: int = 90,
    drop_rx_fraction: float = 0.0,
) -> list[str]:
    """Write interleaved empty/occupied captures with N source_id boards."""
    sources = [f"10.128.93.{20 + i}" for i in range(n_rx)]
    t0 = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
    rows: list[dict[str, str]] = []
    seq = 0
    rng = np.random.default_rng(0)

    for cls_i, (prefix, occupied) in enumerate(
        (("empty", False), ("occupied", True))
    ):
        for s in range(sessions_per_class):
            sid = f"{prefix}_{s:02d}"
            label = f"{prefix}_{s:02d}"
            base = t0 + timedelta(minutes=cls_i * 60 + s * 5)
            for k in range(packets_per_stream):
                ts = base + timedelta(milliseconds=k * 80)
                for src in sources:
                    if drop_rx_fraction > 0 and rng.random() < drop_rx_fraction:
                        continue
                    seq += 1
                    rows.append(
                        {
                            "session_id": sid,
                            "label": label,
                            "source_id": src,
                            "seq": str(seq),
                            "mac": "aa:bb:cc:dd:ee:ff",
                            "rssi": str(-40 if occupied else -45),
                            "channel": "36",
                            "len": str(IQ_LEN),
                            "device_ts": str(seq * 1000),
                            "host_ts": ts.isoformat(),
                            "noise_floor": "-90",
                            "fft_gain": "20",
                            "agc_gain": "10",
                            "iq": _iq(seq + hash(src) % 1000, occupied),
                        }
                    )

    # Export order: session, source, time (matches export_training_csv).
    rows.sort(key=lambda r: (r["session_id"], r["source_id"], r["host_ts"], int(r["seq"])))
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    return sources


def assert_true(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_fuse_unit_n(n_rx: int) -> None:
    configure_from_iq_len(IQ_LEN)
    with tempfile.TemporaryDirectory() as td:
        csv_path = Path(td) / f"n{n_rx}.csv"
        sources = write_synthetic_csv(csv_path, n_rx=n_rx)
        config = FeatureConfig()
        packets, labels, session_labels, stream_keys, group_keys = load_packets(
            csv_path, config=config
        )
        assert_true(len(packets) > 0, "no packets")
        from csi_features import compute_baseline_phase_profile, compute_baseline_profile

        bp = compute_baseline_profile(packets, labels)
        bph = compute_baseline_phase_profile(packets, labels)
        ws = build_windows(
            packets,
            labels,
            session_labels,
            WindowSpec(30, 15, max_span_s=12.0),
            bp,
            bph,
            config=config,
            session_keys=stream_keys,
            group_keys=group_keys,
        )
        assert_true(ws.y.size > 0, f"N={n_rx}: no windows")
        assert_true(ws.median_span_s > 0, f"N={n_rx}: median span should be >0")
        got = sorted({s for s in ws.sources if s not in ("", "unknown", "fused")})
        assert_true(got == sources, f"N={n_rx}: sources {got} != {sources}")

        fused, order = fuse_multirx_windows(ws, bin_s=1.0, min_rx=2)
        assert_true(order == sources, f"N={n_rx}: order mismatch")
        assert_true(fused.sources[0] == "fused", f"N={n_rx}: not fused")
        assert_true(
            fused.X.shape[1] == ws.X.shape[1] * n_rx,
            f"N={n_rx}: dims {fused.X.shape[1]} != {ws.X.shape[1]}*{n_rx}",
        )
        assert_true(fused.y.size > 0, f"N={n_rx}: 0 fused windows")
        assert_true(set(fused.y.tolist()) == {LABEL_EMPTY, LABEL_OBJECT}, "both labels")

        vote = vote_or_session_score(ws, model_name="logreg", bin_s=1.0)
        assert_true(bool(vote), f"N={n_rx}: OR-vote empty")
        print(
            f"  unit N={n_rx}: per-RX windows={ws.y.size} dims={ws.X.shape[1]}  "
            f"fused={fused.y.size} dims={fused.X.shape[1]}  "
            f"OR-vote bal={vote['balanced_accuracy']:.3f}"
        )


def test_train_cli(n_rx: int, out_dir: Path) -> Path:
    csv_path = out_dir / f"synthetic_n{n_rx}.csv"
    model_path = out_dir / f"model_n{n_rx}.joblib"
    sources = write_synthetic_csv(csv_path, n_rx=n_rx, sessions_per_class=3)

    cmd = [
        sys.executable,
        str(ROOT / "train_object_detector.py"),
        "--csv",
        str(csv_path),
        "--out",
        str(model_path),
        "--deploy",
        "--rx-fusion",
        "auto",
        "--window",
        "30",
        "--stride",
        "15",
        "--time-blocks",
        "2",
    ]
    print(f"  train CLI N={n_rx}: …")
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr)
        raise AssertionError(f"train failed N={n_rx} rc={proc.returncode}")

    assert_true(model_path.is_file(), "model not written")
    bundle = joblib.load(model_path)
    assert_true(bundle.get("rx_fusion") == "concat", f"expected concat, got {bundle.get('rx_fusion')}")
    assert_true(bundle.get("rx_sources_order") == sources, "rx_sources_order mismatch")
    n_per = bundle.get("n_features_per_rx")
    assert_true(isinstance(n_per, int) and n_per > 0, "n_features_per_rx missing")
    pipe = bundle["pipeline"]
    X = np.zeros((1, n_per * n_rx), dtype=np.float64)
    proba = pipe.predict_proba(X)
    assert_true(proba.shape == (1, 2), f"predict_proba shape {proba.shape}")
    print(
        f"  train CLI N={n_rx}: OK  fusion={bundle['rx_fusion']}  "
        f"metrics_bal={bundle.get('metrics', {}).get('balanced_accuracy', float('nan')):.3f}"
    )
    return model_path


def test_train_none_and_min_all(out_dir: Path) -> None:
    csv_path = out_dir / "synthetic_n3_dropout.csv"
    write_synthetic_csv(csv_path, n_rx=3, drop_rx_fraction=0.15)

    for fusion, extra, expect in (
        ("none", [], "none"),
        ("concat", ["--rx-min", "all"], "concat"),
    ):
        model_path = out_dir / f"model_fusion_{fusion}.joblib"
        cmd = [
            sys.executable,
            str(ROOT / "train_object_detector.py"),
            "--csv",
            str(csv_path),
            "--out",
            str(model_path),
            "--deploy",
            "--rx-fusion",
            fusion,
            *extra,
            "--time-blocks",
            "2",
        ]
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
        if proc.returncode != 0:
            print(proc.stdout)
            print(proc.stderr)
            raise AssertionError(f"train --rx-fusion {fusion} failed")
        bundle = joblib.load(model_path)
        assert_true(
            bundle.get("rx_fusion") == expect,
            f"fusion={fusion}: got {bundle.get('rx_fusion')} expect {expect}",
        )
        print(f"  train --rx-fusion {fusion} {' '.join(extra)}: OK → {expect}")


def test_single_rx_auto_skips_fusion(out_dir: Path) -> None:
    csv_path = out_dir / "synthetic_n1.csv"
    model_path = out_dir / "model_n1.joblib"
    write_synthetic_csv(csv_path, n_rx=1)
    cmd = [
        sys.executable,
        str(ROOT / "train_object_detector.py"),
        "--csv",
        str(csv_path),
        "--out",
        str(model_path),
        "--deploy",
        "--rx-fusion",
        "auto",
        "--time-blocks",
        "2",
    ]
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr)
        raise AssertionError("N=1 train failed")
    bundle = joblib.load(model_path)
    assert_true(bundle.get("rx_fusion") == "none", "N=1 should not concat")
    print("  train N=1 auto: OK (no fusion)")


def test_multirx_live_detector(out_dir: Path) -> None:
    from detect_live import MultiRxLiveDetector, make_live_detector
    from csi_features import configure_from_iq_len

    csv_path = out_dir / "live_n3.csv"
    model_path = out_dir / "live_model.joblib"
    sources = write_synthetic_csv(csv_path, n_rx=3, sessions_per_class=3)
    cmd = [
        sys.executable,
        str(ROOT / "train_object_detector.py"),
        "--csv",
        str(csv_path),
        "--out",
        str(model_path),
        "--deploy",
        "--rx-fusion",
        "concat",
        "--time-blocks",
        "2",
    ]
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    assert_true(proc.returncode == 0, f"train for live failed\n{proc.stderr}")
    bundle = joblib.load(model_path)
    det = make_live_detector(bundle, fast=True)
    assert_true(isinstance(det, MultiRxLiveDetector), "expected MultiRxLiveDetector")
    assert_true(det.min_rx == 1, f"default min_rx should be 1, got {det.min_rx}")
    configure_from_iq_len(IQ_LEN)

    ready = None
    t0 = 1000.0
    # Feed ~window packets to each RX with aligned timestamps
    for k in range(bundle["window_size"] + 5):
        for src in sources:
            iq = [int(x) for x in _iq(k * 10 + hash(src) % 100, occupied=False).split(",")]
            ready = det.on_packet(
                iq,
                source_id=src,
                rssi=-45.0,
                agc_gain=10.0,
                fft_gain=20.0,
                seq=k,
                arrival=t0 + k * 0.08,
            )
    assert_true(ready is not None and ready.get("ready"), f"not ready: {ready}")
    assert_true(ready.get("rx_present", 0) >= 2, f"rx_present={ready}")
    assert_true("state" in ready, "missing state")
    print(
        f"  live MultiRx: OK  state={ready['state']}  "
        f"rx={ready.get('rx_present')}/{ready.get('rx_total')}  "
        f"p={ready.get('p_presence', ready.get('p_object'))}"
    )

    # Remove one RX — default fault-tolerant live should keep predicting.
    det.idle_s = 0.4
    last = None
    for k in range(bundle["window_size"] + 8):
        for src in sources[:2]:
            iq = [int(x) for x in _iq(500 + k * 3 + hash(src) % 7, occupied=True).split(",")]
            last = det.on_packet(
                iq,
                source_id=src,
                rssi=-40.0,
                agc_gain=10.0,
                fft_gain=20.0,
                seq=k,
                arrival=t0 + 50 + k * 0.08,
            )
    assert_true(last is not None and last.get("ready"), f"2/3 not ready: {last}")
    assert_true(last.get("rx_present", 0) >= 1, f"rx_present after remove: {last}")
    print(f"  live remove-1: OK  rx={last.get('rx_present')}/{last.get('rx_total')}")

    # Only 1 of N — must still predict (min_rx=1).
    det1 = make_live_detector(bundle, fast=True)
    det1.idle_s = 0.4
    last = None
    for k in range(bundle["window_size"] + 8):
        src = sources[0]
        iq = [int(x) for x in _iq(700 + k * 2, occupied=False).split(",")]
        last = det1.on_packet(
            iq,
            source_id=src,
            rssi=-41.0,
            agc_gain=10.0,
            fft_gain=20.0,
            seq=k,
            arrival=t0 + 200 + k * 0.08,
        )
    assert_true(last is not None and last.get("ready"), f"1/3 not ready: {last}")
    assert_true(last.get("rx_present") == 1, f"expected rx_present=1: {last}")
    print(f"  live only-1: OK  rx={last.get('rx_present')}/{last.get('rx_total')}")

    # Hot-plug a new IP into the idle third slot.
    det2 = make_live_detector(bundle, fast=True)
    det2.idle_s = 0.4
    new_ip = "10.9.9.9"
    last = None
    for k in range(bundle["window_size"] + 5):
        live_srcs = [sources[0], sources[1], new_ip]
        for src in live_srcs:
            iq = [int(x) for x in _iq(900 + k * 5 + hash(src) % 11, occupied=False).split(",")]
            last = det2.on_packet(
                iq,
                source_id=src,
                rssi=-42.0,
                agc_gain=10.0,
                fft_gain=20.0,
                seq=k,
                arrival=3000.0 + k * 0.08,
            )
    assert_true(last is not None and last.get("ready"), f"hotplug not ready: {last}")
    assert_true(new_ip in (last.get("rx_aliases") or {}), f"no alias: {last}")
    print(f"  live hotplug: OK  aliases={last.get('rx_aliases')}")


def main() -> int:
    print("=== multi-RX fusion tests ===")
    try:
        for n in (2, 3, 5):
            test_fuse_unit_n(n)
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            for n in (2, 3, 5):
                test_train_cli(n, out_dir)
            test_train_none_and_min_all(out_dir)
            test_single_rx_auto_skips_fusion(out_dir)
            test_multirx_live_detector(out_dir)
    except Exception as exc:
        print(f"\nFAIL: {exc}")
        return 1
    print("\nALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
