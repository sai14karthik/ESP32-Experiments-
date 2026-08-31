#!/usr/bin/env python3
"""Export baseline + object sessions from Postgres to training CSV."""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

import psycopg

DEFAULT_DATABASE_URL = "postgresql:///csi"
EXPORT_SQL = """
SELECT
    s.label,
    c.seq,
    c.mac,
    c.rssi,
    c.channel,
    c.len,
    c.device_ts,
    c.host_ts,
    c.noise_floor,
    c.fft_gain,
    c.agc_gain,
    '{' || array_to_string(c.iq, ',') || '}' AS iq
FROM csi_samples c
JOIN csi_sessions s ON s.id = c.session_id
WHERE lower(coalesce(s.label, '')) LIKE '%baseline%'
   OR lower(coalesce(s.label, '')) LIKE '%empty%'
   OR lower(coalesce(s.label, '')) LIKE '%object%'
ORDER BY s.started_at, c.host_ts
"""


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent / "exports" / "training_packets.csv",
    )
    p.add_argument("--database-url", default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL))
    args = p.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)

    with psycopg.connect(args.database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT label, count(*) AS n
            FROM csi_sessions
            WHERE lower(coalesce(label, '')) LIKE '%baseline%'
               OR lower(coalesce(label, '')) LIKE '%empty%'
               OR lower(coalesce(label, '')) LIKE '%object%'
            GROUP BY label
            ORDER BY label
            """
        )
        sessions = cur.fetchall()
        if not sessions:
            sys.exit(
                "No baseline/empty/object sessions in Postgres.\n"
                "Capture first:\n"
                "  ./run_ingest.sh --method 4.3 --channel 11 --label baseline_mini\n"
                "  ./run_ingest.sh --method 4.3 --channel 11 --label object_mini"
            )

        has_empty = any("baseline" in (lab or "").lower() or "empty" in (lab or "").lower() for lab, _ in sessions)
        has_object = any("object" in (lab or "").lower() for lab, _ in sessions)
        if not has_empty or not has_object:
            sys.exit(
                f"Need at least one baseline/empty AND one object session. Found: {sessions}"
            )

        print("Sessions to export:")
        for lab, n in sessions:
            print(f"  {lab!r}: {n} packets")

        cur.execute(EXPORT_SQL)
        rows = cur.fetchall()

    fields = [
        "label", "seq", "mac", "rssi", "channel", "len", "device_ts", "host_ts",
        "noise_floor", "fft_gain", "agc_gain", "iq",
    ]
    with args.out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(fields)
        for row in rows:
            w.writerow(row)

    print(f"\nWrote {len(rows)} packets → {args.out}")


if __name__ == "__main__":
    main()
