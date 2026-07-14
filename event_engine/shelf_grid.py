"""货架网格末行识别：每个 shelf 独立取「已标注层中最大 layer」为底行。"""

from __future__ import annotations

from typing import Any

from event_engine.annotation_boxes import flatten_annotation_boxes
from event_engine.box_identity import box_collision_token


def _grid_shape_from_shelf(shelf: dict[str, Any], fallback: tuple[int, int] | None) -> tuple[int, int]:
    shape = shelf.get("grid_shape")
    if isinstance(shape, (list, tuple)) and len(shape) >= 2:
        try:
            rows = max(1, int(shape[0]))
            cols = max(1, int(shape[1]))
            return rows, cols
        except (TypeError, ValueError):
            pass
    if fallback:
        return fallback
    return 4, 4


def _infer_layer_column_from_box_id(
    box_id: str,
    *,
    grid_rows: int,
    grid_cols: int,
) -> tuple[int, int] | None:
    """从 box_id 推断 layer/column（1-based）。支持 2013、13 等末三位/全量线性编号。"""
    text = str(box_id or "").strip()
    if not text.isdigit():
        return None
    num = int(text)
    linear = num % 1000 if num >= 1000 else num
    if linear <= 0:
        return None
    max_cells = grid_rows * grid_cols
    if linear > max_cells:
        linear = num
    if linear <= 0 or linear > max_cells:
        return None
    layer = (linear - 1) // grid_cols + 1
    column = (linear - 1) % grid_cols + 1
    return layer, column


def _resolve_layer_column(
    box: dict[str, Any],
    *,
    grid_rows: int,
    grid_cols: int,
) -> tuple[int, int] | None:
    layer_raw = box.get("layer")
    col_raw = box.get("column")
    try:
        if layer_raw is not None and col_raw is not None:
            layer = int(layer_raw)
            column = int(col_raw)
            if layer > 0 and column > 0:
                return layer, column
    except (TypeError, ValueError):
        pass
    box_id = str(box.get("box_id") or box.get("id") or "").strip()
    if box_id:
        return _infer_layer_column_from_box_id(box_id, grid_rows=grid_rows, grid_cols=grid_cols)
    return None


def _effective_bottom_layer(*, grid_rows: int, max_layer_seen: int) -> int:
    """底行 layer：该 shelf 已标注格子的最大 layer（未标满 grid 时如 9 格即 layer=3）。"""
    if max_layer_seen > 0:
        return max_layer_seen
    return max(1, grid_rows)


def _bottom_ids_for_shelf_boxes(
    boxes: list[dict[str, Any]],
    *,
    grid_rows: int,
    grid_cols: int,
) -> tuple[int, set[str]]:
    """返回 (effective_bottom_layer, 底行 box_id 集合)。"""
    layer_by_id: dict[str, int] = {}
    max_layer_seen = 0
    for box in boxes:
        if not isinstance(box, dict):
            continue
        bid = str(box.get("box_id") or "").strip()
        if not bid:
            continue
        lc = _resolve_layer_column(box, grid_rows=grid_rows, grid_cols=grid_cols)
        if lc is None:
            continue
        layer, _col = lc
        layer_by_id[bid] = layer
        max_layer_seen = max(max_layer_seen, layer)
    bottom_layer = _effective_bottom_layer(grid_rows=grid_rows, max_layer_seen=max_layer_seen)
    bottom_ids = {bid for bid, layer in layer_by_id.items() if layer == bottom_layer}
    return bottom_layer, bottom_ids


def bottom_row_box_ids_for_config(config_data: dict[str, Any]) -> dict[str, set[str]]:
    """按 shelf_code 返回底行 box_id 集合；双货架会得到两组底行。"""
    if not isinstance(config_data, dict):
        return {}

    top_shape: tuple[int, int] | None = None
    top_gs = config_data.get("grid_shape")
    if isinstance(top_gs, (list, tuple)) and len(top_gs) >= 2:
        try:
            top_shape = (max(1, int(top_gs[0])), max(1, int(top_gs[1])))
        except (TypeError, ValueError):
            top_shape = None

    out: dict[str, set[str]] = {}
    shelves = config_data.get("shelves")
    if isinstance(shelves, list) and shelves:
        for shelf in shelves:
            if not isinstance(shelf, dict):
                continue
            shelf_code = str(shelf.get("shelf_code") or shelf.get("shelf_id") or "").strip() or "__default__"
            grid_rows, grid_cols = _grid_shape_from_shelf(shelf, top_shape)
            boxes = shelf.get("boxes") or []
            if not isinstance(boxes, list):
                continue
            _bottom_layer, bottom_ids = _bottom_ids_for_shelf_boxes(
                boxes, grid_rows=grid_rows, grid_cols=grid_cols
            )
            out[shelf_code] = bottom_ids
        return out

    grid_rows, grid_cols = top_shape or (4, 4)
    boxes = flatten_annotation_boxes(config_data)
    _bottom_layer, bottom_ids = _bottom_ids_for_shelf_boxes(
        boxes, grid_rows=grid_rows, grid_cols=grid_cols
    )
    out["__default__"] = bottom_ids
    return out


