"""从标注 JSON 加载并缩放到推理分辨率（与 visual-dps event_engine/annotation_boxes 一致）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np


def flatten_annotation_boxes(config_data: dict) -> list[dict[str, Any]]:
    """解析 visual-dps 标注 JSON：shelves[] 或 legacy 顶层 boxes[]。"""
    if not isinstance(config_data, dict):
        return []

    raw_boxes: list[dict[str, Any]] = []
    shelves = config_data.get("shelves")
    if isinstance(shelves, list):
        for shelf in shelves:
            if not isinstance(shelf, dict):
                continue
            shelf_code = str(shelf.get("shelf_code", "") or "").strip()
            boxes = shelf.get("boxes", [])
            if not isinstance(boxes, list):
                continue
            for box in boxes:
                if not isinstance(box, dict):
                    continue
                item = dict(box)
                if shelf_code and not item.get("shelf_code"):
                    item["shelf_code"] = shelf_code
                raw_boxes.append(item)

    if raw_boxes:
        return raw_boxes

    boxes = config_data.get("boxes", [])
    return boxes if isinstance(boxes, list) else []


def _scale_polygon_points(points, sx: float, sy: float) -> list[list[float]]:
    out: list[list[float]] = []
    for pt in points:
        if isinstance(pt, (list, tuple)) and len(pt) >= 2:
            out.append([float(pt[0]) * sx, float(pt[1]) * sy])
    return out


def _polygon_max_extent(points) -> tuple[float, float]:
    max_x = 0.0
    max_y = 0.0
    for pt in points:
        if isinstance(pt, (list, tuple)) and len(pt) >= 2:
            max_x = max(max_x, float(pt[0]))
            max_y = max(max_y, float(pt[1]))
    return max_x, max_y


def _norm_polygon_valid(norm_pts) -> bool:
    if not isinstance(norm_pts, list) or len(norm_pts) < 3:
        return False
    for pt in norm_pts:
        if not isinstance(pt, (list, tuple)) or len(pt) < 2:
            continue
        x = float(pt[0])
        y = float(pt[1])
        if x < -0.01 or x > 1.01 or y < -0.01 or y > 1.01:
            return False
    return True


def _scale_polygon_to_frame(
    pts,
    norm_pts,
    ann_w: float | None,
    ann_h: float | None,
    target_w: int,
    target_h: int,
) -> list[list[float]]:
    tw = float(target_w)
    th = float(target_h)
    if _norm_polygon_valid(norm_pts):
        return _scale_polygon_points(norm_pts, tw, th)

    if not isinstance(pts, list) or len(pts) < 3:
        return []

    max_x, max_y = _polygon_max_extent(pts)
    if ann_w and ann_h and ann_w > 0 and ann_h > 0:
        sx = tw / float(ann_w) if max_x <= float(ann_w) * 1.05 else tw / max_x
        sy = th / float(ann_h) if max_y <= float(ann_h) * 1.05 else th / max_y
    elif max_x > 0 and max_y > 0:
        sx = tw / max_x
        sy = th / max_y
    else:
        sx = sy = 1.0
    return _scale_polygon_points(pts, sx, sy)


def build_scaled_boxes(
    raw_boxes: list[dict[str, Any]],
    ann_w: float | None,
    ann_h: float | None,
    target_w: int,
    target_h: int,
) -> list[dict[str, Any]]:
    scaled: list[dict[str, Any]] = []
    for box in raw_boxes:
        pts = box.get("video_polygon", [])
        norm_pts = box.get("video_polygon_norm", [])
        mapped_pts = _scale_polygon_to_frame(pts, norm_pts, ann_w, ann_h, target_w, target_h)
        if len(mapped_pts) < 3:
            continue
        new_box = dict(box)
        new_box["video_polygon"] = mapped_pts
        new_box["orig_contour"] = np.int32(mapped_pts).reshape((-1, 1, 2))
        scaled.append(new_box)
    return scaled


def load_annotation_config(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"标注 JSON 不存在: {path}")
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("标注 JSON 根节点必须是 object")
    return data


def _annotation_size_from_config(config_data: dict[str, Any]) -> tuple[float | None, float | None]:
    annotation_size = config_data.get("annotation_size", {})
    if not isinstance(annotation_size, dict):
        annotation_size = {}
    ann_w = annotation_size.get("width")
    ann_h = annotation_size.get("height")
    try:
        ann_w = float(ann_w) if ann_w is not None else None
        ann_h = float(ann_h) if ann_h is not None else None
    except (TypeError, ValueError):
        ann_w, ann_h = None, None
    return ann_w, ann_h


def load_scaled_boxes_from_config(
    config_data: dict[str, Any],
    infer_w: int,
    infer_h: int,
) -> list[dict[str, Any]]:
    """从标注 dict 加载并缩放到推理分辨率（与 load_scaled_boxes 一致）。"""
    if not isinstance(config_data, dict):
        return []
    raw_boxes = flatten_annotation_boxes(config_data)
    ann_w, ann_h = _annotation_size_from_config(config_data)
    return build_scaled_boxes(raw_boxes, ann_w, ann_h, infer_w, infer_h)


def load_scaled_boxes(json_path: str | Path, infer_w: int, infer_h: int) -> list[dict[str, Any]]:
    config_data = load_annotation_config(json_path)
    return load_scaled_boxes_from_config(config_data, infer_w, infer_h)


def boxes_for_json_export(scaled_boxes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """导出到 pose JSON 的可序列化货框（去掉 orig_contour）。"""
    out: list[dict[str, Any]] = []
    for box in scaled_boxes:
        item = {
            k: v
            for k, v in box.items()
            if k != "orig_contour" and not k.startswith("_")
        }
        out.append(item)
    return out
