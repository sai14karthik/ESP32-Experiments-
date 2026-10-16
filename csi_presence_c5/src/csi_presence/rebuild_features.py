"""Recompute presence_windows.feat from csi_samples (keeps labels).

Use after upgrading features.py so already-collected sessions match FEATURE_VERSION.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from uuid import UUID

import psycopg

from csi_presence.csi_parse import CsiSample
from csi_presence.db import database_url
from csi_presence.features import FEATURE_VERSION, window_features
from csi_presence.ports_util import ROOT
from csi_presence.quality import window_ok

COLLECT_TOML = ROOT / "configs" / "collect.toml"


def _window_sec() -> float:
    if not COLLECT_TOML.exists():
        return 1.0
    try:
        import tomllib

        with COLLECT_TOML.open("rb") as f:
            cfg = tomllib.load(f)
        return float((cfg.get("session") or {}).get("window_sec", 1.0))
    except Exception:
        return 1.0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Rebuild presence_windows features from csi_samples"
    )
    p.add_argument("--database-url", default=None)
    p.add_argument("--session-tag", default=None)
    p.add_argument(
        "--window-sec",
        type=float,
        default=None,
        help="feature window length (default collect.toml)",
    )
    args = p.parse_args(argv)
    url = database_url(args.database_url)
    win_sec = args.window_sec if args.window_sec is not None else _window_sec()

    # Ensure column exists
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE presence_windows ADD COLUMN IF NOT EXISTS feature_version INTEGER"
        )
        conn.commit()

    where = ""
    params: tuple = ()
    if args.session_tag:
        where = "WHERE session_tag = %s"
        params = (args.session_tag,)

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, session_id, host_ts, label
            FROM presence_windows
            {where}
            ORDER BY session_id, host_ts
            """,
            params,
        )
        windows = cur.fetchall()

    if not windows:
        print("no presence_windows to rebuild")
        return 0

    print(
        f"rebuild {len(windows)} windows → feature_version={FEATURE_VERSION} "
        f"window_sec={win_sec}"
    )
    updated = 0
    skipped = 0
    by_session: dict[UUID, list] = {}
    for wid, sid, host_ts, label in windows:
        by_session.setdefault(sid, []).append((wid, host_ts, label))

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        for sid, wins in by_session.items():
            cur.execute(
                """
                SELECT host_ts, iq, rssi, mac, source_id
                FROM csi_samples
                WHERE session_id = %s
                ORDER BY host_ts
                """,
                (sid,),
            )
            rows = cur.fetchall()
            samples: list[CsiSample] = []
            for hts, iq, rssi, mac, source_id in rows:
                if not iq:
                    continue
                ts = hts.timestamp() if isinstance(hts, datetime) else float(hts)
                samples.append(
                    CsiSample(
                        raw_line="",
                        rssi=float(rssi) if rssi is not None else None,
                        iq=list(iq),
                        host_ts=ts,
                        source_id=source_id or "",
                        mac=(mac or "").lower(),
                    )
                )
            if not samples:
                skipped += len(wins)
                print(f"  skip session {sid}: no csi_samples")
                continue

            for wid, host_ts, _label in wins:
                t = host_ts.timestamp() if isinstance(host_ts, datetime) else float(host_ts)
                win = [s for s in samples if t - win_sec <= s.host_ts <= t]
                ok, _why = window_ok(win, window_sec=win_sec)
                feats = window_features(win) if ok else None
                if feats is None:
                    skipped += 1
                    continue
                cur.execute(
                    """
                    UPDATE presence_windows
                    SET feat = %s, n_csi = %s, feature_version = %s
                    WHERE id = %s
                    """,
                    (feats.tolist(), len(win), FEATURE_VERSION, wid),
                )
                updated += 1
            conn.commit()
            print(f"  session {sid}: updated batch")

    print(f"done: updated={updated} skipped={skipped} version={FEATURE_VERSION}")
    print("next: ./scripts/train_presence.sh")
    return 0 if updated else 1


if __name__ == "__main__":
    raise SystemExit(main())
