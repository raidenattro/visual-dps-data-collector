"""标定预览图渲染。"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from spatial_pose.calibration import SpatialCalibration, transform_points
from spatial_pose.grid import grid_line_segments_world


def render_calibration_preview(
    frame_bgr: np.ndarray,
    cal: SpatialCalibration,
    *,
    image_points: np.ndarray | None = None,
    errors: list[float] | None = None,
) -> np.ndarray:
    """在帧上绘制控制点与地面网格。"""
    out = frame_bgr.copy()
    vis = cal.visualization()
    width_m = float(vis.get("grid_width_m") or 2.0)
    depth_m = float(vis.get("grid_depth_m") or 9.6)
    spacing_m = float(vis.get("grid_spacing_m") or 2.4)

    for (x0, y0), (x1, y1) in grid_line_segments_world(
        width_m=width_m,
        depth_m=depth_m,
        spacing_m=spacing_m,
    ):
        pts = np.array([[x0, y0], [x1, y1]], dtype=np.float64)
        px = transform_points(pts, cal.h_world_to_image)
        p0 = tuple(np.round(px[0]).astype(int))
        p1 = tuple(np.round(px[1]).astype(int))
        cv2.line(out, p0, p1, (80, 210, 80), 2, cv2.LINE_AA)

    if image_points is not None:
        for i, p in enumerate(image_points):
            centre = tuple(np.round(p).astype(int))
            cv2.circle(out, centre, 7, (20, 20, 245), -1, cv2.LINE_AA)
            cv2.putText(
                out,
                f"P{i + 1}",
                (centre[0] + 7, centre[1] - 7),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.45,
                (255, 255, 255),
                2,
                cv2.LINE_AA,
            )

    rmse = cal.ground_control_rmse_px
    cv2.rectangle(out, (8, out.shape[0] - 42), (410, out.shape[0] - 8), (20, 20, 20), -1)
    cv2.putText(
        out,
        f"Ground calibration RMSE: {rmse:.2f} px",
        (18, out.shape[0] - 18),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.58,
        (80, 235, 255),
        2,
        cv2.LINE_AA,
    )
    return out


def write_calibration_preview(
    frame_bgr: np.ndarray,
    cal: SpatialCalibration,
    output_path: Path,
    *,
    image_points: np.ndarray | None = None,
) -> Path:
    img = render_calibration_preview(frame_bgr, cal, image_points=image_points)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(output_path), img)
    return output_path.resolve()
