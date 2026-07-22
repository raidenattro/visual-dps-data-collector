"""volume_calibration 单元测试。"""

from __future__ import annotations

import numpy as np

from spatial_pose.schema import empty_spatial_config, normalize_spatial_config
from spatial_pose.volume_calibration import (
    assign_volume_world_corners,
    default_layer_z_m,
    normalize_layer_z_m,
    validate_volume_corners_image,
    yz_to_layer_index,
)


def _synthetic_volume_config():
    cfg = empty_spatial_config("test")
    cfg["volume"] = {
        "enabled": True,
        "width_m": 2.0,
        "depth_m": 4.0,
        "height_m": 2.0,
        "corners_image_px": [
            [100, 400],
            [300, 400],
            [280, 200],
            [120, 200],
            [110, 350],
            [290, 350],
            [270, 180],
            [130, 180],
        ],
    }
    return normalize_spatial_config(cfg)


def test_assign_volume_world_corners():
    cfg = _synthetic_volume_config()
    pts = assign_volume_world_corners(cfg)
    assert pts.shape == (8, 3)
    assert float(pts[1, 0]) == 2.0
    assert float(pts[4, 2]) == 2.0


def test_validate_volume_corners_rmse_small():
    cfg = _synthetic_volume_config()
    rmse = validate_volume_corners_image(cfg)
    assert rmse < 5.0


def test_yz_to_layer_index():
    z = normalize_layer_z_m(None, height_m=2.4, layer_count=4)
    assert yz_to_layer_index(0.3, z) == 1
    assert yz_to_layer_index(1.3, z) == 3
    assert yz_to_layer_index(2.4, z) == 4


def test_default_layer_z_m_count():
    z = default_layer_z_m(2.0, 5)
    assert len(z) == 6
    assert z[0] == 0.0
    assert abs(z[-1] - 2.0) < 1e-6
