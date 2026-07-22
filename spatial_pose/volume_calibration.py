"""立体作业空间：8 角点、侧面映射、层线、线框预览。"""

from __future__ import annotations

import math
from typing import Any, Literal, TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from spatial_pose.calibration import SpatialCalibration
from spatial_pose.schema import EXPECTED_VOLUME_CORNERS, VOLUME_CORNER_LABELS

FaceSide = Literal["left", "right"]


def volume_physical(config: dict[str, Any]) -> dict[str, float]:
    vol = config.get("volume") if isinstance(config.get("volume"), dict) else {}
    return {
        "width_m": float(vol.get("width_m") or 2.0),
        "depth_m": float(vol.get("depth_m") or 9.6),
        "height_m": float(vol.get("height_m") or 2.4),
    }


def assign_volume_world_corners(config: dict[str, Any]) -> np.ndarray:
    """8 角点世界坐标 (x,y,z)。顺序见 VOLUME_CORNER_LABELS。"""
    p = volume_physical(config)
    w, d, h = p["width_m"], p["depth_m"], p["height_m"]
    return np.array(
        [
            [0.0, 0.0, 0.0],
            [w, 0.0, 0.0],
            [w, d, 0.0],
            [0.0, d, 0.0],
            [0.0, 0.0, h],
            [w, 0.0, h],
            [w, d, h],
            [0.0, d, h],
        ],
        dtype=np.float64,
    )


def _corners_image_array(config: dict[str, Any]) -> np.ndarray | None:
    vol = config.get("volume") or {}
    pts = vol.get("corners_image_px") or []
    if len(pts) != EXPECTED_VOLUME_CORNERS:
        return None
    return np.array([[float(p[0]), float(p[1])] for p in pts], dtype=np.float64)


def _face_image_quad(corners: np.ndarray, side: FaceSide) -> np.ndarray:
    """侧面四边形图像坐标（顺序：近下、远下、远上、近上）。"""
    if side == "left":
        # x=0: BL, FL, FL_top, TL
        return np.array([corners[0], corners[3], corners[7], corners[4]], dtype=np.float64)
    # x=W: BR, FR, FR_top, TR
    return np.array([corners[1], corners[2], corners[6], corners[5]], dtype=np.float64)


def _face_world_yz_quad(depth_m: float, height_m: float) -> np.ndarray:
    """侧面 (y,z) 平面坐标。"""
    d, h = float(depth_m), float(height_m)
    return np.array([[0.0, 0.0], [d, 0.0], [d, h], [0.0, h]], dtype=np.float64)


def compute_face_homography(
    corners: np.ndarray,
    side: FaceSide,
    *,
    depth_m: float,
    height_m: float,
) -> tuple[np.ndarray, np.ndarray, float]:
    """返回 H_image→yz、H_yz→image、RMSE。"""
    from spatial_pose.calibration import compute_homography

    src = _face_image_quad(corners, side)
    dst = _face_world_yz_quad(depth_m, height_m)
    h_yz_to_image, h_image_to_yz, rmse, _ = compute_homography(dst, src)
    return h_image_to_yz, h_yz_to_image, rmse


def default_layer_z_m(height_m: float, layer_count: int) -> list[float]:
    count = max(1, int(layer_count))
    h = max(0.01, float(height_m))
    return [round(i * h / count, 6) for i in range(count + 1)]


def normalize_layer_z_m(
    layer_z_m: list[float] | None,
    *,
    height_m: float,
    layer_count: int | None = None,
    allow_equal_split: bool = True,
) -> list[float]:
    h = max(0.01, float(height_m))
    if layer_z_m and len(layer_z_m) >= 2:
        out = [float(z) for z in layer_z_m]
        out[0] = 0.0
        out[-1] = h
        return out
    if not allow_equal_split:
        return [0.0, h]
    count = max(1, int(layer_count or 4))
    return default_layer_z_m(h, count)


def refine_layer_z_from_image_lines(
    corners: np.ndarray,
    side: FaceSide,
    image_lines: list[list[list[float]]],
    *,
    depth_m: float,
    height_m: float,
) -> list[float]:
    """手标层线中点 → layer_z_m 边界（含 0 与 H）。"""
    h = max(0.01, float(height_m))
    zs: list[float] = [0.0, h]
    h_i2yz, _, _ = compute_face_homography(corners, side, depth_m=depth_m, height_m=h)
    for seg in image_lines:
        if not seg or len(seg) < 2:
            continue
        mid_u = (float(seg[0][0]) + float(seg[1][0])) / 2.0
        mid_v = (float(seg[0][1]) + float(seg[1][1])) / 2.0
        yz = image_point_to_face_yz((mid_u, mid_v), h_i2yz, depth_m=depth_m, height_m=h)
        if yz is None:
            continue
        z = float(yz[1])
        if 0.0 < z < h:
            zs.append(z)
    zs = sorted(set(round(v, 4) for v in zs))
    if len(zs) < 2:
        return [0.0, h]
    zs[0] = 0.0
    zs[-1] = h
    return zs


