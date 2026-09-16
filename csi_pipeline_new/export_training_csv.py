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

# Parenthesized because callers AND additional filters onto it — a bare OR
# chain would bind as `a OR b OR (c AND filter)` and quietly export everything.
BASE_WHERE = """(
    lower(coalesce(s.label, '')) LIKE '%baseline%'
    OR lower(coalesce(s.label, '')) LIKE '%empty%'
    OR lower(coalesce(s.label, '')) LIKE '%object%'
    OR lower(coalesce(s.label, '')) LIKE '%occupied%'
)"""

EXPORT_SELECT = f"""
SELECT
    s.id::text AS session_id,
    s.label,
    coalesce(c.source_id, '') AS source_id,
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
"""

# Per-board order so multi-C5 TCP fan-in does not interleave RX timelines.
EXPORT_ORDER_BY = (
    "\nORDER BY s.started_at, coalesce(c.source_id, ''), c.host_ts, c.seq NULLS LAST\n"
)


def build_export_query(
    include: list[str],
    exclude: list[str],
    session_ids: list[str],
    source_ids: list[str] | None = None,
) -> tuple[str, list]:
    params: list = []
    clauses = ""
    if include:
        clauses += " AND (" + " OR ".join(["lower(s.label) LIKE %s"] * len(include)) + ")"
        params.extend(f"%{p}%" for p in include)
    for p in exclude:
        clauses += " AND lower(s.label) NOT LIKE %s"
        params.append(f"%{p}%")
    if session_ids:
        clauses += " AND s.id::text = ANY(%s)"
        params.append(session_ids)
    if source_ids:
        clauses += " AND c.source_id = ANY(%s)"
        params.append(source_ids)
    head = EXPORT_SELECT.replace("%", "%%") if params else EXPORT_SELECT
    return head + clauses + EXPORT_ORDER_BY, params


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
    p.add_argument(
        "--session-id",
        help="Comma-separated session UUIDs to export (e.g. from csi_sessions.id)",
    )
    p.add_argument(
        "--source-id",
        help="Comma-separated TCP source_id (client IP) to keep — one RX board",
    )
    args = p.parse_args()
    include = _parse_patterns(args.include)
    exclude = _parse_patterns(args.exclude)
    session_ids = [s.strip() for s in (args.session_id or "").split(",") if s.strip()]
    source_ids = [s.strip() for s in (args.source_id or "").split(",") if s.strip()]

    args.out.parent.mkdir(parents=True, exist_ok=True)

    with psycopg.connect(args.database_url) as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT s.label, count(c.id) AS n
            FROM csi_sessions s
            LEFT JOIN csi_samples c ON c.session_id = s.id
            WHERE {BASE_WHERE}
            GROUP BY s.label
            ORDER BY label
            """
        )
        sessions = cur.fetchall()
        if not sessions:
            sys.exit(
                "No baseline/empty/object/occupied sessions in Postgres.\n"
                "Capture first:\n"
                "  ./run_multi_ingest.sh --label empty_01\n"
                "  ./run_multi_ingest.sh --label occupied_01"
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
        has_object = any(
            "object" in (lab or "").lower() or "occupied" in (lab or "").lower()
            for lab, _ in selected
        )
        if not has_empty or not has_object:
            sys.exit(
                f"Need at least one baseline/empty AND one object/occupied session. "
                f"After filters: {selected}"
            )

        print("Sessions to export:")
        for lab, n in selected:
            print(f"  {lab!r}: {n} packets")
        if include:
            print(f"  (include filter: {include})")
        if exclude:
            print(f"  (exclude filter: {exclude})")
        if session_ids:
            print(f"  (session-id filter: {session_ids})")
        if source_ids:
            print(f"  (source-id filter: {source_ids})")

        # Show multi-RX fan-in breakdown (helps confirm 3 C5s landed).
        cur.execute(
            f"""
            SELECT coalesce(c.source_id, '(null)'), count(*)
            FROM csi_samples c
            JOIN csi_sessions s ON s.id = c.session_id
            WHERE {BASE_WHERE}
            GROUP BY 1
            ORDER BY 1
            """
        )
        sources = cur.fetchall()
        if sources:
            print("source_id (RX) packet totals in matched label set:")
            for sid, n in sources:
                print(f"  {sid}: {n}")

        sql, params = build_export_query(include, exclude, session_ids, source_ids or None)
        cur.execute(sql, params)
        rows = cur.fetchall()

    fields = [
        "session_id",
        "label",
        "source_id",
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
