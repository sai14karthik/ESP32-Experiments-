"""Windowed CSI features for binary presence (v3).

Best-practice stack (ESPectre / Espressif / csi_pipeline_new / arxiv gain papers):

* Per-packet **gain normalization** (AGC cancel)
* **Dead / DC bins** masked by packet width (53 LLTF / 117 HT)
* Light **Hampel** scrub on the amplitude vector (spike rejection)
* **No RSSI/AGC meta** in the vector (leakage shortcut)
* Spatial **CV turbulence** + temporal Δ on normalized field
* Quality: need ≥3 packets (caller also applies continuity gates)

FEATURE_VERSION must match collect / rebuild / train / live.
"""

from __future__ import annotations

import numpy as np

from .csi_parse import CsiSample

FEATURE_VERSION = 3
FEATURE_DIM = 69  # 32 mean + 32 std + 5 scalars
_N_SUB = 32
_EPS = 1e-9
_MIN_PACKETS = 3


def _dead_mask(n_bins: int) -> np.ndarray:
    """Boolean mask True = keep (active). 1-based dead bins → 0-based index."""
    keep = np.ones(n_bins, dtype=bool)
    if n_bins == 117:
        for b in (58, 59, 60):  # 1-based
            i = b - 1
            if 0 <= i < n_bins:
                keep[i] = False
    elif n_bins == 53:
        i = 26  # DC
        if 0 <= i < n_bins:
            keep[i] = False
    elif n_bins >= 8:
        mid = n_bins // 2
        keep[mid] = False
    return keep


def _hampel_1d(x: np.ndarray, *, k: float = 3.0) -> np.ndarray:
    """Replace outliers with median (Hampel). Works on short spectrum vectors."""
    if x.size < 5:
        return x
    med = np.median(x)
    mad = np.median(np.abs(x - med))
    if mad < _EPS:
        return x
    thresh = k * 1.4826 * mad
    out = x.copy()
    out[np.abs(x - med) > thresh] = med
    return out


def _amplitude_from_iq(iq: list[int]) -> np.ndarray:
    vals = np.asarray(iq, dtype=np.float64)
    if vals.size < 4 or vals.size % 2:
        return np.zeros(0, dtype=np.float64)
    im = vals[0::2]
    re = vals[1::2]
    return np.hypot(im, re)


def _packet_amp_norm(iq: list[int]) -> np.ndarray | None:
    amp = _amplitude_from_iq(iq)
    if amp.size < 8:
        return None
    keep = _dead_mask(amp.size)
    active = amp[keep]
    if active.size < 4:
        active = amp
    active = _hampel_1d(active)
    scale = float(np.mean(active))
    if scale < _EPS:
        return None
    # Rebuild full-length normalized vector with dead bins zeroed
    out = np.zeros_like(amp)
    out[keep] = amp[keep] / scale
    # Also scrub full vector lightly
    out = _hampel_1d(out)
    return out


def samples_to_amp_matrix(samples: list[CsiSample]) -> np.ndarray:
    """Stack gain-normalized amplitude vectors; pad/trunc to median length."""
    amps: list[np.ndarray] = []
    for s in samples:
        a = _packet_amp_norm(s.iq)
        if a is None:
            continue
        amps.append(a)
    if not amps:
        return np.zeros((0, 0), dtype=np.float64)
    lens = [len(a) for a in amps]
    target = int(np.median(lens))
    rows = []
    for a in amps:
        if len(a) < target:
            v = np.pad(a, (0, target - len(a)))
        else:
            v = a[:target]
        rows.append(v)
    return np.vstack(rows)


def _subsample(vec: np.ndarray, n: int = _N_SUB) -> np.ndarray:
    if vec.size == 0:
        return np.zeros(n, dtype=np.float64)
    step = max(1, len(vec) // n)
    out = vec[::step][:n]
    if len(out) < n:
        out = np.pad(out, (0, n - len(out)))
    return out.astype(np.float64)


def window_features(samples: list[CsiSample]) -> np.ndarray | None:
    """Compact v3 feature vector. Shape: (FEATURE_DIM,) float64."""
    if len(samples) < _MIN_PACKETS:
        return None
    mat = samples_to_amp_matrix(samples)
    if mat.size == 0 or mat.shape[0] < _MIN_PACKETS:
        return None

    mean_amp = mat.mean(axis=0)
    std_amp = mat.std(axis=0)
    # Ignore near-zero dead pads in stats
    active = mean_amp > 1e-6
    if active.sum() >= 8:
        mean_use = mean_amp[active]
        std_use = std_amp[active]
    else:
        mean_use, std_use = mean_amp, std_amp

    mean_s = _subsample(mean_use)
    std_s = _subsample(std_use)

    mu = float(np.mean(mean_use))
    turb = float(np.std(mean_use) / mu) if mu > _EPS else 0.0
    diff = float(np.abs(np.diff(mat, axis=0)).mean())

    n = len(mean_use)
    lo, hi = max(0, n // 2 - n // 8), min(n, n // 2 + n // 8)
    band = float(mean_use[lo:hi].mean()) if hi > lo else 0.0
    edge_n = max(1, n // 8)
    edges = float(np.concatenate([mean_use[:edge_n], mean_use[-edge_n:]]).mean())
    band_ratio = band / (edges + _EPS)

    feats = np.concatenate(
        [
            mean_s,
            std_s,
            np.array(
                [
                    turb,
                    diff,
                    band_ratio,
                    float(mat.shape[0]),
                    float(mat[:, active].std()) if active.sum() else float(mat.std()),
                ],
                dtype=np.float64,
            ),
        ]
    )
    assert feats.shape == (FEATURE_DIM,), feats.shape
    return feats