def layer_lines_preview_from_drawn(
    image_lines: list[list[list[float]]],
    *,
    layer_z_m: list[float] | None = None,
) -> list[dict[str, Any]]:
    """手标层线 → 预览（不自动生成均分层线）。"""
    inner_z: list[float] = []
    if layer_z_m and len(layer_z_m) > 2:
        inner_z = [float(z) for z in layer_z_m[1:-1]]
    out: list[dict[str, Any]] = []
    for i, seg in enumerate(image_lines):
        if not seg or len(seg) < 2:
            continue
        item: dict[str, Any] = {
            "image": [
                [float(seg[0][0]), float(seg[0][1])],
                [float(seg[1][0]), float(seg[1][1])],
            ]
        }
        if i < len(inner_z):
            item["z_m"] = inner_z[i]
        out.append(item)
    return out


def image_point_to_face_yz(
    uv: tuple[float, float],
    h_image_to_yz: np.ndarray,
    *,
    depth_m: float,
    height_m: float,
) -> tuple[float, float] | None:
    from spatial_pose.calibration import transform_points

    pt = np.array([[uv[0], uv[1]]], dtype=np.float64)
    yz = transform_points(pt, h_image_to_yz)[0]
    y, z = float(yz[0]), float(yz[1])
    if y < -0.05 or y > depth_m + 0.05 or z < -0.05 or z > height_m + 0.05:
        return None
    return y, z


def yz_to_layer_index(z_m: float, layer_z_m: list[float]) -> int | None:
    if not layer_z_m or len(layer_z_m) < 2:
        return None
    z = float(z_m)
    for i in range(len(layer_z_m) - 1):
        lo = float(layer_z_m[i])
        hi = float(layer_z_m[i + 1])
        if i == len(layer_z_m) - 2:
            if lo <= z <= hi + 1e-6:
                return i + 1
        elif lo <= z < hi:
            return i + 1
    return None


def _point_line_distance(px: float, py: float, x0: float, y0: float, x1: float, y1: float) -> float:
    dx = x1 - x0
    dy = y1 - y0
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return math.hypot(px - x0, py - y0)
    t = max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / (dx * dx + dy * dy)))
    proj_x = x0 + t * dx
    proj_y = y0 + t * dy
    return math.hypot(px - proj_x, py - proj_y)


def shelf_face_enabled(config: dict[str, Any], side: FaceSide) -> bool:
    """侧面货架是否启用（机位可能只出现左或右一侧）。"""
    shelf = config.get("shelf_faces") if isinstance(config.get("shelf_faces"), dict) else {}
    face = shelf.get(side) if isinstance(shelf.get(side), dict) else {}
    return bool(face.get("enabled", True))


def pick_face_for_uv(
    uv: tuple[float, float],
    corners: np.ndarray,
    *,
    enabled_sides: tuple[FaceSide, ...] | None = None,
) -> FaceSide | None:
    """根据到左右侧面四边形中心距离选择更近的侧面；可限定仅启用的侧面。"""
    sides: tuple[FaceSide, ...] = enabled_sides or ("left", "right")
    if not sides:
        return None
    if len(sides) == 1:
        return sides[0]
    left = _face_image_quad(corners, "left")
    right = _face_image_quad(corners, "right")
    lc = left.mean(axis=0)
    rc = right.mean(axis=0)
    u, v = float(uv[0]), float(uv[1])
    dl = math.hypot(u - lc[0], v - lc[1])
    dr = math.hypot(u - rc[0], v - rc[1])
    if "left" not in sides:
        return "right"
    if "right" not in sides:
        return "left"
    return "left" if dl <= dr else "right"


def bottom_world_xy(config: dict[str, Any]) -> np.ndarray:
    """立体底面四角世界坐标 (x,y)，顺序 BL→BR→FR→FL。"""
    p = volume_physical(config)
    w, d = p["width_m"], p["depth_m"]
    return np.array([[0.0, 0.0], [w, 0.0], [w, d], [0.0, d]], dtype=np.float64)


def scaled_volume_corners_image(
    config: dict[str, Any],
    *,
    infer_width: int | None = None,
    infer_height: int | None = None,
) -> np.ndarray:
    """8 角点图像坐标，按标定分辨率缩放到 infer 尺寸。"""
    corners = _corners_image_array(config)
    if corners is None:
        raise ValueError(f"volume 需要 {EXPECTED_VOLUME_CORNERS} 个角点")
    calib_res = config.get("calibration", {}).get("resolution") or [0, 0]
    calib_w, calib_h = int(calib_res[0]), int(calib_res[1])
    target_w = int(infer_width or calib_w or 852)
    target_h = int(infer_height or calib_h or 480)
    if calib_w > 0 and calib_h > 0 and (calib_w != target_w or calib_h != target_h):
        from spatial_pose.calibration import scale_image_points

        return scale_image_points(corners, calib_w, calib_h, target_w, target_h)
    return corners.copy()


