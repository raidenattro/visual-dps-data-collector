"""floor_xy 投射单元测试。"""

from __future__ import annotations

from spatial_pose.calibration import compute_and_update_config
from spatial_pose.floor_projection import FloorSmoothState, project_foot_for_frame
from tests.test_spatial_calibration import _synthetic_calibration_config


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


def test_low_score_ankle_skipped():
    cfg = _synthetic_calibration_config()
    cal = compute_and_update_config(cfg)
    person = {
        "person_id": 0,
        "keypoints": [[0, 0, 0.1] for _ in range(17)],
    }
    out = project_foot_for_frame(cal, [person], FloorSmoothState())
    assert out.foot_uv_px is None
