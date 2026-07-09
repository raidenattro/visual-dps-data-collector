"""全骨骼速度特征提取服务：写 skeleton_velocity / skeleton_motion_segments Parquet。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from event_engine.skeleton_features import SKELETON_FEATURE_SCHEMA, extract_skeleton_features_from_frames
from pose_store import (
    MANIFEST_FILE,
    STORAGE_V2_PARQUET,
    RecordLocator,
    load_all_frames,
    load_manifest,
    patch_v2_manifest,
)

from api.wrist_features_service import (
    _infer_size_from_frames,
    _load_boxes_for_wrist_features,
    _require_pyarrow,
    _video_fps,
    _write_parquet,
)

SKELETON_VELOCITY_FILE = "skeleton_velocity.parquet"
SKELETON_MOTION_SEGMENTS_FILE = "skeleton_motion_segments.parquet"


def _velocity_empty_schema():
    pa, _ = _require_pyarrow()
    from model_assets import COCO17_KEYPOINT_NAMES

    schema: dict[str, Any] = {
        "frame_idx": pa.array([], type=pa.int32()),
        "source_frame_idx": pa.array([], type=pa.int32()),
        "timestamp_sec": pa.array([], type=pa.float64()),
        "person_id": pa.array([], type=pa.int32()),
        "person_track_id": pa.array([], type=pa.int32()),
        "torso_x": pa.array([], type=pa.float64()),
        "torso_y": pa.array([], type=pa.float64()),
        "torso_speed": pa.array([], type=pa.float64()),
        "torso_speed_norm": pa.array([], type=pa.float64()),
        "torso_velocity_valid": pa.array([], type=pa.bool_()),
        "body_mean_speed": pa.array([], type=pa.float64()),
        "body_max_speed": pa.array([], type=pa.float64()),
        "upper_mean_speed": pa.array([], type=pa.float64()),
        "lower_mean_speed": pa.array([], type=pa.float64()),
        "wrist_max_speed": pa.array([], type=pa.float64()),
        "elbow_max_speed": pa.array([], type=pa.float64()),
        "wrist_torso_ratio": pa.array([], type=pa.float64()),
    }
    for i in range(len(COCO17_KEYPOINT_NAMES)):
        schema[f"kpt_{i}_speed"] = pa.array([], type=pa.float64())
    return schema


def _motion_segment_empty_schema():
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
        "motion_frame_count": pa.array([], type=pa.int32()),
        "motion_valid_ratio": pa.array([], type=pa.float64()),
        "torso_speed_p50": pa.array([], type=pa.float64()),
        "torso_speed_max": pa.array([], type=pa.float64()),
        "lower_mean_speed_p50": pa.array([], type=pa.float64()),
        "body_mean_speed_p50": pa.array([], type=pa.float64()),
        "wrist_torso_ratio_p50": pa.array([], type=pa.float64()),
    }


def skeleton_feature_paths_for_locator(locator: RecordLocator) -> tuple[Path, Path]:
    if locator.storage == STORAGE_V2_PARQUET:
        base = locator.path
        return base / SKELETON_VELOCITY_FILE, base / SKELETON_MOTION_SEGMENTS_FILE
    stem = locator.path.stem
    parent = locator.path.parent
    return (
        parent / f"{stem}.{SKELETON_VELOCITY_FILE}",
        parent / f"{stem}.{SKELETON_MOTION_SEGMENTS_FILE}",
    )


def skeleton_features_already_extracted(locator: RecordLocator) -> bool:
    vel_path, seg_path = skeleton_feature_paths_for_locator(locator)
    return vel_path.is_file() and seg_path.is_file()


def write_skeleton_feature_parquet(
    locator: RecordLocator,
    *,
    velocity_rows: list[dict[str, Any]],
    motion_segment_rows: list[dict[str, Any]],
) -> tuple[Path, Path]:
    vel_path, seg_path = skeleton_feature_paths_for_locator(locator)
    vel_path.parent.mkdir(parents=True, exist_ok=True)
    _write_parquet(vel_path, velocity_rows, _velocity_empty_schema())
    _write_parquet(seg_path, motion_segment_rows, _motion_segment_empty_schema())
    return vel_path, seg_path


def _patch_skeleton_features_manifest_v2(
    record_dir: Path,
    *,
    annotation_source: str | None,
    velocity_count: int,
    motion_segment_count: int,
) -> None:
    manifest_path = record_dir / MANIFEST_FILE
    if not manifest_path.is_file():
        return
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    files = dict(manifest.get("files") or {})
    files["skeleton_velocity"] = SKELETON_VELOCITY_FILE
    files["skeleton_motion_segments"] = SKELETON_MOTION_SEGMENTS_FILE
    features_meta = {
        "schema": SKELETON_FEATURE_SCHEMA,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "velocity_count": velocity_count,
        "motion_segment_count": motion_segment_count,
        "annotation_source": annotation_source,
        "note": "全骨骼速度特征；motion_segments=碰撞段+段内躯干/全身运动统计",
    }
    patch_v2_manifest(
        record_dir,
        {
            "files": files,
            "skeleton_features": features_meta,
        },
    )


def extract_skeleton_features_for_record(
    locator: RecordLocator,
    *,
    annotation_path: Path | None = None,
    max_gap_frames: int = 1,
    skip_if_exists: bool = False,
    include_keypoint_detail: bool = True,
) -> dict[str, Any]:
    """为单条记录提取全骨骼速度特征并落盘。"""
    if skip_if_exists and skeleton_features_already_extracted(locator):
        vel_path, seg_path = skeleton_feature_paths_for_locator(locator)
        return {
            "status": "skipped",
            "record_id": locator.record_id,
            "velocity_path": str(vel_path),
            "motion_segments_path": str(seg_path),
        }

    manifest = load_manifest(locator)
    frames = load_all_frames(locator)
    if not frames:
        raise ValueError(f"记录 {locator.record_id} 无帧数据")

    infer_w, infer_h = _infer_size_from_frames(frames, manifest)
    fps = _video_fps(manifest)

    boxes, ann_path, ann_source = _load_boxes_for_wrist_features(
        locator,
        manifest,
        infer_w=infer_w,
        infer_h=infer_h,
        annotation_path=annotation_path,
    )
    if not boxes:
        raise ValueError(
            f"记录 {locator.record_id} 无可用货框标注（请检查 reflection 合并或 manifest.annotation）"
        )

    result = extract_skeleton_features_from_frames(
        frames,
        boxes,
        infer_width=infer_w,
        infer_height=infer_h,
        video_fps=fps,
        max_gap_frames=max_gap_frames,
        include_keypoint_detail=include_keypoint_detail,
    )

    vel_path, seg_path = write_skeleton_feature_parquet(
        locator,
        velocity_rows=result["velocity_rows"],
        motion_segment_rows=result["motion_segment_rows"],
    )

    if locator.storage == STORAGE_V2_PARQUET:
        _patch_skeleton_features_manifest_v2(
            locator.path,
            annotation_source=ann_source,
            velocity_count=result["velocity_count"],
            motion_segment_count=result["motion_segment_count"],
        )

    return {
        "status": "ok",
        "record_id": locator.record_id,
        "velocity_path": str(vel_path),
        "motion_segments_path": str(seg_path),
        "velocity_count": result["velocity_count"],
        "motion_segment_count": result["motion_segment_count"],
        "segment_count": result["segment_count"],
        "box_count": len(boxes),
        "annotation": str(ann_path) if ann_path else None,
        "annotation_source": ann_source,
    }


def load_skeleton_features_payload(
    locator: RecordLocator,
    *,
    frame_idx: int | None = None,
    include_all_velocity: bool = False,
) -> dict[str, Any]:
    """读取已落盘的全骨骼速度特征。"""
    vel_path, seg_path = skeleton_feature_paths_for_locator(locator)
    if not vel_path.is_file():
        return {
            "available": False,
            "record_id": locator.record_id,
            "hint": "请先运行 scripts/data/extract_skeleton_features.py 提取特征",
        }

    _, pq = _require_pyarrow()
    seg_rows = pq.read_table(seg_path).to_pylist() if seg_path.is_file() else []

    vel_meta = pq.read_metadata(vel_path)
    velocity_count = int(vel_meta.num_rows) if vel_meta else 0

    velocity_by_frame: dict[str, list[dict[str, Any]]] = {}
    frame_velocity: list[dict[str, Any]] = []

    if include_all_velocity:
        vel_rows = pq.read_table(vel_path).to_pylist()
        velocity_count = len(vel_rows)
        for row in vel_rows:
            if not isinstance(row, dict):
                continue
            fi = int(row.get("frame_idx") or 0)
            velocity_by_frame.setdefault(str(fi), []).append(row)
    elif frame_idx is not None and frame_idx > 0:
        fi = int(frame_idx)
        table = pq.read_table(vel_path, filters=[("frame_idx", "=", fi)])
        frame_velocity = table.to_pylist()

    manifest = {}
    try:
        manifest = load_manifest(locator)
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        pass
    meta = (
        manifest.get("skeleton_features")
        if isinstance(manifest.get("skeleton_features"), dict)
        else {}
    )

    if frame_idx is not None and frame_idx > 0 and not include_all_velocity:
        return {
            "available": True,
            "record_id": locator.record_id,
            "frame_idx": int(frame_idx),
            "frame_velocity": frame_velocity,
        }

    payload: dict[str, Any] = {
        "available": True,
        "record_id": locator.record_id,
        "motion_segments": seg_rows,
        "velocity_count": velocity_count,
        "motion_segment_count": len(seg_rows),
        "extracted_at": meta.get("extracted_at"),
        "schema": meta.get("schema") or SKELETON_FEATURE_SCHEMA,
    }
    if include_all_velocity:
        payload["velocity_by_frame"] = velocity_by_frame
    if frame_idx is not None and frame_idx > 0:
        payload["frame_idx"] = int(frame_idx)
        payload["frame_velocity"] = frame_velocity
    return payload
