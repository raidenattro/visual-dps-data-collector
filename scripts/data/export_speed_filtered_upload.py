#!/usr/bin/env python3
"""将速度过滤后的推测结果导出为上传评估目录（格式同 rule-baseline-prod-test）。

保守默认：lower_mean_speed_p50 ≤ 60

用法（项目根目录）:
  python scripts/data/export_speed_filtered_upload.py
  python scripts/data/export_speed_filtered_upload.py \\
    --input-dir localdata/export/rule-baseline-prod-test \\
    --output-dir localdata/export/rule-speed-lower60-prod-test
  python scripts/data/evaluate_inference_upload.py \\
    --dirs localdata/export/rule-baseline-prod-test \\
             localdata/export/rule-speed-lower60-prod-test --in-place
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
from event_engine.speed_filter import (
    DEFAULT_SPEED_FEATURE,
    DEFAULT_SPEED_THRESHOLD,
    build_motion_segments_for_upload,
    filter_upload_frames_by_speed,
)
from api.inference_eval_service import load_inference_json_file
from api.record_service import locate_record_by_id

DEFAULT_INPUT = ROOT / "localdata/export/rule-baseline-prod-test"
DEFAULT_OUTPUT = ROOT / "localdata/export/rule-speed-lower60-prod-test"
MANIFEST_NAME = "_manifest.json"


def _load_input_manifest(input_dir: Path) -> dict[str, Any]:
    path = input_dir / MANIFEST_NAME
    if not path.is_file():
        raise FileNotFoundError(f"缺少 manifest: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_clip_path(input_dir: Path, entry: dict[str, Any]) -> Path:
    file_name = str(entry.get("file") or "").strip()
    if file_name:
        p = input_dir / file_name
        if p.is_file():
            return p
    path_field = str(entry.get("path") or "").strip()
    if path_field:
        p = Path(path_field)
        if p.is_file():
            return p
    clip_name = str(entry.get("clip_name") or "").strip()
    if clip_name:
        p = input_dir / f"{clip_name}.json" if not clip_name.endswith(".json") else input_dir / clip_name
        if p.is_file():
            return p
    raise FileNotFoundError(f"找不到 clip 文件: {entry.get('record_id')}")


def _process_record(
    entry: dict[str, Any],
    *,
    input_dir: Path,
    output_dir: Path,
    feature: str,
    max_threshold: float,
) -> dict[str, Any]:
    record_id = str(entry.get("record_id") or "").strip()
    try:
        clip_path = _resolve_clip_path(input_dir, entry)
    except FileNotFoundError as exc:
        return {"record_id": record_id, "status": "error", "error": str(exc)}

    loc = locate_record_by_id(record_id)
    if not loc:
        return {"record_id": record_id, "status": "error", "error": "本地记录不存在"}

    upload_frames = load_inference_json_file(clip_path)
    motion_segments = build_motion_segments_for_upload(loc, upload_frames)
    if not motion_segments:
        return {"record_id": record_id, "status": "error", "error": "无法构建运动碰撞段"}

    filtered_frames, meta = filter_upload_frames_by_speed(
        upload_frames,
        motion_segments,
        feature=feature,
        max_threshold=max_threshold,
    )

    out_name = str(entry.get("file") or clip_path.name)
    out_path = output_dir / out_name
    out_path.write_text(json.dumps(filtered_frames, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "status": "ok",
        "record_id": record_id,
        "clip_name": entry.get("clip_name") or out_path.stem,
        "camera_slug": entry.get("camera_slug"),
        "file": out_name,
        "path": str(out_path.resolve()),
        "frame_count_exported": len(filtered_frames),
        "picking_frame_count_before": meta.get("picking_before"),
        "picking_frame_count": meta.get("picking_after"),
        "alarms_before": meta.get("alarms_before"),
        "alarms_after": meta.get("alarms_after"),
        "alarms_dropped": meta.get("alarms_dropped"),
        "annotation_file": entry.get("annotation_file"),
        "infer_width": entry.get("infer_width"),
        "infer_height": entry.get("infer_height"),
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
    input_manifest: dict[str, Any],
    *,
    output_dir: Path,
    feature: str,
    max_threshold: float,
    input_dir: Path,
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    params = deepcopy(input_manifest.get("params") or {})
    params["speed_filter"] = {
        "enabled": True,
        "feature": feature,
        "max_threshold": max_threshold,
        "source_dir": str(input_dir),
        "fail_open": True,
    }
    ok = [r for r in results if r.get("status") == "ok"]
    err = [r for r in results if r.get("status") == "error"]
    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "source_manifest": str((input_dir / MANIFEST_NAME).resolve()),
        "params": params,
        "record_count": len(results),
        "exported_count": len(ok),
        "error_count": len(err),
        "records": results,
        "summary": {
            "picking_frames_before": sum(int(r.get("picking_frame_count_before") or 0) for r in ok),
            "picking_frames_after": sum(int(r.get("picking_frame_count") or 0) for r in ok),
            "alarms_dropped": sum(int(r.get("alarms_dropped") or 0) for r in ok),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="导出速度过滤后的上传推测 JSON 目录")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--feature", default=DEFAULT_SPEED_FEATURE)
    parser.add_argument("--threshold", type=float, default=DEFAULT_SPEED_THRESHOLD)
    parser.add_argument("--dry-run", action="store_true", help="仅打印将处理的记录数")
    args = parser.parse_args()

    resolve_config_path(None)
    input_dir = args.input_dir.resolve()
    output_dir = args.output_dir.resolve()

    if not input_dir.is_dir():
        print(f"输入目录不存在: {input_dir}")
        return 2

    manifest = _load_input_manifest(input_dir)
    records = list(manifest.get("records") or [])
    if not records:
        print("manifest 无 records")
        return 2

    if args.dry_run:
        print(f"将处理 {len(records)} 条记录")
        print(f"输入: {input_dir}")
        print(f"输出: {output_dir}")
        print(f"规则: {args.feature} ≤ {args.threshold}")
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for entry in records:
        rid = str(entry.get("record_id") or "")
        res = _process_record(
            entry,
            input_dir=input_dir,
            output_dir=output_dir,
            feature=args.feature,
            max_threshold=args.threshold,
        )
        results.append(res)
        if res.get("status") == "ok":
            print(
                f"{rid}: picking {res.get('picking_frame_count_before')}→"
                f"{res.get('picking_frame_count')} dropped={res.get('alarms_dropped')}"
            )
        else:
            print(f"{rid}: ERROR {res.get('error')}")

    out_manifest = _build_output_manifest(
        manifest,
        output_dir=output_dir,
        feature=args.feature,
        max_threshold=args.threshold,
        input_dir=input_dir,
        results=results,
    )
    manifest_path = output_dir / MANIFEST_NAME
    manifest_path.write_text(json.dumps(out_manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    ok_n = out_manifest.get("exported_count", 0)
    err_n = out_manifest.get("error_count", 0)
    summary = out_manifest.get("summary") or {}
    print(
        f"\n完成: {ok_n}/{len(records)} ok, {err_n} errors\n"
        f"picking帧: {summary.get('picking_frames_before')} → {summary.get('picking_frames_after')}\n"
        f"告警丢弃: {summary.get('alarms_dropped')}\n"
        f"输出: {output_dir}"
    )
    return 0 if err_n == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
