"""地面单应标定与脚部 floor_xy 投射（handoff 脚部足迹整合）。"""

from spatial_pose.calibration import (
    SpatialCalibration,
    calibration_path_for_slug,
    compute_homography,
    load_calibration,
    save_calibration,
)
from spatial_pose.floor_projection import FloorSmoothState, project_foot_for_frame

__all__ = [
    "SpatialCalibration",
    "calibration_path_for_slug",
    "compute_homography",
    "load_calibration",
    "save_calibration",
    "FloorSmoothState",
    "project_foot_for_frame",
]
