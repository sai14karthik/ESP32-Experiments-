"""Live EMPTY/OCCUPIED from CSI TCP :9055 (no camera / YOLO)."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from csi_presence.live.runtime import run_score_loop
from csi_presence.model_io import (
    DEFAULT_CALIBRATION,
    DEFAULT_MODEL,
    PresenceDecision,
    load_bundle,
    load_calibration,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Live CSI presence (TCP :9055) — stop collect first"
    )
    p.add_argument("--listen-tcp", type=int, default=9055)
    p.add_argument(
        "--seconds",
        type=float,
        default=1e9,
        help="run length (default ~forever until Ctrl+C)",
    )
    p.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    p.add_argument(
        "--calibration",
        type=Path,
        default=DEFAULT_CALIBRATION,
        help="site_calibration.joblib (optional)",
    )
    p.add_argument(
        "--no-calibration",
        action="store_true",
        help="ignore calibration; threshold=0.5",
    )
    p.add_argument("--threshold", type=float, default=None)
    p.add_argument("--hysteresis", type=float, default=None)
    p.add_argument("--ema-alpha", type=float, default=None)
    p.add_argument(
        "--debounce",
        type=int,
        default=None,
        help="consecutive hops to flip state (default 2)",
    )
    p.add_argument("--live-hz", type=float, default=4.0)
    args = p.parse_args(argv)

    bundle = load_bundle(args.model)
    cal = None if args.no_calibration else load_calibration(args.calibration)
    decision = PresenceDecision.from_bundle(
        bundle,
        cal,
        threshold=args.threshold,
        hysteresis=args.hysteresis,
        ema_alpha=args.ema_alpha,
        debounce=args.debounce,
    )

    print(
        f"model={args.model}  feat_dim={bundle.get('feat_dim')}  "
        f"feature_version={bundle.get('feature_version')}  "
        f"window={bundle.get('window_sec')}s hop={bundle.get('hop_sec')}s"
    )
    print(
        f"decision: thr={decision.threshold:.3f}  "
        f"enter≥{decision.enter_at:.3f} exit≤{decision.exit_at:.3f}  "
        f"ema={decision.ema_alpha:.2f} debounce={decision.debounce}"
    )
    if not cal and args.threshold is None:
        print("calibration: none — run ./scripts/calibrate_presence.sh")
    print(
        f"CSI=TCP :{args.listen_tcp}  (stop collect_session / other :9055 owners first)"
    )
    print("Ctrl+C to stop")

    last_print = 0.0
    live_period = 1.0 / max(args.live_hz, 0.2)
    n_empty = 0
    n_occ = 0

    def on_score(
        elapsed: float,
        p_raw: float,
        p_ema: float,
        occupied: bool,
        thr: float,
        n_csi: int,
        status: str,
    ) -> None:
        nonlocal last_print, n_empty, n_occ
        if status == "OK":
            if occupied:
                n_occ += 1
            else:
                n_empty += 1
        now = time.time()
        if now - last_print < live_period:
            return
        last_print = now
        if status == "STALE":
            tag = "STALE"
        elif status == "WAIT":
            tag = "WAIT"
        else:
            tag = "OCCUPIED" if occupied else "EMPTY"
        print(
            f"\r  t={elapsed:6.1f}s  {tag:8s}  "
            f"P={p_raw:.3f} ema={p_ema:.3f} thr={thr:.3f}  "
            f"empty={n_empty} occupied={n_occ}  n={n_csi}    ",
            end="",
            flush=True,
        )

    scores = run_score_loop(
        bundle=bundle,
        listen_tcp=args.listen_tcp,
        seconds=args.seconds,
        calibration=cal,
        decision=decision,
        on_score=on_score,
    )
    print()
    if not scores:
        print("WARNING: no scores — no CSI received")
        return 1
    print(
        f"done: {len(scores)} scores  "
        f"P mean={sum(scores)/len(scores):.3f}  "
        f"live empty={n_empty} occupied={n_occ}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
