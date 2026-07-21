"""spatial 标定单元测试。"""

from __future__ import annotations

import numpy as np

from spatial_pose.calibration import (
    build_world_points_from_physical,
    compute_and_update_config,
    compute_homography,
    scale_image_points,
    transform_points,
)
from spatial_pose.schema import empty_spatial_config


def _synthetic_calibration_config() -> dict:
    world = build_world_points_from_physical(
        {"aisle_width_m": 2.0, "marker_spacing_m": 2.4, "marker_pairs": 5}
    )
    # 简单透视：x' = 100 + 200*x, y' = 400 + 50*y
    image_pts = []
    for x, y in world:
        image_pts.append([100.0 + 200.0 * x, 400.0 + 50.0 * y])
    cfg = empty_spatial_config("test-cam")
    cfg["calibration"]["resolution"] = [640, 480]
    cfg["calibration"]["image_points_px"] = image_pts
    cfg["physical"] = {"aisle_width_m": 2.0, "marker_spacing_m": 2.4, "marker_pairs": 5}
    return cfg


def test_build_world_points_count_and_corners():
    world = build_world_points_from_physical(
        {"aisle_width_m": 2.0, "marker_spacing_m": 2.4, "marker_pairs": 5}
    )
    assert world.shape == (10, 2)
    assert float(world[0, 0]) == 0.0 and float(world[0, 1]) == 9.6
    assert float(world[-1, 1]) == 0.0


def test_homography_roundtrip_rmse_near_zero():
    cfg = _synthetic_calibration_config()
    cal = compute_and_update_config(cfg)
    assert cal.ground_control_rmse_px < 1e-4
    uv = np.array([[300.0, 450.0]], dtype=np.float64)
    xy = transform_points(uv, cal.h_image_to_world)[0]
    back = transform_points(xy.reshape(1, 2), cal.h_world_to_image)[0]
    assert np.linalg.norm(back - uv[0]) < 1e-4


def test_scale_image_points():
    pts = np.array([[100.0, 50.0], [200.0, 100.0]], dtype=np.float64)
    scaled = scale_image_points(pts, 640, 480, 320, 240)
    assert abs(scaled[0, 0] - 50.0) < 1e-6
    assert abs(scaled[0, 1] - 25.0) < 1e-6


def test_compute_homography_identity_plane():
    world = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype=np.float64)
    image = np.array([[10, 10], [110, 10], [110, 110], [10, 110]], dtype=np.float64)
    _, h_i2w, rmse, _ = compute_homography(world, image)
    xy = transform_points(np.array([[60.0, 60.0]]), h_i2w)[0]
    assert abs(xy[0] - 0.5) < 0.05
    assert abs(xy[1] - 0.5) < 0.05
    assert rmse < 1e-6
