"""Print empty/occupied distribution from PostgreSQL presence_windows."""

from __future__ import annotations

import argparse

from csi_presence.db import database_url, distribution_report


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Presence window label distribution (Postgres)")
    p.add_argument("--database-url", default=None, help="default $DATABASE_URL or postgresql:///csi")
    p.add_argument("--session", "-s", default=None, help="filter by session_tag")
    args = p.parse_args(argv)
    try:
        print(distribution_report(args.database_url, tag=args.session))
    except Exception as exc:
        print(f"ERROR: {exc}")
        print(f"  URL={database_url(args.database_url)}")
        print("  Is Postgres up?  psql postgresql:///csi -c 'SELECT 1'")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
