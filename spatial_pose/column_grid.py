"""地面作业列区域：边界与列号判定。"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from spatial_pose.calibration import SpatialCalibration


def column_boundaries_from_count(width_m: float, column_count: int) -> list[float]:
    """等分通道宽，返回 column_count+1 条边界 x（米）。"""
    width = max(0.01, float(width_m))
    count = max(1, int(column_count))
    step = width / count
    return [round(i * step, 6) for i in range(count + 1)]


def normalize_column_boundaries(
    boundaries_x_m: list[float] | None,
    *,
    width_m: float,
    column_count: int | None = None,
    allow_equal_split: bool = True,
) -> list[float]:
    """补齐或校验列边界；无手动标定时默认整宽 1 列，不自动等分。"""
    width = max(0.01, float(width_m))
    if boundaries_x_m and len(boundaries_x_m) >= 2:
        out = [float(x) for x in boundaries_x_m]
        out[0] = 0.0
        out[-1] = width
        return out
    if not allow_equal_split:
        return [0.0, width]
    count = max(1, int(column_count or 4))
    return column_boundaries_from_count(width, count)


def column_lines_image_from_drawn(
    image_lines: list[list[list[float]]],
    *,
    boundaries_x_m: list[float] | None = None,
    boundaries_y_m: list[float] | None = None,
) -> list[dict[str, Any]]:
    """用户手标列线 → 预览线段（不再由等分边界重算）。"""
    inner: list[float] = []
    key = "world_x_m"
    if boundaries_y_m and len(boundaries_y_m) > 2:
        inner = [float(y) for y in boundaries_y_m[1:-1]]
        key = "world_y_m"
    elif boundaries_x_m and len(boundaries_x_m) > 2:
        inner = [float(x) for x in boundaries_x_m[1:-1]]
    out: list[dict[str, Any]] = []
    for i, seg in enumerate(image_lines):
        if not seg or len(seg) < 2:
            continue
        item: dict[str, Any] = {
            "image": [
                [float(seg[0][0]), float(seg[0][1])],
                [float(seg[1][0]), float(seg[1][1])],
            ]
        }
        if i < len(inner):
            item[key] = inner[i]
        out.append(item)
    return out


def floor_xy_to_column(x_m: float, boundaries_x_m: list[float]) -> int | None:
    """floor x（米）→ 列号（1-based）。"""
    if not boundaries_x_m or len(boundaries_x_m) < 2:
        return None
    x = float(x_m)
    for i in range(len(boundaries_x_m) - 1):
        lo = float(boundaries_x_m[i])
        hi = float(boundaries_x_m[i + 1])
        if i == len(boundaries_x_m) - 2:
            if lo <= x <= hi + 1e-6:
                return i + 1
        elif lo <= x < hi:
            return i + 1
    return None


def _segment_intersect(
    p1: tuple[float, float],
    p2: tuple[float, float],
    q1: tuple[float, float],
    q2: tuple[float, float],
    *,
    eps: float = 1e-6,
) -> tuple[float, float] | None:
    """两线段交点（交点落在两段内部时返回）。"""
    x1, y1 = p1
    x2, y2 = p2
    x3, y3 = q1
    x4, y4 = q2
    den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(den) < eps:
        return None
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / den
    u = -((x1 - x2) * (y1 - y3) - (y1 - y2) * (x1 - x3)) / den
    if -eps <= t <= 1.0 + eps and -eps <= u <= 1.0 + eps:
        return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))
    return None


def _world_y_from_image_uv(cal: "SpatialCalibration", u: float, v: float) -> float:
    from spatial_pose.calibration import transform_points

    uv = np.array([[float(u), float(v)]], dtype=np.float64)
    xy = transform_points(uv, cal.h_image_to_world)[0]
    tuning = cal.config.get("tuning") or {}
    scale_xy = tuning.get("scale_xy") or [1.0, 1.0]
    offset_xy = tuning.get("offset_xy_m") or [0.0, 0.0]
    return float(xy[1]) * float(scale_xy[1]) + float(offset_xy[1])


def floor_y_to_column(y_m: float, boundaries_y_m: list[float]) -> int | None:
    """floor y（纵深，米）→ 列号（1-based）。"""
    if not boundaries_y_m or len(boundaries_y_m) < 2:
        return None
    y = float(y_m)
    for i in range(len(boundaries_y_m) - 1):
        lo = float(boundaries_y_m[i])
        hi = float(boundaries_y_m[i + 1])
        if i == len(boundaries_y_m) - 2:
            if lo <= y <= hi + 1e-6:
                return i + 1
        elif lo <= y < hi:
            return i + 1
    return None


def resolve_column_boundaries(
    config: dict[str, Any],
    *,
    width_m: float,
    depth_m: float,
) -> tuple[list[float], list[float], str]:
    """返回 (boundaries_x_m, boundaries_y_m, axis) axis 为 'x' 或 'y'。"""
    gc = config.get("ground_columns") if isinstance(config.get("ground_columns"), dict) else {}
    bym = gc.get("boundaries_y_m") or []
    bxm = gc.get("boundaries_x_m") or []
    axis = str(gc.get("column_axis") or "").strip().lower()
    width = max(0.01, float(width_m))
    depth = max(0.01, float(depth_m))
    if axis == "y" or (axis != "x" and isinstance(bym, list) and len(bym) >= 2):
        out = normalize_depth_boundaries(bym, depth_m=depth)
        return [0.0, width], out, "y"
    if isinstance(bxm, list) and len(bxm) >= 2:
        out = normalize_column_boundaries(bxm, width_m=width, allow_equal_split=False)
        return out, [0.0, depth], "x"
    return [0.0, width], [0.0, depth], "x"


def normalize_depth_boundaries(
    boundaries_y_m: list[float] | None,
    *,
    depth_m: float,
) -> list[float]:
    """补齐纵深列边界（含 0 与 depth）。"""
    depth = max(0.01, float(depth_m))
    if boundaries_y_m and len(boundaries_y_m) >= 2:
        out = [float(y) for y in boundaries_y_m]
        out[0] = 0.0
        out[-1] = depth
        return out
    return [0.0, depth]


def refine_boundaries_from_image_lines(
    cal: "SpatialCalibration",
    image_lines: list[list[list[float]]],
    *,
    width_m: float,
    depth_m: float,
    corners_image_px: list[list[float]] | None = None,
) -> tuple[list[float], list[float]]:
    """BL–FL 与 BR–FR 连线端点经 floor homography 反算纵深 → boundaries_y_m。"""
    _ = corners_image_px
    width = max(0.01, float(width_m))
    depth = max(0.01, float(depth_m))
    ys: list[float] = [0.0, depth]
    for seg in image_lines:
        if not seg or len(seg) < 2:
            continue
        p1 = (float(seg[0][0]), float(seg[0][1]))
        p2 = (float(seg[1][0]), float(seg[1][1]))
        y1 = _world_y_from_image_uv(cal, p1[0], p1[1])
        y2 = _world_y_from_image_uv(cal, p2[0], p2[1])
        y = (y1 + y2) / 2.0
        if 0.0 < y < depth:
            ys.append(y)
    ys = sorted(set(round(v, 4) for v in ys))
    if len(ys) < 2:
        return [0.0, width], [0.0, depth]
    ys[0] = 0.0
    ys[-1] = depth
    return [0.0, width], ys


def column_lines_image_from_boundaries(
    corners_image_px: list[list[float]],
    boundaries_x_m: list[float],
    *,
    width_m: float,
) -> list[dict[str, Any]]:
    """由 8 角点底面四边形 + x 边界生成地面列竖线（图像线段）。"""
    if len(corners_image_px) < 4 or len(boundaries_x_m) < 2:
        return []
    width = max(0.01, float(width_m))
    bl = corners_image_px[0]
    br = corners_image_px[1]
    fr = corners_image_px[2]
    fl = corners_image_px[3]

    def lerp(a: list[float], b: list[float], t: float) -> list[float]:
        return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]

    segments: list[dict[str, Any]] = []
    for x_m in boundaries_x_m[1:-1]:
        t = float(x_m) / width
        near = lerp(bl, br, t)
        far = lerp(fl, fr, t)
        segments.append({"world_x_m": float(x_m), "image": [near, far]})
    return segments


def ground_columns_block(config: dict[str, Any]) -> dict[str, Any]:
    gc = config.get("ground_columns") if isinstance(config.get("ground_columns"), dict) else {}
    vol = config.get("volume") if isinstance(config.get("volume"), dict) else {}
    width_m = float(vol.get("width_m") or config.get("visualization", {}).get("grid_width_m") or 2.0)
    depth_m = float(vol.get("depth_m") or config.get("visualization", {}).get("grid_depth_m") or 9.6)
    bxm, bym, axis = resolve_column_boundaries(config, width_m=width_m, depth_m=depth_m)
    drawn = gc.get("boundaries_image_px") or []
    if axis == "y":
        count = max(1, len(bym) - 1)
    elif bxm and len(bxm) >= 2:
        count = max(1, len(bxm) - 1)
    elif isinstance(drawn, list) and drawn:
        count = max(1, len(drawn) + 1)
    else:
        count = 1
    return {
        "column_count": count,
        "boundaries_x_m": bxm,
        "boundaries_y_m": bym,
        "column_axis": axis,
        "width_m": width_m,
        "depth_m": depth_m,
    }
