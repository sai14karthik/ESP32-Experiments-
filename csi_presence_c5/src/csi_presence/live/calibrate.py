"""Empty-room calibration → site_calibration.joblib threshold."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from csi_presence.live.runtime import run_score_loop
from csi_presence.model_io import (
    DEFAULT_CALIBRATION,
    DEFAULT_DEBOUNCE,
    DEFAULT_HYSTERESIS,
    DEFAULT_MODEL,
    PresenceDecision,
    load_bundle,
    save_bundle,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Calibrate presence threshold from empty-room CSI (leave room EMPTY)"
    )
    p.add_argument("--listen-tcp", type=int, default=9055)
    p.add_argument(
        "--seconds",
        type=float,
        default=90.0,
        help="empty-room collection length (default 90s)",
    )
    p.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    p.add_argument("--out", type=Path, default=DEFAULT_CALIBRATION)
    p.add_argument(
        "--k-std",
        type=float,
        default=3.0,
        help="also use empty_mean + k_std * empty_std (default 3)",
    )
    p.add_argument(
        "--percentile",
        type=float,
        default=95.0,
        help="empty-score percentile (default 95; ESPectre-style)",
    )
    p.add_argument(
        "--pctl-mult",
        type=float,
        default=1.1,
        help="threshold ≥ percentile * mult (default 1.1)",
    )
    p.add_argument(
        "--hysteresis",
        type=float,
        default=DEFAULT_HYSTERESIS,
        help=f"enter/exit band around threshold (default {DEFAULT_HYSTERESIS})",
    )
    p.add_argument(
        "--debounce",
        type=int,
        default=DEFAULT_DEBOUNCE,
        help=f"consecutive hops to flip (default {DEFAULT_DEBOUNCE})",
    )
    p.add_argument(
        "--min-scores",
        type=int,
        default=40,
        help="abort if fewer scored windows (default 40)",
    )
    p.add_argument(
        "--max-empty-mean",
        type=float,
        default=0.45,
        help="refuse calibration if empty mean P exceeds this (model too hot)",
    )
    args = p.parse_args(argv)

    bundle = load_bundle(args.model)
    # During calibration, use raw scores (no hysteresis state) via a fresh decision
    # with threshold 1.0 so state stays EMPTY while we collect the score distribution.
    cal_decision = PresenceDecision(
        threshold=1.0, hysteresis=0.0, ema_alpha=1.0
    )
    print(
        f"model={args.model}  leave the room EMPTY for {args.seconds:.0f}s  "
        f"TCP :{args.listen_tcp}"
    )
    print("stop collect_session if it owns :9055")

    last = 0.0

    def on_score(
        elapsed: float,
        p_raw: float,
        p_ema: float,
        occupied: bool,
        thr: float,
        n_csi: int,
        status: str,
    ) -> None:
        nonlocal last
        import time

        now = time.time()
        if now - last < 1.0:
            return
        last = now
        print(
            f"\r  calibrating t={elapsed:5.1f}s  P={p_raw:.3f}  "
            f"status={status}  n={n_csi}    ",
            end="",
            flush=True,
        )

    scores = run_score_loop(
        bundle=bundle,
        listen_tcp=args.listen_tcp,
        seconds=args.seconds,
        calibration=None,
        decision=cal_decision,
        on_score=on_score,
    )
    print()
    if len(scores) < args.min_scores:
        raise SystemExit(
            f"only {len(scores)} scores (need ≥{args.min_scores}) — "
            "check peer+sense CSI on :9055"
        )

    arr = np.asarray(scores, dtype=np.float64)
    empty_mean = float(arr.mean())
    empty_std = float(arr.std())
    pctl = float(np.percentile(arr, args.percentile))
    thr_pctl = pctl * args.pctl_mult
    thr_std = empty_mean + args.k_std * empty_std
    # Conservative: above empty distribution, ESPectre-style P95×1.1, capped
    threshold = min(0.95, max(thr_pctl, thr_std, empty_mean + 0.05))
    if empty_mean > args.max_empty_mean:
        raise SystemExit(
            f"REFUSED: empty-room mean P={empty_mean:.3f} > {args.max_empty_mean} — "
            "collect more empty data, rebuild features, retrain, then recalibrate"
        )
    if empty_mean > 0.35:
        print(
            f"WARNING: empty-room mean P={empty_mean:.3f} is elevated — "
            "prefer more empty training / check feature_version"
        )

    cal = {
        "threshold": threshold,
        "hysteresis": float(args.hysteresis),
        "debounce": int(args.debounce),
        "empty_mean": empty_mean,
        "empty_std": empty_std,
        "empty_pctl": pctl,
        "percentile": args.percentile,
        "pctl_mult": args.pctl_mult,
        "k_std": args.k_std,
        "n_scores": len(scores),
        "calibrated_at": datetime.now(timezone.utc).isoformat(),
        "model_path": str(args.model),
        "feature_version": bundle.get("feature_version"),
        "window_sec": bundle.get("window_sec"),
        "hop_sec": bundle.get("hop_sec"),
    }
    out = save_bundle(args.out, cal)
    print(
        f"empty mean={empty_mean:.3f} std={empty_std:.3f} "
        f"p{args.percentile:.0f}={pctl:.3f}×{args.pctl_mult} → threshold={threshold:.3f}  "
        f"hysteresis=±{args.hysteresis} debounce={args.debounce}"
    )
    print(f"saved → {out}")
    print("next: ./scripts/live_presence.sh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
