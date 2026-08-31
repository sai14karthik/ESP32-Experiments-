"""CSI features shared by training and live detection (v3)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

FEATURE_VERSION = 3

N_SUBCARRIERS = 117
DEAD_1BASED = {58, 59, 60}
ACTIVE_IDX = np.array(
    [i for i in range(N_SUBCARRIERS) if (i + 1) not in DEAD_1BASED],
    dtype=np.int32,
)
N_ACTIVE = int(ACTIVE_IDX.size)
EPS = 2.0

LABEL_EMPTY = 0
LABEL_OBJECT = 1


@dataclass(frozen=True)
class WindowSpec:
    size: int
    stride: int


@dataclass(frozen=True)
class PacketRecord:
    """Per-packet CSI + metadata used for window feature extraction."""

    amp: np.ndarray
    phase: np.ndarray
    rssi: float
    agc_gain: float
    fft_gain: float


def iq_list_to_amplitudes(iq: list[int]) -> np.ndarray:
    """imag,real interleaved ints → magnitude per subcarrier."""
    vals = np.asarray(iq, dtype=np.int32)
    if vals.size != N_SUBCARRIERS * 2:
        raise ValueError(f"expected {N_SUBCARRIERS * 2} iq ints, got {vals.size}")
    im = vals[0::2].astype(np.float64)
    re = vals[1::2].astype(np.float64)
    return np.hypot(im, re)


def sanitize_phase(im: np.ndarray, re: np.ndarray) -> np.ndarray:
    """Remove linear phase trend across active subcarriers (CFO / SFO mitigation)."""
    raw = np.arctan2(im, re)
    x = ACTIVE_IDX.astype(np.float64)
    active = raw[ACTIVE_IDX]
    coef = np.polyfit(x, active, 1)
    trend = np.polyval(coef, x)
    out = raw.copy()
    out[ACTIVE_IDX] = np.arctan2(np.sin(active - trend), np.cos(active - trend))
    return out


def iq_list_to_packet(
    iq: list[int],
    *,
    rssi: float = 0.0,
    agc_gain: float = 0.0,
    fft_gain: float = 0.0,
) -> PacketRecord:
    vals = np.asarray(iq, dtype=np.int32)
    if vals.size != N_SUBCARRIERS * 2:
        raise ValueError(f"expected {N_SUBCARRIERS * 2} iq ints, got {vals.size}")
    im = vals[0::2].astype(np.float64)
    re = vals[1::2].astype(np.float64)
    return PacketRecord(
        amp=np.hypot(im, re),
        phase=sanitize_phase(im, re),
        rssi=float(rssi),
        agc_gain=float(agc_gain),
        fft_gain=float(fft_gain),
    )


def iq_field_to_amplitudes(iq_field: str) -> np.ndarray:
    parts = iq_field.strip().strip("{}").split(",")
    vals = [int(x) for x in parts if x.strip() != ""]
    return iq_list_to_amplitudes(vals)


def parse_optional_float(value: str | None, default: float = 0.0) -> float:
    if value is None:
        return default
    value = value.strip()
    if value == "" or value.lower() == "null":
        return default
    return float(value)


def compute_baseline_profile(
    packets: list[PacketRecord],
    labels: list[int],
) -> np.ndarray:
    """Median empty-room amplitude per active subcarrier."""
    empty = [p.amp[ACTIVE_IDX] for p, lab in zip(packets, labels) if lab == LABEL_EMPTY]
    if not empty:
        mat = np.stack([p.amp[ACTIVE_IDX] for p in packets], axis=0)
        return np.median(mat, axis=0)
    return np.median(np.stack(empty, axis=0), axis=0)


def compute_baseline_phase_profile(
    packets: list[PacketRecord],
    labels: list[int],
) -> np.ndarray:
    """Median empty-room sanitized phase per active subcarrier."""
    empty = [p.phase[ACTIVE_IDX] for p, lab in zip(packets, labels) if lab == LABEL_EMPTY]
    if not empty:
        mat = np.stack([p.phase[ACTIVE_IDX] for p in packets], axis=0)
        return np.median(mat, axis=0)
    return np.median(np.stack(empty, axis=0), axis=0)


def _adjacent_phase_diff(phases: np.ndarray) -> np.ndarray:
    active = phases[ACTIVE_IDX]
    diff = np.diff(active)
    return np.arctan2(np.sin(diff), np.cos(diff))


def _amp_v2_block(
    mat: np.ndarray,
    *,
    active_idx: np.ndarray,
    baseline_profile: np.ndarray | None,
) -> list[np.ndarray]:
    mean = mat.mean(axis=0)
    std = mat.std(axis=0)
    parts: list[np.ndarray] = [mean, std]
    if baseline_profile is not None:
        delta = (mean - baseline_profile) / (baseline_profile + EPS)
        parts.append(delta)
        band_mask = (active_idx >= 45) & (active_idx <= 61)
        band_energy = mean[band_mask].mean()
        other_energy = mean[~band_mask].mean()
        ratio = band_energy / (other_energy + EPS)
        parts.append(np.array([ratio, mean.mean(), std.mean(), (std / (mean + EPS)).mean()]))
    return parts


def _phase_block(
    packets: list[PacketRecord],
    *,
    baseline_phase: np.ndarray | None,
) -> list[np.ndarray]:
    phase_mat = np.stack([p.phase[ACTIVE_IDX] for p in packets], axis=0)
    mean = phase_mat.mean(axis=0)
    std = phase_mat.std(axis=0)
    parts: list[np.ndarray] = [mean, std]

    diff_mat = np.stack([_adjacent_phase_diff(p.phase) for p in packets], axis=0)
    diff_mean = diff_mat.mean(axis=0)
    diff_std = diff_mat.std(axis=0)
    parts.extend([diff_mean, diff_std])

    if baseline_phase is not None:
        phase_delta = np.arctan2(
            np.sin(mean - baseline_phase),
            np.cos(mean - baseline_phase),
        )
        parts.append(phase_delta)

    return parts


def _meta_block(packets: list[PacketRecord]) -> np.ndarray:
    rssi = np.array([p.rssi for p in packets], dtype=np.float64)
    agc = np.array([p.agc_gain for p in packets], dtype=np.float64)
    fft = np.array([p.fft_gain for p in packets], dtype=np.float64)
    return np.array(
        [
            rssi.mean(),
            rssi.std(),
            rssi.min(),
            rssi.max(),
            agc.mean(),
            agc.std(),
            fft.mean(),
            fft.std(),
        ],
        dtype=np.float64,
    )


def _correlation_block(mat: np.ndarray, baseline_profile: np.ndarray | None) -> np.ndarray:
    mean = mat.mean(axis=0)
    if baseline_profile is not None and np.std(mean) > 1e-9 and np.std(baseline_profile) > 1e-9:
        baseline_corr = float(np.corrcoef(mean, baseline_profile)[0, 1])
    else:
        baseline_corr = 0.0

    adjacent = mean[:-1]
    nxt = mean[1:]
    if np.std(adjacent) > 1e-9 and np.std(nxt) > 1e-9:
        adjacent_corr = float(np.corrcoef(adjacent, nxt)[0, 1])
    else:
        adjacent_corr = 0.0

    if mat.shape[0] > 1:
        temporal = [
            float(np.corrcoef(mat[i], mat[i + 1])[0, 1])
            for i in range(mat.shape[0] - 1)
            if np.std(mat[i]) > 1e-9 and np.std(mat[i + 1]) > 1e-9
        ]
        temporal_corr = float(np.mean(temporal)) if temporal else 0.0
    else:
        temporal_corr = 0.0

    return np.array([baseline_corr, adjacent_corr, temporal_corr], dtype=np.float64)


def _temporal_slope_block(mat: np.ndarray) -> np.ndarray:
    delta = mat[-1] - mat[0]
    t = np.arange(mat.shape[0], dtype=np.float64)
    energy = mat.sum(axis=1)
    if mat.shape[0] > 1 and np.std(t) > 1e-9:
        energy_slope = float(np.polyfit(t, energy, 1)[0])
    else:
        energy_slope = 0.0
    return np.concatenate([delta, np.array([energy_slope], dtype=np.float64)])


def _sequence_block(mat: np.ndarray, baseline_profile: np.ndarray | None) -> np.ndarray:
    if baseline_profile is None:
        normed = mat
    else:
        normed = (mat - baseline_profile) / (baseline_profile + EPS)
    return normed.reshape(-1)


def window_to_features(
    packets: list[PacketRecord] | list[np.ndarray],
    *,
    active_idx: np.ndarray = ACTIVE_IDX,
    baseline_profile: np.ndarray | None = None,
    baseline_phase: np.ndarray | None = None,
) -> np.ndarray:
    """
    Feature vector (v3):
      - v2 amplitude block (mean, std, delta, globals)
      - phase mean/std + adjacent Δphase mean/std + phase delta vs baseline
      - RSSI + AGC/FFT gain window stats
      - amplitude correlation (baseline, adjacent-SC, temporal)
      - per-SC temporal delta + energy slope
      - flattened baseline-normalized amplitude sequence (W × N_ACTIVE)
    """
    if packets and isinstance(packets[0], np.ndarray):
        packets = [
            PacketRecord(amp=a, phase=np.zeros(N_SUBCARRIERS), rssi=0.0, agc_gain=0.0, fft_gain=0.0)
            for a in packets  # type: ignore[misc]
        ]

    mat = np.stack([p.amp for p in packets], axis=0)[:, active_idx]
    parts: list[np.ndarray] = []

    parts.extend(_amp_v2_block(mat, active_idx=active_idx, baseline_profile=baseline_profile))
    parts.extend(_phase_block(packets, baseline_phase=baseline_phase))
    parts.append(_meta_block(packets))
    parts.append(_correlation_block(mat, baseline_profile))
    parts.append(_temporal_slope_block(mat))
    parts.append(_sequence_block(mat, baseline_profile))

    return np.concatenate(parts)


def feature_dim(
    baseline_profile: np.ndarray | None,
    *,
    window_size: int = 30,
    with_baseline_phase: bool = True,
) -> int:
    amp = N_ACTIVE * 2
    if baseline_profile is not None:
        amp = N_ACTIVE * 3 + 4
    phase = N_ACTIVE * 2 + (N_ACTIVE - 1) * 2 + (N_ACTIVE if with_baseline_phase else 0)
    meta = 8
    corr = 3
    temporal = N_ACTIVE + 1
    sequence = window_size * N_ACTIVE
    return amp + phase + meta + corr + temporal + sequence


def packets_from_legacy_amps(amps: list[np.ndarray]) -> list[PacketRecord]:
    return [
        PacketRecord(amp=a, phase=np.zeros(N_SUBCARRIERS), rssi=0.0, agc_gain=0.0, fft_gain=0.0)
        for a in amps
    ]