def compute_bottom_floor_homography(
    config: dict[str, Any],
    *,
    infer_width: int | None = None,
    infer_height: int | None = None,
) -> tuple[np.ndarray, np.ndarray, float, np.ndarray]:
    """立体底面 4 角点 → floor_xy 单应矩阵（world→image / image→world）。"""
    from spatial_pose.calibration import compute_homography

    corners = scaled_volume_corners_image(
        config,
        infer_width=infer_width,
        infer_height=infer_height,
    )
    bottom_world = bottom_world_xy(config)
    bottom_img = corners[:4]
    return compute_homography(bottom_world, bottom_img)


def validate_volume_corners_image(config: dict[str, Any]) -> float:
    """底面/顶面重投影 RMSE（像素）。"""
    from spatial_pose.calibration import compute_homography

    corners = _corners_image_array(config)
    if corners is None:
        return 0.0
    p = volume_physical(config)
    w, d, h = p["width_m"], p["depth_m"], p["height_m"]
    bottom_world = np.array([[0, 0], [w, 0], [w, d], [0, d]], dtype=np.float64)
    top_world = bottom_world.copy()
    bottom_img = corners[:4]
    top_img = corners[4:8]
    _, _, rmse_b, _ = compute_homography(bottom_world, bottom_img)
    _, _, rmse_t, _ = compute_homography(top_world, top_img)
    return float((rmse_b + rmse_t) / 2.0)


def volume_wireframe_segments(corners: np.ndarray) -> list[dict[str, Any]]:
    """12 棱线段。"""
    edges = [
        (0, 1),
        (1, 2),
        (2, 3),
        (3, 0),
        (4, 5),
        (5, 6),
        (6, 7),
        (7, 4),
        (0, 4),
        (1, 5),
        (2, 6),
        (3, 7),
    ]
    segs: list[dict[str, Any]] = []
    for i, j in edges:
        segs.append({"image": [[float(corners[i][0]), float(corners[i][1])], [float(corners[j][0]), float(corners[j][1])]]})
    return segs


def layer_lines_from_z(
    corners: np.ndarray,
    side: FaceSide,
    layer_z_m: list[float],
    *,
    depth_m: float,
    height_m: float,
) -> list[dict[str, Any]]:
    """由 layer_z_m 生成侧面水平层线（图像）。"""
    from spatial_pose.calibration import transform_points

    h_yz_to_image, _, _ = compute_face_homography(corners, side, depth_m=depth_m, height_m=height_m)
    lines: list[dict[str, Any]] = []
    for z in layer_z_m:
        p0 = transform_points(np.array([[0.0, float(z)]], dtype=np.float64), h_yz_to_image)[0]
        p1 = transform_points(np.array([[depth_m, float(z)]], dtype=np.float64), h_yz_to_image)[0]
        lines.append({"z_m": float(z), "image": [[float(p0[0]), float(p0[1])], [float(p1[0]), float(p1[1])]]})
    return lines


