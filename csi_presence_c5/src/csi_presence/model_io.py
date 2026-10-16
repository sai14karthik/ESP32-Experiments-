"""Load/save presence model + site calibration bundles."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib

from csi_presence.ports_util import ROOT

MODELS_DIR = ROOT / "models"
DEFAULT_MODEL = MODELS_DIR / "presence.joblib"
DEFAULT_CALIBRATION = MODELS_DIR / "site_calibration.joblib"

# Live decision defaults (csi_pipeline_new / ESPectre-style)
DEFAULT_EMA_ALPHA = 0.3
DEFAULT_HYSTERESIS = 0.06
DEFAULT_DEBOUNCE = 2


def ensure_models_dir() -> Path:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    return MODELS_DIR


def save_bundle(path: Path, bundle: dict[str, Any]) -> Path:
    ensure_models_dir()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, path)
    return path


def load_bundle(path: Path | None = None) -> dict[str, Any]:
    p = Path(path) if path else DEFAULT_MODEL
    if not p.exists():
        raise FileNotFoundError(
            f"missing model {p} — train first: ./scripts/train_presence.sh"
        )
    return joblib.load(p)


def load_calibration(path: Path | None = None) -> dict[str, Any] | None:
    p = Path(path) if path else DEFAULT_CALIBRATION
    if not p.exists():
        return None
    return joblib.load(p)


def resolve_threshold(
    *,
    calibration: dict[str, Any] | None,
    default_threshold: float = 0.5,
    override: float | None = None,
) -> float:
    if override is not None:
        return float(override)
    if calibration and "threshold" in calibration:
        return float(calibration["threshold"])
    return float(default_threshold)


def resolve_hysteresis(
    *,
    bundle: dict[str, Any] | None,
    calibration: dict[str, Any] | None,
    override: float | None = None,
) -> float:
    if override is not None:
        return float(override)
    if calibration and calibration.get("hysteresis") is not None:
        return float(calibration["hysteresis"])
    if bundle and bundle.get("hysteresis") is not None:
        return float(bundle["hysteresis"])
    return DEFAULT_HYSTERESIS


def resolve_ema_alpha(
    *,
    bundle: dict[str, Any] | None,
    override: float | None = None,
) -> float:
    if override is not None:
        return float(override)
    if bundle and bundle.get("ema_alpha") is not None:
        return float(bundle["ema_alpha"])
    return DEFAULT_EMA_ALPHA


@dataclass
class PresenceDecision:
    """EMA + hysteresis + debounce (anti-flicker).

    Best practice (WifiSpaceMapper / DensePose-WiFi / csi_pipeline_new):
    separate enter/exit levels; require N consecutive votes before flipping.
    """

    threshold: float = 0.5
    hysteresis: float = DEFAULT_HYSTERESIS
    ema_alpha: float = DEFAULT_EMA_ALPHA
    debounce: int = 2  # consecutive hops above/below before flip
    _ema: float | None = None
    _occupied: bool = False
    _enter_votes: int = 0
    _exit_votes: int = 0

    @classmethod
    def from_bundle(
        cls,
        bundle: dict[str, Any],
        calibration: dict[str, Any] | None = None,
        *,
        threshold: float | None = None,
        hysteresis: float | None = None,
        ema_alpha: float | None = None,
        debounce: int | None = None,
    ) -> PresenceDecision:
        thr = resolve_threshold(calibration=calibration, override=threshold)
        deb = DEFAULT_DEBOUNCE
        if debounce is not None:
            deb = int(debounce)
        elif calibration and calibration.get("debounce") is not None:
            deb = int(calibration["debounce"])
        elif bundle and bundle.get("debounce") is not None:
            deb = int(bundle["debounce"])
        return cls(
            threshold=thr,
            hysteresis=resolve_hysteresis(
                bundle=bundle, calibration=calibration, override=hysteresis
            ),
            ema_alpha=resolve_ema_alpha(bundle=bundle, override=ema_alpha),
            debounce=max(1, deb),
        )

    @property
    def enter_at(self) -> float:
        return float(min(0.99, self.threshold + self.hysteresis))

    @property
    def exit_at(self) -> float:
        return float(max(0.01, self.threshold - self.hysteresis))

    def update(self, p_raw: float) -> tuple[bool, float]:
        """Return (occupied_state, ema_score)."""
        p = float(p_raw)
        if self._ema is None:
            self._ema = p
        else:
            a = self.ema_alpha
            self._ema = a * p + (1.0 - a) * self._ema

        if not self._occupied:
            if self._ema >= self.enter_at:
                self._enter_votes += 1
                self._exit_votes = 0
                if self._enter_votes >= self.debounce:
                    self._occupied = True
                    self._enter_votes = 0
            else:
                self._enter_votes = 0
        else:
            if self._ema <= self.exit_at:
                self._exit_votes += 1
                self._enter_votes = 0
                if self._exit_votes >= self.debounce:
                    self._occupied = False
                    self._exit_votes = 0
            else:
                self._exit_votes = 0
        return self._occupied, float(self._ema)

    def reset(self) -> None:
        self._ema = None
        self._occupied = False
        self._enter_votes = 0
        self._exit_votes = 0


def score_to_occupied(
    p_occupied: float,
    *,
    calibration: dict[str, Any] | None,
    default_threshold: float = 0.5,
) -> tuple[bool, float]:
    """Stateless threshold (prefer PresenceDecision for live)."""
    thr = resolve_threshold(
        calibration=calibration, default_threshold=default_threshold
    )
    return p_occupied >= thr, thr
