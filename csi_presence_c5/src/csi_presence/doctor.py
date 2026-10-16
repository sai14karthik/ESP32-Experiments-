"""Pipeline health check: DB, feature versions, model, calibration, :9055."""

from __future__ import annotations

import argparse
import socket
from pathlib import Path

from csi_presence.db import database_url
from csi_presence.features import FEATURE_VERSION
from csi_presence.model_io import DEFAULT_CALIBRATION, DEFAULT_MODEL, load_bundle, load_calibration


def _port_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="CSI presence pipeline doctor")
    p.add_argument("--database-url", default=None)
    p.add_argument("--listen-tcp", type=int, default=9055)
    args = p.parse_args(argv)
    url = database_url(args.database_url)
    errs = 0

    print(f"feature_version code={FEATURE_VERSION}")
    print(f"DATABASE_URL={url}")

    try:
        import psycopg

        with psycopg.connect(url) as conn, conn.cursor() as cur:
            for t in ("csi_sessions", "csi_samples", "presence_windows"):
                cur.execute(
                    "SELECT to_regclass(%s)",
                    (f"public.{t}",),
                )
                ok = cur.fetchone()[0] is not None
                print(f"  table {t}: {'OK' if ok else 'MISSING'}")
                if not ok:
                    errs += 1
            cur.execute(
                """
                SELECT COALESCE(feature_version, 1), count(*),
                       count(*) FILTER (WHERE label=0),
                       count(*) FILTER (WHERE label=1)
                FROM presence_windows
                GROUP BY 1
                ORDER BY 1
                """
            )
            rows = cur.fetchall()
            if not rows:
                print("  presence_windows: EMPTY (collect first)")
            for ver, n, e, o in rows:
                mark = "OK" if int(ver) == FEATURE_VERSION else "STALE→rebuild"
                print(f"  windows v{ver}: n={n} empty={e} occupied={o}  [{mark}]")
                if int(ver) != FEATURE_VERSION:
                    errs += 1
            cur.execute("SELECT count(*) FROM csi_samples")
            print(f"  csi_samples: {cur.fetchone()[0]}")
    except Exception as exc:
        print(f"  postgres FAIL: {exc}")
        errs += 1

    if DEFAULT_MODEL.exists():
        try:
            b = load_bundle()
            bv = b.get("feature_version")
            print(
                f"  model: OK feat_dim={b.get('feat_dim')} "
                f"feature_version={bv} sessions={b.get('n_sessions')}"
            )
            if bv is not None and int(bv) != FEATURE_VERSION:
                print("  model STALE vs code — retrain after rebuild")
                errs += 1
            m = b.get("metrics") or {}
            if m.get("group_cv_bal_acc") is not None:
                print(f"  group_cv_bal_acc={m['group_cv_bal_acc']}")
            if m.get("negative_control_bal_acc") is not None:
                print(f"  negative_control_bal_acc={m['negative_control_bal_acc']}")
            print(f"  evaluation_trustworthy={m.get('evaluation_trustworthy')}")
        except Exception as exc:
            print(f"  model FAIL: {exc}")
            errs += 1
    else:
        print("  model: missing (./scripts/train_presence.sh)")

    cal = load_calibration()
    if cal:
        print(
            f"  calibration: OK thr={cal.get('threshold')} "
            f"hyst={cal.get('hysteresis')} debounce={cal.get('debounce')}"
        )
    else:
        print("  calibration: missing (./scripts/calibrate_presence.sh)")

    free = _port_free(args.listen_tcp)
    print(
        f"  :{args.listen_tcp} {'FREE' if free else 'IN USE'} "
        f"({'ok for collect/live' if free else 'stop other owner first'})"
    )

    print("PASS" if errs == 0 else f"ISSUES={errs}")
    return 0 if errs == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
