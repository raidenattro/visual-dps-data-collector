"""与 ShelfPickSense analysis/annotation + records 对齐（visual-dps 侧复刻，勿改 ShelfPickSense）。

供 export_rule_baseline_frames、simulate_frame_events_infer_collision 等与
infer-collision 对比时使用同一套 infer 尺寸与货框缩放逻辑。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from event_engine.annotation_boxes import flatten_annotation_boxes

# 与 ShelfPickSense analysis/constants.py 一致
DEFAULT_POSE_INFER_WIDTH = 852.0
DEFAULT_POSE_INFER_HEIGHT = 480.0
MANIFEST_FILE = "manifest.json"


def annotation_size(annotation: dict[str, Any]) -> tuple[float | None, float | None]:
    ann_size = annotation.get("annotation_size") if isinstance(annotation.get("annotation_size"), dict) else {}
    ann_w = float(ann_size.get("width") or 0) or None
    ann_h = float(ann_size.get("height") or 0) or None
    return ann_w, ann_h


def _positive_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def _parse_polygon_list(raw: Any) -> list[tuple[float, float]]:
    if not isinstance(raw, list) or len(raw) < 3:
        return []
    pts: list[tuple[float, float]] = []
    for pt in raw:
        if isinstance(pt, (list, tuple)) and len(pt) >= 2:
            pts.append((float(pt[0]), float(pt[1])))
    return pts


def polygon_points(box: dict[str, Any]) -> list[tuple[float, float]]:
    return _parse_polygon_list(box.get("video_polygon"))


def normalized_polygon_points(box: dict[str, Any]) -> list[tuple[float, float]]:
    return _parse_polygon_list(box.get("video_polygon_norm"))


def scale_polygon_to_frame(
    pts: list[tuple[float, float]],
    *,
    ann_w: float | None,
    ann_h: float | None,
    target_w: float,
    target_h: float,
) -> list[tuple[float, float]]:
    """与 ShelfPickSense analysis/annotation.scale_polygon_to_frame 一致。"""
    if not pts:
        return []
    tw, th = float(target_w), float(target_h)
    if ann_w and ann_h and ann_w > 0 and ann_h > 0:
        sx = tw / float(ann_w)
        sy = th / float(ann_h)
    else:
        max_x = max(p[0] for p in pts)
        max_y = max(p[1] for p in pts)
        if max_x > 0 and max_y > 0:
            sx, sy = tw / max_x, th / max_y
        else:
            sx = sy = 1.0
    return [(x * sx, y * sy) for x, y in pts]


def infer_polygon_points(
    box: dict[str, Any],
    *,
    infer_w: float,
    infer_h: float,
    ann_w: float | None,
    ann_h: float | None,
) -> list[tuple[float, float]]:
    """与 ShelfPickSense analysis/annotation.infer_polygon_points 一致。"""
    norm_pts = normalized_polygon_points(box)
    if norm_pts:
        return [(x * infer_w, y * infer_h) for x, y in norm_pts]
    raw_pts = polygon_points(box)
    return scale_polygon_to_frame(raw_pts, ann_w=ann_w, ann_h=ann_h, target_w=infer_w, target_h=infer_h)


def _infer_from_manifest(record_dir: Path) -> tuple[float, float] | None:
    path = record_dir / MANIFEST_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None

    infer_w = _positive_float(data.get("infer_width"))
    infer_h = _positive_float(data.get("infer_height"))
    if infer_w and infer_h:
        return infer_w, infer_h

    annotation = data.get("annotation")
    if isinstance(annotation, dict):
        ann_size = annotation.get("annotation_size")
        if isinstance(ann_size, dict):
            infer_w = _positive_float(ann_size.get("width"))
            infer_h = _positive_float(ann_size.get("height"))
            if infer_w and infer_h:
                return infer_w, infer_h
    return None


def _infer_from_annotation_meta(annotation: dict[str, Any]) -> tuple[float, float] | None:
    infer_w = _positive_float(annotation.get("infer_width"))
    infer_h = _positive_float(annotation.get("infer_height"))
    if infer_w and infer_h:
        return infer_w, infer_h
    return None


def _infer_from_pose_pipeline(annotation: dict[str, Any]) -> tuple[float, float]:
    """与 ShelfPickSense analysis/records._infer_from_pose_pipeline 一致。"""
    ann_w, ann_h = annotation_size(annotation)
    if ann_w and ann_h:
        aw, ah = int(round(ann_w)), int(round(ann_h))
        if aw == 640 and ah == 360:
            return DEFAULT_POSE_INFER_WIDTH, DEFAULT_POSE_INFER_HEIGHT
        infer_h = DEFAULT_POSE_INFER_HEIGHT
        infer_w = round(infer_h * (ann_w / ann_h))
        return float(infer_w), float(infer_h)
    return DEFAULT_POSE_INFER_WIDTH, DEFAULT_POSE_INFER_HEIGHT


def resolve_infer_frame_size(
    record_dir: Path,
    annotation: dict[str, Any],
) -> tuple[float, float]:
    """与 ShelfPickSense analysis/records.resolve_infer_frame_size 一致。"""
    from_manifest = _infer_from_manifest(record_dir)
    if from_manifest:
        return from_manifest

    from_annotation = _infer_from_annotation_meta(annotation)
    if from_annotation:
        return from_annotation

    return _infer_from_pose_pipeline(annotation)


def build_collision_boxes(
    annotation: dict[str, Any],
    *,
    infer_w: float,
    infer_h: float,
) -> list[dict[str, Any]]:
    """按 ShelfPickSense build_box_index 顺序构建 InferCollisionProcessor 货框列表。"""
    ann_w, ann_h = annotation_size(annotation)
    boxes: list[dict[str, Any]] = []
    for raw in flatten_annotation_boxes(annotation):
        scaled = infer_polygon_points(raw, infer_w=infer_w, infer_h=infer_h, ann_w=ann_w, ann_h=ann_h)
        if len(scaled) < 3:
            continue
        item = dict(raw)
        mapped_pts = [[x, y] for x, y in scaled]
        item["orig_contour"] = np.asarray(mapped_pts, dtype=np.float32).reshape((-1, 1, 2))
        boxes.append(item)
    return boxes
