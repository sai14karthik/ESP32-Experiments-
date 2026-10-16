"""Train empty/occupied classifier from Postgres presence_windows."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import (
    balanced_accuracy_score,
    classification_report,
    confusion_matrix,
    f1_score,
)
from sklearn.model_selection import (
    GroupKFold,
    GroupShuffleSplit,
    StratifiedShuffleSplit,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from csi_presence.db import database_url
from csi_presence.features import FEATURE_VERSION
from csi_presence.model_io import (
    DEFAULT_DEBOUNCE,
    DEFAULT_EMA_ALPHA,
    DEFAULT_HYSTERESIS,
    DEFAULT_MODEL,
    ensure_models_dir,
    save_bundle,
)
from csi_presence.ports_util import ROOT

COLLECT_TOML = ROOT / "configs" / "collect.toml"


def _load_window_hop() -> tuple[float, float]:
    window_sec, hop_sec = 1.0, 0.125
    if not COLLECT_TOML.exists():
        return window_sec, hop_sec
    try:
        import tomllib

        with COLLECT_TOML.open("rb") as f:
            cfg = tomllib.load(f)
        sess = cfg.get("session") or {}
        window_sec = float(sess.get("window_sec", window_sec))
        hop_sec = float(sess.get("hop_sec", hop_sec))
    except Exception:
        pass
    return window_sec, hop_sec


def load_windows(
    url: str,
    *,
    session_tag: str | None = None,
    method: str | None = None,
    feature_version: int | None = FEATURE_VERSION,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    """Return X, y, groups(session_id), tags."""
    import psycopg

    clauses: list[str] = []
    args: list[Any] = []
    if session_tag:
        clauses.append("session_tag = %s")
        args.append(session_tag)
    if method:
        clauses.append("method = %s")
        args.append(method)
    if feature_version is not None:
        # Legacy rows (NULL) count as version 1
        clauses.append("COALESCE(feature_version, 1) = %s")
        args.append(int(feature_version))
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT session_id, label, feat, COALESCE(session_tag, '')
            FROM presence_windows
            {where}
            ORDER BY host_ts
            """,
            args,
        )
        rows = cur.fetchall()

    if not rows:
        raise SystemExit(
            "no presence_windows rows — collect first, then ./scripts/db_dist.sh"
        )

    xs: list[list[float]] = []
    ys: list[int] = []
    groups: list[str] = []
    tags: list[str] = []
    dims: Counter[int] = Counter()
    for sid, label, feat, tag in rows:
        if feat is None or len(feat) < 4:
            continue
        vec = [float(v) for v in feat]
        dims[len(vec)] += 1
        xs.append(vec)
        ys.append(int(label))
        groups.append(str(sid))
        tags.append(tag or "")

    if not xs:
        raise SystemExit("rows found but no usable feat vectors")

    # Pad/trunc to modal feature dim (should already match)
    feat_dim = dims.most_common(1)[0][0]
    X = np.zeros((len(xs), feat_dim), dtype=np.float64)
    for i, v in enumerate(xs):
        n = min(len(v), feat_dim)
        X[i, :n] = v[:n]
    y = np.asarray(ys, dtype=np.int64)
    g = np.asarray(groups)
    return X, y, g, tags


