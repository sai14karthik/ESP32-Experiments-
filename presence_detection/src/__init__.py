"""Presence-detection package: wrappers around ``csi_pipeline_new`` capture/train/live."""

from __future__ import annotations

from presence_detection.src.paths import (
    CSI_PIPELINE,
    EXPORTS_DIR,
    MODELS_DIR,
    PRESENCE_ROOT,
    REPO_ROOT,
    calibration_path,
    model_path,
)

__all__ = [
    "CSI_PIPELINE",
    "EXPORTS_DIR",
    "MODELS_DIR",
    "PRESENCE_ROOT",
    "REPO_ROOT",
    "calibration_path",
    "model_path",
]
