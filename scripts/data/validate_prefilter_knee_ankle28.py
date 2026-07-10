#!/usr/bin/env python3
"""前置门控特征对比：lower_mean_speed（髋膝踝平均）vs knee_ankle_mean_speed（膝踝平均）。

只读 timeline，内存重算，不写回记录包。
参数与先前一致：pose_frame_interval=2, alarm_min=3, cooldown=0, threshold=60。

用法（项目根目录）:
  python scripts/data/validate_prefilter_knee_ankle28.py
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
from event_engine.speed_gated_collision import SpeedGateConfig, recompute_prefilter_upload_frames
from event_engine.skeleton_features import extract_subsampled_velocity_from_frames
from pose_store import load_all_frames, load_manifest

from api.accuracy_service import GroundTruthSegment
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
from scripts.data.upload_export_common import (
    export_indices_for_record,
    load_baseline_manifest,
    resolve_baseline_clip_path,
)

LOCAL_BASELINE_MANIFEST = ROOT / "localdata/export/rule-baseline-local-prod-test/_manifest.json"
LOCAL_BASELINE_DIR = LOCAL_BASELINE_MANIFEST.parent

OUT_JSON = ROOT / "docs/prefilter-knee-ankle-experiment.json"
OUT_MD = ROOT / "docs/prefilter-knee-ankle-speed-gate.md"

POSE_FRAME_INTERVAL = 2
ALARM_MIN = 3
ALARM_COOLDOWN = 0
DEFAULT_THRESHOLD = 60.0
FRAME_THRESHOLDS = [30, 40, 50, 60, 70, 80, 100, 120]

FEATURES = (
    ("lower_mean_speed", "髋+膝+踝 6 点算术平均"),
    ("knee_ankle_mean_speed", "膝+踝 4 点算术平均（不含髋）"),
)

# 昨日前置漏报重点 clip（相对 local baseline）
SQUAT_WATCH_CLIPS = (
    "clip_0009_start_00-37-59_rtmpose_m.json",
    "clip_0013_start_00-42-48_rtmpose_m.json",
    "clip_0020_start_00-48-44_rtmpose_m.json",
    "clip_0013_start_00-29-53_rtmpose_m.json",
)


def _load_manifest_records() -> list[dict[str, Any]]:
    manifest = load_baseline_manifest(LOCAL_BASELINE_MANIFEST)
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

    export_indices = export_indices_for_record(
        entry,
        baseline_dir=LOCAL_BASELINE_DIR,
        timeline_frames=timeline_frames,
        pose_frame_interval=POSE_FRAME_INTERVAL,
    )
    if not export_indices:
        return {"record_id": record_id, "error": "无导出抽帧索引"}

    gt_segments, _ = _load_ground_truth_segments(loc)
    if not gt_segments:
        return {"record_id": record_id, "error": "无标真"}

    clip_path = resolve_baseline_clip_path(LOCAL_BASELINE_DIR, entry)
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


def _frame_feature_speeds(
    velocity_rows: list[dict[str, Any]],
    frame_set: set[int],
    feature: str,
) -> list[float]:
    vals: list[float] = []
    for row in velocity_rows:
        fi = int(row.get("frame_idx") or 0)
        if fi not in frame_set:
            continue
        v = _float_or_none(row.get(feature))
        if v is not None:
            vals.append(v)
    return vals


def _p50(vals: list[float]) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    return s[len(s) // 2]


def _evaluate_frames(paths, *, record_id: str, upload_file: str, frames: list[dict[str, Any]]) -> dict[str, Any]:
    clip = UploadClipInput(upload_file=upload_file, frames=frames, record_id=record_id)
    return evaluate_uploaded_clip(paths, clip)


def _recompute_and_eval(
    paths,
    ctx: dict[str, Any],
    *,
    feature: str,
    threshold: float,
) -> dict[str, Any]:
    frames = recompute_prefilter_upload_frames(
        ctx["timeline_frames"],
        ctx["export_indices"],
        ctx["boxes"],
        record_id=ctx["record_id"],
        speed_gate=SpeedGateConfig(feature=feature, max_threshold=threshold, fail_open=True),
        infer_width=ctx["infer_w"],
        infer_height=ctx["infer_h"],
        video_fps=ctx["fps"],
        alarm_min_consecutive_frames=ALARM_MIN,
        alarm_cooldown_frames=ALARM_COOLDOWN,
    )
    return _evaluate_frames(
        paths,
        record_id=ctx["record_id"],
        upload_file=ctx["upload_file"],
        frames=frames,
    )


def _missed_segments(clip_result: dict[str, Any]) -> list[dict[str, Any]]:
    diag = clip_result.get("diagnostics") or {}
    return list(diag.get("missed_segments") or [])


def _gate_blocked_frames(
    ctx: dict[str, Any],
    *,
    feature: str,
    threshold: float,
    frame_set: set[int],
) -> list[dict[str, Any]]:
    """标定帧上若速度超阈值则视为会被门控跳过。"""
    blocked: list[dict[str, Any]] = []
    for row in ctx["velocity_rows"]:
        fi = int(row.get("frame_idx") or 0)
        if fi not in frame_set:
            continue
        lower_v = _float_or_none(row.get("lower_mean_speed"))
        knee_v = _float_or_none(row.get("knee_ankle_mean_speed"))
        use_v = _float_or_none(row.get(feature))
        if use_v is not None and use_v > threshold:
            blocked.append({
                "frame_idx": fi,
                "lower_mean_speed": lower_v,
                "knee_ankle_mean_speed": knee_v,
                "gate_feature": feature,
                "gate_value": use_v,
            })
    return blocked


def _build_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 前置门控特征对比：膝踝平均速度 vs 下半躯体平均速度",
        "",
        f"> 生成时间：{report.get('generated_at', '')}",
        f"> 脚本：`scripts/data/validate_prefilter_knee_ankle28.py`",
        "",
        "## 1. 实验目的",
        "",
        "验证用 **膝+踝 4 点算术平均**（`knee_ankle_mean_speed`）替代 **髋+膝+踝 6 点算术平均**（`lower_mean_speed`）",
        "作为前置速度门控特征，能否缓解下蹲/起身过快被误过滤的问题。",
        "",
        "> 说明：原 `lower_mean_speed` 已是算术平均（非求和）；本次变化是 **去掉髋部 2 点**，降低蹲起时髋部位移对门控的影响。",
        "",
        "## 2. 参数（与先前一致）",
        "",
        "| 参数 | 值 |",
        "|------|-----|",
        f"| pose_frame_interval | {report['params']['pose_frame_interval']} |",
        f"| alarm_min_consecutive_frames | {report['params']['alarm_min_consecutive_frames']} |",
        f"| alarm_cooldown_frames | {report['params']['alarm_cooldown_frames']} |",
        f"| max_threshold | {report['params']['default_threshold']} |",
        f"| baseline 对照 | rule-baseline-local-prod-test（本仓库重算） |",
        f"| 记录数 | {report.get('prepared_count')} |",
        "",
        "## 3. 特征定义",
        "",
        "| 特征 | 关键点 | 聚合 |",
        "|------|--------|------|",
        "| `lower_mean_speed` | 髋(11,12)+膝(13,14)+踝(15,16) | 有效点速度算术平均 |",
        "| `knee_ankle_mean_speed` | 膝(13,14)+踝(15,16) | 有效点速度算术平均 |",
        "",
        "单点速度：`speed = hypot(Δx, Δy) / Δt`，先 3 帧中值滤波再差分。",
        "",
        "## 4. 帧级区分度（标真重叠帧 vs baseline 误报帧）",
        "",
        "| 特征 | 标真 P50 | 误报 P50 |",
        "|------|----------|----------|",
    ]
    disc = report.get("frame_discrimination") or {}
    for feat, _label in FEATURES:
        row = disc.get(feat) or {}
        gt = row.get("gt_overlap") or {}
        fa = row.get("false_alarm") or {}
        lines.append(
            f"| `{feat}` | {gt.get('p50', '—')} | {fa.get('p50', '—')} |"
        )

    lines.extend([
        "",
        "## 5. 阈值网格（前置，相对 local baseline）",
        "",
    ])
    for feat, label in FEATURES:
        lines.append(f"### {feat}（{label}）")
        lines.append("")
        lines.append("| 阈值 ≤ | TP | FP | 召回率 |")
        lines.append("|--------|-----|-----|--------|")
        grid = (report.get("threshold_grids") or {}).get(feat) or []
        for row in grid:
            lines.append(
                f"| {row.get('max_threshold')} | {row.get('detected')} | "
                f"{row.get('false_alarms')} | {row.get('recall')} |"
            )
        lines.append("")

    at60 = report.get("at_threshold_60") or {}
    lines.extend([
        "## 6. 阈值 60 对比汇总",
        "",
        "| 特征 | TP | FP | FN段 | 召回率 |",
        "|------|-----|-----|------|--------|",
    ])
    for feat, label in FEATURES:
        row = at60.get(feat) or {}
        lines.append(
            f"| `{feat}` | {row.get('detected')} | {row.get('false_alarms')} | "
            f"{row.get('missed')} | {row.get('recall')} |"
        )

    baseline = report.get("local_baseline_summary") or {}
    lines.extend([
        "",
        f"local baseline（无过滤）：TP={baseline.get('detected')} FP={baseline.get('false_alarms')} "
        f"recall={baseline.get('recall')}",
        "",
        "## 7. 昨日漏报重点 clip 复查",
        "",
        "| clip | 特征 | 漏报段 | 标真帧门控分析 |",
        "|------|------|--------|----------------|",
    ])
    for item in report.get("squat_watch") or []:
        missed_txt = "; ".join(
            f"{s.get('frame_start')}-{s.get('frame_end')} ({','.join(s.get('gt_tokens') or [])})"
            for s in (item.get("missed_segments") or [])
        ) or "—"
        gate_txt = ""
        for feat in ("lower_mean_speed", "knee_ankle_mean_speed"):
            blocked = (item.get("gate_blocked_at_gt") or {}).get(feat) or []
            if blocked:
                frames = ",".join(str(b["frame_idx"]) for b in blocked[:5])
                if len(blocked) > 5:
                    frames += "..."
                gate_txt += f"{feat}: {len(blocked)}帧超阈({frames}); "
        lines.append(
            f"| `{item.get('upload_file')}` | {item.get('feature')} | {missed_txt} | {gate_txt or '—'} |"
        )

    lines.extend([
        "",
        "## 8. 结论",
        "",
        report.get("conclusion", "（待填）"),
        "",
    ])
    return "\n".join(lines)


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

    # local baseline 评估
    baseline_clips: list[dict[str, Any]] = []
    for ctx in prepared:
        if ctx.get("baseline_frames"):
            baseline_clips.append(
                _evaluate_frames(
                    paths,
                    record_id=ctx["record_id"],
                    upload_file=ctx["upload_file"],
                    frames=ctx["baseline_frames"],
                )
            )
    local_baseline_summary = aggregate_upload_clip_results(baseline_clips)

    # 帧级区分度
    frame_discrimination: dict[str, Any] = {}
    for feat, _ in FEATURES:
        gt_speeds: list[float] = []
        fa_speeds: list[float] = []
        for ctx in prepared:
            gt_frames = _gt_overlap_frames(ctx.get("baseline_frames") or [], ctx["gt_segments"])
            fa_frames = _false_alarm_frames(ctx.get("baseline_frames") or [], ctx["gt_segments"])
            gt_speeds.extend(_frame_feature_speeds(ctx["velocity_rows"], gt_frames, feat))
            fa_speeds.extend(_frame_feature_speeds(ctx["velocity_rows"], fa_frames, feat))
        frame_discrimination[feat] = {
            "gt_overlap": {"count": len(gt_speeds), "p50": _p50(gt_speeds)},
            "false_alarm": {"count": len(fa_speeds), "p50": _p50(fa_speeds)},
            "frame_threshold_scan": _threshold_scan_le(gt_speeds, fa_speeds, LOWER_THRESHOLDS),
        }

    # 阈值网格 + threshold=60 明细
    threshold_grids: dict[str, list[dict[str, Any]]] = {}
    at_threshold_60: dict[str, Any] = {}
    clip_results_at_60: dict[str, list[dict[str, Any]]] = {}

    for feat, label in FEATURES:
        print(f"\n=== 特征: {feat} ({label}) ===")
        grid: list[dict[str, Any]] = []
        for thr in FRAME_THRESHOLDS:
            clip_results: list[dict[str, Any]] = []
            for ctx in prepared:
                clip_results.append(_recompute_and_eval(paths, ctx, feature=feat, threshold=thr))
            summary = aggregate_upload_clip_results(clip_results)
            grid.append({
                "max_threshold": thr,
                "feature": feat,
                "detected": summary.get("detected"),
                "missed": summary.get("missed"),
                "false_alarms": summary.get("false_alarms"),
                "recall": summary.get("recall"),
                "precision_proxy": summary.get("precision_proxy"),
            })
            print(
                f"  threshold={thr}: TP={summary.get('detected')} "
                f"FP={summary.get('false_alarms')} recall={summary.get('recall')}"
            )
        threshold_grids[feat] = grid
        clip_results_at_60[feat] = [
            _recompute_and_eval(paths, ctx, feature=feat, threshold=DEFAULT_THRESHOLD)
            for ctx in prepared
        ]
        at_threshold_60[feat] = aggregate_upload_clip_results(clip_results_at_60[feat])

    # 重点 clip 漏报与门控帧分析
    squat_watch: list[dict[str, Any]] = []
    for feat, _ in FEATURES:
        by_file = {c.get("upload_file") or "": c for c in clip_results_at_60[feat]}
        ctx_by_file = {c["upload_file"]: c for c in prepared}
        for watch_file in SQUAT_WATCH_CLIPS:
            clip_res = by_file.get(watch_file)
            ctx = ctx_by_file.get(watch_file)
            if not clip_res or not ctx:
                continue
            missed = _missed_segments(clip_res)
            gt_frames = _gt_overlap_frames(ctx.get("baseline_frames") or [], ctx["gt_segments"])
            # 仅分析漏报段覆盖的标真帧
            miss_frames: set[int] = set()
            for seg in missed:
                for fi in range(int(seg.get("frame_start") or 0), int(seg.get("frame_end") or 0) + 1):
                    miss_frames.add(fi)
            target_frames = miss_frames or gt_frames
            squat_watch.append({
                "upload_file": watch_file,
                "feature": feat,
                "missed_segments": missed,
                "gate_blocked_at_gt": {
                    f: _gate_blocked_frames(ctx, feature=f, threshold=DEFAULT_THRESHOLD, frame_set=target_frames)
                    for f, _ in FEATURES
                },
            })

    # 结论自动生成
    old = at_threshold_60.get("lower_mean_speed") or {}
    new = at_threshold_60.get("knee_ankle_mean_speed") or {}
    old_missed = int(old.get("missed") or 0)
    new_missed = int(new.get("missed") or 0)
    old_fp = int(old.get("false_alarms") or 0)
    new_fp = int(new.get("false_alarms") or 0)
    recovered = old_missed - new_missed
    if recovered > 0 and new_fp <= old_fp + 5:
        conclusion = (
            f"膝踝平均特征在阈值 60 下召回改善：漏报段 {old_missed}→{new_missed}（少 {recovered} 段），"
            f"FP {old_fp}→{new_fp}。建议前置门控改用 `knee_ankle_mean_speed`。"
        )
    elif recovered > 0:
        conclusion = (
            f"膝踝平均特征减少漏报 {recovered} 段，但 FP 从 {old_fp} 升至 {new_fp}，"
            "需权衡是否采用或调高阈值。"
        )
    elif new_fp < old_fp:
        conclusion = (
            f"漏报段数不变（{old_missed}），FP {old_fp}→{new_fp} 略优，"
            "对下蹲漏报帮助有限，但误报压制略有改善。"
        )
    else:
        conclusion = (
            f"膝踝平均特征未改善漏报（均为 {old_missed} 段），FP {old_fp}→{new_fp}，"
            "下蹲起身过快问题需结合水平速度门控或连续帧门控等其他方案。"
        )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "record_count": len(records),
        "prepared_count": len(prepared),
        "errors": errors,
        "params": {
            "pose_frame_interval": POSE_FRAME_INTERVAL,
            "alarm_min_consecutive_frames": ALARM_MIN,
            "alarm_cooldown_frames": ALARM_COOLDOWN,
            "default_threshold": DEFAULT_THRESHOLD,
            "baseline_dir": str(LOCAL_BASELINE_DIR),
            "speed_filter_stage": "prefilter",
            "writeback_timeline": False,
        },
        "feature_definitions": {f: d for f, d in FEATURES},
        "local_baseline_summary": local_baseline_summary,
        "frame_discrimination": frame_discrimination,
        "threshold_grids": threshold_grids,
        "at_threshold_60": at_threshold_60,
        "squat_watch": squat_watch,
        "conclusion": conclusion,
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_MD.write_text(_build_markdown(report), encoding="utf-8")

    print(f"\n=== 阈值 60 对比 ===")
    print(f"lower_mean_speed:      TP={old.get('detected')} FP={old_fp} missed={old_missed} recall={old.get('recall')}")
    print(f"knee_ankle_mean_speed: TP={new.get('detected')} FP={new_fp} missed={new_missed} recall={new.get('recall')}")
    print(f"\n结论: {conclusion}")
    print(f"JSON: {OUT_JSON}")
    print(f"MD:   {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
