#!/usr/bin/env python3
"""Vigorous E2E: seed DB → rebuild → train → fake-cal → live TCP → doctor checks."""

from __future__ import annotations

import json
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
from csi_presence.db import PresenceDb, database_url
from csi_presence.features import FEATURE_VERSION, window_features
from csi_presence.model_io import (
    DEFAULT_CALIBRATION,
    DEFAULT_MODEL,
    PresenceDecision,
    load_bundle,
    load_calibration,
    save_bundle,
)
from csi_presence.quality import window_ok

ROOT = Path(__file__).resolve().parents[1]
URL = database_url()
PEER = PEER_MAC_4_2


def _iq(seed: int = 0) -> str:
    rng = np.random.default_rng(seed)
    vals = (rng.integers(-8, 9, size=234)).tolist()
    return ",".join(str(v) for v in vals)


def _line(seq: int, rssi: int, iq: str) -> str:
    return (
        f"CSI_DATA,{seq},{PEER},{rssi},11,-90,1,2,6,{1000 + seq},100,0,234,0,"
        f'"[{iq}]"'
    )


def seed(tag: str) -> None:
    """4 sessions, balanced labels, separable temporal churn for occupied."""
    db = PresenceDb(URL, batch_size=64)
    db.ensure_schema()
    for sess in range(4):
        db.start_session(
            method="4.2", label=tag, recv_port="tcp:9055", session_tag=tag
        )
        t0 = time.time() + sess * 1000  # distinct timelines
        for label in (0, 1):
            iq0 = _iq(sess * 10 + label)
            buf = []
            for i in range(60):
                # occupied: inject frame-varying IQ so temporal diff rises
                if label == 1:
                    iq = _iq(sess * 1000 + label * 100 + i)
                    rssi = -48
                else:
                    iq = iq0
                    rssi = -62
                line = _line(i, rssi, iq)
                s = parse_csi_line(
                    line, t0 + label * 5.0 + i * 0.05, source_id="10.128.93.21"
                )
                assert s is not None
                db.enqueue_sample(s)
                buf.append(s)
            db.flush_samples()
            for w in range(20):
                now = t0 + label * 5.0 + 1.0 + w * 0.1
                win = [s for s in buf if now - 1.0 <= s.host_ts <= now]
                ok, _ = window_ok(win, window_sec=1.0)
                feats = window_features(win) if ok else None
                if feats is None:
                    continue
                db.insert_window(
                    host_ts=now,
                    label=label,
                    label_src="forced",
                    method="4.2",
                    n_csi=len(win),
                    sources=["10.128.93.21"],
                    macs=[PEER],
                    feat=feats.tolist(),
                    feature_version=FEATURE_VERSION,
                )
        db.end_session()
    db.close()
    print(f"[seed] tag={tag} feature_version={FEATURE_VERSION}")


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(cmd))
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if r.stdout:
        print(r.stdout[-2000:])
    if r.returncode != 0 and check:
        print(r.stderr[-2000:], file=sys.stderr)
        raise SystemExit(r.returncode)
    return r


def count_windows(tag: str, ver: int) -> tuple[int, int, int]:
    with psycopg.connect(URL) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*), count(*) FILTER (WHERE label=0),
                   count(*) FILTER (WHERE label=1)
            FROM presence_windows
            WHERE session_tag = %s AND feature_version = %s
            """,
            (tag, ver),
        )
        n, e, o = cur.fetchone()
    return int(n), int(e), int(o)


def main() -> int:
    tag = f"vig_{uuid.uuid4().hex[:8]}"
    print("URL", URL)
    print("=== 1) unit tests ===")
    run(["uv", "run", "pytest", "tests/", "-q"])

    print("=== 2) seed ===")
    seed(tag)
    n, e, o = count_windows(tag, FEATURE_VERSION)
    print(f"seeded windows n={n} empty={e} occupied={o}")
    assert n >= 80 and e >= 30 and o >= 30, (n, e, o)

    print("=== 3) rebuild (idempotent v3) ===")
    run(["uv", "run", "csi-rebuild-features", "--session-tag", tag])
    n2, e2, o2 = count_windows(tag, FEATURE_VERSION)
    print(f"after rebuild n={n2} empty={e2} occupied={o2}")
    assert n2 >= 50, n2

    print("=== 4) train ===")
    run(
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
        ]
    )
    assert DEFAULT_MODEL.exists()
    bundle = load_bundle()
    assert int(bundle.get("feature_version") or 0) == FEATURE_VERSION
    assert bundle.get("feat_dim") == len(bundle["scaler"].mean_)
    meta = json.loads(DEFAULT_MODEL.with_suffix(".json").read_text())
    print(
        "train bal_acc",
        meta.get("metrics", {}).get("bal_acc"),
        "gcv",
        meta.get("metrics", {}).get("group_cv_bal_acc"),
        "neg",
        meta.get("metrics", {}).get("negative_control_bal_acc"),
        "trust",
        meta.get("metrics", {}).get("evaluation_trustworthy"),
    )
    assert "ema_alpha" in bundle and "hysteresis" in bundle and "debounce" in bundle

    print("=== 5) synthetic calibration bundle ===")
    # Mimic empty-room scores (low P)
    rng = np.random.default_rng(0)
    empty_scores = list(rng.normal(0.12, 0.04, size=100).clip(0, 1))
    empty_mean = float(np.mean(empty_scores))
    p95 = float(np.percentile(empty_scores, 95))
    thr = min(0.95, max(p95 * 1.1, empty_mean + 3 * float(np.std(empty_scores))))
    save_bundle(
        DEFAULT_CALIBRATION,
        {
            "threshold": thr,
            "hysteresis": 0.06,
            "debounce": 2,
            "empty_mean": empty_mean,
            "empty_pctl": p95,
            "n_scores": len(empty_scores),
            "feature_version": FEATURE_VERSION,
        },
    )
    cal = load_calibration()
    assert cal and cal["threshold"] > empty_mean
    print(f"cal thr={cal['threshold']:.3f}")

    print("=== 6) decision unit ===")
    d = PresenceDecision.from_bundle(bundle, cal)
    assert d.debounce >= 1
    # need debounce votes to enter
    entered = False
    for _ in range(d.debounce + 1):
        entered = d.update(0.99)[0]
    assert entered is True

    print("=== 7) live TCP E2E ===")
    ip = subprocess.check_output(
        "ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true",
        shell=True,
        text=True,
    ).strip()
    if not ip:
        print("SKIP live TCP (no LAN IP)")
    else:
        port = 19059
        iq = _iq(42)

        def feeder() -> None:
            for _ in range(100):
                try:
                    s = socket.create_connection((ip, port), timeout=0.5)
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                return
            try:
                for i in range(80):
                    s.sendall((_line(i, -55, iq) + "\n").encode())
                    time.sleep(0.04)
            except BrokenPipeError:
                pass
            finally:
                s.close()

        threading.Thread(target=feeder, daemon=True).start()
        r = run(
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
                "--debounce",
                "1",
            ],
            check=False,
        )
        assert r.returncode == 0, r.stderr
        assert "done:" in r.stdout or "P=" in r.stdout

    print("=== 8) doctor + db_dist ===")
    run(["uv", "run", "csi-db-dist", "--session", tag], check=False)
    run(["uv", "run", "csi-doctor"], check=False)

    print("=== 9) cleanup ===")
    with psycopg.connect(URL) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM csi_sessions WHERE label = %s", (tag,))
        conn.commit()
    print("=== VIGOROUS E2E PASSED ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
