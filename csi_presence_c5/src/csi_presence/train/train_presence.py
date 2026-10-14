"""Train RandomForest empty(0) / occupied(1) from session windows.jsonl."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report, confusion_matrix
from sklearn.model_selection import train_test_split

from csi_presence.ports_util import ROOT

SESSIONS = ROOT / "data" / "sessions"
MODELS = ROOT / "models"


def load_xy(session_dirs: list[Path]) -> tuple[np.ndarray, np.ndarray]:
    xs: list[list[float]] = []
    ys: list[int] = []
    for d in session_dirs:
        path = d / "windows.jsonl"
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            xs.append(rec["feat"])
            ys.append(int(rec["label"]))
    if not xs:
        raise SystemExit(f"No windows found under {session_dirs}")
    return np.asarray(xs, dtype=np.float64), np.asarray(ys, dtype=np.int64)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Train binary CSI presence model")
    p.add_argument(
        "--sessions",
        default=str(SESSIONS),
        help="Directory of session folders (or a single session path)",
    )
    p.add_argument("--out", default=str(MODELS / "presence_rf.joblib"))
    p.add_argument("--test-size", type=float, default=0.25)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args(argv)

    root = Path(args.sessions)
    if (root / "windows.jsonl").exists():
        dirs = [root]
    else:
        dirs = sorted([d for d in root.iterdir() if d.is_dir()])
    if not dirs:
        raise SystemExit(f"No sessions in {root}")

    X, y = load_xy(dirs)
    print(f"samples={len(y)} empty={int((y == 0).sum())} occupied={int((y == 1).sum())}")
    if len(set(y.tolist())) < 2:
        raise SystemExit("Need both empty and occupied windows — collect both labels first")

    Xtr, Xte, ytr, yte = train_test_split(
        X, y, test_size=args.test_size, random_state=args.seed, stratify=y
    )
    clf = RandomForestClassifier(
        n_estimators=200,
        max_depth=12,
        random_state=args.seed,
        n_jobs=-1,
        class_weight="balanced",
    )
    clf.fit(Xtr, ytr)
    pred = clf.predict(Xte)
    print(classification_report(yte, pred, target_names=["empty", "occupied"]))
    print("confusion:\n", confusion_matrix(yte, pred))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": clf, "feat_dim": X.shape[1]}, out)
    print(f"saved → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
