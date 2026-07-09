#!/usr/bin/env python3
"""从记录包 timeline 只读重算前置速度过滤碰撞，导出上传评估目录。

不写回记录包；唯一输出 localdata/export/rule-speed-prefilter-prod-test/

用法（项目根目录）:
  python scripts/data/export_prefilter_upload.py
  python scripts/data/export_prefilter_upload.py --threshold 50
  python scripts/data/evaluate_inference_upload.py \\
    --dirs localdata/export/rule-baseline-prod-test \\
             localdata/export/rule-speed-prefilter-prod-test --in-place
"""

from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.data.evaluate_combo1_segment_filter import _ensure_cv2_point_polygon_test

_ensure_cv2_point_polygon_test()

from config_loader import resolve_config_path
from event_engine.speed_filter import export_frame_indices
from event_engine.speed_gated_collision import SpeedGateConfig, recompute_prefilter_upload_frames
from pose_store import load_all_frames, load_manifest

from api.inference_eval_service import load_inference_json_file
from api.record_service import locate_record_by_id
from api.wrist_features_service import _infer_size_from_frames, _load_boxes_for_wrist_features, _video_fps

DEFAULT_BASELINE_MANIFEST = ROOT / "localdata/export/rule-baseline-prod-test/_manifest.json"
DEFAULT_OUTPUT = ROOT / "localdata/export/rule-speed-prefilter-prod-test"
MANIFEST_NAME = "_manifest.json"

POSE_FRAME_INTERVAL = 2
ALARM_MIN_CONSECUTIVE = 3
ALARM_COOLDOWN = 0
DEFAULT_THRESHOLD = 60.0


def _load_baseline_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"缺少 baseline manifest: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_baseline_clip_path(baseline_dir: Path, entry: dict[str, Any]) -> Path | None:
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


def _export_indices_for_record(
    entry: dict[str, Any],
    *,
    baseline_dir: Path,
    timeline_frames: list[dict[str, Any]],
    pose_frame_interval: int,
) -> set[int]:
    clip_path = _resolve_baseline_clip_path(baseline_dir, entry)
    if clip_path is not None:
        upload_frames = load_inference_json_file(clip_path)
        indices = export_frame_indices(upload_frames)
        if indices:
            return indices

    # 回退：按 timeline 范围等间隔抽帧
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


def _process_record(
    entry: dict[str, Any],
    *,
    baseline_dir: Path,
    output_dir: Path,
    speed_gate: SpeedGateConfig,
    pose_frame_interval: int,
    alarm_min: int,
    alarm_cooldown: int,
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

    export_indices = _export_indices_for_record(
        entry,
        baseline_dir=baseline_dir,
        timeline_frames=timeline_frames,
        pose_frame_interval=pose_frame_interval,
    )
    if not export_indices:
        return {"record_id": record_id, "status": "error", "error": "无法确定导出抽帧索引"}

    upload_rows = recompute_prefilter_upload_frames(
        timeline_frames,
        export_indices,
        boxes,
        record_id=record_id,
        speed_gate=speed_gate,
        infer_width=infer_w,
        infer_height=infer_h,
        video_fps=fps,
        alarm_min_consecutive_frames=alarm_min,
        alarm_cooldown_frames=alarm_cooldown,
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


def _build_output_manifest(
    baseline_manifest: dict[str, Any],
    *,
    output_dir: Path,
    baseline_dir: Path,
    speed_gate: SpeedGateConfig,
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    params = deepcopy(baseline_manifest.get("params") or {})
    params["collision_engine"] = "speed_gated_box_human_det_infer"
    params["speed_filter"] = {
        "enabled": True,
        "stage": "prefilter",
        "feature": speed_gate.feature,
        "max_threshold": speed_gate.max_threshold,
        "fail_open": speed_gate.fail_open,
        "writeback_timeline": False,
    }
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


def main() -> int:
    parser = argparse.ArgumentParser(description="前置速度过滤：只读 timeline 重算并导出 upload 目录")
    parser.add_argument("--baseline-manifest", type=Path, default=DEFAULT_BASELINE_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--feature", default="lower_mean_speed")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--pose-frame-interval", type=int, default=POSE_FRAME_INTERVAL)
    parser.add_argument("--alarm-min", type=int, default=ALARM_MIN_CONSECUTIVE)
    parser.add_argument("--alarm-cooldown", type=int, default=ALARM_COOLDOWN)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    resolve_config_path(None)
    baseline_manifest_path = args.baseline_manifest.resolve()
    baseline_dir = baseline_manifest_path.parent
    output_dir = args.output_dir.resolve()

    baseline_manifest = _load_baseline_manifest(baseline_manifest_path)
    records = list(baseline_manifest.get("records") or [])
    if not records:
        print("baseline manifest 无 records")
        return 2

    speed_gate = SpeedGateConfig(
        feature=args.feature,
        max_threshold=args.threshold,
        fail_open=True,
    )

    if args.dry_run:
        print(f"将处理 {len(records)} 条记录（只读 timeline，不写回记录包）")
        print(f"输出: {output_dir}")
        print(f"规则: 帧级 {speed_gate.feature} ≤ {speed_gate.max_threshold}")
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for entry in records:
        rid = str(entry.get("record_id") or "")
        res = _process_record(
            entry,
            baseline_dir=baseline_dir,
            output_dir=output_dir,
            speed_gate=speed_gate,
            pose_frame_interval=args.pose_frame_interval,
            alarm_min=args.alarm_min,
            alarm_cooldown=args.alarm_cooldown,
        )
        results.append(res)
        if res.get("status") == "ok":
            print(f"{rid}: exported={res.get('frame_count_exported')} picking={res.get('picking_frame_count')}")
        else:
            print(f"{rid}: ERROR {res.get('error')}")

    out_manifest = _build_output_manifest(
        baseline_manifest,
        output_dir=output_dir,
        baseline_dir=baseline_dir,
        speed_gate=speed_gate,
        results=results,
    )
    (output_dir / MANIFEST_NAME).write_text(
        json.dumps(out_manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    ok_n = out_manifest.get("exported_count", 0)
    err_n = out_manifest.get("error_count", 0)
    summary = out_manifest.get("summary") or {}
    print(
        f"\n完成: {ok_n}/{len(records)} ok, {err_n} errors\n"
        f"picking帧合计: {summary.get('picking_frames')}\n"
        f"输出: {output_dir}"
    )
    return 0 if err_n == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
