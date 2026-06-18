"""手腕特征提取服务：写 wrist_velocity / wrist_box_segments Parquet。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from annotation_store import (
    annotation_dir_for_source,
    annotation_path_for_video_stem,
    materialize_tier_annotation_from_master,
    resolve_video_stem_from_record,
)
from config_loader import parse_record_path_segments, resolve_app_paths
from event_engine.annotation_boxes import (
    flatten_annotation_boxes,
    load_scaled_boxes,
    load_scaled_boxes_from_config,
)
from event_engine.wrist_features import FEATURE_SCHEMA, extract_wrist_features_from_frames
from pose_store import (
    STORAGE_V2_PARQUET,
    MANIFEST_FILE,
    RecordLocator,
    load_all_frames,
    load_manifest,
    patch_v2_manifest,
)

from api.annotate_service import normalize_pose_tier
from api.record_service import (
    meta_path_for_record,
    resolve_annotation_path_for_record,
    resolve_reflection_annotation_path,
)

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


def _record_meta_for_annotation(
    locator: RecordLocator,
    manifest: dict[str, Any],
) -> dict[str, Any] | None:
    """合并 sidecar meta 与 manifest.collect_config（批采记录常无 sidecar）。"""
    meta: dict[str, Any] = {}
    sidecar = meta_path_for_record(locator.record_id, locator)
    if sidecar.is_file():
        try:
            raw = json.loads(sidecar.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                meta.update(raw)
        except json.JSONDecodeError:
            pass
    cc = manifest.get("collect_config")
    if isinstance(cc, dict):
        for key in ("camera_label", "camera_slug", "video_stem", "source_video"):
            val = str(cc.get(key) or "").strip()
            if val and not str(meta.get(key) or "").strip():
                meta[key] = val
    return meta if meta else None


def _manifest_embedded_annotation(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """采集 manifest 内嵌标注（与碰撞落盘时使用的货框一致）。"""
    ann = manifest.get("annotation")
    if not isinstance(ann, dict):
        return None
    if flatten_annotation_boxes(ann):
        return ann
    return None


def _embedded_annotation_box_count(manifest: dict[str, Any]) -> int:
    ann = _manifest_embedded_annotation(manifest)
    if not ann:
        return 0
    explicit = int(ann.get("box_count") or 0)
    if explicit > 0:
        return explicit
    return len(flatten_annotation_boxes(ann))


def resolve_annotation_path_for_wrist_features(
    locator: RecordLocator,
    manifest: dict[str, Any],
    *,
    annotation_path: Path | None = None,
) -> Path | None:
    """
    手腕特征用标注路径：多货架机位优先 reflection 合并（与采集碰撞 / 准确率重算一致）。
    """
    if annotation_path and annotation_path.is_file():
        return annotation_path

    paths = resolve_app_paths()
    meta = _record_meta_for_annotation(locator, manifest)
    parsed_tier, _, _ = parse_record_path_segments(locator.record_id)
    tier = normalize_pose_tier(parsed_tier) if parsed_tier else None

    if tier:
        tier_dir = annotation_dir_for_source(paths, tier)
        reflection_merged = resolve_reflection_annotation_path(
            meta,
            ann_dir=tier_dir,
            fallback_ann_dir=paths.annotation_dir,
        )
        if reflection_merged and reflection_merged.is_file():
            return reflection_merged

        video_stem = resolve_video_stem_from_record(
            locator.record_id,
            json_dir=paths.json_dir,
            pose_path=locator.path,
            meta=meta,
        )
        tier_path = annotation_path_for_video_stem(video_stem, annotation_dir=tier_dir)
        if tier_path.is_file():
            return tier_path
        mat = materialize_tier_annotation_from_master(
            video_stem,
            paths=paths,
            source=tier,
        )
        if mat and mat.is_file():
            return mat

    return resolve_annotation_path_for_record(
        locator.record_id,
        locator=locator,
        meta=meta,
    )


def _load_boxes_for_wrist_features(
    locator: RecordLocator,
    manifest: dict[str, Any],
    *,
    infer_w: int,
    infer_h: int,
    annotation_path: Path | None = None,
) -> tuple[list[dict[str, Any]], Path | None, str]:
    """
    加载推理空间货框；若文件标注框数少于 manifest 内嵌标注则改用内嵌（避免仅单货架包内副本）。
    返回 (boxes, ann_path, source_label)。
    """
    ann_path = resolve_annotation_path_for_wrist_features(
        locator,
        manifest,
        annotation_path=annotation_path,
    )
    embedded = _manifest_embedded_annotation(manifest)
    embedded_count = _embedded_annotation_box_count(manifest)

    boxes: list[dict[str, Any]] = []
    source = "none"

    if ann_path and ann_path.is_file():
        boxes = load_scaled_boxes(ann_path, infer_w, infer_h)
        source = ann_path.name

    if embedded and embedded_count > len(boxes):
        boxes = load_scaled_boxes_from_config(embedded, infer_w, infer_h)
        source = "manifest.annotation"
        ann_path = None

    return boxes, ann_path, source


def _patch_features_manifest_v2(
    record_dir: Path,
    *,
    annotation_source: str | None,
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
        "annotation_source": annotation_source,
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
            annotation_source=ann_source,
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
        "annotation_source": ann_source,
    }


def load_wrist_features_payload(
    locator: RecordLocator,
    *,
    frame_idx: int | None = None,
    include_all_velocity: bool = False,
) -> dict[str, Any]:
    """读取已落盘的手腕特征，供回放 API 使用。

    默认只返回碰撞段（体积小）；速度按 frame_idx 按需查询，避免整表 JSON 拖慢回放。
    """
    vel_path, seg_path = wrist_feature_paths_for_locator(locator)
    if not vel_path.is_file():
        return {
            "available": False,
            "record_id": locator.record_id,
            "hint": "请先运行 scripts/data/extract_wrist_features.py 提取特征",
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
    meta = manifest.get("wrist_features") if isinstance(manifest.get("wrist_features"), dict) else {}

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
        "segments": seg_rows,
        "velocity_count": velocity_count,
        "segment_count": len(seg_rows),
        "extracted_at": meta.get("extracted_at"),
        "schema": meta.get("schema") or FEATURE_SCHEMA,
    }
    if include_all_velocity:
        payload["velocity_by_frame"] = velocity_by_frame
    if frame_idx is not None and frame_idx > 0:
        payload["frame_idx"] = int(frame_idx)
        payload["frame_velocity"] = frame_velocity
    return payload
