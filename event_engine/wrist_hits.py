"""手腕与货框命中检测（与 collision.py / export_pose_xlsx 规则一致）。"""

from __future__ import annotations

from typing import Any

from event_engine.box_identity import box_collision_token

# COCO17：9=left_wrist，10=right_wrist
WRIST_KEYPOINTS = ((9, "left_wrist"), (10, "right_wrist"))
WRIST_KPT_SCORE_MIN = 0.3


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


def read_wrist_point(
    person: dict[str, Any],
    kpt_idx: int,
    *,
    score_min: float = WRIST_KPT_SCORE_MIN,
) -> tuple[float, float, float] | None:
    """读取有效手腕点；无效返回 None。"""
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


def person_wrist_hits(
    person: dict[str, Any],
    boxes: list[dict[str, Any]],
    *,
    score_min: float = WRIST_KPT_SCORE_MIN,
) -> list[dict[str, Any]]:
    """检测此人手腕进入的货框，返回 [{wrist, token, x, y, score}, ...]。"""
    hits: list[dict[str, Any]] = []
    for kpt_idx, wrist_name in WRIST_KEYPOINTS:
        pt = read_wrist_point(person, kpt_idx, score_min=score_min)
        if pt is None:
            continue
        wx, wy, ws = pt
        for box in boxes:
            contour = box.get("orig_contour")
            if contour is None:
                continue
            if _point_in_contour(wx, wy, contour):
                token = box_collision_token(box)
                if token:
                    hits.append({
                        "wrist": wrist_name,
                        "token": token,
                        "x": wx,
                        "y": wy,
                        "score": ws,
                    })
                break
    return hits
