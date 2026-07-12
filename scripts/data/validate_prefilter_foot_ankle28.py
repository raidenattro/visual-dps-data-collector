#!/usr/bin/env python3
"""脚部踝点速度前置门控阈值选取（低置信度不参与计算）。

只读 timeline，内存重算，不写回记录包。
参数对齐 manifest：pose_frame_interval=2, alarm_min=3, cooldown=0。

用法（项目根目录）:
  python scripts/data/validate_prefilter_foot_ankle28.py
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
from event_engine.wrist_hits import WRIST_KPT_SCORE_MIN
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

OUT_JSON = ROOT / "docs/json/prefilter-foot-ankle-experiment.json"
OUT_MD = ROOT / "docs/prefilter-foot-ankle-speed-gate.md"

POSE_FRAME_INTERVAL = 2
ALARM_MIN = 3
ALARM_COOLDOWN = 0

# 踝点专用阈网格（含细粒度扫描）
ANKLE_THRESHOLDS = [25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 80, 100, 120]

FOOT_FEATURES = (
    ("ankle_mean_speed", "左右踝(15,16)算术平均，score≥0.3 才参与"),
    ("ankle_max_speed", "左右踝(15,16)取最大，score≥0.3 才参与"),
)

REF_FEATURES = (
    ("knee_ankle_mean_speed", "膝+踝 4 点算术平均（对照 knee@65）"),
    ("lower_mean_speed", "髋+膝+踝 6 点算术平均（对照 lower@60）"),
)

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
    blocked: list[dict[str, Any]] = []
    for row in ctx["velocity_rows"]:
        fi = int(row.get("frame_idx") or 0)
        if fi not in frame_set:
            continue
        use_v = _float_or_none(row.get(feature))
        if use_v is not None and use_v > threshold:
            blocked.append({
                "frame_idx": fi,
                "ankle_mean_speed": _float_or_none(row.get("ankle_mean_speed")),
                "ankle_max_speed": _float_or_none(row.get("ankle_max_speed")),
                "knee_ankle_mean_speed": _float_or_none(row.get("knee_ankle_mean_speed")),
                "gate_feature": feature,
                "gate_value": use_v,
            })
    return blocked


def _pick_optimal_threshold(
    grid: list[dict[str, Any]],
    *,
    ref_recall: float,
    ref_fp: int,
    recall_floor_delta: float = 0.0,
) -> dict[str, Any] | None:
    """在召回不低于 ref_recall - recall_floor_delta 的前提下选 FP 最小；同 FP 取召回最高。"""
    floor = ref_recall - recall_floor_delta
    candidates = [
        row for row in grid
        if float(row.get("recall") or 0) >= floor
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda r: (
            int(r.get("false_alarms") or 0),
            -float(r.get("recall") or 0),
            -float(r.get("detected") or 0),
        )
    )
    best = candidates[0]
    return {
        **best,
        "fp_vs_reference": int(best.get("false_alarms") or 0) - ref_fp,
        "recall_vs_reference": round(float(best.get("recall") or 0) - ref_recall, 4),
        "recall_floor": floor,
    }


def _build_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 脚部踝点速度前置门控阈值选取",
        "",
        f"> 生成时间：{report.get('generated_at', '')}",
        f"> 脚本：`scripts/data/validate_prefilter_foot_ankle28.py`",
        "",
        "## 1. 实验目的",
        "",
        "基于 **左右踝骨骼点**（COCO17 索引 15/16）计算帧间速度，",
        "选取合理前置门控阈值；**低置信度点（score < WRIST_KPT_SCORE_MIN）不参与速度计算**。",
        "",
        "## 2. 参数（对齐 _manifest.json）",
        "",
        "| 参数 | 值 |",
        "|------|-----|",
        f"| pose_frame_interval | {report['params']['pose_frame_interval']} |",
        f"| alarm_min_consecutive_frames | {report['params']['alarm_min_consecutive_frames']} |",
        f"| alarm_cooldown_frames | {report['params']['alarm_cooldown_frames']} |",
        f"| kpt_score_min | {report['params']['kpt_score_min']} |",
        f"| baseline 对照 | rule-baseline-local-prod-test |",
        f"| 记录数 | {report.get('prepared_count')} |",
        "",
        "## 3. 特征定义",
        "",
        "| 特征 | 关键点 | 聚合 | 置信度 |",
        "|------|--------|------|--------|",
        "| `ankle_mean_speed` | 左踝(15)+右踝(16) | 有效点速度算术平均 | score ≥ 0.3 |",
        "| `ankle_max_speed` | 左踝(15)+右踝(16) | 有效点速度取 max | score ≥ 0.3 |",
        "",
        "单点速度：`speed = hypot(Δx, Δy) / Δt`，先 3 帧中值滤波再差分。",
        "若单帧两踝均低置信度，则该帧 `ankle_*_speed` 为 `null`，门控 fail-open 不阻断。",
        "",
        "## 4. 帧级区分度（标真重叠帧 vs baseline 误报帧）",
        "",
        "| 特征 | 标真 P50 | 误报 P50 | 有效样本数（标真/误报） |",
        "|------|----------|----------|-------------------------|",
    ]
    disc = report.get("frame_discrimination") or {}
    for feat, _label in FOOT_FEATURES + REF_FEATURES:
        row = disc.get(feat) or {}
        gt = row.get("gt_overlap") or {}
        fa = row.get("false_alarm") or {}
        lines.append(
            f"| `{feat}` | {gt.get('p50', '—')} | {fa.get('p50', '—')} | "
            f"{gt.get('count', 0)} / {fa.get('count', 0)} |"
        )

    lines.extend([
        "",
        "## 5. 踝点特征阈值网格（相对 local baseline）",
        "",
    ])
    for feat, label in FOOT_FEATURES:
        lines.append(f"### {feat}（{label}）")
        lines.append("")
        lines.append("| 阈值 ≤ | TP | FP | FN段 | 召回率 |")
        lines.append("|--------|-----|-----|------|--------|")
        grid = (report.get("threshold_grids") or {}).get(feat) or []
        opt = (report.get("optimal_thresholds") or {}).get(feat) or {}
        opt_thr = opt.get("max_threshold")
        for row in grid:
            mark = " **←推荐**" if opt_thr is not None and row.get("max_threshold") == opt_thr else ""
            lines.append(
                f"| {row.get('max_threshold')} | {row.get('detected')} | "
                f"{row.get('false_alarms')} | {row.get('missed')} | {row.get('recall')}{mark} |"
            )
        if opt:
            lines.append("")
            lines.append(
                f"推荐阈值：**{opt_thr}**（召回 {opt.get('recall')}，"
                f"FP {opt.get('false_alarms')}，相对 knee@65 FP {opt.get('fp_vs_reference'):+d}）"
            )
        lines.append("")

    ref = report.get("reference_at_fixed_threshold") or {}
    lines.extend([
        "## 6. 对照方案（固定阈）",
        "",
        "| 方案 | 特征 | 阈值 | TP | FP | FN段 | 召回率 |",
        "|------|------|------|-----|-----|------|--------|",
    ])
    for key, row in ref.items():
        lines.append(
            f"| {key} | `{row.get('feature')}` | {row.get('max_threshold')} | "
            f"{row.get('detected')} | {row.get('false_alarms')} | "
            f"{row.get('missed')} | {row.get('recall')} |"
        )

    baseline = report.get("local_baseline_summary") or {}
    lines.extend([
        "",
        f"local baseline（无过滤）：TP={baseline.get('detected')} FP={baseline.get('false_alarms')} "
        f"recall={baseline.get('recall')}",
        "",
        "## 7. 漏报重点 clip 门控分析",
        "",
        "| clip | 特征@阈 | 漏报段 | 标真帧超阈 |",
        "|------|---------|--------|------------|",
    ])
    for item in report.get("squat_watch") or []:
        missed_txt = "; ".join(
            f"{s.get('frame_start')}-{s.get('frame_end')} ({','.join(s.get('gt_tokens') or [])})"
            for s in (item.get("missed_segments") or [])
        ) or "—"
        gate_txt = ""
        for feat, thr in (item.get("gate_checks") or []):
            blocked = (item.get("gate_blocked") or {}).get(feat) or []
            if blocked:
                frames = ",".join(str(b["frame_idx"]) for b in blocked[:4])
                if len(blocked) > 4:
                    frames += "..."
                gate_txt += f"{feat}@{thr}: {len(blocked)}帧({frames}); "
        lines.append(
            f"| `{item.get('upload_file')}` | {item.get('feature')}@{item.get('threshold')} | "
            f"{missed_txt} | {gate_txt or '—'} |"
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
    baseline_fp = int(local_baseline_summary.get("false_alarms") or 0)
    baseline_recall = float(local_baseline_summary.get("recall") or 0)

    all_features = FOOT_FEATURES + REF_FEATURES
    frame_discrimination: dict[str, Any] = {}
    for feat, _ in all_features:
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

    # 对照：knee@65、lower@60（先算，供最优阈参照）
    reference_at_fixed_threshold: dict[str, Any] = {}
    ref_specs = (
        ("knee@65", "knee_ankle_mean_speed", 65.0),
        ("lower@60", "lower_mean_speed", 60.0),
    )
    for key, feat, thr in ref_specs:
        clip_results = [
            _recompute_and_eval(paths, ctx, feature=feat, threshold=thr)
            for ctx in prepared
        ]
        summary = aggregate_upload_clip_results(clip_results)
        reference_at_fixed_threshold[key] = {
            "feature": feat,
            "max_threshold": thr,
            **summary,
        }
        print(
            f"\n对照 {key}: TP={summary.get('detected')} FP={summary.get('false_alarms')} "
            f"recall={summary.get('recall')}"
        )

    knee_ref = reference_at_fixed_threshold.get("knee@65") or {}
    knee_fp = int(knee_ref.get("false_alarms") or 0)
    knee_recall = float(knee_ref.get("recall") or 0)

    threshold_grids: dict[str, list[dict[str, Any]]] = {}
    optimal_thresholds: dict[str, Any] = {}
    clip_results_by_feat_thr: dict[str, dict[float, list[dict[str, Any]]]] = {}

    for feat, label in FOOT_FEATURES:
        print(f"\n=== 踝点特征: {feat} ({label}) ===")
        grid: list[dict[str, Any]] = []
        clip_results_by_feat_thr[feat] = {}
        for thr in ANKLE_THRESHOLDS:
            clip_results: list[dict[str, Any]] = []
            for ctx in prepared:
                clip_results.append(_recompute_and_eval(paths, ctx, feature=feat, threshold=thr))
            clip_results_by_feat_thr[feat][thr] = clip_results
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
        optimal_thresholds[feat] = _pick_optimal_threshold(
            grid,
            ref_recall=knee_recall,
            ref_fp=knee_fp,
            recall_floor_delta=0.0,
        )

    # 重点 clip：用各踝点最优阈分析
    squat_watch: list[dict[str, Any]] = []
    for feat, _ in FOOT_FEATURES:
        opt = optimal_thresholds.get(feat) or {}
        thr = float(opt.get("max_threshold") or 60)
        clip_results = clip_results_by_feat_thr[feat].get(thr)
        if not clip_results:
            clip_results = [
                _recompute_and_eval(paths, ctx, feature=feat, threshold=thr)
                for ctx in prepared
            ]
        by_file = {c.get("upload_file") or "": c for c in clip_results}
        ctx_by_file = {c["upload_file"]: c for c in prepared}
        for watch_file in SQUAT_WATCH_CLIPS:
            clip_res = by_file.get(watch_file)
            ctx = ctx_by_file.get(watch_file)
            if not clip_res or not ctx:
                continue
            missed = _missed_segments(clip_res)
            gt_frames = _gt_overlap_frames(ctx.get("baseline_frames") or [], ctx["gt_segments"])
            miss_frames: set[int] = set()
            for seg in missed:
                for fi in range(int(seg.get("frame_start") or 0), int(seg.get("frame_end") or 0) + 1):
                    miss_frames.add(fi)
            target_frames = miss_frames or gt_frames
            squat_watch.append({
                "upload_file": watch_file,
                "feature": feat,
                "threshold": thr,
                "missed_segments": missed,
                "gate_checks": [(feat, thr)],
                "gate_blocked": {
                    feat: _gate_blocked_frames(ctx, feature=feat, threshold=thr, frame_set=target_frames),
                },
            })

    # 结论
    parts: list[str] = []
    for feat, label in FOOT_FEATURES:
        opt = optimal_thresholds.get(feat) or {}
        if not opt:
            parts.append(f"`{feat}` 无法在召回≥knee@65（{knee_recall}）时选出更优阈。")
            continue
        thr = opt.get("max_threshold")
        fp = int(opt.get("false_alarms") or 0)
        recall = float(opt.get("recall") or 0)
        fp_delta = int(opt.get("fp_vs_reference") or 0)
        if fp_delta < 0:
            parts.append(
                f"`{feat}` 推荐阈 **{thr}**：FP {fp}（较 knee@65 少 {-fp_delta}），"
                f"召回 {recall}（与 knee@65 持平或更高）。"
            )
        elif fp_delta == 0:
            parts.append(f"`{feat}`@{thr} 与 knee@65 FP 相同（{fp}），召回 {recall}。")
        else:
            parts.append(
                f"`{feat}`@{thr} FP {fp} 未优于 knee@65（FP {knee_fp}，多 {fp_delta}），"
                f"召回 {recall}。"
            )
    mean_opt = optimal_thresholds.get("ankle_mean_speed") or {}
    max_opt = optimal_thresholds.get("ankle_max_speed") or {}
    if max_opt and int(max_opt.get("fp_vs_reference") or 0) < 0:
        parts.append(
            "踝点 **max** 聚合在保持 knee@65 召回时可略压 FP，"
            "但单踝噪声敏感；**mean** 更稳但区分度不足，不建议单独替代 knee@65。"
        )
    elif mean_opt:
        parts.append(
            "踝点速度标真 P50 低于误报，但段级门控仍不如膝踝联合特征；"
            "建议继续以 `knee_ankle_mean_speed@65` 为主，踝点特征作辅助分析。"
        )
    conclusion = " ".join(parts)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "record_count": len(records),
        "prepared_count": len(prepared),
        "errors": errors,
        "params": {
            "pose_frame_interval": POSE_FRAME_INTERVAL,
            "alarm_min_consecutive_frames": ALARM_MIN,
            "alarm_cooldown_frames": ALARM_COOLDOWN,
            "kpt_score_min": WRIST_KPT_SCORE_MIN,
            "baseline_dir": str(LOCAL_BASELINE_DIR),
            "speed_filter_stage": "prefilter",
            "writeback_timeline": False,
        },
        "feature_definitions": {f: d for f, d in FOOT_FEATURES + REF_FEATURES},
        "local_baseline_summary": local_baseline_summary,
        "frame_discrimination": frame_discrimination,
        "threshold_grids": threshold_grids,
        "optimal_thresholds": optimal_thresholds,
        "reference_at_fixed_threshold": reference_at_fixed_threshold,
        "squat_watch": squat_watch,
        "conclusion": conclusion,
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_MD.write_text(_build_markdown(report), encoding="utf-8")

    print(f"\n=== 推荐阈 ===")
    for feat, _ in FOOT_FEATURES:
        opt = optimal_thresholds.get(feat) or {}
        print(f"{feat}: thr={opt.get('max_threshold')} TP={opt.get('detected')} FP={opt.get('false_alarms')} recall={opt.get('recall')}")
    print(f"\n结论: {conclusion}")
    print(f"JSON: {OUT_JSON}")
    print(f"MD:   {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
