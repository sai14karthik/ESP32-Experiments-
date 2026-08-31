"""CSI amplitude features shared by training and live detection."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

FEATURE_VERSION = 2

N_SUBCARRIERS = 117
DEAD_1BASED = {58, 59, 60}
ACTIVE_IDX = np.array(
    [i for i in range(N_SUBCARRIERS) if (i + 1) not in DEAD_1BASED],
    dtype=np.int32,
)
N_ACTIVE = int(ACTIVE_IDX.size)

LABEL_EMPTY = 0
LABEL_OBJECT = 1


@dataclass(frozen=True)
class WindowSpec:
    size: int
    stride: int


def iq_list_to_amplitudes(iq: list[int]) -> np.ndarray:
    """imag,real interleaved ints → magnitude per subcarrier."""
    vals = np.asarray(iq, dtype=np.int32)
    if vals.size != N_SUBCARRIERS * 2:
        raise ValueError(f"expected {N_SUBCARRIERS * 2} iq ints, got {vals.size}")
    im = vals[0::2].astype(np.float64)
    re = vals[1::2].astype(np.float64)
    return np.hypot(im, re)


def iq_field_to_amplitudes(iq_field: str) -> np.ndarray:
    parts = iq_field.strip().strip("{}").split(",")
    vals = [int(x) for x in parts if x.strip() != ""]
    return iq_list_to_amplitudes(vals)


def compute_baseline_profile(
    amps: list[np.ndarray],
    labels: list[int],
) -> np.ndarray:
    """Median empty-room amplitude per active subcarrier (robust template)."""
    empty = [a[ACTIVE_IDX] for a, lab in zip(amps, labels) if lab == LABEL_EMPTY]
    if not empty:
        mat = np.stack([a[ACTIVE_IDX] for a in amps], axis=0)
        return np.median(mat, axis=0)
    return np.median(np.stack(empty, axis=0), axis=0)


def window_to_features(
    amps: list[np.ndarray],
    *,
    active_idx: np.ndarray = ACTIVE_IDX,
    baseline_profile: np.ndarray | None = None,
) -> np.ndarray:
    """
    Feature vector (v2):
      - per-SC mean amplitude
      - per-SC std
      - per-SC relative delta vs empty baseline: (mean - base) / (base + eps)
      - global: mean RSSI proxy (mean of window means), coeff of variation
    """
    mat = np.stack(amps, axis=0)[:, active_idx]
    mean = mat.mean(axis=0)
    std = mat.std(axis=0)
    parts: list[np.ndarray] = [mean, std]

    if baseline_profile is not None:
        eps = 2.0
        delta = (mean - baseline_profile) / (baseline_profile + eps)
        parts.append(delta)
        # sc46–sc62 (1-based) — strongest static-object band in our captures.
        sc_indices = active_idx
        band_mask = (sc_indices >= 45) & (sc_indices <= 61)
        band_energy = mean[band_mask].mean()
        other_energy = mean[~band_mask].mean()
        ratio = band_energy / (other_energy + eps)
        parts.append(np.array([ratio, mean.mean(), std.mean(), (std / (mean + eps)).mean()]))

    return np.concatenate(parts)


def feature_dim(baseline_profile: np.ndarray | None) -> int:
    if baseline_profile is None:
        return N_ACTIVE * 2
    return N_ACTIVE * 3 + 4
