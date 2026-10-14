"""Load the healthcare YOLO26-pose pipeline for binary presence GT.

We do **not** train activity here. The pose model is only asked:
someone in frame? → occupied (1) / empty (0).

Presets + defaults come from healthcare_pose_activity (single source of truth).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

_POSE_SRC = Path(__file__).resolve().parents[4] / "healthcare_pose_activity" / "src"
# parents: cv_ground_truth → csi_presence → src → csi_presence_c5 → camera_module
# Wait: __file__ = .../csi_presence_c5/src/csi_presence/cv_ground_truth/yolo_pipeline.py
# parents[0]=cv_ground_truth, [1]=csi_presence, [2]=src, [3]=csi_presence_c5, [4]=camera_module
if str(_POSE_SRC) not in sys.path:
    sys.path.insert(0, str(_POSE_SRC))

from live_infer import DEFAULT_QUALITY, QUALITY  # noqa: E402

__all__ = [
    "QUALITY",
    "DEFAULT_QUALITY",
    "resolve_profile",
    "pick_device",
    "infer_kwargs",
    "frame_has_person",
]


def resolve_profile(
    quality: str | None = None,
    model: str | None = None,
    imgsz: int | None = None,
    conf: float | None = None,
) -> dict[str, Any]:
    qname = quality or os.environ.get("POSE_QUALITY", DEFAULT_QUALITY)
    if qname not in QUALITY:
        raise ValueError(f"unknown quality={qname!r}; choose from {tuple(QUALITY)}")
    q = QUALITY[qname]
    model_name = model or os.environ.get("POSE_MODEL") or q["model"]
    # Presence GT default conf is stricter than healthcare live (0.25) —
    # empty-room false positives (chairs) otherwise label as occupied.
    _PRESENCE_CONF = 0.45
    return {
        "quality": qname,
        "model": model_name,
        "imgsz": int(imgsz if imgsz is not None else q["imgsz"]),
        "conf": float(
            conf if conf is not None else float(os.environ.get("POSE_CONF", _PRESENCE_CONF))
        ),
        "map": q["map"],
    }


def pick_device(requested: str = "mps") -> str:
    try:
        import torch

        if (
            requested == "mps"
            and getattr(torch.backends, "mps", None)
            and torch.backends.mps.is_available()
        ):
            return "mps"
        if requested not in ("mps", "cpu") and torch.cuda.is_available():
            return requested
    except Exception:
        pass
    return "cpu" if requested == "mps" else requested


def infer_kwargs(profile: dict[str, Any], device: str) -> dict[str, Any]:
    """Match healthcare live_infer._infer_kwargs (person class only)."""
    return dict(
        conf=profile["conf"],
        imgsz=profile["imgsz"],
        device=device,
        verbose=False,
        show=False,
        classes=[0],
        max_det=10,
        nms=False,
    )


def frame_has_person(
    result,
    *,
    min_box_area: float = 8000.0,
    min_box_conf: float = 0.45,
    min_keypoints: int = 4,
    min_kpt_conf: float = 0.35,
) -> bool:
    """Binary presence GT — stricter than raw YOLO so empty rooms stay empty.

    Requires a decent-size person box + (when pose kpts exist) several visible joints.
    Chairs / coats / posters often fire conf=0.25 boxes without real pose.
    """
    if result.boxes is None or len(result.boxes) == 0:
        return False
    xyxy = result.boxes.xyxy.cpu().numpy()
    confs = (
        result.boxes.conf.cpu().numpy()
        if result.boxes.conf is not None
        else [1.0] * len(xyxy)
    )
    kpts = None
    if getattr(result, "keypoints", None) is not None and result.keypoints is not None:
        try:
            # (N, K, 3) x,y,conf
            kpts = result.keypoints.data.cpu().numpy()
        except Exception:
            kpts = None

    for i, box in enumerate(xyxy):
        if float(confs[i]) < min_box_conf:
            continue
        x1, y1, x2, y2 = box
        if float((x2 - x1) * (y2 - y1)) < min_box_area:
            continue
        if kpts is not None and i < len(kpts):
            visible = int((kpts[i][:, 2] >= min_kpt_conf).sum())
            if visible < min_keypoints:
                continue
        return True
    return False
