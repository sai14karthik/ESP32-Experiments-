"""Windowed CSI features for binary presence."""

from __future__ import annotations

import numpy as np

from .csi_parse import CsiSample


def samples_to_amp_matrix(samples: list[CsiSample]) -> np.ndarray:
    """Stack amplitude vectors; pad/trunc to median length."""
    amps = [s.amplitude for s in samples if s.amplitude]
    if not amps:
        return np.zeros((0, 0), dtype=np.float64)
    lens = [len(a) for a in amps]
    target = int(np.median(lens))
    rows = []
    for a in amps:
        v = np.asarray(a, dtype=np.float64)
        if len(v) < target:
            v = np.pad(v, (0, target - len(v)))
        else:
            v = v[:target]
        rows.append(v)
    return np.vstack(rows)


def window_features(samples: list[CsiSample]) -> np.ndarray | None:
    """
    Compact feature vector from a time window of CSI samples.
    Shape: (D,) float64 — mean/std amp, RSSI stats, temporal energy.
    """
    # Dense hop (0.125s) still uses a 1s window — need a few CSI frames
    if len(samples) < 2:
        return None
    mat = samples_to_amp_matrix(samples)
    if mat.size == 0:
        return None

    mean_amp = mat.mean(axis=0)
    std_amp = mat.std(axis=0)
    # Subsample subcarriers to keep dim bounded
    step = max(1, len(mean_amp) // 32)
    mean_s = mean_amp[::step][:32]
    std_s = std_amp[::step][:32]
    if len(mean_s) < 32:
        mean_s = np.pad(mean_s, (0, 32 - len(mean_s)))
        std_s = np.pad(std_s, (0, 32 - len(std_s)))

    rssi = np.array([s.rssi for s in samples if s.rssi is not None], dtype=np.float64)
    if rssi.size == 0:
        rssi_mean, rssi_std = 0.0, 0.0
    else:
        rssi_mean, rssi_std = float(rssi.mean()), float(rssi.std())

    # Frame-to-frame amp change (motion proxy)
    if mat.shape[0] >= 2:
        diff = np.abs(np.diff(mat, axis=0)).mean()
    else:
        diff = 0.0

    feats = np.concatenate(
        [
            mean_s,
            std_s,
            np.array(
                [rssi_mean, rssi_std, diff, float(mat.shape[0]), float(mat.mean())],
                dtype=np.float64,
            ),
        ]
    )
    return feats
