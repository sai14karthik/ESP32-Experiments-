#!/usr/bin/env python3
"""Does the object change the channel, or only the received power?

RSSI and AGC gain answer "is less energy arriving". They cannot tell you
whether the *channel* changed, because a flat attenuation and a structured
one look identical to a single scalar. This script separates the two:

  flat term   — mean magnitude across subcarriers. What RSSI already sees.
  shape term  — the per-subcarrier profile after dividing each packet by its
                own active-bin mean, so gain (AGC, FFT, path loss) cancels
                exactly. Only frequency-selective effects survive.

An object blocking the line of sight should produce BOTH: less power, and a
subcarrier-dependent dip from multipath. If only the flat term moves, you
have a power meter, not a CSI sensor — a single RSSI threshold would do the
same job and the 117-bin channel response is decoration.

Every shape number is reported next to a within-class control: the same
computation run on empty-vs-empty. That is the noise floor. A between-class
difference only means something if it clears it.

  ./probe_check.py                      # reads Postgres, labels LIKE 'probe%'
  ./probe_check.py --like probe2%
  ./probe_check.py --csv exports/training_packets.csv
"""

from __future__ import annotations

import argparse
import csv as csvmod
import os
import sys
from pathlib import Path

import numpy as np

from csi_features import (
    ACTIVE_IDX,
    LABEL_EMPTY,
    LABEL_OBJECT,
    PacketRecord,
    iq_list_to_packet,
    parse_optional_float,
)

DEFAULT_DATABASE_URL = "postgresql:///csi"


def classify(label: str) -> int | None:
    lab = label.lower()
    if "object" in lab:
        return LABEL_OBJECT
    if "baseline" in lab or "empty" in lab:
        return LABEL_EMPTY
    return None


def _num(value) -> float:
    """Coerce a field to float. Postgres hands back numerics, CSV hands back strings."""
    if value is None:
        return 0.0
    if isinstance(value, str):
        return parse_optional_float(value)
    return float(value)


def _make(iq: list[int], rssi, agc, fft) -> PacketRecord:
    return iq_list_to_packet(
        iq,
        rssi=_num(rssi),
        agc_gain=_num(agc),
        fft_gain=_num(fft),
        normalize_gain=True,
    )


def load_from_db(like: str) -> tuple[list[PacketRecord], list[int], list[str]]:
    import psycopg

    url = os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    packets: list[PacketRecord] = []
    y: list[int] = []
    sessions: list[str] = []
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.label, c.rssi, c.agc_gain, c.fft_gain, c.iq
            FROM csi_samples c JOIN csi_sessions s ON s.id = c.session_id
            WHERE lower(coalesce(s.label,'')) LIKE %s
            ORDER BY s.started_at, c.host_ts
            """,
            (like.lower(),),
        )
        for label, rssi, agc, fft, iq in cur:
            lab = classify(label or "")
            if lab is None or not iq:
                continue
            try:
                packets.append(_make(list(iq), rssi, agc, fft))
            except ValueError:
                continue
            y.append(lab)
            sessions.append(label)
    return packets, y, sessions


def load_from_csv(path: Path) -> tuple[list[PacketRecord], list[int], list[str]]:
    packets: list[PacketRecord] = []
    y: list[int] = []
    sessions: list[str] = []
    with path.open(newline="") as fh:
        for row in csvmod.DictReader(fh):
            lab = classify(row.get("label", ""))
            if lab is None:
                continue
            try:
                iq = [int(x) for x in row["iq"].strip("{}").split(",") if x.strip()]
                packets.append(_make(iq, row.get("rssi"), row.get("agc_gain"), row.get("fft_gain")))
            except ValueError:
                continue
            y.append(lab)
            sessions.append(row["label"])
    return packets, y, sessions


def auc(values: np.ndarray, labels: np.ndarray) -> float:
    """Rank-based AUC, folded to >=0.5 so direction doesn't matter."""
    pos, neg = values[labels == LABEL_OBJECT], values[labels == LABEL_EMPTY]
    if pos.size == 0 or neg.size == 0:
        return float("nan")
    ranks = np.argsort(np.argsort(np.concatenate([pos, neg]))) + 1
    a = (ranks[: pos.size].sum() - pos.size * (pos.size + 1) / 2) / (pos.size * neg.size)
    return float(max(a, 1.0 - a))


