"""Paths for the presence_detection workstream."""

from __future__ import annotations

from pathlib import Path

PRESENCE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PRESENCE_ROOT.parent
CSI_PIPELINE = REPO_ROOT / "csi_pipeline_new"
MODELS_DIR = PRESENCE_ROOT / "models"
EXPORTS_DIR = PRESENCE_ROOT / "exports"


def model_path() -> Path:
    """Prefer presence_detection/models; fall back to csi_pipeline_new/models."""
    local = MODELS_DIR / "object_detector.joblib"
    if local.is_file():
        return local
    return CSI_PIPELINE / "models" / "object_detector.joblib"


def calibration_path() -> Path:
    local = MODELS_DIR / "site_calibration.joblib"
    if local.is_file():
        return local
    return CSI_PIPELINE / "models" / "site_calibration.joblib"
