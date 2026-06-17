"""基于已有骨架数据，仅重算碰撞/告警并写回记录。"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from event_engine.annotation_boxes import (
    boxes_for_json_export,
    load_annotation_config,
    load_scaled_boxes,
)
from event_engine.collision import CollisionProcessor
from pose_store import (
    STORAGE_V1_JSON,
    STORAGE_V2_PARQUET,
    RecordLocator,
    load_all_frames,
    load_manifest,
    patch_v2_manifest,
    write_timeline_parquet,
)

from api.record_service import meta_path_for_record, persist_playback_annotation


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


def _collision_params(manifest: dict[str, Any]) -> tuple[int, int, float]:
    collision_cfg = manifest.get("collision") if isinstance(manifest.get("collision"), dict) else {}
    alarm_min = int(collision_cfg.get("alarm_min_consecutive_frames") or 3)
    alarm_cd = int(collision_cfg.get("alarm_cooldown_frames") or 6)
    fps = float(manifest.get("fps") or 15.0)
    if fps <= 0:
        fps = 15.0
    return alarm_min, alarm_cd, fps


def _build_annotation_meta(
    ann_path: Path,
    *,
    infer_w: int,
    infer_h: int,
) -> dict[str, Any]:
    ann_cfg = load_annotation_config(ann_path)
    scaled_boxes = load_scaled_boxes(ann_path, infer_w, infer_h)
    return {
        "source_file": ann_path.name,
        "annotation_size": {"width": infer_w, "height": infer_h},
        "source_annotation_size": ann_cfg.get("annotation_size"),
        "source_info": ann_cfg.get("source_info"),
        "shelves": ann_cfg.get("shelves"),
        "grid_shape": ann_cfg.get("grid_shape"),
        "boxes": boxes_for_json_export(scaled_boxes),
        "box_count": len(scaled_boxes),
    }


def recompute_record_collisions(
    locator: RecordLocator,
    annotation_path: Path,
    *,
    video_stem: str = "",
    alarm_min_consecutive_frames: int | None = None,
    alarm_cooldown_frames: int | None = None,
) -> dict[str, Any]:
    """不重跑骨架推理，仅按新 ROI 重算 collisions / alarm_collisions；不修改 review 复核数据。"""
    if not annotation_path.is_file():
        raise FileNotFoundError(f"标注不存在: {annotation_path}")

    manifest = load_manifest(locator)
    frames = load_all_frames(locator)
    if not frames:
        raise ValueError("记录无帧数据，无法重算碰撞")

    infer_w, infer_h = _infer_size_from_frames(frames, manifest)
    scaled_boxes = load_scaled_boxes(annotation_path, infer_w, infer_h)
    if not scaled_boxes:
        raise ValueError(f"标注无有效货框: {annotation_path.name}")

    alarm_min, alarm_cd, fps = _collision_params(manifest)
    if alarm_min_consecutive_frames is not None:
        alarm_min = max(1, int(alarm_min_consecutive_frames))
    if alarm_cooldown_frames is not None:
        alarm_cd = max(1, int(alarm_cooldown_frames))

    processor = CollisionProcessor(
        scaled_boxes,
        alarm_min_consecutive_frames=alarm_min,
        alarm_cooldown_frames=alarm_cd,
        video_fps=fps,
    )

    collision_frames = 0
    alarm_frames = 0
    for fr in frames:
        if not isinstance(fr, dict):
            continue
        idx = int(fr.get("source_frame_idx") or fr.get("frame_idx") or 0)
        event = processor.process({"frame_idx": idx, "persons": fr.get("persons") or []})
        fr["collisions"] = list(event.get("collisions") or [])
        fr["alarm_collisions"] = list(event.get("alarm_collisions") or [])
        if fr["collisions"]:
            collision_frames += 1
        if fr["alarm_collisions"]:
            alarm_frames += 1

    annotation_meta = _build_annotation_meta(annotation_path, infer_w=infer_w, infer_h=infer_h)
    collision_meta = {
        "enabled": True,
        "alarm_min_consecutive_frames": alarm_min,
        "alarm_cooldown_frames": alarm_cd,
        "recomputed_at": datetime.now(timezone.utc).isoformat(),
        "recomputed_from_annotation": annotation_path.name,
        "skeleton_reused": True,
    }

    if locator.storage == STORAGE_V2_PARQUET:
        write_timeline_parquet(locator.path, frames)
        patch_v2_manifest(
            locator.path,
            {
                "annotation": annotation_meta,
                "collision": collision_meta,
            },
        )
    elif locator.storage == STORAGE_V1_JSON:
        data = dict(manifest)
        data["frames"] = frames
        data["annotation"] = annotation_meta
        data["collision"] = collision_meta
        with open(locator.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    else:
        raise ValueError(f"不支持的存储类型: {locator.storage}")

    stem = video_stem or annotation_path.stem
    if locator.path.is_dir():
        try:
            shutil.copy2(annotation_path, locator.path / "annotation.json")
        except OSError:
            pass
    persist_playback_annotation(
        annotation_path,
        video_stem=stem,
        pose_path=locator.path,
        source_video=str((annotation_meta.get("source_info") or {}).get("source_video") or ""),
    )

    sidecar = meta_path_for_record(locator.record_id, locator)
    if sidecar.is_file():
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
            if isinstance(meta, dict):
                meta["annotation_file"] = annotation_path.name
                meta["has_annotation"] = True
                sidecar.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            pass

    try:
        from record_index_store import refresh_record_summary

        refresh_record_summary(locator.record_id)
    except Exception:
        pass

    return {
        "record_id": locator.record_id,
        "frame_count": len(frames),
        "collision_frame_count": collision_frames,
        "alarm_frame_count": alarm_frames,
        "annotation_file": annotation_path.name,
        "storage": locator.storage,
    }


def recompute_records_collisions(
    record_ids: list[str],
    annotation_path: Path,
    *,
    locate_record,
    video_stem: str = "",
    alarm_min_consecutive_frames: int | None = None,
    alarm_cooldown_frames: int | None = None,
    resolve_annotation=None,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for rid in record_ids:
        rid = str(rid or "").strip()
        if not rid:
            continue
        locator = locate_record(rid)
        if not locator:
            errors.append({"record_id": rid, "error": "记录不存在"})
            continue
        ann_path = annotation_path
        if resolve_annotation is not None:
            try:
                ann_path = resolve_annotation(locator) or annotation_path
            except (OSError, ValueError, FileNotFoundError):
                ann_path = annotation_path
        if not ann_path or not ann_path.is_file():
            errors.append({"record_id": rid, "error": "未找到可用标注 JSON"})
            continue
        try:
            results.append(
                recompute_record_collisions(
                    locator,
                    ann_path,
                    video_stem=video_stem or ann_path.stem,
                    alarm_min_consecutive_frames=alarm_min_consecutive_frames,
                    alarm_cooldown_frames=alarm_cooldown_frames,
                )
            )
        except (OSError, ValueError, FileNotFoundError, RuntimeError) as exc:
            errors.append({"record_id": rid, "error": str(exc)})
    return {
        "status": "ok" if results else "failed",
        "recomputed": results,
        "errors": errors,
        "annotation_file": annotation_path.name,
    }
