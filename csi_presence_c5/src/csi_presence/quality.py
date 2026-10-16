"""Window continuity / quality gates shared by collect and live."""

from __future__ import annotations

from csi_presence.csi_parse import CsiSample

# Best practice: reject stall-spanning or too-thin windows (csi_pipeline_new WindowSpec)
DEFAULT_MIN_CSI = 3
DEFAULT_MAX_SPAN_MULT = 2.5


def window_span_sec(samples: list[CsiSample]) -> float:
    if len(samples) < 2:
        return 0.0
    return float(samples[-1].host_ts - samples[0].host_ts)


def window_ok(
    samples: list[CsiSample],
    *,
    window_sec: float,
    min_csi: int = DEFAULT_MIN_CSI,
    max_span_mult: float = DEFAULT_MAX_SPAN_MULT,
) -> tuple[bool, str]:
    """Return (ok, reason). reason empty when ok."""
    if len(samples) < min_csi:
        return False, f"n_csi<{min_csi}"
    span = window_span_sec(samples)
    max_span = max(window_sec * max_span_mult, window_sec + 0.5)
    if span > max_span:
        return False, f"span={span:.2f}s>{max_span:.2f}s"
    # Large gaps inside window (stall hole)
    if len(samples) >= 3:
        gaps = [
            samples[i + 1].host_ts - samples[i].host_ts
            for i in range(len(samples) - 1)
        ]
        if max(gaps) > max(window_sec * 0.75, 1.0):
            return False, "internal_gap"
    return True, ""
