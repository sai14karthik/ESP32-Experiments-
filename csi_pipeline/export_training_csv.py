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

BASE_WHERE = """
    lower(coalesce(s.label, '')) LIKE '%baseline%'
    OR lower(coalesce(s.label, '')) LIKE '%empty%'
    OR lower(coalesce(s.label, '')) LIKE '%object%'
"""

EXPORT_SQL = f"""
SELECT
    s.id::text AS session_id,
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
    '{{' || array_to_string(c.iq, ',') || '}}' AS iq
FROM csi_samples c
JOIN csi_sessions s ON s.id = c.session_id
WHERE {BASE_WHERE}
ORDER BY s.started_at, c.host_ts
"""


def _parse_patterns(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def _label_matches(label: str, patterns: list[str]) -> bool:
    lab = label.lower()
    return any(p in lab for p in patterns)


def _filter_sessions(
    sessions: list[tuple[str, int]],
    *,
    include: list[str],
    exclude: list[str],
) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for lab, n in sessions:
        if include and not _label_matches(lab, include):
            continue
        if exclude and _label_matches(lab, exclude):
            continue
        out.append((lab, n))
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent / "exports" / "training_packets.csv",
    )
    p.add_argument("--database-url", default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL))
    p.add_argument(
        "--include",
        help="Comma-separated label substrings to export (e.g. baseline_desk,object_desk)",
    )
    p.add_argument(
        "--exclude",
        help="Comma-separated label substrings to skip (e.g. baseline_1hr,object_1hr)",
    )
    args = p.parse_args()
    include = _parse_patterns(args.include)
    exclude = _parse_patterns(args.exclude)

    args.out.parent.mkdir(parents=True, exist_ok=True)

    with psycopg.connect(args.database_url) as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT label, count(*) AS n
            FROM csi_sessions
            WHERE {BASE_WHERE}
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

        selected = _filter_sessions(sessions, include=include, exclude=exclude)
        if not selected:
            sys.exit(
                f"No sessions match filters include={include!r} exclude={exclude!r}.\n"
                f"Available: {sessions}"
            )

        has_empty = any(
            "baseline" in (lab or "").lower() or "empty" in (lab or "").lower()
            for lab, _ in selected
        )
        has_object = any("object" in (lab or "").lower() for lab, _ in selected)
        if not has_empty or not has_object:
            sys.exit(
                f"Need at least one baseline/empty AND one object session. "
                f"After filters: {selected}"
            )

        print("Sessions to export:")
        for lab, n in selected:
            print(f"  {lab!r}: {n} packets")
        if include:
            print(f"  (include filter: {include})")
        if exclude:
            print(f"  (exclude filter: {exclude})")

        sql = EXPORT_SQL
        params: list[str] = []
        if include:
            sql += " AND (" + " OR ".join(["lower(s.label) LIKE %s"] * len(include)) + ")"
            params.extend(f"%{p}%" for p in include)
        if exclude:
            for _ in exclude:
                sql += " AND lower(s.label) NOT LIKE %s"
            params.extend(f"%{p}%" for p in exclude)

        cur.execute(sql, params)
        rows = cur.fetchall()

    fields = [
        "session_id",
        "label",
        "seq",
        "mac",
        "rssi",
        "channel",
        "len",
        "device_ts",
        "host_ts",
        "noise_floor",
        "fft_gain",
        "agc_gain",
        "iq",
    ]
    with args.out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(fields)
        for row in rows:
            w.writerow(row)

    print(f"\nWrote {len(rows)} packets → {args.out}")


if __name__ == "__main__":
    main()
