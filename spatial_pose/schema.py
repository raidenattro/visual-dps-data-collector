"""spatial 标定 JSON 结构与默认值。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

SPATIAL_SCHEMA = 2
SPATIAL_SCHEMA_V1 = 1
EXPECTED_CONTROL_POINTS = 10
EXPECTED_VOLUME_CORNERS = 8

VOLUME_CORNER_LABELS = [
    "BL",
    "BR",
    "FR",
    "FL",
    "TL",
    "TR",
    "FR_top",
    "FL_top",
]

DEFAULT_PHYSICAL = {
    "aisle_width_m": 2.0,
    "marker_spacing_m": 2.4,
    "marker_pairs": 5,
}

DEFAULT_VOLUME = {
    "enabled": False,
    "width_m": 2.0,
    "depth_m": 9.6,
    "height_m": 2.4,
    "corners_image_px": [],
    "corner_labels": list(VOLUME_CORNER_LABELS),
}

DEFAULT_GROUND_COLUMNS = {
    "column_count": 1,
    "boundaries_x_m": [],
    "boundaries_y_m": [],
    "boundaries_image_px": [],
    "column_axis": "y",
}

DEFAULT_SHELF_FACE = {
    "enabled": True,
    "layer_count": 1,
    "layer_lines_image_px": [],
    "layer_z_m": [],
}

DEFAULT_SHELF_FACES = {
    "left": deepcopy(DEFAULT_SHELF_FACE),
    "right": deepcopy(DEFAULT_SHELF_FACE),
}

DEFAULT_RUNTIME = {
    "floor_bounds_m": {
        "x_min": -0.7,
        "x_max": 2.7,
        "y_min": -1.5,
        "y_max": 12.5,
    },
    "foot_score_min": 0.35,
    "wrist_score_min": 0.35,
    "smooth_alpha_normal": 0.2,
    "smooth_alpha_jump": 0.08,
    "smooth_jump_threshold_m": 1.4,
    "trail_tail_frames": 90,
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
        "volume": deepcopy(DEFAULT_VOLUME),
        "ground_columns": deepcopy(DEFAULT_GROUND_COLUMNS),
        "shelf_faces": deepcopy(DEFAULT_SHELF_FACES),
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


def _as_float_list(raw: Any, size: int | None = None) -> list[list[float]]:
    if not isinstance(raw, (list, tuple)):
        return []
    pairs: list[list[float]] = []
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            pairs.append([float(item[0]), float(item[1])])
    if size is not None and len(pairs) != size:
        return pairs
    return pairs


def _as_line_segments(raw: Any) -> list[list[list[float]]]:
    """[[[u0,v0],[u1,v1]], ...]"""
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[list[list[float]]] = []
    for seg in raw:
        if not isinstance(seg, (list, tuple)) or len(seg) < 2:
            continue
        p0, p1 = seg[0], seg[1]
        if isinstance(p0, (list, tuple)) and isinstance(p1, (list, tuple)) and len(p0) >= 2 and len(p1) >= 2:
            out.append([[float(p0[0]), float(p0[1])], [float(p1[0]), float(p1[1])]])
    return out


def _normalize_shelf_face(raw: Any, *, default_layer_count: int = 4) -> dict[str, Any]:
    base = deepcopy(DEFAULT_SHELF_FACE)
    if not isinstance(raw, dict):
        base["layer_count"] = max(1, default_layer_count)
        return base
    base["enabled"] = bool(raw.get("enabled", base.get("enabled", True)))
    base["layer_count"] = max(1, int(raw.get("layer_count") or default_layer_count))
    lines = _as_line_segments(raw.get("layer_lines_image_px"))
    if lines:
        base["layer_lines_image_px"] = lines
    z_raw = raw.get("layer_z_m")
    if isinstance(z_raw, (list, tuple)) and z_raw:
        base["layer_z_m"] = [float(z) for z in z_raw]
    return base


def normalize_spatial_config(raw: dict[str, Any], *, camera_slug: str = "") -> dict[str, Any]:
    """合并默认值并规范化字段。"""
    base = empty_spatial_config(camera_slug or raw.get("camera_slug") or "")
    if not isinstance(raw, dict):
        return base

    for key in ("physical", "calibration", "computed", "tuning", "runtime", "visualization"):
        block = raw.get(key)
        if isinstance(block, dict):
            base[key].update(block)

    vol = raw.get("volume")
    if isinstance(vol, dict):
        base["volume"].update(vol)
        corners = _as_float_list(vol.get("corners_image_px"))
        if corners:
            base["volume"]["corners_image_px"] = corners

    gc = raw.get("ground_columns")
    if isinstance(gc, dict):
        base["ground_columns"].update(gc)
        base["ground_columns"]["column_count"] = max(1, int(gc.get("column_count") or base["ground_columns"]["column_count"]))
        bxm = gc.get("boundaries_x_m")
        if isinstance(bxm, (list, tuple)) and bxm:
            base["ground_columns"]["boundaries_x_m"] = [float(x) for x in bxm]
        bym = gc.get("boundaries_y_m")
        if isinstance(bym, (list, tuple)) and bym:
            base["ground_columns"]["boundaries_y_m"] = [float(y) for y in bym]
        axis = gc.get("column_axis")
        if axis in ("x", "y"):
            base["ground_columns"]["column_axis"] = axis
        bip = gc.get("boundaries_image_px")
        if isinstance(bip, list) and bip:
            if isinstance(bip[0], (list, tuple)) and len(bip[0]) >= 2 and isinstance(bip[0][0], (list, tuple)):
                base["ground_columns"]["boundaries_image_px"] = _as_line_segments(bip)
            else:
                base["ground_columns"]["boundaries_image_px"] = _as_float_list(bip)

    sf = raw.get("shelf_faces")
    if isinstance(sf, dict):
        base["shelf_faces"]["left"] = _normalize_shelf_face(sf.get("left"))
        base["shelf_faces"]["right"] = _normalize_shelf_face(sf.get("right"))

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


def has_volume_floor_ready(config: dict[str, Any]) -> bool:
    """立体底面可用于 floor_xy 投射。"""
    vol = config.get("volume") if isinstance(config.get("volume"), dict) else {}
    if not vol.get("enabled"):
        return False
    corners = vol.get("corners_image_px") or []
    if len(corners) != EXPECTED_VOLUME_CORNERS:
        return False
    try:
        for key in ("width_m", "depth_m", "height_m"):
            if float(vol.get(key) or 0) <= 0:
                return False
    except (TypeError, ValueError):
        return False
    return True


def validate_calibration_ready(config: dict[str, Any]) -> None:
    """旧版 10 地面点 homography 校验（兼容遗留 JSON）。"""
    pts = config.get("calibration", {}).get("image_points_px") or []
    if len(pts) != EXPECTED_CONTROL_POINTS:
        raise ValueError(f"需要 {EXPECTED_CONTROL_POINTS} 个地面控制点，当前 {len(pts)} 个")
    res = config.get("calibration", {}).get("resolution") or [0, 0]
    if not res or int(res[0]) <= 0 or int(res[1]) <= 0:
        raise ValueError("calibration.resolution 无效")


def validate_floor_calibration_ready(config: dict[str, Any]) -> None:
    """floor_xy 可用：立体底面（主流程）或旧版 10 地面点。"""
    if has_volume_floor_ready(config):
        validate_volume_ready(config)
        return
    validate_calibration_ready(config)


def validate_volume_ready(config: dict[str, Any]) -> None:
    """立体作业空间 8 角点校验。"""
    vol = config.get("volume") or {}
    if not vol.get("enabled"):
        return
    corners = vol.get("corners_image_px") or []
    if len(corners) != EXPECTED_VOLUME_CORNERS:
        raise ValueError(f"volume 需要 {EXPECTED_VOLUME_CORNERS} 个角点，当前 {len(corners)} 个")
    for key in ("width_m", "depth_m", "height_m"):
        if float(vol.get(key) or 0) <= 0:
            raise ValueError(f"volume.{key} 无效")
