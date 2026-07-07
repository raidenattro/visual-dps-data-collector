"""碰撞变体 sidecar：现场参数下 wrist / hand_extended 预计算 timeline。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from event_engine.annotation_boxes import load_scaled_boxes
from event_engine.collision_sim import (
    filter_pose_inference_frames,
    simulate_frame_events_from_frames,
    stored_pose_frame_interval,
)
from event_engine.wrist_hits import ProbeMode
from pose_store import (
    MANIFEST_FILE,
    RecordLocator,
    load_all_frames,
    load_manifest,
    patch_v2_manifest,
)

from api.accuracy_service import resolve_annotation_for_accuracy_record
from config_loader import resolve_app_paths

COLLISION_VARIANTS_SCHEMA = 1
TIMELINE_COLLISION_WRIST = "timeline_collision_wrist.parquet"
TIMELINE_COLLISION_HAND_EXT_010 = "timeline_collision_hand_ext_0.10.parquet"
TIMELINE_COLLISION_HAND_EXT_020 = "timeline_collision_hand_ext_0.20.parquet"
TIMELINE_COLLISION_HAND_EXT_030 = "timeline_collision_hand_ext_0.30.parquet"
TIMELINE_COLLISION_HAND_EXT_040 = "timeline_collision_hand_ext_0.40.parquet"

VARIANT_DEFS: tuple[tuple[str, ProbeMode, float | None, str], ...] = (
    ("wrist", "wrist", None, TIMELINE_COLLISION_WRIST),
    ("hand_ext_0.10", "hand_extended", 0.1, TIMELINE_COLLISION_HAND_EXT_010),
    ("hand_ext_0.20", "hand_extended", 0.2, TIMELINE_COLLISION_HAND_EXT_020),
    ("hand_ext_0.30", "hand_extended", 0.3, TIMELINE_COLLISION_HAND_EXT_030),
    ("hand_ext_0.40", "hand_extended", 0.4, TIMELINE_COLLISION_HAND_EXT_040),
)


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


def _timeline_row_from_event(frame: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    return {
        "frame_idx": int(frame.get("frame_idx") or 0),
        "source_frame_idx": int(frame.get("source_frame_idx") or frame.get("frame_idx") or 0),
        "timestamp_sec": float(frame.get("timestamp_sec") or 0.0),
        "infer_width": int(frame.get("infer_width") or 0),
        "infer_height": int(frame.get("infer_height") or 0),
        "collisions": list(event.get("collisions") or []),
        "alarm_collisions": list(event.get("alarm_collisions") or []),
    }


def write_collision_variant_timeline(path: Path, rows: list[dict[str, Any]]) -> None:
    pa, pq = _require_pyarrow()
    path = Path(path)
    if rows:
        pq.write_table(pa.Table.from_pylist(rows), path, compression="zstd")
        return
    pq.write_table(
        pa.table(
            {
                "frame_idx": pa.array([], type=pa.int32()),
                "source_frame_idx": pa.array([], type=pa.int32()),
                "timestamp_sec": pa.array([], type=pa.float64()),
                "infer_width": pa.array([], type=pa.int32()),
                "infer_height": pa.array([], type=pa.int32()),
                "collisions": pa.array([], type=pa.list_(pa.string())),
                "alarm_collisions": pa.array([], type=pa.list_(pa.string())),
            }
        ),
        path,
        compression="zstd",
    )


def collision_variants_meta(manifest: dict[str, Any]) -> dict[str, Any] | None:
    raw = manifest.get("collision_variants")
    return raw if isinstance(raw, dict) else None


def variant_file_for_key(manifest: dict[str, Any], variant_key: str) -> Path | None:
    meta = collision_variants_meta(manifest)
    if not meta:
        return None
    variants = meta.get("variants")
    if not isinstance(variants, dict):
        return None
    entry = variants.get(variant_key)
    if not isinstance(entry, dict):
        return None
    fname = str(entry.get("file") or "").strip()
    return Path(fname) if fname else None


def variants_available(locator: RecordLocator) -> dict[str, Any]:
    manifest = load_manifest(locator)
    meta = collision_variants_meta(manifest) or {}
    params = meta.get("params") if isinstance(meta.get("params"), dict) else {}
    out_variants: dict[str, Any] = {}
    for key, probe_mode, ext_ratio, fname in VARIANT_DEFS:
        path = locator.path / fname if locator.path.is_dir() else None
        exists = bool(path and path.is_file())
        out_variants[key] = {
            "file": fname,
            "probe_mode": probe_mode,
            "extension_ratio": ext_ratio,
            "available": exists,
        }
    return {
        "record_id": locator.record_id,
        "schema": meta.get("schema") or COLLISION_VARIANTS_SCHEMA,
        "params": params,
        "variants": out_variants,
        "generated_at": meta.get("generated_at"),
    }


def load_collision_variant_timeline(
    locator: RecordLocator,
    variant_key: str,
    *,
    include_events: bool = True,
) -> list[dict[str, Any]]:
    manifest = load_manifest(locator)
    meta = collision_variants_meta(manifest)
    if not meta:
        raise FileNotFoundError("记录未生成 collision_variants")

    variants = meta.get("variants")
    if not isinstance(variants, dict) or variant_key not in variants:
        raise ValueError(f"未知碰撞变体: {variant_key}")

    entry = variants[variant_key]
    if not isinstance(entry, dict):
        raise ValueError(f"碰撞变体配置无效: {variant_key}")

    fname = str(entry.get("file") or "").strip()
    if not fname:
        raise ValueError(f"碰撞变体无文件: {variant_key}")

    path = locator.path / fname
    if not path.is_file():
        raise FileNotFoundError(f"碰撞变体文件不存在: {fname}")

    _, pq = _require_pyarrow()
    rows = pq.read_table(path).to_pylist()
    out: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        row = {
            "frame_idx": int(r.get("frame_idx") or 0),
            "source_frame_idx": int(r.get("source_frame_idx") or r.get("frame_idx") or 0),
            "timestamp_sec": float(r.get("timestamp_sec") or 0.0),
            "infer_width": int(r.get("infer_width") or 0),
            "infer_height": int(r.get("infer_height") or 0),
        }
        if include_events:
            row["collisions"] = list(r.get("collisions") or [])
            row["alarm_collisions"] = list(r.get("alarm_collisions") or [])
        out.append(row)
    return out


def collision_variants_already_built(locator: RecordLocator) -> bool:
    if not locator.path.is_dir():
        return False
    for _key, _mode, _ratio, fname in VARIANT_DEFS:
        if not (locator.path / fname).is_file():
            return False
    manifest = load_manifest(locator)
    return collision_variants_meta(manifest) is not None


def build_collision_variants_for_record(
    locator: RecordLocator,
    *,
    pose_frame_interval: int = 2,
    alarm_min_consecutive_frames: int = 3,
    alarm_cooldown_frames: int = 0,
    skip_if_exists: bool = False,
    annotation_path: Path | None = None,
) -> dict[str, Any]:
    """为单条记录生成 wrist + hand_extended 碰撞变体 parquet。"""
    if skip_if_exists and collision_variants_already_built(locator):
        return {
            "status": "skipped",
            "record_id": locator.record_id,
            "reason": "already_built",
        }

    if not locator.path.is_dir():
        raise ValueError(f"仅 v2 记录包支持 collision_variants: {locator.record_id}")

    manifest = load_manifest(locator)
    all_frames = load_all_frames(locator)
    if not all_frames:
        raise ValueError(f"记录 {locator.record_id} 无帧数据")

    paths = resolve_app_paths()
    ann_path = annotation_path
    if ann_path is None or not ann_path.is_file():
        from config_loader import parse_record_path_segments

        tier, _slug, _record_name = parse_record_path_segments(locator.record_id)
        ann_path = resolve_annotation_for_accuracy_record(
            paths, locator, pose_tier=tier or "rtmpose-m"
        )
    if not ann_path or not ann_path.is_file():
        raise FileNotFoundError(f"未找到标注: {locator.record_id}")

    infer_w, infer_h = _infer_size_from_frames(all_frames, manifest)
    boxes = load_scaled_boxes(ann_path, infer_w, infer_h)
    if not boxes:
        raise ValueError(f"标注无有效货框: {locator.record_id}")

    stored_interval = stored_pose_frame_interval(manifest)
    frames = filter_pose_inference_frames(
        all_frames,
        pose_frame_interval,
        stored_interval=stored_interval,
    )
    fps = _video_fps(manifest)

    built: list[dict[str, Any]] = []
    variants_meta: dict[str, Any] = {}

    for key, probe_mode, ext_ratio, fname in VARIANT_DEFS:
        events = simulate_frame_events_from_frames(
            frames,
            boxes,
            alarm_min_consecutive_frames=alarm_min_consecutive_frames,
            alarm_cooldown_frames=alarm_cooldown_frames,
            video_fps=fps,
            probe_mode=probe_mode,
            extension_ratio=float(ext_ratio) if ext_ratio is not None else 0.4,
        )
        rows = [
            _timeline_row_from_event(fr, ev)
            for fr, ev in zip(frames, events)
        ]
        out_path = locator.path / fname
        write_collision_variant_timeline(out_path, rows)
        alarm_frames = sum(1 for r in rows if r.get("alarm_collisions"))
        built.append({
            "variant": key,
            "file": fname,
            "frame_count": len(rows),
            "alarm_frame_count": alarm_frames,
        })
        entry: dict[str, Any] = {
            "file": fname,
            "probe_mode": probe_mode,
            "frame_count": len(rows),
            "alarm_frame_count": alarm_frames,
        }
        if ext_ratio is not None:
            entry["extension_ratio"] = ext_ratio
        variants_meta[key] = entry

    params = {
        "pose_frame_interval": max(1, int(pose_frame_interval)),
        "alarm_min_consecutive_frames": max(1, int(alarm_min_consecutive_frames)),
        "alarm_cooldown_frames": max(0, int(alarm_cooldown_frames)),
    }
    collision_variants_doc = {
        "schema": COLLISION_VARIANTS_SCHEMA,
        "params": params,
        "variants": variants_meta,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "skeleton_reused": True,
        "annotation_file": ann_path.name,
        "stored_pose_frame_interval": stored_interval,
        "frame_count_source": len(all_frames),
        "frame_count_used": len(frames),
    }

    files = dict(manifest.get("files") or {})
    for key, _mode, _ratio, fname in VARIANT_DEFS:
        files[f"collision_{key}"] = fname

    patch_v2_manifest(
        locator.path,
        {
            "files": files,
            "collision_variants": collision_variants_doc,
        },
    )

    return {
        "status": "ok",
        "record_id": locator.record_id,
        "annotation_file": ann_path.name,
        "frame_count_used": len(frames),
        "variants": built,
        "params": params,
    }
