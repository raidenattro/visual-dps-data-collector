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
    x_min, x_max, y_min, y_max = cal.ground_map_bounds()
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
class StickyFootTracker:
    """按上一帧足部像素位置跟踪同一人，跨帧跳变过大时开启新轨迹段。"""

    last_uv: tuple[float, float] | None = None
    last_frame_idx: int = 0
    trail_segment_id: int = 0
    max_uv_jump_px: float = 100.0
    max_frame_gap: int = 25

    @classmethod
    def from_calibration(cls, cal: SpatialCalibration) -> StickyFootTracker:
        rt = cal.runtime()
        infer_w = int(cal.infer_width or 852)
        ratio = float(rt.get("sticky_max_uv_jump_ratio", 0.12))
        return cls(
            max_uv_jump_px=max(40.0, infer_w * ratio),
            max_frame_gap=int(rt.get("sticky_max_frame_gap", 25)),
        )

    def _start_new_segment(self) -> None:
        self.trail_segment_id += 1
        self.last_uv = None

    def pick_person(
        self,
        persons: list[dict[str, Any]],
        *,
        frame_idx: int,
        score_min: float,
    ) -> tuple[dict[str, Any] | None, int, bool]:
        """返回 (person, trail_segment_id, segment_break)。"""
        segment_break = False
        if frame_idx > 0 and self.last_frame_idx > 0:
            if frame_idx - self.last_frame_idx > self.max_frame_gap:
                self._start_new_segment()
                segment_break = True

        candidates: list[tuple[dict[str, Any], tuple[float, float]]] = []
        for person in persons:
            if not isinstance(person, dict):
                continue
            uv = foot_uv_from_person(person, score_min=score_min)
            if uv:
                candidates.append((person, uv))

        if not candidates:
            self.last_uv = None
            return None, self.trail_segment_id, segment_break

        chosen: dict[str, Any] | None = None
        chosen_uv: tuple[float, float] | None = None

        if self.last_uv is not None:
            best_person, best_uv = min(
                candidates,
                key=lambda item: float(
                    np.hypot(item[1][0] - self.last_uv[0], item[1][1] - self.last_uv[1])
                ),
            )
            dist = float(np.hypot(best_uv[0] - self.last_uv[0], best_uv[1] - self.last_uv[1]))
            if dist <= self.max_uv_jump_px:
                chosen, chosen_uv = best_person, best_uv
            else:
                self._start_new_segment()
                segment_break = True
                chosen = pick_primary_person(persons)
                chosen_uv = foot_uv_from_person(chosen, score_min=score_min) if chosen else None
        else:
            chosen = pick_primary_person(persons)
            chosen_uv = foot_uv_from_person(chosen, score_min=score_min) if chosen else None

        if chosen_uv is not None:
            self.last_uv = chosen_uv
        self.last_frame_idx = frame_idx
        return chosen, self.trail_segment_id, segment_break


@dataclass
class FloorProjectionResult:
    foot_uv_px: list[float] | None = None
    raw_floor_xy_m: list[float] | None = None
    floor_xy_m: list[float] | None = None
    trail_segment_id: int = 0


def project_foot_for_frame(
    cal: SpatialCalibration,
    persons: list[dict[str, Any]],
    smooth_state: FloorSmoothState,
    *,
    sticky_tracker: StickyFootTracker | None = None,
    frame_idx: int = 0,
) -> FloorProjectionResult:
    score_min = float(cal.runtime().get("foot_score_min", 0.35))
    trail_segment_id = 0
    if sticky_tracker is not None:
        person, trail_segment_id, segment_break = sticky_tracker.pick_person(
            persons,
            frame_idx=frame_idx,
            score_min=score_min,
        )
        if segment_break:
            smooth_state.smooth_xy = None
    else:
        person = pick_primary_person(persons)
    if person is None:
        return FloorProjectionResult()
    foot_uv = foot_uv_from_person(person, score_min=score_min)
    if foot_uv is None:
        return FloorProjectionResult(trail_segment_id=trail_segment_id)
    raw_xy = project_uv_to_floor(cal, foot_uv)
    if raw_xy is None:
        smooth_state.smooth_xy = None
        return FloorProjectionResult(trail_segment_id=trail_segment_id)
    smooth_xy = smooth_state.apply(raw_xy)
    result = FloorProjectionResult(
        foot_uv_px=[foot_uv[0], foot_uv[1]],
        trail_segment_id=trail_segment_id,
    )
    result.raw_floor_xy_m = [raw_xy[0], raw_xy[1]]
    if smooth_xy is not None:
        result.floor_xy_m = [smooth_xy[0], smooth_xy[1]]
    else:
        result.floor_xy_m = [raw_xy[0], raw_xy[1]]
    return result
