"""手腕与货框命中检测（与 collision.py / export_pose_xlsx 规则一致）。"""

from __future__ import annotations

import math
from typing import Any, Literal

from event_engine.box_identity import box_collision_token

# COCO17：7=left_elbow，8=right_elbow，9=left_wrist，10=right_wrist
WRIST_KEYPOINTS = ((9, "left_wrist"), (10, "right_wrist"))
ARM_PROBE_SIDES = (
    (7, 9, "left_wrist"),
    (8, 10, "right_wrist"),
)
WRIST_KPT_SCORE_MIN = 0.3
DEFAULT_EXTENSION_RATIO = 0.4
DEFAULT_MIN_FOREARM_PX = 15.0

ProbeMode = Literal["wrist", "hand_extended"]


def _point_in_contour(x: float, y: float, contour: Any) -> bool:
    """点是否在多边形内（含边界）；优先 cv2，不可用时射线法。"""
    try:
        import cv2

        if hasattr(cv2, "pointPolygonTest"):
            return cv2.pointPolygonTest(contour, (x, y), False) >= 0
    except Exception:
        pass

    pts = list(contour) if contour is not None else []
    if len(pts) < 3:
        try:
            import numpy as np

            arr = np.asarray(contour, dtype=float)
            if arr.size >= 6:
                if arr.ndim == 3:
                    arr = arr.reshape(-1, 2)
                elif arr.ndim == 2 and arr.shape[1] != 2:
                    arr = arr.reshape(-1, 2)
                poly = [(float(x), float(y)) for x, y in arr]
            else:
                return False
        except Exception:
            return False
    else:
        poly: list[tuple[float, float]] = []
        for pt in pts:
            if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                poly.append((float(pt[0]), float(pt[1])))
            else:
                try:
                    import numpy as np

                    arr = np.asarray(pt, dtype=float).reshape(-1)
                    if arr.size >= 2:
                        poly.append((float(arr[0]), float(arr[1])))
                except Exception:
                    continue
    if len(poly) < 3:
        return False

    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if ((y1 > y) != (y2 > y)) and (x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-12) + x1):
            inside = not inside
    return inside


def read_keypoint(
    person: dict[str, Any],
    kpt_idx: int,
    *,
    score_min: float = WRIST_KPT_SCORE_MIN,
) -> tuple[float, float, float] | None:
    """读取有效关键点；无效返回 None。"""
    keypoints = person.get("keypoints") or []
    if len(keypoints) <= kpt_idx:
        return None
    kp = keypoints[kpt_idx]
    if not isinstance(kp, (list, tuple)) or len(kp) < 3:
        return None
    score = float(kp[2])
    if score <= score_min:
        return None
    return float(kp[0]), float(kp[1]), score


def read_wrist_point(
    person: dict[str, Any],
    kpt_idx: int,
    *,
    score_min: float = WRIST_KPT_SCORE_MIN,
) -> tuple[float, float, float] | None:
    """读取有效手腕点；无效返回 None。"""
    return read_keypoint(person, kpt_idx, score_min=score_min)


def read_collision_probe(
    person: dict[str, Any],
    elbow_idx: int,
    wrist_idx: int,
    side_name: str,
    *,
    mode: ProbeMode = "wrist",
    extension_ratio: float = DEFAULT_EXTENSION_RATIO,
    score_min: float = WRIST_KPT_SCORE_MIN,
    min_forearm_px: float = DEFAULT_MIN_FOREARM_PX,
    fallback_to_wrist: bool = True,
) -> tuple[float, float, float, str] | None:
    """碰撞探针点：wrist 或沿肘→腕方向延长模拟手部。

    返回 (x, y, score, probe_kind)，probe_kind 为 wrist / hand_sim / wrist_fallback。
    """
    wrist_pt = read_keypoint(person, wrist_idx, score_min=score_min)
    if mode == "wrist":
        if wrist_pt is None:
            return None
        wx, wy, ws = wrist_pt
        return wx, wy, ws, "wrist"

    if wrist_pt is None:
        return None
    wx, wy, ws = wrist_pt
    elbow_pt = read_keypoint(person, elbow_idx, score_min=score_min)
    if elbow_pt is None:
        if fallback_to_wrist:
            return wx, wy, ws, "wrist_fallback"
        return None

    ex, ey, _es = elbow_pt
    fx, fy = wx - ex, wy - ey
    forearm_len = math.hypot(fx, fy)
    if forearm_len < min_forearm_px:
        if fallback_to_wrist:
            return wx, wy, ws, "wrist_fallback"
        return None

    ratio = max(0.0, float(extension_ratio))
    hx = wx + ratio * fx
    hy = wy + ratio * fy
    return hx, hy, ws, "hand_sim"


def _person_probe_hits_for_boxes(
    person: dict[str, Any],
    boxes: list[dict[str, Any]],
    *,
    score_min: float = WRIST_KPT_SCORE_MIN,
    probe_mode: ProbeMode = "wrist",
    extension_ratio: float = DEFAULT_EXTENSION_RATIO,
    min_forearm_px: float = DEFAULT_MIN_FOREARM_PX,
    fallback_to_wrist: bool = True,
) -> list[dict[str, Any]]:
    """检测此人各侧探针进入的货框。"""
    hits: list[dict[str, Any]] = []
    for elbow_idx, wrist_idx, side_name in ARM_PROBE_SIDES:
        probe = read_collision_probe(
            person,
            elbow_idx,
            wrist_idx,
            side_name,
            mode=probe_mode,
            extension_ratio=extension_ratio,
            score_min=score_min,
            min_forearm_px=min_forearm_px,
            fallback_to_wrist=fallback_to_wrist,
        )
        if probe is None:
            continue
        px, py, ps, probe_kind = probe
        for box in boxes:
            contour = box.get("orig_contour")
            if contour is None:
                continue
            if _point_in_contour(px, py, contour):
                token = box_collision_token(box)
                if token:
                    hits.append({
                        "wrist": side_name,
                        "token": token,
                        "x": px,
                        "y": py,
                        "score": ps,
                        "probe_kind": probe_kind,
                        "probe_mode": probe_mode,
                    })
                break
    return hits


def person_wrist_hits(
    person: dict[str, Any],
    boxes: list[dict[str, Any]],
    *,
    score_min: float = WRIST_KPT_SCORE_MIN,
) -> list[dict[str, Any]]:
    """检测此人手腕进入的货框，返回 [{wrist, token, x, y, score}, ...]。"""
    return _person_probe_hits_for_boxes(
        person,
        boxes,
        score_min=score_min,
        probe_mode="wrist",
    )


def person_collision_probe_hits(
    person: dict[str, Any],
    boxes: list[dict[str, Any]],
    *,
    score_min: float = WRIST_KPT_SCORE_MIN,
    probe_mode: ProbeMode = "wrist",
    extension_ratio: float = DEFAULT_EXTENSION_RATIO,
    min_forearm_px: float = DEFAULT_MIN_FOREARM_PX,
    fallback_to_wrist: bool = True,
) -> list[dict[str, Any]]:
    """按探针模式检测进入货框，返回含 probe_kind 的命中列表。"""
    return _person_probe_hits_for_boxes(
        person,
        boxes,
        score_min=score_min,
        probe_mode=probe_mode,
        extension_ratio=extension_ratio,
        min_forearm_px=min_forearm_px,
        fallback_to_wrist=fallback_to_wrist,
    )