def compute_volume_computed_fields(config: dict[str, Any], cal: "SpatialCalibration | None" = None) -> dict[str, Any]:
    """计算 volume RMSE、侧面 homography 等，写回 computed 子集。"""
    vol = config.get("volume") if isinstance(config.get("volume"), dict) else {}
    if not vol.get("enabled"):
        return {}
    corners = _corners_image_array(config)
    if corners is None:
        return {}
    p = volume_physical(config)
    rmse = validate_volume_corners_image(config)
    face_h: dict[str, Any] = {}
    face_rmse: dict[str, float] = {}
    for side in ("left", "right"):
        if not shelf_face_enabled(config, side):
            continue
        try:
            h_i2yz, h_yz2i, fr = compute_face_homography(
                corners, side, depth_m=p["depth_m"], height_m=p["height_m"]
            )
        except (RuntimeError, ValueError):
            # 侧面在图像中退化为直线时跳过 homography
            continue
        face_h[side] = {
            "image_to_face_yz": h_i2yz.tolist(),
            "face_yz_to_image": h_yz2i.tolist(),
        }
        face_rmse[side] = fr

    out: dict[str, Any] = {
        "volume_rmse_px": rmse,
        "face_homographies": face_h,
        "face_rmse_px": face_rmse,
        "volume_corner_labels": list(VOLUME_CORNER_LABELS),
    }

    gc = config.get("ground_columns") if isinstance(config.get("ground_columns"), dict) else {}
    boundaries = gc.get("boundaries_x_m") or []
    drawn_col = gc.get("boundaries_image_px") or []
    col_segs: list[list[list[float]]] = []
    if isinstance(drawn_col, list) and drawn_col:
        for item in drawn_col:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                col_segs.append([[float(item[0][0]), float(item[0][1])], [float(item[1][0]), float(item[1][1])]])
    if col_segs:
        if cal is not None:
            from spatial_pose.column_grid import refine_boundaries_from_image_lines

            boundaries_x_m, boundaries_y_m = refine_boundaries_from_image_lines(
                cal,
                col_segs,
                width_m=p["width_m"],
                depth_m=p["depth_m"],
                corners_image_px=vol.get("corners_image_px"),
            )
            gc["boundaries_x_m"] = boundaries_x_m
            gc["boundaries_y_m"] = boundaries_y_m
            gc["column_count"] = max(1, len(boundaries_y_m) - 1)
            gc["column_axis"] = "y"
        else:
            gc["boundaries_x_m"] = [0.0, p["width_m"]]
            gc["boundaries_y_m"] = [0.0, p["depth_m"]]
            gc["column_count"] = 1
        from spatial_pose.column_grid import column_lines_image_from_drawn

        out["column_lines_image"] = column_lines_image_from_drawn(
            col_segs,
            boundaries_x_m=gc.get("boundaries_x_m"),
            boundaries_y_m=gc.get("boundaries_y_m"),
        )
    else:
        gc["boundaries_x_m"] = [0.0, p["width_m"]]
        gc["boundaries_y_m"] = [0.0, p["depth_m"]]
        gc["column_count"] = 1
        out["column_lines_image"] = []

    out["volume_wireframe_segments"] = volume_wireframe_segments(corners)

    shelf = config.get("shelf_faces") if isinstance(config.get("shelf_faces"), dict) else {}
    layer_preview: dict[str, Any] = {}
    for side in ("left", "right"):
        face_cfg = shelf.get(side) if isinstance(shelf.get(side), dict) else {}
        if not shelf_face_enabled(config, side):
            layer_preview[side] = []
            continue
        drawn = face_cfg.get("layer_lines_image_px") or []
        segs: list[list[list[float]]] = []
        if isinstance(drawn, list) and drawn:
            for item in drawn:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    segs.append([[float(item[0][0]), float(item[0][1])], [float(item[1][0]), float(item[1][1])]])
        if segs:
            lz = refine_layer_z_from_image_lines(
                corners, side, segs, depth_m=p["depth_m"], height_m=p["height_m"]
            )
            face_cfg["layer_z_m"] = lz
            face_cfg["layer_count"] = max(1, len(lz) - 1)
            layer_preview[side] = layer_lines_preview_from_drawn(segs, layer_z_m=lz)
        else:
            face_cfg["layer_z_m"] = [0.0, p["height_m"]]
            face_cfg["layer_count"] = 1
            layer_preview[side] = []
    out["layer_lines_image"] = layer_preview
    return out


def volume_runtime(config: dict[str, Any]) -> dict[str, Any]:
    """供投射使用的运行时结构。"""
    vol = config.get("volume") if isinstance(config.get("volume"), dict) else {}
    if not vol.get("enabled"):
        return {}
    corners = _corners_image_array(config)
    if corners is None:
        return {}
    computed = config.get("computed") if isinstance(config.get("computed"), dict) else {}
    face_h = computed.get("face_homographies") if isinstance(computed.get("face_homographies"), dict) else {}
    p = volume_physical(config)
    shelf = config.get("shelf_faces") if isinstance(config.get("shelf_faces"), dict) else {}
    faces: dict[str, Any] = {}
    for side in ("left", "right"):
        face_cfg = shelf.get(side) if isinstance(shelf.get(side), dict) else {}
        enabled = shelf_face_enabled(config, side)
        block = face_h.get(side) if isinstance(face_h.get(side), dict) else {}
        mat = block.get("image_to_face_yz")
        if not mat:
            h_i2yz, _, _ = compute_face_homography(
                corners, side, depth_m=p["depth_m"], height_m=p["height_m"]
            )
        else:
            h_i2yz = np.array(mat, dtype=np.float64)
        lc = max(1, int(face_cfg.get("layer_count") or 1))
        lz = normalize_layer_z_m(
            face_cfg.get("layer_z_m"),
            height_m=p["height_m"],
            layer_count=lc,
            allow_equal_split=False,
        )
        faces[side] = {
            "enabled": enabled,
            "h_image_to_yz": h_i2yz,
            "layer_z_m": lz,
            "layer_count": lc,
        }
    from spatial_pose.column_grid import ground_columns_block

    return {
        "corners_image_px": vol.get("corners_image_px"),
        "physical": p,
        "faces": faces,
        "ground_columns": ground_columns_block(config),
    }
