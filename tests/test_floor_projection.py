"""floor_xy 投射单元测试。"""

from __future__ import annotations

from spatial_pose.calibration import compute_and_update_config
from spatial_pose.floor_projection import FloorSmoothState, project_foot_for_frame
from tests.test_spatial_calibration import _synthetic_calibration_config


def test_project_foot_from_volume_bottom():
    from spatial_pose.calibration import build_spatial_calibration
    from tests.test_spatial_calibration import _synthetic_volume_config

    cal = build_spatial_calibration(_synthetic_volume_config(), infer_width=640, infer_height=480)
    person = {
        "person_id": 0,
        "keypoints": [[0, 0, 0]] * 17,
    }
    person["keypoints"][15] = [300.0, 450.0, 0.9]
    person["keypoints"][16] = [310.0, 450.0, 0.9]
    smooth = FloorSmoothState()
    out = project_foot_for_frame(cal, [person], smooth)
    assert out.foot_uv_px is not None
    assert out.floor_xy_m is not None


def test_project_foot_from_ankles():
    cfg = _synthetic_calibration_config()
    cal = compute_and_update_config(cfg, infer_width=640, infer_height=480)
    person = {
        "person_id": 0,
        "keypoints": [[0, 0, 0]] * 17,
    }
    # 设置左右踝在图像中心附近
    person["keypoints"][15] = [300.0, 450.0, 0.9]
    person["keypoints"][16] = [310.0, 450.0, 0.9]
    smooth = FloorSmoothState()
    out = project_foot_for_frame(cal, [person], smooth)
    assert out.foot_uv_px is not None
    assert out.raw_floor_xy_m is not None
    assert out.floor_xy_m is not None
    assert len(out.floor_xy_m) == 2


def test_floor_smooth_state_ema():
    st = FloorSmoothState(alpha_normal=0.5, jump_threshold_m=10.0)
    a = st.apply((0.0, 0.0))
    assert a == (0.0, 0.0)
    b = st.apply((2.0, 0.0))
    assert b == (1.0, 0.0)


def test_assign_trail_segment_ids_breaks_on_jump():
    from floor_foot_store import assign_trail_segment_ids

    rows = [
        {"frame_idx": 1, "floor_x_m": 0.0, "floor_y_m": 0.0, "foot_u_px": 10.0, "foot_v_px": 10.0},
        {"frame_idx": 2, "floor_x_m": 0.1, "floor_y_m": 0.1, "foot_u_px": 12.0, "foot_v_px": 11.0},
        {"frame_idx": 3, "floor_x_m": 2.5, "floor_y_m": 0.1, "foot_u_px": 400.0, "foot_v_px": 20.0},
    ]
    out = assign_trail_segment_ids(rows, jump_threshold_m=1.4, max_uv_jump_px=120.0)
    assert out[0]["trail_segment_id"] == 0
    assert out[1]["trail_segment_id"] == 0
    assert out[2]["trail_segment_id"] == 1


def test_sticky_foot_tracker_new_segment_on_uv_jump():
    from spatial_pose.calibration import compute_and_update_config
    from spatial_pose.floor_projection import StickyFootTracker, foot_uv_from_person
    from tests.test_spatial_calibration import _synthetic_calibration_config

    cal = compute_and_update_config(_synthetic_calibration_config(), infer_width=852, infer_height=480)
    sticky = StickyFootTracker.from_calibration(cal)
    person_a = {"person_id": 0, "keypoints": [[0, 0, 0.0] for _ in range(17)]}
    person_a["keypoints"][15] = [100.0, 400.0, 0.9]
    person_a["keypoints"][16] = [110.0, 400.0, 0.9]
    person_b = {"person_id": 1, "keypoints": [[0, 0, 0.0] for _ in range(17)]}
    person_b["keypoints"][15] = [700.0, 400.0, 0.9]
    person_b["keypoints"][16] = [710.0, 400.0, 0.9]

    _, seg0, _ = sticky.pick_person([person_a], frame_idx=1, score_min=0.35)
    _, seg1, br = sticky.pick_person([person_b], frame_idx=2, score_min=0.35)
    assert seg0 == 0
    assert seg1 == 1
    assert br is True
    assert foot_uv_from_person(person_b) is not None


def test_project_uv_outside_ground_map_returns_none():
    from spatial_pose.floor_projection import project_uv_to_floor

    cfg = _synthetic_calibration_config()
    cal = compute_and_update_config(cfg, infer_width=640, infer_height=480)
    # 投射到网格外的大坐标（取决于标定，用极端 UV 试探）
    out = project_uv_to_floor(cal, (-9999.0, -9999.0))
    assert out is None


def test_ground_map_bounds_from_visualization():
    cfg = _synthetic_calibration_config()
    cal = compute_and_update_config(cfg)
    x_min, x_max, y_min, y_max = cal.ground_map_bounds()
    assert x_min == 0.0
    assert y_min == 0.0
    assert x_max == 2.0
    assert y_max == 9.6


def test_low_score_ankle_skipped():
    cfg = _synthetic_calibration_config()
    cal = compute_and_update_config(cfg)
    person = {
        "person_id": 0,
        "keypoints": [[0, 0, 0.1] for _ in range(17)],
    }
    out = project_foot_for_frame(cal, [person], FloorSmoothState())
    assert out.foot_uv_px is None
