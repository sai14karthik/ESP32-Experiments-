"""Unit tests for features v3 + PresenceDecision."""

from __future__ import annotations

import numpy as np

from csi_presence.csi_parse import CsiSample
from csi_presence.features import FEATURE_DIM, FEATURE_VERSION, window_features
from csi_presence.model_io import PresenceDecision
from csi_presence.quality import window_ok


def _samples(scale: int = 1, n: int = 5) -> list[CsiSample]:
    base = np.array([((i % 17) - 8) for i in range(234)], dtype=int)
    out = []
    for t in range(n):
        iq = (base * scale).tolist()
        out.append(CsiSample(raw_line="", rssi=-50, iq=iq, host_ts=float(t) * 0.1))
    return out


def test_feature_version_and_dim():
    assert FEATURE_VERSION >= 3
    f = window_features(_samples())
    assert f is not None
    assert f.shape == (FEATURE_DIM,)


def test_gain_invariance():
    f1 = window_features(_samples(1))
    f2 = window_features(_samples(4))
    assert f1 is not None and f2 is not None
    assert np.max(np.abs(f1[:64] - f2[:64])) < 1e-5


def test_min_packets():
    assert window_features(_samples(n=2)) is None


def test_window_ok():
    ok, _ = window_ok(_samples(n=5), window_sec=1.0)
    assert ok
    bad, reason = window_ok(_samples(n=1), window_sec=1.0)
    assert not bad and "n_csi" in reason


def test_hysteresis_debounce():
    d = PresenceDecision(threshold=0.5, hysteresis=0.06, ema_alpha=1.0, debounce=2)
    assert d.update(0.56)[0] is False  # need 2 votes
    assert d.update(0.56)[0] is True
    assert d.update(0.44)[0] is True  # need 2 exit votes
    assert d.update(0.44)[0] is False
