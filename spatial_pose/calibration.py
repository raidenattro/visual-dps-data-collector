"""地面标定：读/写 JSON、单应矩阵、分辨率缩放。"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from spatial_pose.schema import (
    EXPECTED_CONTROL_POINTS,
    empty_spatial_config,
    normalize_spatial_config,
    validate_calibration_ready,
)


def transform_points(points: np.ndarray, homography: np.ndarray) -> np.ndarray:
    if hasattr(cv2, "perspectiveTransform"):
        return cv2.perspectiveTransform(
            points.reshape(-1, 1, 2).astype(np.float64),
            homography,
        ).reshape(-1, 2)
    # numpy 回退
    pts = points.reshape(-1, 2).astype(np.float64)
    ones = np.ones((pts.shape[0], 1), dtype=np.float64)
    homog = np.hstack([pts, ones])
    mapped = (homography @ homog.T).T
    mapped[:, 0] /= mapped[:, 2]
    mapped[:, 1] /= mapped[:, 2]
    return mapped[:, :2]


def _find_homography_dlt(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """DLT 单应矩阵（src → dst）。"""
    n = src.shape[0]
    if n < 4:
        raise ValueError("至少需要 4 对点")
    a_list: list[list[float]] = []
    for i in range(n):
        x, y = float(src[i, 0]), float(src[i, 1])
        u, v = float(dst[i, 0]), float(dst[i, 1])
        a_list.append([-x, -y, -1, 0, 0, 0, u * x, u * y, u])
        a_list.append([0, 0, 0, -x, -y, -1, v * x, v * y, v])
    a_mat = np.array(a_list, dtype=np.float64)
    _, _, vt = np.linalg.svd(a_mat)
    h = vt[-1].reshape(3, 3)
    if abs(h[2, 2]) > 1e-12:
        h /= h[2, 2]
    return h


def _find_homography(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    if hasattr(cv2, "findHomography"):
        h, _ = cv2.findHomography(src.astype(np.float64), dst.astype(np.float64), method=0)
        if h is None:
            raise RuntimeError("findHomography 失败")
        return h
    return _find_homography_dlt(src, dst)


def build_world_points_from_physical(physical: dict[str, Any]) -> np.ndarray:
    """由通道宽、间距、行数生成 world 控制点（与 handoff 顺序一致：远→近，每行左/右）。"""
    width_m = float(physical.get("aisle_width_m") or 2.0)
    spacing_m = float(physical.get("marker_spacing_m") or 2.4)
    pairs = int(physical.get("marker_pairs") or 5)
    if pairs < 1:
        raise ValueError("marker_pairs 必须 >= 1")

    depth_m = spacing_m * (pairs - 1)
    rows: list[list[float]] = []
    for i in range(pairs):
        y = depth_m - i * spacing_m
        rows.append([0.0, y])
        rows.append([width_m, y])
    return np.array(rows, dtype=np.float64)


def scale_image_points(
    image_points: np.ndarray,
    calib_w: int,
    calib_h: int,
    target_w: int,
    target_h: int,
) -> np.ndarray:
    if calib_w <= 0 or calib_h <= 0:
        return image_points.copy()
    if calib_w == target_w and calib_h == target_h:
        return image_points.copy()
    sx = float(target_w) / float(calib_w)
    sy = float(target_h) / float(calib_h)
    scaled = image_points.copy()
    scaled[:, 0] *= sx
    scaled[:, 1] *= sy
    return scaled


def compute_homography(
    world_points: np.ndarray,
    image_points: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, float, np.ndarray]:
    """返回 H_world→image、H_image→world、RMSE(px)、逐点误差。"""
    if world_points.shape[0] != image_points.shape[0]:
        raise ValueError("world/image 控制点数量不一致")
    h_world_to_image = _find_homography(
        world_points.astype(np.float64),
        image_points.astype(np.float64),
    )
    h_image_to_world = np.linalg.inv(h_world_to_image)
    projected = transform_points(world_points, h_world_to_image)
    errors = np.linalg.norm(projected - image_points, axis=1)
    rmse = float(math.sqrt(float(np.mean(errors**2))))
    return h_world_to_image, h_image_to_world, rmse, errors


@dataclass
class SpatialCalibration:
    """运行时标定对象（含指定 infer 分辨率下的 H）。"""

    config: dict[str, Any]
    infer_width: int
    infer_height: int
    h_image_to_world: np.ndarray
    h_world_to_image: np.ndarray
    ground_control_rmse_px: float

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled"))

    @property
    def camera_slug(self) -> str:
        return str(self.config.get("camera_slug") or "")

    def runtime(self) -> dict[str, Any]:
        block = self.config.get("runtime")
        return block if isinstance(block, dict) else {}

    def visualization(self) -> dict[str, Any]:
        block = self.config.get("visualization")
        return block if isinstance(block, dict) else {}

    def floor_bounds(self) -> tuple[float, float, float, float]:
        bounds = self.runtime().get("floor_bounds_m") or {}
        return (
            float(bounds.get("x_min", -0.7)),
            float(bounds.get("x_max", 2.7)),
            float(bounds.get("y_min", -1.5)),
            float(bounds.get("y_max", 12.5)),
        )

    def manifest_summary(self) -> dict[str, Any]:
        rel = self.config.get("_calibration_rel") or f"spatial/{self.camera_slug}.json"
        return {
            "enabled": self.enabled,
            "calibration_file": rel,
            "ground_control_rmse_px": self.ground_control_rmse_px,
            "floor_xy_enabled": True,
            "infer_width": self.infer_width,
            "infer_height": self.infer_height,
        }


def calibration_path_for_slug(spatial_dir: Path, camera_slug: str) -> Path:
    slug = str(camera_slug or "").strip()
    if not slug:
        raise ValueError("camera_slug 不能为空")
    return Path(spatial_dir) / f"{slug}.json"


def _image_points_array(config: dict[str, Any]) -> np.ndarray:
    pts = config.get("calibration", {}).get("image_points_px") or []
    arr = np.array([[float(p[0]), float(p[1])] for p in pts], dtype=np.float64)
    if arr.shape[0] != EXPECTED_CONTROL_POINTS:
        raise ValueError(f"需要 {EXPECTED_CONTROL_POINTS} 个 image_points_px")
    return arr


def _world_points_array(config: dict[str, Any]) -> np.ndarray:
    calib = config.get("calibration") or {}
    custom = calib.get("world_points_m")
    if isinstance(custom, list) and len(custom) == EXPECTED_CONTROL_POINTS:
        return np.array([[float(p[0]), float(p[1])] for p in custom], dtype=np.float64)
    physical = config.get("physical") or {}
    return build_world_points_from_physical(physical)


def compute_and_update_config(
    config: dict[str, Any],
    *,
    infer_width: int | None = None,
    infer_height: int | None = None,
) -> SpatialCalibration:
    """根据 config 计算 homography 并写回 computed 字段。"""
    norm = normalize_spatial_config(config, camera_slug=str(config.get("camera_slug") or ""))
    validate_calibration_ready(norm)

    calib_res = norm["calibration"]["resolution"]
    calib_w, calib_h = int(calib_res[0]), int(calib_res[1])
    target_w = int(infer_width or calib_w)
    target_h = int(infer_height or calib_h)

    image_points = _image_points_array(norm)
    image_points = scale_image_points(image_points, calib_w, calib_h, target_w, target_h)
    world_points = _world_points_array(norm)

    tuning = norm.get("tuning") or {}
    override = tuning.get("homography_override")
    if isinstance(override, list) and len(override) == 3:
        h_image_to_world = np.array(override, dtype=np.float64)
        h_world_to_image = np.linalg.inv(h_image_to_world)
        projected = transform_points(world_points, h_world_to_image)
        errors = np.linalg.norm(projected - image_points, axis=1)
        rmse = float(math.sqrt(float(np.mean(errors**2))))
    else:
        h_world_to_image, h_image_to_world, rmse, errors = compute_homography(
            world_points, image_points
        )

    norm["computed"] = {
        "image_to_ground_homography": h_image_to_world.tolist(),
        "ground_to_image_homography": h_world_to_image.tolist(),
        "ground_control_rmse_px": rmse,
        "ground_control_errors_px": errors.tolist(),
        "infer_width": target_w,
        "infer_height": target_h,
    }
    norm["enabled"] = True

    cal = SpatialCalibration(
        config=norm,
        infer_width=target_w,
        infer_height=target_h,
        h_image_to_world=h_image_to_world,
        h_world_to_image=h_world_to_image,
        ground_control_rmse_px=rmse,
    )
    return cal


def save_calibration(path: Path, config: dict[str, Any]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    norm = normalize_spatial_config(config, camera_slug=str(config.get("camera_slug") or ""))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(norm, f, ensure_ascii=False, indent=2)
    return path.resolve()


def load_calibration(
    spatial_dir: Path,
    camera_slug: str,
    *,
    infer_width: int | None = None,
    infer_height: int | None = None,
    require_enabled: bool = False,
) -> SpatialCalibration | None:
    slug = str(camera_slug or "").strip()
    if not slug:
        return None
    path = calibration_path_for_slug(spatial_dir, slug)
    if not path.is_file():
        return None
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        return None
    norm = normalize_spatial_config(raw, camera_slug=slug)
    norm["_calibration_rel"] = f"spatial/{slug}.json"
    if require_enabled and not norm.get("enabled"):
        return None
    try:
        validate_calibration_ready(norm)
    except ValueError:
        return None
    return compute_and_update_config(
        norm,
        infer_width=infer_width,
        infer_height=infer_height,
    )


def load_calibration_json(spatial_dir: Path, camera_slug: str) -> dict[str, Any] | None:
    slug = str(camera_slug or "").strip()
    if not slug:
        return None
    path = calibration_path_for_slug(spatial_dir, slug)
    if not path.is_file():
        return None
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        return None
    return normalize_spatial_config(raw, camera_slug=slug)