def shape_delta(a: np.ndarray, b: np.ndarray) -> tuple[float, float, int]:
    """RMS and peak difference between two mean normalized profiles."""
    d = a.mean(axis=0) - b.mean(axis=0)
    return float(np.sqrt((d**2).mean())), float(np.abs(d).max()), int(np.argmax(np.abs(d)))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--like", default="probe%", help="SQL LIKE pattern on session label")
    p.add_argument("--csv", type=Path, help="Read an export CSV instead of Postgres")
    args = p.parse_args()

    packets, y_list, sessions = (
        load_from_csv(args.csv) if args.csv else load_from_db(args.like)
    )
    if not packets:
        sys.exit("No matching packets. Check --like / --csv.")

    y = np.asarray(y_list)
    if len(np.unique(y)) < 2:
        sys.exit("Need both empty and object packets.")

    # Normalized per-packet shape over active bins only, and the flat term
    # that normalization removed.
    shape = np.stack([pk.amp for pk in packets])[:, ACTIVE_IDX]
    scale = np.array([pk.amp_scale for pk in packets])
    rssi = np.array([pk.rssi for pk in packets])

    emp, obj = shape[y == LABEL_EMPTY], shape[y == LABEL_OBJECT]
    n_emp, n_obj = emp.shape[0], obj.shape[0]

    print(f"packets: empty={n_emp}  object={n_obj}  active bins={shape.shape[1]}")
    print(f"sessions: {', '.join(sorted(set(sessions)))}\n")

    # ---- flat term ---------------------------------------------------------
    s_emp, s_obj = scale[y == LABEL_EMPTY].mean(), scale[y == LABEL_OBJECT].mean()
    db = 20.0 * np.log10(s_obj / s_emp) if s_emp > 0 else float("nan")
    print("FLAT TERM  (what RSSI already sees)")
    print(f"  mean |H|      empty={s_emp:8.2f}   object={s_obj:8.2f}   {db:+.2f} dB")
    print(f"  mean RSSI     empty={rssi[y == LABEL_EMPTY].mean():8.2f}   "
          f"object={rssi[y == LABEL_OBJECT].mean():8.2f}")
    print(f"  AUC, RSSI alone ............ {auc(rssi, y):.3f}")
    print(f"  AUC, mean magnitude alone .. {auc(scale, y):.3f}\n")

    # ---- shape term --------------------------------------------------------
    rms, peak, bin_i = shape_delta(obj, emp)

    # Control: compare empty against empty, no object anywhere. This is drift
    # plus noise — the floor a real effect has to clear.
    #
    # How the empty class is split matters. Halving by arrival order spans only
    # the drift inside one recording. When there are several empty sessions,
    # alternating them makes the control span the same stretch of wall-clock
    # time as the object-vs-empty comparison, which is the honest comparison.
    empty_sessions = sorted({s for lab, s in zip(y_list, sessions) if lab == LABEL_EMPTY})
    if len(empty_sessions) > 1:
        first = {s for i, s in enumerate(empty_sessions) if i % 2 == 0}
        mask = np.array([s in first for lab, s in zip(y_list, sessions) if lab == LABEL_EMPTY])
        c_rms, c_peak, _ = shape_delta(emp[mask], emp[~mask])
        control_desc = f"alternating sessions ({len(empty_sessions)} empty blocks)"
    else:
        half = n_emp // 2
        c_rms, c_peak, _ = shape_delta(emp[:half], emp[half:])
        control_desc = "first half vs second half of one session"

    print("SHAPE TERM  (per-packet gain divided out — only frequency-selective effects)")
    print(f"  object vs empty   RMS={rms:.4f}   peak={peak:.4f} at active bin {bin_i}")
    print(f"  empty vs empty    RMS={c_rms:.4f}   peak={c_peak:.4f}   <- control")
    print(f"  control split ..... {control_desc}")
    ratio = rms / c_rms if c_rms > 0 else float("inf")
    print(f"  ratio ..................... {ratio:.2f}x the control\n")

    # Best single subcarrier, after gain is gone. If this is high while the
    # flat AUCs are also high, check that it isn't just the flat term leaking
    # back in through an imperfect normalization.
    per_bin = np.array([auc(shape[:, j], y) for j in range(shape.shape[1])])
    best = int(np.argmax(per_bin))
    print(f"  AUC, best single subcarrier  {per_bin[best]:.3f}  (active bin {best})")
    print(f"  AUC, median subcarrier ..... {np.median(per_bin):.3f}\n")

    # ---- verdict -----------------------------------------------------------
    # The control splits one class in half, so it spans the drift *within* a
    # recording. If each class is a single back-to-back session, the between
    # comparison additionally spans the gap between recordings — a wider time
    # separation than the control covers. The ratio is then biased upward and
    # this whole script inherits the confound it exists to detect.
    per_class_sessions = {LABEL_EMPTY: set(), LABEL_OBJECT: set()}
    for lab, sess in zip(y_list, sessions):
        per_class_sessions[lab].add(sess)
    interleaved = min(len(s) for s in per_class_sessions.values()) > 1

    print("=" * 68)
    flat_auc = max(auc(rssi, y), auc(scale, y))
    if not interleaved:
        print("INCONCLUSIVE — one session per class, recorded back to back.")
        print("The control spans drift inside a recording; the comparison also")
        print("spans the gap between recordings, so the ratio below is inflated")
        print("by an unknown amount. Re-capture alternating A/B/A/B and rerun.")
        print("-" * 68)
    if ratio < 2.0:
        print("The channel shape barely moves more than an empty room's own noise.")
        print("Whatever the object does here, it does it to received power only.")
    elif per_bin[best] <= flat_auc + 0.02:
        print("Shape changes, but no subcarrier beats the flat term. CSI is")
        print("tracking the same attenuation RSSI reports, not new information.")
    else:
        print(f"Frequency-selective: shape moves {ratio:.1f}x the control, and the")
        print(f"best subcarrier ({per_bin[best]:.3f}) beats the flat term ({flat_auc:.3f}).")
        print("This is the regime where CSI can carry something RSSI cannot.")
    print("=" * 68)
    print("\nSingle-scalar AUCs are in-sample and unsplit — direction only, not")
    print("an accuracy claim. Run train_object_detector.py for a real estimate.")


if __name__ == "__main__":
    main()
