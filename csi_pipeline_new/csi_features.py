"""CSI features shared by training and live detection (v4).

What changed in v4, and why:

* **Amplitude is gain-normalized.** Each packet's amplitude vector is divided
  by its own active-bin mean, so the receiver's AGC/FFT gain state cancels.
  The raw ESP32 CSI integers are scaled by that gain, and in the 1-hour sample
  capture the gain state differs systematically between the baseline and the
  object session (`fft_gain` sits at 16-17 for baseline, 18 for object). Left
  uncorrected, that scale factor leaks into every amplitude feature — including
  the baseline-relative delta — so a model can separate the classes without
  ever using the channel. The trade is deliberate: we keep the *shape* of the
  frequency response and discard absolute power. Absolute power is still
  measured, but separately and honestly, as the RSSI baseline the trainer
  prints.

* **The metadata block is off by default.** On that same capture, a single
  threshold on window-mean RSSI classifies 98.3% of windows correctly. Feeding
  RSSI/AGC/FFT gain in as features hands the model that shortcut. Re-enable
  deliberately with ``FeatureConfig(use_meta=True)``.

* **The flattened sequence block is off by default.** It was 3420 of the 4460
  v3 dimensions against ~2270 windows.

* **Feature layout lives in FeatureConfig,** which is stored in the model
  bundle, so live detection reproduces training exactly instead of relying on
  the two call sites staying in sync.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

FEATURE_VERSION = 4

N_SUBCARRIERS = 117
DEAD_1BASED = frozenset({58, 59, 60})
ACTIVE_IDX = np.array(
    [i for i in range(N_SUBCARRIERS) if (i + 1) not in DEAD_1BASED],
    dtype=np.int32,
)
N_ACTIVE = int(ACTIVE_IDX.size)

# Denominator guard for relative deltas. Raw CSI magnitudes run ~10-30, so the
# v3 value of 2.0 was ~10% of a typical bin. After gain normalization the mean
# active bin is 1.0 by construction, so the guard has to shrink to match or it
# flattens every delta.
EPS_RAW = 2.0
EPS_NORMALIZED = 0.05

# Subcarriers straddling the dead bins, where an object on the direct path
# tends to show up first. Empirical, from the 4.3 ESP-NOW captures.
BAND_LO, BAND_HI = 45, 61

LABEL_EMPTY = 0
LABEL_OBJECT = 1


@dataclass(frozen=True)
class FeatureConfig:
    """Which feature blocks are built, and how amplitude is scaled.

    Stored in the model bundle and replayed at inference time.
    """

    normalize_gain: bool = True
    use_phase: bool = True
    use_meta: bool = False
    use_sequence: bool = False

    @property
    def eps(self) -> float:
        return EPS_NORMALIZED if self.normalize_gain else EPS_RAW

    def to_dict(self) -> dict[str, bool]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "FeatureConfig":
        if not data:
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: bool(v) for k, v in data.items() if k in known})

    def describe(self) -> str:
        on = [
            name
            for name, flag in (
                ("gain-normalized", self.normalize_gain),
                ("phase", self.use_phase),
                ("meta(RSSI/AGC)", self.use_meta),
                ("sequence", self.use_sequence),
            )
            if flag
        ]
        return "+".join(on) if on else "amplitude-only"


DEFAULT_CONFIG = FeatureConfig()


@dataclass(frozen=True)
class WindowSpec:
    """Window geometry plus the continuity guards applied when slicing.

    ``build_windows`` slides over packet *indices*, so without these guards a
    "30-packet window" silently spans whatever wall-clock time the link
    happened to take. Both sample sessions contain three multi-minute stalls,
    during which a window covers 17 minutes instead of the usual 2.2 seconds —
    and the temporal slope and correlation features treat those samples as
    evenly spaced regardless.
    """

    size: int
    stride: int
    max_span_s: float | None = 12.0  # ~5x the observed 2.2 s median span
    max_seq_gap: int | None = 256  # normal stored-packet seq delta is ~7 (p99 28)


@dataclass(frozen=True)
class PacketRecord:
    """Per-packet CSI plus the metadata needed for windowing and diagnostics."""

    amp: np.ndarray
    phase: np.ndarray
    rssi: float
    agc_gain: float
    fft_gain: float
    # Mean raw active-bin magnitude before normalization. Retained so absolute
    # power stays inspectable without silently re-entering the feature vector.
    amp_scale: float = 1.0
    seq: int | None = None
    host_ts: float | None = None


def iq_list_to_amplitudes(iq: list[int]) -> np.ndarray:
    """imag,real interleaved ints -> raw magnitude per subcarrier."""
    vals = np.asarray(iq, dtype=np.int32)
    if vals.size != N_SUBCARRIERS * 2:
        raise ValueError(f"expected {N_SUBCARRIERS * 2} iq ints, got {vals.size}")
    im = vals[0::2].astype(np.float64)
    re = vals[1::2].astype(np.float64)
    return np.hypot(im, re)


def sanitize_phase(im: np.ndarray, re: np.ndarray) -> np.ndarray:
    """Remove the linear phase trend across active subcarriers (CFO / SFO)."""
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
    seq: int | None = None,
    host_ts: float | None = None,
    normalize_gain: bool = True,
) -> PacketRecord:
    """Build a PacketRecord, cancelling receiver gain unless told not to.

    Dividing by the packet's own active-bin mean removes any per-packet
    multiplicative factor exactly, whatever the firmware's gain formula is —
    which is why this is preferred over trying to invert AGC/FFT gain directly.
    """
    vals = np.asarray(iq, dtype=np.int32)
    if vals.size != N_SUBCARRIERS * 2:
        raise ValueError(f"expected {N_SUBCARRIERS * 2} iq ints, got {vals.size}")
    im = vals[0::2].astype(np.float64)
    re = vals[1::2].astype(np.float64)

    amp = np.hypot(im, re)
    scale = float(amp[ACTIVE_IDX].mean())
    if normalize_gain:
        amp = amp / (scale if scale > 1e-9 else 1.0)

    return PacketRecord(
        amp=amp,
        phase=sanitize_phase(im, re),
        rssi=float(rssi),
        agc_gain=float(agc_gain),
        fft_gain=float(fft_gain),
        amp_scale=scale,
        seq=seq,
        host_ts=host_ts,
    )


def parse_optional_float(value: str | None, default: float = 0.0) -> float:
    if value is None:
        return default
    value = value.strip()
    if value == "" or value.lower() == "null":
        return default
    return float(value)


def window_is_contiguous(packets: list[PacketRecord], spec: WindowSpec) -> bool:
    """True when a window is a real slice of time rather than a stall artifact."""
    if spec.max_span_s is not None:
        times = [p.host_ts for p in packets if p.host_ts is not None]
        if len(times) >= 2 and (max(times) - min(times)) > spec.max_span_s:
            return False
    if spec.max_seq_gap is not None:
        seqs = [p.seq for p in packets if p.seq is not None]
        for a, b in zip(seqs, seqs[1:]):
            if b >= a and (b - a) > spec.max_seq_gap:  # b < a is a counter wrap
                return False
    return True


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


def _amp_block(
    mat: np.ndarray,
    *,
    active_idx: np.ndarray,
    baseline_profile: np.ndarray | None,
    eps: float,
) -> list[np.ndarray]:
    mean = mat.mean(axis=0)
    std = mat.std(axis=0)
    parts: list[np.ndarray] = [mean, std]
    if baseline_profile is not None:
        delta = (mean - baseline_profile) / (baseline_profile + eps)
        parts.append(delta)
        # active_idx and mean are aligned elementwise, so this selects the
        # active bins whose subcarrier index falls in the band.
        band_mask = (active_idx >= BAND_LO) & (active_idx <= BAND_HI)
        band_energy = mean[band_mask].mean()
        other_energy = mean[~band_mask].mean()
        ratio = band_energy / (other_energy + eps)
        parts.append(
            np.array([ratio, mean.mean(), std.mean(), (std / (mean + eps)).mean()])
        )
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
    parts.extend([diff_mat.mean(axis=0), diff_mat.std(axis=0)])

    if baseline_phase is not None:
        phase_delta = np.arctan2(
            np.sin(mean - baseline_phase),
            np.cos(mean - baseline_phase),
        )
        parts.append(phase_delta)

    return parts


def _meta_block(packets: list[PacketRecord]) -> np.ndarray:
    """Receiver state. Off by default — see the module docstring."""
    rssi = np.array([p.rssi for p in packets], dtype=np.float64)
    agc = np.array([p.agc_gain for p in packets], dtype=np.float64)
    fft = np.array([p.fft_gain for p in packets], dtype=np.float64)
    return np.array(
        [
            rssi.mean(), rssi.std(), rssi.min(), rssi.max(),
            agc.mean(), agc.std(), fft.mean(), fft.std(),
        ],
        dtype=np.float64,
    )


def _correlation_block(mat: np.ndarray, baseline_profile: np.ndarray | None) -> np.ndarray:
    mean = mat.mean(axis=0)
    if baseline_profile is not None and np.std(mean) > 1e-9 and np.std(baseline_profile) > 1e-9:
        baseline_corr = float(np.corrcoef(mean, baseline_profile)[0, 1])
    else:
        baseline_corr = 0.0

    adjacent, nxt = mean[:-1], mean[1:]
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


def _sequence_block(
    mat: np.ndarray, baseline_profile: np.ndarray | None, eps: float
) -> np.ndarray:
    if baseline_profile is None:
        return mat.reshape(-1)
    return ((mat - baseline_profile) / (baseline_profile + eps)).reshape(-1)


def window_to_features(
    packets: list[PacketRecord],
    *,
    config: FeatureConfig = DEFAULT_CONFIG,
    active_idx: np.ndarray = ACTIVE_IDX,
    baseline_profile: np.ndarray | None = None,
    baseline_phase: np.ndarray | None = None,
) -> np.ndarray:
    """Build one feature vector from a window of packets.

    Always present: amplitude mean/std, baseline-relative delta and band
    globals, amplitude correlations (baseline / adjacent-bin / temporal),
    per-bin temporal delta and energy slope. Optional per ``config``: phase
    block, receiver metadata block, flattened sequence block.
    """
    mat = np.stack([p.amp for p in packets], axis=0)[:, active_idx]
    eps = config.eps
    parts: list[np.ndarray] = []

    parts.extend(
        _amp_block(mat, active_idx=active_idx, baseline_profile=baseline_profile, eps=eps)
    )
    if config.use_phase:
        parts.extend(_phase_block(packets, baseline_phase=baseline_phase))
    if config.use_meta:
        parts.append(_meta_block(packets))
    parts.append(_correlation_block(mat, baseline_profile))
    parts.append(_temporal_slope_block(mat))
    if config.use_sequence:
        parts.append(_sequence_block(mat, baseline_profile, eps))

    return np.concatenate(parts)


def feature_dim(
    baseline_profile: np.ndarray | None,
    *,
    window_size: int = 30,
    config: FeatureConfig = DEFAULT_CONFIG,
    with_baseline_phase: bool = True,
) -> int:
    """Measured, not derived — a hand-written formula drifts from the code."""
    rng = np.random.default_rng(0)
    probe = [
        PacketRecord(
            amp=1.0 + 0.01 * rng.standard_normal(N_SUBCARRIERS),
            phase=0.01 * rng.standard_normal(N_SUBCARRIERS),
            rssi=-50.0,
            agc_gain=30.0,
            fft_gain=16.0,
        )
        for _ in range(window_size)
    ]
    bp = None if baseline_profile is None else np.ones(N_ACTIVE)
    bph = np.zeros(N_ACTIVE) if with_baseline_phase else None
    return int(
        window_to_features(
            probe, config=config, baseline_profile=bp, baseline_phase=bph
        ).size
    )
