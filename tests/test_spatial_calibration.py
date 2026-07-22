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


def _synthetic_volume_config() -> dict:
    """立体底面 homography 合成标定（无 10 地面点）。"""
    cfg = empty_spatial_config("test-vol")
    cfg["enabled"] = True
    cfg["volume"]["enabled"] = True
    cfg["volume"]["width_m"] = 2.0
    cfg["volume"]["depth_m"] = 9.6
    cfg["volume"]["height_m"] = 2.4
    cfg["calibration"]["resolution"] = [640, 480]
    bottom = [(0.0, 0.0), (2.0, 0.0), (2.0, 9.6), (0.0, 9.6)]
    image_pts = []
    for x, y in bottom:
        image_pts.append([100.0 + 200.0 * x, 400.0 + 50.0 * y])
    for x, y in bottom:
        image_pts.append([100.0 + 200.0 * x, 350.0 + 50.0 * y])
    cfg["volume"]["corners_image_px"] = image_pts
    cfg["visualization"] = {"grid_width_m": 2.0, "grid_depth_m": 9.6, "grid_spacing_m": 2.4}
    return cfg


def test_prepare_volume_only_save():
    from spatial_pose.calibration import prepare_spatial_config_for_save

    cfg = _synthetic_volume_config()
    norm, cal = prepare_spatial_config_for_save(cfg, infer_width=640, infer_height=480)
    assert cal is not None
    assert norm["enabled"] is True
    assert norm["computed"]["floor_source"] == "volume_bottom"
    assert len(norm["computed"]["image_to_ground_homography"]) == 3


def test_build_spatial_calibration_from_volume():
    from spatial_pose.calibration import build_spatial_calibration

    cfg = _synthetic_volume_config()
    cal = build_spatial_calibration(cfg, infer_width=640, infer_height=480)
    assert cal.ground_control_rmse_px < 1e-3
    uv = __import__("numpy").array([[300.0, 450.0]], dtype=float)
    xy = transform_points(uv, cal.h_image_to_world)[0]
    assert 0.0 <= xy[0] <= 2.5
    assert 0.0 <= xy[1] <= 10.0


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
