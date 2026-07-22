"""column_grid 单元测试。"""

from __future__ import annotations

from spatial_pose.column_grid import (
    _segment_intersect,
    column_boundaries_from_count,
    floor_xy_to_column,
    normalize_column_boundaries,
    refine_boundaries_from_image_lines,
)
from tests.test_spatial_calibration import _synthetic_volume_config


def test_column_boundaries_equal_split():
    b = column_boundaries_from_count(2.0, 4)
    assert b == [0.0, 0.5, 1.0, 1.5, 2.0]


def test_normalize_without_equal_split():
    b = normalize_column_boundaries(None, width_m=2.0, allow_equal_split=False)
    assert b == [0.0, 2.0]


def test_floor_xy_to_column():
    b = normalize_column_boundaries(None, width_m=2.0, column_count=4)
    assert floor_xy_to_column(0.25, b) == 1
    assert floor_xy_to_column(1.75, b) == 4
    assert floor_xy_to_column(-1.0, b) is None


def test_segment_intersect():
    hit = _segment_intersect((0, 0), (10, 10), (0, 10), (10, 0))
    assert hit is not None
    assert abs(hit[0] - 5.0) < 1e-6 and abs(hit[1] - 5.0) < 1e-6
    assert _segment_intersect((0, 0), (1, 1), (5, 5), (6, 6)) is None


def test_refine_boundaries_from_bl_fl_br_fr_line():
    """BL–FL 与 BR–FR 横截线反算纵深 boundaries_y_m。"""
    from spatial_pose.calibration import build_spatial_calibration

    cfg = _synthetic_volume_config()
    cal = build_spatial_calibration(cfg, infer_width=640, infer_height=480)
    corners = cfg["volume"]["corners_image_px"]
    bl, br, fr, fl = corners[0], corners[1], corners[2], corners[3]
    mid_left = [(bl[0] + fl[0]) / 2, (bl[1] + fl[1]) / 2]
    mid_right = [(br[0] + fr[0]) / 2, (br[1] + fr[1]) / 2]
    col_line = [mid_left, mid_right]
    bxm, bym = refine_boundaries_from_image_lines(
        cal, [col_line], width_m=2.0, depth_m=9.6, corners_image_px=corners
    )
    assert bxm == [0.0, 2.0]
    assert bym[0] == 0.0 and bym[-1] == 9.6
    assert len(bym) >= 3
    inner = bym[1:-1]
    assert all(0.0 < y < 9.6 for y in inner)
    assert abs(inner[0] - 4.8) < 0.5