def bottom_row_collision_tokens(config_data: dict[str, Any]) -> set[str]:
    """全部货架底行货位的碰撞 token（Box_{box_id}）。"""
    tokens: set[str] = set()
    for _shelf, ids in bottom_row_box_ids_for_config(config_data).items():
        for bid in ids:
            tok = box_collision_token({"box_id": bid})
            if tok:
                tokens.add(tok)
    return tokens


def tag_bottom_row_on_boxes(
    boxes: list[dict[str, Any]],
    config_data: dict[str, Any],
) -> list[dict[str, Any]]:
    """为 scaled boxes 打上 is_bottom_row 标记（按 shelf_code + box_id 匹配）。"""
    per_shelf = bottom_row_box_ids_for_config(config_data)
    all_bottom: set[tuple[str, str]] = set()
    for shelf_code, ids in per_shelf.items():
        for bid in ids:
            all_bottom.add((shelf_code, bid))
            all_bottom.add(("__default__", bid))
            all_bottom.add(("", bid))

    tagged: list[dict[str, Any]] = []
    for box in boxes:
        item = dict(box)
        bid = str(item.get("box_id") or item.get("id") or "").strip()
        shelf_code = str(item.get("shelf_code") or "").strip() or "__default__"
        item["is_bottom_row"] = (
            (shelf_code, bid) in all_bottom
            or ("__default__", bid) in all_bottom
            or ("", bid) in all_bottom
            or bid in per_shelf.get(shelf_code, set())
            or bid in per_shelf.get("__default__", set())
        )
        tagged.append(item)
    return tagged


def summarize_bottom_rows(config_data: dict[str, Any]) -> list[dict[str, Any]]:
    """可读摘要：每个 shelf 的 grid、有效底行 layer、box_id 列表。"""
    rows: list[dict[str, Any]] = []
    shelves = config_data.get("shelves") if isinstance(config_data, dict) else None
    top_shape: tuple[int, int] | None = None
    top_gs = config_data.get("grid_shape") if isinstance(config_data, dict) else None
    if isinstance(top_gs, (list, tuple)) and len(top_gs) >= 2:
        try:
            top_shape = (max(1, int(top_gs[0])), max(1, int(top_gs[1])))
        except (TypeError, ValueError):
            top_shape = None

    if isinstance(shelves, list) and shelves:
        for shelf in shelves:
            if not isinstance(shelf, dict):
                continue
            shelf_code = str(shelf.get("shelf_code") or shelf.get("shelf_id") or "").strip() or "__default__"
            grid_rows, grid_cols = _grid_shape_from_shelf(shelf, top_shape)
            boxes = shelf.get("boxes") or []
            bottom_layer, bottom_ids = _bottom_ids_for_shelf_boxes(
                boxes if isinstance(boxes, list) else [],
                grid_rows=grid_rows,
                grid_cols=grid_cols,
            )
            ids = sorted(bottom_ids)
            rows.append({
                "shelf_code": shelf_code,
                "grid_shape": [grid_rows, grid_cols],
                "bottom_layer": bottom_layer,
                "box_ids": ids,
                "tokens": [box_collision_token({"box_id": i}) for i in ids],
            })
        return rows

    grid_rows, grid_cols = top_shape or (4, 4)
    boxes = flatten_annotation_boxes(config_data) if isinstance(config_data, dict) else []
    bottom_layer, bottom_ids = _bottom_ids_for_shelf_boxes(
        boxes, grid_rows=grid_rows, grid_cols=grid_cols
    )
    ids = sorted(bottom_ids)
    rows.append({
        "shelf_code": "__default__",
        "grid_shape": [grid_rows, grid_cols],
        "bottom_layer": bottom_layer,
        "box_ids": ids,
        "tokens": [box_collision_token({"box_id": i}) for i in ids],
    })
    return rows
