"""Local smoke: seed DB → train → calibrate bundle → short live TCP."""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import numpy as np
import psycopg

from csi_presence.csi_parse import PEER_MAC_4_2, parse_csi_line
from csi_presence.db import PresenceDb
from csi_presence.features import window_features
from csi_presence.model_io import (
    DEFAULT_CALIBRATION,
    DEFAULT_MODEL,
    load_bundle,
    load_calibration,
    save_bundle,
)

ROOT = Path(__file__).resolve().parents[1]
URL = "postgresql:///csi"


def main() -> int:
    tag = f"trainsmoke_{uuid.uuid4().hex[:8]}"
    iq = ",".join(str(i % 17 - 8) for i in range(234))
    peer = PEER_MAC_4_2

    db = PresenceDb(URL, batch_size=32)
    db.ensure_schema()
    for label in (0, 1, 0, 1):
        db.start_session(
            method="4.2", label=tag, recv_port="tcp:9055", session_tag=tag
        )
        t0 = time.time()
        buf = []
        base_rssi = -45 if label == 1 else -65
        for i in range(80):
            rssi = base_rssi + (i % 3)
            line = (
                f"CSI_DATA,{i},{peer},{rssi},11,-90,1,2,6,{1000 + i},100,0,234,0,"
                f'"[{iq}]"'
            )
            s = parse_csi_line(line, t0 + i * 0.05, source_id="10.128.93.21")
            if s is None:
                raise SystemExit(f"parse failed: {line[:80]}")
            db.enqueue_sample(s)
            buf.append(s)
        db.flush_samples()
        # hops inside sample span (~4s of CSI at 20 Hz)
        for w in range(25):
            now = t0 + 1.0 + w * 0.1
            win = [s for s in buf if now - 1.0 <= s.host_ts <= now]
            feats = window_features(win)
            if feats is None:
                raise SystemExit(f"features None n={len(win)} now={now - t0:.2f}")
            f = feats.tolist()
            f = [v + 2.0 for v in f] if label == 1 else [v - 2.0 for v in f]
            db.insert_window(
                host_ts=now,
                label=label,
                label_src="forced",
                method="4.2",
                n_csi=len(win),
                sources=["10.128.93.21"],
                macs=[peer],
                feat=f,
            )
        db.end_session()
    db.close()
    print("seeded", tag)

    r = subprocess.run(
        [
            "uv",
            "run",
            "csi-train-presence",
            "--session-tag",
            tag,
            "--min-per-class",
            "20",
            "--test-size",
            "0.25",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    print(r.stdout)
    if r.returncode != 0:
        print(r.stderr, file=sys.stderr)
        return r.returncode
    bundle = load_bundle()
    print("train OK bal_acc", bundle["metrics"].get("bal_acc"))

    scores_empty = list(np.random.default_rng(0).normal(0.15, 0.05, size=80))
    empty_mean = float(np.mean(scores_empty))
    empty_std = float(np.std(scores_empty))
    thr = min(
        0.95,
        max(
            empty_mean + 3 * empty_std,
            float(np.percentile(scores_empty, 99)),
            empty_mean + 0.05,
        ),
    )
    save_bundle(
        DEFAULT_CALIBRATION,
        {
            "threshold": thr,
            "empty_mean": empty_mean,
            "empty_std": empty_std,
            "n_scores": len(scores_empty),
            "k_std": 3.0,
        },
    )
    cal = load_calibration()
    assert cal and cal["threshold"] > empty_mean
    print("calibrate OK thr", cal["threshold"])

    ip = subprocess.check_output(
        "ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true",
        shell=True,
        text=True,
    ).strip()
    if not ip:
        print("skip live TCP (no LAN IP)")
    else:
        port = 19057

        def feeder() -> None:
            for _ in range(80):
                try:
                    s = socket.create_connection((ip, port), timeout=0.5)
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                return
            try:
                for i in range(60):
                    line = (
                        f"CSI_DATA,{i},{peer},{-52},11,-90,1,2,6,{2000 + i},100,0,234,0,"
                        f'"[{iq}]"\n'
                    )
                    s.sendall(line.encode())
                    time.sleep(0.04)
            except BrokenPipeError:
                pass
            finally:
                s.close()

        threading.Thread(target=feeder, daemon=True).start()
        r2 = subprocess.run(
            [
                "uv",
                "run",
                "csi-live-presence",
                "--listen-tcp",
                str(port),
                "--seconds",
                "5",
                "--model",
                str(DEFAULT_MODEL),
                "--calibration",
                str(DEFAULT_CALIBRATION),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        print(r2.stdout[-1200:])
        if r2.returncode != 0:
            print(r2.stderr[-800:], file=sys.stderr)
            return r2.returncode

    with psycopg.connect(URL) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM csi_sessions WHERE label = %s", (tag,))
        conn.commit()
    print("=== TRAIN/LIVE SMOKE PASSED ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