def _split(
    X: np.ndarray,
    y: np.ndarray,
    groups: np.ndarray,
    *,
    test_size: float,
    seed: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    n_groups = len(set(groups.tolist()))
    if n_groups >= 2 and test_size > 0:
        try:
            gss = GroupShuffleSplit(n_splits=1, test_size=test_size, random_state=seed)
            tr, te = next(gss.split(X, y, groups))
            if len(tr) and len(te) and len(set(y[tr].tolist())) > 1:
                return X[tr], X[te], y[tr], y[te]
        except ValueError:
            pass
        print(
            f"[train] group split weak (n_sessions={n_groups}); "
            "falling back to stratified row split (leak risk if one long session)"
        )
    if test_size <= 0 or len(X) < 20:
        return X, X[:0], y, y[:0]
    sss = StratifiedShuffleSplit(n_splits=1, test_size=test_size, random_state=seed)
    tr, te = next(sss.split(X, y))
    return X[tr], X[te], y[tr], y[te]


def _make_clf(seed: int) -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(
        max_depth=6,
        learning_rate=0.08,
        max_iter=200,
        random_state=seed,
    )


def _negative_control_empty(
    X: np.ndarray, y: np.ndarray, groups: np.ndarray, *, seed: int
) -> float | None:
    """Empty-vs-empty session split should be ~0.5; high ⇒ leakage / confound."""
    empty_sessions = sorted({g for g, lab in zip(groups.tolist(), y.tolist()) if lab == 0})
    if len(empty_sessions) < 2:
        return None
    # Split empty sessions into two pseudo-classes. A good model should NOT
    # separate them well (~0.5). High bal_acc ⇒ session confound / leakage.
    mask = y == 0
    Xe, ge = X[mask], groups[mask]
    sess_lab = {s: i % 2 for i, s in enumerate(empty_sessions)}
    ye = np.array([sess_lab[g] for g in ge.tolist()], dtype=np.int64)
    if len(set(ye.tolist())) < 2:
        return None
    try:
        gss = GroupShuffleSplit(n_splits=1, test_size=0.4, random_state=seed)
        tr, te = next(gss.split(Xe, ye, ge))
    except ValueError:
        return None
    if len(set(ye[tr].tolist())) < 2 or len(te) < 5:
        return None
    pipe = Pipeline(
        [("scaler", StandardScaler()), ("clf", _make_clf(seed))]
    )
    pipe.fit(Xe[tr], ye[tr])
    pred = pipe.predict(Xe[te])
    return float(balanced_accuracy_score(ye[te], pred))


def _group_cv_bal_acc(
    X: np.ndarray, y: np.ndarray, groups: np.ndarray, *, seed: int
) -> float | None:
    """Session-grouped CV — best practice vs random window splits (leakage)."""
    n_groups = len(set(groups.tolist()))
    if n_groups < 3:
        return None
    n_splits = min(5, n_groups)
    gkf = GroupKFold(n_splits=n_splits)
    scores: list[float] = []
    for tr, te in gkf.split(X, y, groups):
        if len(set(y[tr].tolist())) < 2 or len(set(y[te].tolist())) < 1:
            continue
        pipe = Pipeline(
            [
                ("scaler", StandardScaler()),
                ("clf", _make_clf(seed)),
            ]
        )
        pipe.fit(X[tr], y[tr])
        pred = pipe.predict(X[te])
        scores.append(float(balanced_accuracy_score(y[te], pred)))
    if not scores:
        return None
    return float(np.mean(scores))


def train(
    X: np.ndarray,
    y: np.ndarray,
    groups: np.ndarray,
    *,
    test_size: float = 0.2,
    seed: int = 42,
) -> tuple[Any, StandardScaler, dict[str, Any]]:
    n_groups = len(set(groups.tolist()))
    used_group_holdout = False
    X_tr = X_te = y_tr = y_te = None  # type: ignore[assignment]

    if n_groups >= 2 and test_size > 0:
        try:
            gss = GroupShuffleSplit(n_splits=1, test_size=test_size, random_state=seed)
            tr_idx, te_idx = next(gss.split(X, y, groups))
            if (
                len(tr_idx)
                and len(te_idx)
                and len(set(y[tr_idx].tolist())) > 1
                and len(set(y[te_idx].tolist())) > 1
            ):
                X_tr, X_te = X[tr_idx], X[te_idx]
                y_tr, y_te = y[tr_idx], y[te_idx]
                used_group_holdout = True
        except ValueError:
            pass

    if X_tr is None:
        if n_groups >= 2:
            print(
                f"[train] group split weak (n_sessions={n_groups}); "
                "falling back to stratified row split (leak risk)"
            )
        X_tr, X_te, y_tr, y_te = _split(X, y, groups, test_size=test_size, seed=seed)
        used_group_holdout = False

    # Eval-only fit on train split
    scaler_eval = StandardScaler()
    X_tr_s = scaler_eval.fit_transform(X_tr)
    clf_eval = _make_clf(seed)
    clf_eval.fit(X_tr_s, y_tr)

    metrics: dict[str, Any] = {
        "n_train": int(len(y_tr)),
        "n_test": int(len(y_te)),
        "train_empty": int((y_tr == 0).sum()),
        "train_occupied": int((y_tr == 1).sum()),
        "n_sessions": int(n_groups),
        "holdout": "session_group" if used_group_holdout else "row_or_none",
    }
    labels = [0, 1]
    if len(y_te) and len(set(y_te.tolist())) > 1:
        X_te_s = scaler_eval.transform(X_te)
        pred = clf_eval.predict(X_te_s)
        metrics.update(
            {
                "bal_acc": float(balanced_accuracy_score(y_te, pred)),
                "f1": float(f1_score(y_te, pred, zero_division=0)),
                "confusion": confusion_matrix(y_te, pred, labels=labels).tolist(),
                "report": classification_report(
                    y_te,
                    pred,
                    labels=labels,
                    target_names=["empty", "occupied"],
                    zero_division=0,
                ),
            }
        )
    else:
        pred = clf_eval.predict(X_tr_s)
        note = "no holdout — metrics are train-set (optimistic)"
        if len(y_te) and len(set(y_te.tolist())) <= 1:
            note = (
                "holdout had a single class (session grouping); "
                "metrics are train-set"
            )
        metrics.update(
            {
                "bal_acc": float(balanced_accuracy_score(y_tr, pred)),
                "f1": float(f1_score(y_tr, pred, zero_division=0)),
                "confusion": confusion_matrix(y_tr, pred, labels=labels).tolist(),
                "note": note,
                "report": classification_report(
                    y_tr,
                    pred,
                    labels=labels,
                    target_names=["empty", "occupied"],
                    zero_division=0,
                ),
            }
        )

    gcv = _group_cv_bal_acc(X, y, groups, seed=seed)
    if gcv is not None:
        metrics["group_cv_bal_acc"] = gcv
        print(
            f"group_cv_bal_acc={gcv:.3f}  "
            "(session-held-out folds; trust this over row split)"
        )

    metrics["evaluation_trustworthy"] = bool(
        used_group_holdout
        and len(y_te) > 0
        and len(set(y_te.tolist())) > 1
        and "note" not in metrics
    )
    if not metrics["evaluation_trustworthy"]:
        print(
            "WARNING: eval may be optimistic — collect ≥3 sessions with both classes "
            "for trustworthy session-grouped metrics"
        )

    # Fit final model on ALL data for deployment
    scaler_full = StandardScaler()
    X_all = scaler_full.fit_transform(X)
    clf_full = _make_clf(seed)
    clf_full.fit(X_all, y)
    return clf_full, scaler_full, metrics


def main(argv: list[str] | None = None) -> int:
    window_sec, hop_sec = _load_window_hop()
    p = argparse.ArgumentParser(description="Train presence model from Postgres")
    p.add_argument("--database-url", default=None)
    p.add_argument(
        "--session-tag",
        "--session",
        dest="session_tag",
        default=None,
        help="filter presence_windows.session_tag",
    )
    p.add_argument("--method", default=None, help="filter method e.g. 4.2")
    p.add_argument(
        "--feature-version",
        type=int,
        default=FEATURE_VERSION,
        help=f"only train windows with this feature_version (default {FEATURE_VERSION})",
    )
    p.add_argument(
        "--allow-mixed-features",
        action="store_true",
        help="do not filter by feature_version (not recommended)",
    )
    p.add_argument(
        "--min-per-class",
        type=int,
        default=50,
        help="abort if either class has fewer windows (default 50)",
    )
    p.add_argument("--test-size", type=float, default=0.2)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_MODEL,
        help=f"joblib path (default {DEFAULT_MODEL})",
    )
    args = p.parse_args(argv)

    url = database_url(args.database_url)
    print(f"DB {url}")
    feat_ver = None if args.allow_mixed_features else args.feature_version
    X, y, groups, tags = load_windows(
        url,
        session_tag=args.session_tag,
        method=args.method,
        feature_version=feat_ver,
    )
    if feat_ver is not None:
        print(f"feature_version={feat_ver} (rebuild old rows: ./scripts/rebuild_features.sh)")
    n_empty = int((y == 0).sum())
    n_occ = int((y == 1).sum())
    n_sess = len(set(groups.tolist()))
    tag_set = sorted({t for t in tags if t})
    print(
        f"loaded windows={len(y)}  empty={n_empty} occupied={n_occ}  "
        f"sessions={n_sess}  feat_dim={X.shape[1]}"
    )
    if tag_set:
        print(f"session_tags: {', '.join(tag_set[:20])}" + ("…" if len(tag_set) > 20 else ""))

    if n_empty < args.min_per_class or n_occ < args.min_per_class:
        raise SystemExit(
            f"need ≥{args.min_per_class} per class "
            f"(empty={n_empty} occupied={n_occ}) — collect more / check db_dist"
        )
    ratio = n_empty / max(n_empty + n_occ, 1)
    if ratio < 0.3 or ratio > 0.7:
        print(
            f"WARNING: class imbalance empty_share={ratio:.2f} — "
            "aim ~0.4–0.6 for stable presence models"
        )
    if n_sess < 3:
        print(
            f"WARNING: only {n_sess} session(s) — collect ≥3 mixed sessions "
            "before trusting metrics"
        )

    clf, scaler, metrics = train(
        X, y, groups, test_size=args.test_size, seed=args.seed
    )
    neg = _negative_control_empty(X, y, groups, seed=args.seed)
    if neg is not None:
        metrics["negative_control_bal_acc"] = neg
        print(f"negative_control_bal_acc={neg:.3f}  (empty-vs-empty; want ~0.5)")
        if neg >= 0.7:
            print(
                "WARNING: negative control high — sessions may be confounded "
                "(time-of-day / AP / furniture). Prefer interleaved collects."
            )
            metrics["evaluation_trustworthy"] = False
    print(f"bal_acc={metrics.get('bal_acc'):.3f}  f1={metrics.get('f1'):.3f}")
    if "report" in metrics:
        print(metrics["report"])
    else:
        print("confusion:", metrics.get("confusion"), metrics.get("note", ""))

    methods: list[str] = []
    import psycopg

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT method FROM presence_windows
            WHERE method IS NOT NULL
            """
            + (" AND session_tag = %s" if args.session_tag else ""),
            (args.session_tag,) if args.session_tag else (),
        )
        methods = [r[0] for r in cur.fetchall()]

    bundle = {
        "model": clf,
        "scaler": scaler,
        "feat_dim": int(X.shape[1]),
        "feature_version": FEATURE_VERSION,
        "window_sec": window_sec,
        "hop_sec": hop_sec,
        "threshold": 0.5,
        "ema_alpha": DEFAULT_EMA_ALPHA,
        "hysteresis": DEFAULT_HYSTERESIS,
        "debounce": DEFAULT_DEBOUNCE,
        "method": methods[0] if len(methods) == 1 else (methods or ["auto"]),
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "n_empty": n_empty,
        "n_occupied": n_occ,
        "n_sessions": n_sess,
        "session_tags": tag_set,
        "metrics": {k: v for k, v in metrics.items() if k != "report"},
        "database_url": url,
    }
    ensure_models_dir()
    out = save_bundle(args.out, bundle)
    meta_path = out.with_suffix(".json")
    meta_path.write_text(
        json.dumps({k: v for k, v in bundle.items() if k not in ("model", "scaler")}, indent=2)
        + "\n"
    )
    print(f"saved → {out}")
    print(f"meta  → {meta_path}")
    print("next: ./scripts/calibrate_presence.sh  (empty room) then ./scripts/live_presence.sh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
