"""手腕特征提取服务：写 wrist_velocity / wrist_box_segments Parquet。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from event_engine.annotation_boxes import load_scaled_boxes
from event_engine.wrist_features import FEATURE_SCHEMA, extract_wrist_features_from_frames
from pose_store import (
    STORAGE_V2_PARQUET,
    MANIFEST_FILE,
    RecordLocator,
    load_all_frames,
    load_manifest,
    patch_v2_manifest,
)

from api.record_service import meta_path_for_record, resolve_annotation_path_for_record

WRIST_VELOCITY_FILE = "wrist_velocity.parquet"
WRIST_BOX_SEGMENTS_FILE = "wrist_box_segments.parquet"


def _require_pyarrow():
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq

        return pa, pq
    except ImportError as exc:
        raise RuntimeError("缺少 pyarrow，请执行: pip install pyarrow") from exc


def _infer_size_from_frames(frames: list[dict[str, Any]], manifest: dict[str, Any]) -> tuple[int, int]:
    infer_w = int(manifest.get("infer_width") or 0)
    infer_h = int(manifest.get("infer_height") or 0)
    if infer_w > 0 and infer_h > 0:
        return infer_w, infer_h
    for fr in frames:
        if not isinstance(fr, dict):
            continue
        w = int(fr.get("infer_width") or 0)
        h = int(fr.get("infer_height") or 0)
        if w > 0 and h > 0:
            return w, h
    return 640, 480


def _video_fps(manifest: dict[str, Any]) -> float:
    fps = float(manifest.get("fps") or 15.0)
    return fps if fps > 0 else 15.0


def _write_parquet(path: Path, rows: list[dict[str, Any]], empty_schema: dict[str, Any]) -> None:
    pa, pq = _require_pyarrow()
    path = Path(path)
    if rows:
        pq.write_table(pa.Table.from_pylist(rows), path, compression="zstd")
        return
    pq.write_table(pa.table(empty_schema), path, compression="zstd")


def _velocity_empty_schema():
    pa, _ = _require_pyarrow()
    return {
        "frame_idx": pa.array([], type=pa.int32()),
        "source_frame_idx": pa.array([], type=pa.int32()),
        "timestamp_sec": pa.array([], type=pa.float64()),
        "person_id": pa.array([], type=pa.int32()),
        "person_track_id": pa.array([], type=pa.int32()),
        "wrist": pa.array([], type=pa.string()),
        "x": pa.array([], type=pa.float64()),
        "y": pa.array([], type=pa.float64()),
        "kpt_score": pa.array([], type=pa.float64()),
        "valid": pa.array([], type=pa.bool_()),
        "vx": pa.array([], type=pa.float64()),
        "vy": pa.array([], type=pa.float64()),
        "speed": pa.array([], type=pa.float64()),
        "speed_norm": pa.array([], type=pa.float64()),
        "velocity_valid": pa.array([], type=pa.bool_()),
    }


def _segment_empty_schema():
    pa, _ = _require_pyarrow()
    return {
        "segment_id": pa.array([], type=pa.int32()),
        "event_type": pa.array([], type=pa.string()),
        "person_id": pa.array([], type=pa.int32()),
        "person_track_id": pa.array([], type=pa.int32()),
        "wrist": pa.array([], type=pa.string()),
        "box_token": pa.array([], type=pa.string()),
        "frame_enter": pa.array([], type=pa.int32()),
        "frame_exit": pa.array([], type=pa.int32()),
        "source_frame_enter": pa.array([], type=pa.int32()),
        "source_frame_exit": pa.array([], type=pa.int32()),
        "ts_enter": pa.array([], type=pa.float64()),
        "ts_exit": pa.array([], type=pa.float64()),
        "x_enter": pa.array([], type=pa.float64()),
        "y_enter": pa.array([], type=pa.float64()),
        "x_exit": pa.array([], type=pa.float64()),
        "y_exit": pa.array([], type=pa.float64()),
        "dx": pa.array([], type=pa.float64()),
        "dy": pa.array([], type=pa.float64()),
        "displacement": pa.array([], type=pa.float64()),
        "path_length": pa.array([], type=pa.float64()),
        "duration_sec": pa.array([], type=pa.float64()),
        "frame_count": pa.array([], type=pa.int32()),
        "enter_kpt_score": pa.array([], type=pa.float64()),
        "exit_kpt_score": pa.array([], type=pa.float64()),
        "had_alarm": pa.array([], type=pa.bool_()),
    }


def wrist_feature_paths_for_locator(locator: RecordLocator) -> tuple[Path, Path]:
    """返回 (velocity_path, segments_path)。"""
    if locator.storage == STORAGE_V2_PARQUET:
        base = locator.path
        return base / WRIST_VELOCITY_FILE, base / WRIST_BOX_SEGMENTS_FILE
    stem = locator.path.stem
    parent = locator.path.parent
    return parent / f"{stem}.{WRIST_VELOCITY_FILE}", parent / f"{stem}.{WRIST_BOX_SEGMENTS_FILE}"


def features_already_extracted(locator: RecordLocator) -> bool:
    vel_path, seg_path = wrist_feature_paths_for_locator(locator)
    return vel_path.is_file() and seg_path.is_file()


def write_wrist_feature_parquet(
    locator: RecordLocator,
    *,
    velocity_rows: list[dict[str, Any]],
    segment_rows: list[dict[str, Any]],
) -> tuple[Path, Path]:
    vel_path, seg_path = wrist_feature_paths_for_locator(locator)
    vel_path.parent.mkdir(parents=True, exist_ok=True)
    _write_parquet(vel_path, velocity_rows, _velocity_empty_schema())
    _write_parquet(seg_path, segment_rows, _segment_empty_schema())
    return vel_path, seg_path


def _patch_features_manifest_v2(
    record_dir: Path,
    *,
    annotation_path: Path | None,
    velocity_count: int,
    segment_count: int,
) -> None:
    manifest_path = record_dir / MANIFEST_FILE
    if not manifest_path.is_file():
        return
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    files = dict(manifest.get("files") or {})
    files["wrist_velocity"] = WRIST_VELOCITY_FILE
    files["wrist_box_segments"] = WRIST_BOX_SEGMENTS_FILE
    features_meta = {
        "schema": FEATURE_SCHEMA,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "velocity_count": velocity_count,
        "segment_count": segment_count,
        "annotation_source": annotation_path.name if annotation_path else None,
        "note": "碰撞段=手腕进/出货框占用区间，与告警无关；person_track_id 为后处理分配",
    }
    patch_v2_manifest(
        record_dir,
        {
            "files": files,
            "wrist_features": features_meta,
        },
    )


def extract_wrist_features_for_record(
    locator: RecordLocator,
    *,
    annotation_path: Path | None = None,
    max_gap_frames: int = 1,
    skip_if_exists: bool = False,
) -> dict[str, Any]:
    """
    为单条记录提取手腕速度与碰撞段特征并落盘。
    不重跑 RTMPose，仅读 skeleton + timeline + 标注。
    """
    if skip_if_exists and features_already_extracted(locator):
        vel_path, seg_path = wrist_feature_paths_for_locator(locator)
        return {
            "status": "skipped",
            "record_id": locator.record_id,
            "velocity_path": str(vel_path),
            "segments_path": str(seg_path),
        }

    manifest = load_manifest(locator)
    frames = load_all_frames(locator)
    if not frames:
        raise ValueError(f"记录 {locator.record_id} 无帧数据")

    infer_w, infer_h = _infer_size_from_frames(frames, manifest)
    fps = _video_fps(manifest)

    ann_path = annotation_path
    if ann_path is None and locator.path.is_dir():
        pkg_ann = locator.path / "annotation.json"
        if pkg_ann.is_file():
            ann_path = pkg_ann

    if ann_path is None:
        meta = None
        sidecar = meta_path_for_record(locator.record_id, locator)
        if sidecar.is_file():
            try:
                meta = json.loads(sidecar.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                meta = None
        ann_path = resolve_annotation_path_for_record(
            locator.record_id,
            locator=locator,
            meta=meta,
        )

    boxes: list[dict[str, Any]] = []
    if ann_path and ann_path.is_file():
        boxes = load_scaled_boxes(ann_path, infer_w, infer_h)

    result = extract_wrist_features_from_frames(
        frames,
        boxes,
        infer_width=infer_w,
        infer_height=infer_h,
        video_fps=fps,
        max_gap_frames=max_gap_frames,
    )

    vel_path, seg_path = write_wrist_feature_parquet(
        locator,
        velocity_rows=result["velocity_rows"],
        segment_rows=result["segment_rows"],
    )

    if locator.storage == STORAGE_V2_PARQUET:
        _patch_features_manifest_v2(
            locator.path,
            annotation_path=ann_path if ann_path and ann_path.is_file() else None,
            velocity_count=result["velocity_count"],
            segment_count=result["segment_count"],
        )

    return {
        "status": "ok",
        "record_id": locator.record_id,
        "velocity_path": str(vel_path),
        "segments_path": str(seg_path),
        "velocity_count": result["velocity_count"],
        "segment_count": result["segment_count"],
        "box_count": len(boxes),
        "annotation": str(ann_path) if ann_path else None,
    }
