"""地面网格线（世界坐标 → 图像像素）。"""

from __future__ import annotations

from typing import Any

import numpy as np

from spatial_pose.calibration import SpatialCalibration, transform_points


def grid_line_segments_world(
    *,
    width_m: float = 2.0,
    depth_m: float = 9.6,
    spacing_m: float = 2.4,
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """返回地面网格线段端点（米）。"""
    lines: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for x in (0.0, width_m):
        lines.append(((x, 0.0), (x, depth_m)))
    y = 0.0
    while y <= depth_m + 1e-6:
        lines.append(((0.0, y), (width_m, y)))
        y += spacing_m
    return lines


def grid_segments_image(
    cal: SpatialCalibration,
) -> list[dict[str, Any]]:
    """将网格线段投射到图像像素，供前端/预览绘制。"""
    vis = cal.visualization()
    width_m = float(vis.get("grid_width_m") or 2.0)
    depth_m = float(vis.get("grid_depth_m") or 9.6)
    spacing_m = float(vis.get("grid_spacing_m") or 2.4)
    segments: list[dict[str, Any]] = []
    for (x0, y0), (x1, y1) in grid_line_segments_world(
        width_m=width_m,
        depth_m=depth_m,
        spacing_m=spacing_m,
    ):
        pts = np.array([[x0, y0], [x1, y1]], dtype=np.float64)
        px = transform_points(pts, cal.h_world_to_image)
        segments.append(
            {
                "world": [[x0, y0], [x1, y1]],
                "image": [[float(px[0][0]), float(px[0][1])], [float(px[1][0]), float(px[1][1])]],
            }
        )
    return segments
