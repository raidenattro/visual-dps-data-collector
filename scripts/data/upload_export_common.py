"""上传评估目录导出：manifest28 记录重算公共逻辑。"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from event_engine.speed_filter import export_frame_indices
from pose_store import load_all_frames, load_manifest

from api.inference_eval_service import load_inference_json_file
from api.record_service import locate_record_by_id
from api.wrist_features_service import _infer_size_from_frames, _load_boxes_for_wrist_features, _video_fps

MANIFEST_NAME = "_manifest.json"

POSE_FRAME_INTERVAL = 2
ALARM_MIN_CONSECUTIVE = 3
ALARM_COOLDOWN = 0


def load_baseline_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"缺少 baseline manifest: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_baseline_clip_path(baseline_dir: Path, entry: dict[str, Any]) -> Path | None:
    file_name = str(entry.get("file") or "").strip()
    if file_name:
        p = baseline_dir / file_name
        if p.is_file():
            return p
    path_field = str(entry.get("path") or "").strip()
    if path_field:
        p = Path(path_field)
        if p.is_file():
            return p
    clip_name = str(entry.get("clip_name") or "").strip()
    if clip_name:
        name = clip_name if clip_name.endswith(".json") else f"{clip_name}.json"
        p = baseline_dir / name
        if p.is_file():
            return p
    return None


def export_indices_for_record(
    entry: dict[str, Any],
    *,
    baseline_dir: Path,
    timeline_frames: list[dict[str, Any]],
    pose_frame_interval: int,
) -> set[int]:
    clip_path = resolve_baseline_clip_path(baseline_dir, entry)
    if clip_path is not None:
        upload_frames = load_inference_json_file(clip_path)
        indices = export_frame_indices(upload_frames)
        if indices:
            return indices

    indices: list[int] = []
    for fr in timeline_frames:
        if not isinstance(fr, dict):
            continue
        idx = int(fr.get("source_frame_idx") or fr.get("frame_idx") or 0)
        if idx > 0:
            indices.append(idx)
    if not indices:
        return set()
    min_idx = min(indices)
    interval = max(1, int(pose_frame_interval))
    return {i for i in indices if (i - min_idx) % interval == 0}


def process_record_upload_export(
    entry: dict[str, Any],
    *,
    baseline_dir: Path,
    output_dir: Path,
    recompute_fn: Callable[..., list[dict[str, Any]]],
    recompute_kwargs: dict[str, Any],
    pose_frame_interval: int = POSE_FRAME_INTERVAL,
) -> dict[str, Any]:
    record_id = str(entry.get("record_id") or "").strip()
    loc = locate_record_by_id(record_id)
    if not loc:
        return {"record_id": record_id, "status": "error", "error": "本地记录不存在"}

    timeline_frames = load_all_frames(loc)
    if not timeline_frames:
        return {"record_id": record_id, "status": "error", "error": "记录无帧数据"}

    manifest = load_manifest(loc)
    infer_w, infer_h = _infer_size_from_frames(timeline_frames, manifest)
    fps = _video_fps(manifest)
    boxes, _, _ = _load_boxes_for_wrist_features(loc, manifest, infer_w=infer_w, infer_h=infer_h)
    if not boxes:
        return {"record_id": record_id, "status": "error", "error": "无货框标注"}

    export_indices = export_indices_for_record(
        entry,
        baseline_dir=baseline_dir,
        timeline_frames=timeline_frames,
        pose_frame_interval=pose_frame_interval,
    )
    if not export_indices:
        return {"record_id": record_id, "status": "error", "error": "无法确定导出抽帧索引"}

    upload_rows = recompute_fn(
        timeline_frames,
        export_indices,
        boxes,
        record_id=record_id,
        infer_width=infer_w,
        infer_height=infer_h,
        video_fps=fps,
        **recompute_kwargs,
    )
    if not upload_rows:
        return {"record_id": record_id, "status": "error", "error": "重算无有效导出帧"}

    out_name = str(entry.get("file") or f"{entry.get('clip_name') or record_id}.json")
    if not out_name.endswith(".json"):
        out_name = f"{out_name}.json"
    out_path = output_dir / out_name
    out_path.write_text(json.dumps(upload_rows, ensure_ascii=False, indent=2), encoding="utf-8")

    picking_count = sum(1 for r in upload_rows if r.get("is_picking"))
    return {
        "status": "ok",
        "record_id": record_id,
        "clip_name": entry.get("clip_name") or out_path.stem,
        "camera_slug": entry.get("camera_slug"),
        "file": out_name,
        "path": str(out_path.resolve()),
        "frame_count_exported": len(upload_rows),
        "picking_frame_count": picking_count,
        "annotation_file": entry.get("annotation_file"),
        "infer_width": entry.get("infer_width") or infer_w,
        "infer_height": entry.get("infer_height") or infer_h,
        **{k: entry[k] for k in (
            "frame_count_timeline",
            "frame_count_skeleton",
            "frame_range_min",
            "frame_range_max",
            "stored_pose_frame_interval",
            "infer_size_record_dir",
        ) if k in entry},
    }


def build_output_manifest(
    baseline_manifest: dict[str, Any],
    *,
    baseline_dir: Path,
    results: list[dict[str, Any]],
    params_patch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params = deepcopy(baseline_manifest.get("params") or {})
    if params_patch:
        params.update(params_patch)
    ok = [r for r in results if r.get("status") == "ok"]
    err = [r for r in results if r.get("status") == "error"]
    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "source_manifest": str((baseline_dir / MANIFEST_NAME).resolve()),
        "params": params,
        "record_count": len(results),
        "exported_count": len(ok),
        "error_count": len(err),
        "records": results,
        "summary": {
            "picking_frames": sum(int(r.get("picking_frame_count") or 0) for r in ok),
        },
    }
