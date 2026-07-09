#!/usr/bin/env python3
"""前置速度过滤 28 条验证：帧级阈值网格 + 与 baseline/后置对比。

只读 timeline，内存重算，不写回记录包。

用法（项目根目录）:
  python scripts/data/validate_prefilter28.py
  python scripts/data/export_prefilter_upload.py --threshold 50
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.data.evaluate_combo1_segment_filter import _ensure_cv2_point_polygon_test

_ensure_cv2_point_polygon_test()

from config_loader import resolve_app_paths, resolve_config_path
from event_engine.box_identity import token_matches_any
from event_engine.speed_filter import export_frame_indices
from event_engine.speed_gated_collision import SpeedGateConfig, recompute_prefilter_upload_frames
from event_engine.skeleton_features import extract_subsampled_velocity_from_frames
from pose_store import load_all_frames, load_manifest

from api.accuracy_service import GroundTruthSegment, build_ground_truth_segments
from api.inference_eval_service import (
    UploadClipInput,
    aggregate_upload_clip_results,
    evaluate_uploaded_clip,
    load_inference_json_file,
)
from api.record_service import locate_record_by_id
from api.wrist_features_service import _infer_size_from_frames, _load_boxes_for_wrist_features, _video_fps
from scripts.data.analyze_skeleton_velocity_discrimination import (
    LOWER_THRESHOLDS,
    _float_or_none,
    _load_ground_truth_segments,
    _threshold_scan_le,
)
from scripts.data.export_prefilter_upload import (
    _export_indices_for_record,
    _resolve_baseline_clip_path,
)

BASELINE_MANIFEST = ROOT / "localdata/export/rule-baseline-prod-test/_manifest.json"
BASELINE_DIR = BASELINE_MANIFEST.parent
OUT_JSON = ROOT / "localdata/export/rule-speed-prefilter-prod-test/prefilter_threshold_scan.json"

FRAME_THRESHOLDS = [30, 40, 50, 60, 70, 80, 100, 120]
POSE_FRAME_INTERVAL = 2
ALARM_MIN = 3
ALARM_COOLDOWN = 0


def _load_manifest_records() -> list[dict[str, Any]]:
    manifest = json.loads(BASELINE_MANIFEST.read_text(encoding="utf-8"))
    return list(manifest.get("records") or [])


def _prepare_record(entry: dict[str, Any]) -> dict[str, Any] | None:
    record_id = str(entry.get("record_id") or "").strip()
    loc = locate_record_by_id(record_id)
    if not loc:
        return {"record_id": record_id, "error": "记录不存在"}

    timeline_frames = load_all_frames(loc)
    if not timeline_frames:
        return {"record_id": record_id, "error": "无帧数据"}

    manifest = load_manifest(loc)
    infer_w, infer_h = _infer_size_from_frames(timeline_frames, manifest)
    fps = _video_fps(manifest)
    boxes, _, _ = _load_boxes_for_wrist_features(loc, manifest, infer_w=infer_w, infer_h=infer_h)
    if not boxes:
        return {"record_id": record_id, "error": "无货框标注"}

    export_indices = _export_indices_for_record(
        entry,
        baseline_dir=BASELINE_DIR,
        timeline_frames=timeline_frames,
        pose_frame_interval=POSE_FRAME_INTERVAL,
    )
    if not export_indices:
        return {"record_id": record_id, "error": "无导出抽帧索引"}

    gt_segments, _ = _load_ground_truth_segments(loc)
    if not gt_segments:
        return {"record_id": record_id, "error": "无标真"}

    clip_path = _resolve_baseline_clip_path(BASELINE_DIR, entry)
    baseline_frames = load_inference_json_file(clip_path) if clip_path else []

    velocity_rows = extract_subsampled_velocity_from_frames(
        timeline_frames,
        export_indices,
        infer_width=infer_w,
        infer_height=infer_h,
        video_fps=fps,
    )

    return {
        "record_id": record_id,
        "entry": entry,
        "locator": loc,
        "timeline_frames": timeline_frames,
        "boxes": boxes,
        "infer_w": infer_w,
        "infer_h": infer_h,
        "fps": fps,
        "export_indices": export_indices,
        "gt_segments": gt_segments,
        "baseline_frames": baseline_frames,
        "velocity_rows": velocity_rows,
        "upload_file": str(entry.get("file") or f"{entry.get('clip_name')}.json"),
    }


def _false_alarm_frames(
    frames: list[dict[str, Any]],
    gt_segments: list[GroundTruthSegment],
) -> set[int]:
    out: set[int] = set()
    for fr in frames:
        if not fr.get("is_picking"):
            continue
        fi = int(fr.get("frame_idx") or 0)
        tokens = list(fr.get("rule_alarm_collisions") or fr.get("rule_collisions") or [])
        covered = False
        for gt in gt_segments:
            if fi < gt.frame_start or fi > gt.frame_end:
                continue
            if not tokens:
                covered = True
                break
            if token_matches_any(str(tokens[0]), list(gt.gt_tokens)):
                covered = True
                break
        if not covered:
            out.add(fi)
    return out


def _gt_overlap_frames(
    frames: list[dict[str, Any]],
    gt_segments: list[GroundTruthSegment],
) -> set[int]:
    out: set[int] = set()
    for fr in frames:
        fi = int(fr.get("frame_idx") or 0)
        for gt in gt_segments:
            if gt.frame_start <= fi <= gt.frame_end:
                out.add(fi)
                break
    return out


def _frame_lower_speeds(
    velocity_rows: list[dict[str, Any]],
    frame_set: set[int],
) -> list[float]:
    vals: list[float] = []
    for row in velocity_rows:
        fi = int(row.get("frame_idx") or 0)
        if fi not in frame_set:
            continue
        v = _float_or_none(row.get("lower_mean_speed"))
        if v is not None:
            vals.append(v)
    return vals


def _evaluate_frames(
    paths,
    *,
    record_id: str,
    upload_file: str,
    frames: list[dict[str, Any]],
) -> dict[str, Any]:
    clip = UploadClipInput(upload_file=upload_file, frames=frames, record_id=record_id)
    return evaluate_uploaded_clip(paths, clip)


def _summary_from_clips(clips: list[dict[str, Any]]) -> dict[str, Any]:
    return aggregate_upload_clip_results(clips)


def main() -> int:
    resolve_config_path(None)
    paths = resolve_app_paths()
    records = _load_manifest_records()
    if not records:
        print("manifest 无 records")
        return 2

    prepared: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for entry in records:
        ctx = _prepare_record(entry)
        if not ctx or ctx.get("error"):
            rid = str(entry.get("record_id") or "")
            err = (ctx or {}).get("error") or "unknown"
            errors.append({"record_id": rid, "error": err})
            print(f"{rid}: SKIP {err}")
            continue
        prepared.append(ctx)
        print(f"{ctx['record_id']}: ok export={len(ctx['export_indices'])}")

    if not prepared:
        print("无可用记录")
        return 2

    # baseline 评估（磁盘 clip）
    baseline_clip_results: list[dict[str, Any]] = []
    for ctx in prepared:
        if ctx.get("baseline_frames"):
            baseline_clip_results.append(
                _evaluate_frames(
                    paths,
                    record_id=ctx["record_id"],
                    upload_file=ctx["upload_file"],
                    frames=ctx["baseline_frames"],
                )
            )
    baseline_summary = _summary_from_clips(baseline_clip_results)

    # 无速度门控（内存重算，对照抽帧碰撞是否一致）
    no_gate_clips: list[dict[str, Any]] = []
    for ctx in prepared:
        frames = recompute_prefilter_upload_frames(
            ctx["timeline_frames"],
            ctx["export_indices"],
            ctx["boxes"],
            record_id=ctx["record_id"],
            speed_gate=SpeedGateConfig(max_threshold=1e9, fail_open=True),
            infer_width=ctx["infer_w"],
            infer_height=ctx["infer_h"],
            video_fps=ctx["fps"],
            alarm_min_consecutive_frames=ALARM_MIN,
            alarm_cooldown_frames=ALARM_COOLDOWN,
        )
        no_gate_clips.append(
            _evaluate_frames(
                paths,
                record_id=ctx["record_id"],
                upload_file=ctx["upload_file"],
                frames=frames,
            )
        )
    no_gate_summary = _summary_from_clips(no_gate_clips)

    # 帧级 lower_mean_speed 分布（标真重叠帧 vs baseline 误报帧）
    gt_speeds: list[float] = []
    fa_speeds: list[float] = []
    for ctx in prepared:
        gt_frames = _gt_overlap_frames(ctx.get("baseline_frames") or [], ctx["gt_segments"])
        fa_frames = _false_alarm_frames(ctx.get("baseline_frames") or [], ctx["gt_segments"])
        gt_speeds.extend(_frame_lower_speeds(ctx["velocity_rows"], gt_frames))
        fa_speeds.extend(_frame_lower_speeds(ctx["velocity_rows"], fa_frames))

    frame_discrimination = {
        "gt_overlap_frame_lower_mean_speed": {
            "count": len(gt_speeds),
            "p50": sorted(gt_speeds)[len(gt_speeds) // 2] if gt_speeds else None,
        },
        "false_alarm_frame_lower_mean_speed": {
            "count": len(fa_speeds),
            "p50": sorted(fa_speeds)[len(fa_speeds) // 2] if fa_speeds else None,
        },
        "frame_threshold_scan": _threshold_scan_le(gt_speeds, fa_speeds, LOWER_THRESHOLDS),
    }

    # 前置阈值网格（内存评估）
    threshold_grid: list[dict[str, Any]] = []
    for thr in FRAME_THRESHOLDS:
        clip_results: list[dict[str, Any]] = []
        for ctx in prepared:
            frames = recompute_prefilter_upload_frames(
                ctx["timeline_frames"],
                ctx["export_indices"],
                ctx["boxes"],
                record_id=ctx["record_id"],
                speed_gate=SpeedGateConfig(feature="lower_mean_speed", max_threshold=thr, fail_open=True),
                infer_width=ctx["infer_w"],
                infer_height=ctx["infer_h"],
                video_fps=ctx["fps"],
                alarm_min_consecutive_frames=ALARM_MIN,
                alarm_cooldown_frames=ALARM_COOLDOWN,
            )
            clip_results.append(
                _evaluate_frames(
                    paths,
                    record_id=ctx["record_id"],
                    upload_file=ctx["upload_file"],
                    frames=frames,
                )
            )
        summary = _summary_from_clips(clip_results)
        threshold_grid.append({
            "max_threshold": thr,
            "feature": "lower_mean_speed",
            "detected": summary.get("detected"),
            "missed": summary.get("missed"),
            "false_alarms": summary.get("false_alarms"),
            "recall": summary.get("recall"),
            "precision_proxy": summary.get("precision_proxy"),
        })
        print(
            f"threshold={thr}: TP={summary.get('detected')} "
            f"FP={summary.get('false_alarms')} recall={summary.get('recall')}"
        )

    # 选保守默认：召回率 >= 0.90 下 FP 最小
    best_thr = FRAME_THRESHOLDS[0]
    best_fp = 10**9
    for row in threshold_grid:
        rec = float(row.get("recall") or 0)
        fp = int(row.get("false_alarms") or 0)
        if rec >= 0.90 and fp < best_fp:
            best_fp = fp
            best_thr = float(row["max_threshold"])

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "record_count": len(records),
        "prepared_count": len(prepared),
        "errors": errors,
        "params": {
            "pose_frame_interval": POSE_FRAME_INTERVAL,
            "alarm_min_consecutive_frames": ALARM_MIN,
            "alarm_cooldown_frames": ALARM_COOLDOWN,
            "speed_filter_stage": "prefilter",
            "writeback_timeline": False,
        },
        "baseline_disk_summary": baseline_summary,
        "recompute_no_gate_summary": no_gate_summary,
        "frame_discrimination": frame_discrimination,
        "prefilter_threshold_grid": threshold_grid,
        "recommended_threshold": {
            "max_threshold": best_thr,
            "criterion": "recall>=0.90 下最小 FP",
        },
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nBaseline: TP={baseline_summary.get('detected')} FP={baseline_summary.get('false_alarms')} "
          f"recall={baseline_summary.get('recall')}")
    print(f"No gate:  TP={no_gate_summary.get('detected')} FP={no_gate_summary.get('false_alarms')} "
          f"recall={no_gate_summary.get('recall')}")
    print(f"推荐阈值: {best_thr}")
    print(f"输出: {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
