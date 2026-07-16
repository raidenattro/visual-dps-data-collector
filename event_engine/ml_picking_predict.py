"""ShelfPickSense sklearn 模型：从本仓库记录包推理并与 baseline 融合。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Literal

import pandas as pd

from pose_store import SKELETON_FILE, RecordLocator

MlFeatureMode = Literal["full_skeleton_index", "interval2_only_index"]


def _ensure_shelf_picksense_importable(shelf_root: Path) -> None:
    src = shelf_root / "src"
    if not src.is_dir():
        raise FileNotFoundError(f"ShelfPickSense src 不存在: {src}")
    src_str = str(src.resolve())
    if src_str not in sys.path:
        sys.path.insert(0, src_str)


def load_sklearn_picking_model(model_dir: Path, *, shelf_root: Path):
    _ensure_shelf_picksense_importable(shelf_root)
    from analysis.models import SklearnPickingModel

    return SklearnPickingModel.load(Path(model_dir))


def load_skeleton_dataframe(locator: RecordLocator) -> pd.DataFrame:
    path = locator.path / SKELETON_FILE
    if not path.is_file():
        raise FileNotFoundError(f"缺少 skeleton.parquet: {path}")
    import pyarrow.parquet as pq

    return pq.read_table(path).to_pandas()


def build_record_data(
    locator: RecordLocator,
    *,
    record_id: str,
    annotation: dict[str, Any],
    infer_width: float,
    infer_height: float,
    skeleton_df: pd.DataFrame,
    shelf_root: Path,
) -> Any:
    _ensure_shelf_picksense_importable(shelf_root)
    from analysis.annotation import build_box_index
    from analysis.box_layout import build_box_layout, compute_shelf_layout_stats
    from analysis.labels import RecordLabels
    from analysis.records import RecordData

    box_index = build_box_index(annotation, infer_w=infer_width, infer_h=infer_height)
    box_tokens = sorted(box_index.keys())
    box_layout = build_box_layout(annotation, frame_width=infer_width)
    shelf_stats = compute_shelf_layout_stats(box_layout)

    return RecordData(
        record_id=record_id,
        record_dir=locator.path,
        skeleton=skeleton_df,
        annotation=annotation,
        event_review=None,
        labels=RecordLabels(record_id=record_id),
        infer_width=float(infer_width),
        infer_height=float(infer_height),
        box_tokens=box_tokens,
        box_index=box_index,
        box_layout=box_layout,
        shelf_layout_stats=shelf_stats,
    )


def _frame_has_persons(frame) -> bool:
    return bool(getattr(frame, "persons", None))


def predict_ml_by_frame(
    model: Any,
    record: Any,
    *,
    registry: Any,
    target_frame_indices: set[int],
    fail_open_empty_skeleton: bool = True,
) -> dict[int, dict[str, Any]]:
    from analysis.features.base import FeatureContext
    from analysis.models import PickingPrediction, SklearnPickingModel

    if not isinstance(model, SklearnPickingModel):
        raise TypeError("需要 SklearnPickingModel")

    preds: dict[int, dict[str, Any]] = {}
    for frame in record.frames():
        fi = int(frame.frame_idx)
        if fi not in target_frame_indices:
            continue

        if fail_open_empty_skeleton and not _frame_has_persons(frame):
            preds[fi] = {
                "is_picking": True,
                "picking_prob": 0.0,
                "predicted_box_tokens": [],
                "ml_fail_open": True,
            }
            continue

        ctx = FeatureContext.from_record(record, frame)
        groups = registry.extract_frame_feature_groups_from_context(ctx)
        if not groups:
            if fail_open_empty_skeleton:
                preds[fi] = {
                    "is_picking": True,
                    "picking_prob": 0.0,
                    "predicted_box_tokens": [],
                    "ml_fail_open": True,
                }
            else:
                preds[fi] = {
                    "is_picking": False,
                    "picking_prob": 0.0,
                    "predicted_box_tokens": [],
                    "ml_fail_open": False,
                }
            continue

        best: PickingPrediction | None = None
        for group in groups:
            pred = model.predict_frame(
                group.to_vector(model.frame_feature_names),
                record_id=record.record_id,
                frame_idx=fi,
                box_layout=record.box_layout,
            )
            if best is None or pred.picking_prob > best.picking_prob:
                best = pred

        assert best is not None
        preds[fi] = {
            "is_picking": bool(best.is_picking),
            "picking_prob": float(best.picking_prob),
            "predicted_box_tokens": list(best.predicted_box_tokens),
            "ml_fail_open": False,
        }
    return preds


def make_registry_for_model(model: Any, *, shelf_root: Path) -> Any:
    _ensure_shelf_picksense_importable(shelf_root)
    from analysis.features.registry import default_registry

    reg = default_registry()
    return reg.select_extractors_for_features(
        frame_feature_names=list(model.frame_feature_names),
        box_feature_names=list(model.box_feature_names),
    )


def filter_skeleton_for_interval2(skeleton_df: pd.DataFrame, export_indices: set[int]) -> pd.DataFrame:
    if skeleton_df.empty:
        return skeleton_df
    if "frame_idx" not in skeleton_df.columns:
        return skeleton_df.iloc[0:0]
    mask = skeleton_df["frame_idx"].astype(int).isin(export_indices)
    return skeleton_df.loc[mask].copy()


def fuse_baseline_ml_row(
    baseline_row: dict[str, Any],
    ml_pred: dict[str, Any],
) -> dict[str, Any]:
    baseline_pick = bool(baseline_row.get("is_picking"))
    ml_pick = bool(ml_pred.get("is_picking"))
    final_pick = baseline_pick and ml_pick

    baseline_alarms = list(baseline_row.get("rule_alarm_collisions") or [])
    ml_tokens = list(ml_pred.get("predicted_box_tokens") or [])

    return {
        "record_id": baseline_row.get("record_id"),
        "frame_idx": int(baseline_row.get("frame_idx") or 0),
        "is_picking": final_pick,
        "picking_prob": float(ml_pred.get("picking_prob") or 0.0),
        "rule_collisions": list(baseline_row.get("rule_collisions") or []),
        "rule_alarm_collisions": baseline_alarms if final_pick else [],
        "predicted_box_tokens": ml_tokens if ml_pick else [],
        "baseline_is_picking": baseline_pick,
        "ml_is_picking": ml_pick,
        "ml_vetoed": baseline_pick and not ml_pick,
        "ml_fail_open": bool(ml_pred.get("ml_fail_open")),
    }


def build_ml_only_row(
    *,
    record_id: str,
    frame_idx: int,
    ml_pred: dict[str, Any],
) -> dict[str, Any]:
    ml_pick = bool(ml_pred.get("is_picking"))
    ml_tokens = list(ml_pred.get("predicted_box_tokens") or [])
    return {
        "record_id": record_id,
        "frame_idx": int(frame_idx),
        "is_picking": ml_pick,
        "picking_prob": float(ml_pred.get("picking_prob") or 0.0),
        "rule_collisions": [],
        "rule_alarm_collisions": ml_tokens if ml_pick else [],
        "predicted_box_tokens": ml_tokens if ml_pick else [],
        "ml_is_picking": ml_pick,
        "ml_fail_open": bool(ml_pred.get("ml_fail_open")),
    }


def predict_ml_only_clip(
    *,
    locator: RecordLocator,
    record_id: str,
    annotation: dict[str, Any],
    infer_width: float,
    infer_height: float,
    model: Any,
    registry: Any,
    feature_mode: MlFeatureMode,
    export_indices: set[int],
    shelf_root: Path,
) -> list[dict[str, Any]]:
    skeleton_df = load_skeleton_dataframe(locator)
    if feature_mode == "interval2_only_index":
        skeleton_df = filter_skeleton_for_interval2(skeleton_df, export_indices)

    record = build_record_data(
        locator,
        record_id=record_id,
        annotation=annotation,
        infer_width=infer_width,
        infer_height=infer_height,
        skeleton_df=skeleton_df,
        shelf_root=shelf_root,
    )

    ml_by_frame = predict_ml_by_frame(
        model,
        record,
        registry=registry,
        target_frame_indices=export_indices,
        fail_open_empty_skeleton=False,
    )

    out: list[dict[str, Any]] = []
    for fi in sorted(export_indices):
        ml = ml_by_frame.get(fi)
        if ml is None:
            ml = {
                "is_picking": False,
                "picking_prob": 0.0,
                "predicted_box_tokens": [],
                "ml_fail_open": False,
            }
        out.append(
            build_ml_only_row(
                record_id=record_id,
                frame_idx=fi,
                ml_pred=ml,
            )
        )
    return out


def predict_and_fuse_clip(
    baseline_frames: list[dict[str, Any]],
    *,
    locator: RecordLocator,
    record_id: str,
    annotation: dict[str, Any],
    infer_width: float,
    infer_height: float,
    model: Any,
    registry: Any,
    feature_mode: MlFeatureMode,
    export_indices: set[int],
    shelf_root: Path,
) -> list[dict[str, Any]]:
    skeleton_df = load_skeleton_dataframe(locator)
    if feature_mode == "interval2_only_index":
        skeleton_df = filter_skeleton_for_interval2(skeleton_df, export_indices)

    record = build_record_data(
        locator,
        record_id=record_id,
        annotation=annotation,
        infer_width=infer_width,
        infer_height=infer_height,
        skeleton_df=skeleton_df,
        shelf_root=shelf_root,
    )

    ml_by_frame = predict_ml_by_frame(
        model,
        record,
        registry=registry,
        target_frame_indices=export_indices,
        fail_open_empty_skeleton=True,
    )

    baseline_by_idx = {int(r["frame_idx"]): r for r in baseline_frames if isinstance(r, dict)}
    out: list[dict[str, Any]] = []
    for fi in sorted(export_indices):
        base = baseline_by_idx.get(fi)
        if not base:
            continue
        ml = ml_by_frame.get(fi)
        if ml is None:
            ml = {"is_picking": True, "picking_prob": 0.0, "predicted_box_tokens": [], "ml_fail_open": True}
        out.append(fuse_baseline_ml_row(base, ml))
    return out
