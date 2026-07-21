"""spatial 标定 JSON 结构与默认值。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

SPATIAL_SCHEMA = 1
EXPECTED_CONTROL_POINTS = 10

DEFAULT_PHYSICAL = {
    "aisle_width_m": 2.0,
    "marker_spacing_m": 2.4,
    "marker_pairs": 5,
}

DEFAULT_RUNTIME = {
    "floor_bounds_m": {
        "x_min": -0.7,
        "x_max": 2.7,
        "y_min": -1.5,
        "y_max": 12.5,
    },
    "foot_score_min": 0.35,
    "smooth_alpha_normal": 0.2,
    "smooth_alpha_jump": 0.08,
    "smooth_jump_threshold_m": 1.4,
}

DEFAULT_VISUALIZATION = {
    "grid_width_m": 2.0,
    "grid_depth_m": 9.6,
    "grid_spacing_m": 2.4,
}


def empty_spatial_config(camera_slug: str = "") -> dict[str, Any]:
    """空标定模板。"""
    return {
        "schema": SPATIAL_SCHEMA,
        "camera_slug": str(camera_slug or "").strip(),
        "enabled": False,
        "physical": deepcopy(DEFAULT_PHYSICAL),
        "calibration": {
            "resolution": [0, 0],
            "image_points_px": [],
        },
        "computed": {},
        "tuning": {
            "homography_override": None,
            "scale_xy": [1.0, 1.0],
            "offset_xy_m": [0.0, 0.0],
            "notes": "",
        },
        "runtime": deepcopy(DEFAULT_RUNTIME),
        "visualization": deepcopy(DEFAULT_VISUALIZATION),
    }


def _as_float_list(raw: Any, size: int | None = None) -> list[float]:
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[float] = []
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            out.append(float(item[0]))
            out.append(float(item[1]))
        elif len(out) % 2 == 0 and isinstance(item, (int, float)):
            out.append(float(item))
    pairs = [[out[i], out[i + 1]] for i in range(0, len(out) - 1, 2)]
    if size is not None and len(pairs) != size:
        return pairs
    return pairs


def normalize_spatial_config(raw: dict[str, Any], *, camera_slug: str = "") -> dict[str, Any]:
    """合并默认值并规范化字段。"""
    base = empty_spatial_config(camera_slug or raw.get("camera_slug") or "")
    if not isinstance(raw, dict):
        return base

    for key in ("physical", "calibration", "computed", "tuning", "runtime", "visualization"):
        block = raw.get(key)
        if isinstance(block, dict):
            base[key].update(block)

    base["schema"] = int(raw.get("schema") or SPATIAL_SCHEMA)
    base["camera_slug"] = str(raw.get("camera_slug") or camera_slug or "").strip()
    base["enabled"] = bool(raw.get("enabled", base.get("enabled")))

    pts = raw.get("calibration", {}).get("image_points_px")
    if isinstance(pts, list):
        base["calibration"]["image_points_px"] = _as_float_list(pts)

    res = raw.get("calibration", {}).get("resolution") or base["calibration"]["resolution"]
    if isinstance(res, (list, tuple)) and len(res) >= 2:
        base["calibration"]["resolution"] = [int(res[0]), int(res[1])]

    return base


def validate_calibration_ready(config: dict[str, Any]) -> None:
    """标定可用于投射前的校验。"""
    pts = config.get("calibration", {}).get("image_points_px") or []
    if len(pts) != EXPECTED_CONTROL_POINTS:
        raise ValueError(f"需要 {EXPECTED_CONTROL_POINTS} 个地面控制点，当前 {len(pts)} 个")
    res = config.get("calibration", {}).get("resolution") or [0, 0]
    if not res or int(res[0]) <= 0 or int(res[1]) <= 0:
        raise ValueError("calibration.resolution 无效")
