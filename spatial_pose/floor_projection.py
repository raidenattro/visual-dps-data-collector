"""脚踝 2D → floor_xy_m 投射与平滑。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from spatial_pose.calibration import SpatialCalibration, transform_points

# COCO17: left_ankle=15, right_ankle=16
ANKLE_LEFT = 15
ANKLE_RIGHT = 16


def _pt(keypoints: list, idx: int) -> tuple[float, float, float] | None:
    if idx >= len(keypoints):
        return None
    kp = keypoints[idx]
    if not isinstance(kp, (list, tuple)) or len(kp) < 2:
        return None
    score = float(kp[2]) if len(kp) > 2 else 0.0
    return float(kp[0]), float(kp[1]), score


def pick_primary_person(persons: list[dict[str, Any]]) -> dict[str, Any] | None:
    """优先肩宽最大且踝点可见的 person。"""
    best: dict[str, Any] | None = None
    best_score = -1.0
    for person in persons:
        if not isinstance(person, dict):
            continue
        kpts = person.get("keypoints") or []
        ls = _pt(kpts, 5)
        rs = _pt(kpts, 6)
        la = _pt(kpts, ANKLE_LEFT)
        ra = _pt(kpts, ANKLE_RIGHT)
        if not la and not ra:
            continue
        width = 0.0
        if ls and rs and ls[2] > 0.2 and rs[2] > 0.2:
            width = abs(ls[0] - rs[0])
        ankle_vis = (la[2] if la else 0.0) + (ra[2] if ra else 0.0)
        score = width + ankle_vis * 100.0
        if score > best_score:
            best_score = score
            best = person
    if best is not None:
        return best
    return persons[0] if persons else None


def foot_uv_from_person(
    person: dict[str, Any],
    *,
    score_min: float = 0.35,
) -> tuple[float, float] | None:
    kpts = person.get("keypoints") or []
    feet: list[tuple[float, float]] = []
    for idx in (ANKLE_LEFT, ANKLE_RIGHT):
        pt = _pt(kpts, idx)
        if pt and pt[2] >= score_min:
            feet.append((pt[0], pt[1]))
    if not feet:
        return None
    arr = np.mean(np.array(feet, dtype=np.float64), axis=0)
    return float(arr[0]), float(arr[1])


def project_uv_to_floor(
    cal: SpatialCalibration,
    foot_uv: tuple[float, float],
) -> tuple[float, float] | None:
    uv = np.array([[foot_uv[0], foot_uv[1]]], dtype=np.float64)
    xy = transform_points(uv, cal.h_image_to_world)[0]
    tuning = cal.config.get("tuning") or {}
    scale_xy = tuning.get("scale_xy") or [1.0, 1.0]
    offset_xy = tuning.get("offset_xy_m") or [0.0, 0.0]
    x = float(xy[0]) * float(scale_xy[0]) + float(offset_xy[0])
    y = float(xy[1]) * float(scale_xy[1]) + float(offset_xy[1])
    x_min, x_max, y_min, y_max = cal.floor_bounds()
    if not (x_min <= x <= x_max and y_min <= y <= y_max):
        return None
    return x, y


@dataclass
class FloorSmoothState:
    smooth_xy: np.ndarray | None = None
    alpha_normal: float = 0.2
    alpha_jump: float = 0.08
    jump_threshold_m: float = 1.4

    @classmethod
    def from_calibration(cls, cal: SpatialCalibration) -> FloorSmoothState:
        rt = cal.runtime()
        return cls(
            alpha_normal=float(rt.get("smooth_alpha_normal", 0.2)),
            alpha_jump=float(rt.get("smooth_alpha_jump", 0.08)),
            jump_threshold_m=float(rt.get("smooth_jump_threshold_m", 1.4)),
        )

    def apply(self, raw_xy: tuple[float, float] | None) -> tuple[float, float] | None:
        if raw_xy is None:
            return None
        raw = np.array(raw_xy, dtype=np.float64)
        if self.smooth_xy is None:
            self.smooth_xy = raw.copy()
            return float(raw[0]), float(raw[1])
        delta = float(np.linalg.norm(raw - self.smooth_xy))
        alpha = self.alpha_normal if delta < self.jump_threshold_m else self.alpha_jump
        self.smooth_xy = (1.0 - alpha) * self.smooth_xy + alpha * raw
        return float(self.smooth_xy[0]), float(self.smooth_xy[1])


@dataclass
class FloorProjectionResult:
    foot_uv_px: list[float] | None = None
    raw_floor_xy_m: list[float] | None = None
    floor_xy_m: list[float] | None = None


def project_foot_for_frame(
    cal: SpatialCalibration,
    persons: list[dict[str, Any]],
    smooth_state: FloorSmoothState,
) -> FloorProjectionResult:
    score_min = float(cal.runtime().get("foot_score_min", 0.35))
    person = pick_primary_person(persons)
    if person is None:
        return FloorProjectionResult()
    foot_uv = foot_uv_from_person(person, score_min=score_min)
    if foot_uv is None:
        return FloorProjectionResult()
    raw_xy = project_uv_to_floor(cal, foot_uv)
    smooth_xy = smooth_state.apply(raw_xy)
    result = FloorProjectionResult(
        foot_uv_px=[foot_uv[0], foot_uv[1]],
    )
    if raw_xy is not None:
        result.raw_floor_xy_m = [raw_xy[0], raw_xy[1]]
    if smooth_xy is not None:
        result.floor_xy_m = [smooth_xy[0], smooth_xy[1]]
    elif raw_xy is not None:
        result.floor_xy_m = [raw_xy[0], raw_xy[1]]
    return result
