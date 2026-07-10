#!/usr/bin/env python3
"""膝踝前置门控：先扫描最佳阈值，再以该阈值跑手腕/上肢豁免实验。

阶段一：knee_ankle_mean_speed 阈值网格 → 自动选取最佳
阶段二：在最佳膝踝阈值上叠加 wrist/upper 豁免网格

参数：pose_frame_interval=2, alarm_min=3, cooldown=0

用法:
  python scripts/data/validate_prefilter_knee_ankle_wrist_exempt28.py
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

from api.inference_eval_service import (
    UploadClipInput,
    aggregate_upload_clip_results,
    evaluate_uploaded_clip,
    load_inference_json_file,
)
from api.record_service import locate_record_by_id
from api.wrist_features_service import _infer_size_from_frames, _load_boxes_for_wrist_features, _video_fps
from scripts.data.analyze_skeleton_velocity_discrimination import _float_or_none, _load_ground_truth_segments
from scripts.data.upload_export_common import (
    export_indices_for_record,
    load_baseline_manifest,
    resolve_baseline_clip_path,
)

LOCAL_BASELINE_MANIFEST = ROOT / "localdata/export/rule-baseline-local-prod-test/_manifest.json"
LOCAL_BASELINE_DIR = LOCAL_BASELINE_MANIFEST.parent
OUT_JSON = ROOT / "docs/json/prefilter-knee-ankle-wrist-exempt-experiment.json"
OUT_MD = ROOT / "docs/prefilter-knee-ankle-wrist-exempt-speed-gate.md"
OUT_THRESHOLD_MD = ROOT / "docs/prefilter-knee-ankle-threshold-optimal.md"

GATE_FEATURE = "knee_ankle_mean_speed"
POSE_FRAME_INTERVAL = 2
ALARM_MIN = 3
ALARM_COOLDOWN = 0

# 阶段一：膝踝阈值扫描（比先前更密）
KNEE_ANKLE_SCAN_THRESHOLDS = [30, 40, 50, 55, 60, 65, 70, 75, 80, 90, 100, 110, 120]
EXEMPT_THRESHOLDS = [30, 40, 50, 60, 80, 100]

SQUAT_WATCH = (
    "clip_0009_start_00-37-59_rtmpose_m.json",
    "clip_0013_start_00-42-48_rtmpose_m.json",
    "clip_0020_start_00-48-44_rtmpose_m.json",
    "clip_0013_start_00-29-53_rtmpose_m.json",
)


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
        entry, baseline_dir=LOCAL_BASELINE_DIR, timeline_frames=timeline_frames,
        pose_frame_interval=POSE_FRAME_INTERVAL,
    )
    if not export_indices:
        return {"record_id": record_id, "error": "无导出抽帧索引"}
    gt_segments, _ = _load_ground_truth_segments(loc)
    if not gt_segments:
        return {"record_id": record_id, "error": "无标真"}
    clip_path = resolve_baseline_clip_path(LOCAL_BASELINE_DIR, entry)
    baseline_frames = load_inference_json_file(clip_path) if clip_path else []
    return {
        "record_id": record_id,
        "timeline_frames": timeline_frames,
        "boxes": boxes,
        "infer_w": infer_w,
        "infer_h": infer_h,
        "fps": fps,
        "export_indices": export_indices,
        "gt_segments": gt_segments,
        "baseline_frames": baseline_frames,
        "upload_file": str(entry.get("file") or f"{entry.get('clip_name')}.json"),
    }


def _eval_clip(paths, ctx, gate: SpeedGateConfig) -> dict[str, Any]:
    frames = recompute_prefilter_upload_frames(
        ctx["timeline_frames"], ctx["export_indices"], ctx["boxes"],
        record_id=ctx["record_id"], speed_gate=gate,
        infer_width=ctx["infer_w"], infer_height=ctx["infer_h"], video_fps=ctx["fps"],
        alarm_min_consecutive_frames=ALARM_MIN, alarm_cooldown_frames=ALARM_COOLDOWN,
    )
    return evaluate_uploaded_clip(
        paths, UploadClipInput(upload_file=ctx["upload_file"], frames=frames, record_id=ctx["record_id"]),
    )


def _knee_ankle_gate(threshold: float, **kwargs) -> SpeedGateConfig:
    return SpeedGateConfig(
        feature=GATE_FEATURE, max_threshold=threshold, fail_open=True, **kwargs,
    )


def _gate_label(gate: SpeedGateConfig) -> str:
    parts = [f"膝踝≤{gate.max_threshold}"]
    if gate.wrist_exempt_max_threshold is not None:
        parts.append(f"wrist豁免≤{gate.wrist_exempt_max_threshold}")
    if gate.upper_exempt_max_threshold is not None:
        parts.append(f"upper豁免≤{gate.upper_exempt_max_threshold}")
    if len(parts) == 1:
        return f"膝踝>{gate.max_threshold}（无豁免）"
    return " + ".join(parts)


def _summary_row(summary: dict[str, Any], threshold: float) -> dict[str, Any]:
    return {
        "max_threshold": threshold,
        "detected": summary.get("detected"),
        "missed": summary.get("missed"),
        "false_alarms": summary.get("false_alarms"),
        "recall": summary.get("recall"),
        "precision_proxy": summary.get("precision_proxy"),
    }


def _pick_by_recall_min_fp(grid: list[dict[str, Any]], min_recall: float) -> dict[str, Any] | None:
    """召回率 ≥ min_recall 下 FP 最小。"""
    cands = [r for r in grid if float(r.get("recall") or 0) >= min_recall - 1e-6]
    if not cands:
        return None
    return min(cands, key=lambda r: (int(r.get("false_alarms") or 0), -float(r.get("recall") or 0)))


def _pick_max_recall_min_fp(grid: list[dict[str, Any]]) -> dict[str, Any]:
    """全网格最高召回下 FP 最小。"""
    max_rec = max(float(r.get("recall") or 0) for r in grid)
    cands = [r for r in grid if abs(float(r.get("recall") or 0) - max_rec) < 1e-6]
    return min(cands, key=lambda r: int(r.get("false_alarms") or 0))


def _pareto_frontier(grid: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """漏报段少且 FP 低的不被支配点。"""
    frontier: list[dict[str, Any]] = []
    for r in grid:
        m = int(r.get("missed") or 0)
        fp = int(r.get("false_alarms") or 0)
        dominated = False
        for o in grid:
            om = int(o.get("missed") or 0)
            ofp = int(o.get("false_alarms") or 0)
            if om <= m and ofp <= fp and (om < m or ofp < fp):
                dominated = True
                break
        if not dominated:
            frontier.append(r)
    return sorted(frontier, key=lambda r: float(r.get("recall") or 0))


def _select_optimal_threshold(grid: list[dict[str, Any]]) -> dict[str, Any]:
    """多准则选取；阶段二默认用 primary。"""
    conservative = _pick_by_recall_min_fp(grid, 0.90)
    balanced = _pick_by_recall_min_fp(grid, 0.915)
    recall_92 = _pick_by_recall_min_fp(grid, 0.92)
    max_rec = _pick_max_recall_min_fp(grid)
    frontier = _pareto_frontier(grid)

    # 主选：召回≥92% 且 FP 最小（28 条上通常为 70）
    primary = recall_92 or balanced or conservative or max_rec
    if primary is None:
        primary = grid[0]

    return {
        "conservative_recall90_min_fp": conservative,
        "balanced_recall915_min_fp": balanced,
        "recall92_min_fp": recall_92,
        "max_recall_min_fp": max_rec,
        "pareto_frontier": frontier,
        "selected": primary,
        "selected_threshold": float(primary["max_threshold"]),
        "selection_criterion": "召回率≥92% 下 FP 最小；若无则依次降级 balanced(≥91.5%)/conservative(≥90%)",
    }


def _build_exempt_configs(gate_threshold: float) -> list[tuple[str, SpeedGateConfig]]:
    configs: list[tuple[str, SpeedGateConfig]] = []
    configs.append(("knee_ankle_only", _knee_ankle_gate(gate_threshold)))
    for thr in EXEMPT_THRESHOLDS:
        configs.append((f"wrist_exempt_{thr}", _knee_ankle_gate(gate_threshold, wrist_exempt_max_threshold=thr)))
    for thr in EXEMPT_THRESHOLDS:
        configs.append((f"upper_exempt_{thr}", _knee_ankle_gate(gate_threshold, upper_exempt_max_threshold=thr)))
    for thr in [40, 50, 60, 80, 100]:
        configs.append((
            f"both_exempt_{thr}",
            _knee_ankle_gate(gate_threshold, wrist_exempt_max_threshold=thr, upper_exempt_max_threshold=thr),
        ))
    return configs


def _build_threshold_md(report: dict[str, Any]) -> str:
    sel = report.get("threshold_selection") or {}
    grid = report.get("knee_ankle_threshold_grid") or []
    lines = [
        "# 膝踝前置门控阈值优选",
        "",
        f"> 生成时间：{report.get('generated_at', '')}",
        "> 脚本：`scripts/data/validate_prefilter_knee_ankle_wrist_exempt28.py`（阶段一）",
        "",
        "## 1. 扫描网格",
        "",
        "| 膝踝阈值 ≤ | TP | FP | FN段 | 召回率 |",
        "|-----------|-----|-----|------|--------|",
    ]
    for row in grid:
        mark = " **← 选用**" if float(row.get("max_threshold") or 0) == float(sel.get("selected_threshold") or -1) else ""
        lines.append(
            f"| {row.get('max_threshold')} | {row.get('detected')} | {row.get('false_alarms')} | "
            f"{row.get('missed')} | {row.get('recall')}{mark} |"
        )

    def _fmt_pick(key: str) -> str:
        r = sel.get(key) or {}
        if not r:
            return "—"
        return f"阈{r.get('max_threshold')} TP={r.get('detected')} FP={r.get('false_alarms')} 召回={r.get('recall')}"

    lines.extend([
        "",
        "## 2. 选取准则",
        "",
        "| 档位 | 规则 | 结果 |",
        "|------|------|------|",
        f"| 保守 | 召回≥90%，FP 最小 | {_fmt_pick('conservative_recall90_min_fp')} |",
        f"| 折中 | 召回≥91.5%，FP 最小 | {_fmt_pick('balanced_recall915_min_fp')} |",
        f"| **主选** | **召回≥92%，FP 最小** | **{_fmt_pick('recall92_min_fp')}** |",
        f"| 激进 | 全网格最高召回，FP 最小 | {_fmt_pick('max_recall_min_fp')} |",
        "",
        f"**阶段二豁免实验采用膝踝阈值：{sel.get('selected_threshold')}**",
        "",
        f"选取说明：{sel.get('selection_criterion', '')}",
        "",
        "## 3. Pareto 前沿（漏报段 vs FP）",
        "",
        "| 膝踝阈值 | TP | FP | FN段 | 召回率 |",
        "|---------|-----|-----|------|--------|",
    ])
    for row in sel.get("pareto_frontier") or []:
        lines.append(
            f"| {row.get('max_threshold')} | {row.get('detected')} | {row.get('false_alarms')} | "
            f"{row.get('missed')} | {row.get('recall')} |"
        )
    lines.append("")
    return "\n".join(lines)


def _build_combined_md(report: dict[str, Any]) -> str:
    sel_thr = (report.get("threshold_selection") or {}).get("selected_threshold")
    lines = [
        "# 膝踝最佳阈值 + 手腕/上肢豁免（重跑）",
        "",
        f"> 生成时间：{report.get('generated_at', '')}",
        "> 脚本：`scripts/data/validate_prefilter_knee_ankle_wrist_exempt28.py`",
        "",
        f"## 1. 膝踝门控最佳阈值：**{sel_thr}**",
        "",
        f"详见：`docs/prefilter-knee-ankle-threshold-optimal.md`",
        "",
        "**复合门控**：",
        "",
        "```",
        f"若 knee_ankle_mean_speed ≤ {sel_thr}  → 不 block",
        f"若 knee_ankle_mean_speed > {sel_thr}  → 膝踝快：",
        "    wrist_max ≤ 豁免阈 或 upper_mean ≤ 豁免阈 → 不 block",
        "    否则 → block",
        "```",
        "",
        "## 2. 豁免方案对比（膝踝阈固定为最佳值）",
        "",
        "| 方案 | TP | FP | FN段 | 召回率 | vs 膝踝无豁免 |",
        "|------|-----|-----|------|--------|---------------|",
    ]
    base = report.get("knee_ankle_only_at_optimal") or {}
    for row in report.get("exempt_results") or []:
        s = row.get("summary") or {}
        delta = ""
        if row.get("name") != "knee_ankle_only":
            d_tp = int(s.get("detected") or 0) - int(base.get("detected") or 0)
            d_fp = int(s.get("false_alarms") or 0) - int(base.get("false_alarms") or 0)
            d_fn = int(s.get("missed") or 0) - int(base.get("missed") or 0)
            delta = f"TP{d_tp:+d} FP{d_fp:+d} FN{d_fn:+d}"
        lines.append(
            f"| {row.get('label')} | {s.get('detected')} | {s.get('false_alarms')} | "
            f"{s.get('missed')} | {s.get('recall')} | {delta} |"
        )

    lines.extend([
        "",
        "## 3. 与固定阈 60 的历史结果对照",
        "",
        "| 方案 | TP | FP | FN段 | 召回率 |",
        "|------|-----|-----|------|--------|",
        "| 膝踝@60 无豁免（历史） | 143 | 286 | 13 | 91.67% |",
        "| 膝踝@60 + upper豁免≤100（历史） | 145 | 349 | 11 | 92.95% |",
    ])
    best = report.get("best_exempt") or {}
    bs = best.get("summary") or {}
    lines.append(
        f"| **本次 {best.get('label', '')}** | **{bs.get('detected')}** | **{bs.get('false_alarms')}** | "
        f"**{bs.get('missed')}** | **{bs.get('recall')}** |"
    )

    lines.extend([
        "",
        "## 4. 推荐",
        "",
        report.get("recommendation", ""),
        "",
        "## 5. 下蹲/漏报 clip",
        "",
        "| clip | 方案 | 漏报段 |",
        "|------|------|--------|",
    ])
    for item in report.get("squat_watch") or []:
        missed = "; ".join(
            f"{s.get('frame_start')}-{s.get('frame_end')}"
            for s in (item.get("missed_segments") or [])
        ) or "—"
        lines.append(f"| `{item.get('upload_file')}` | {item.get('label')} | {missed} |")

    lines.extend(["", "## 6. 结论", "", report.get("conclusion", ""), ""])
    return "\n".join(lines)


def main() -> int:
    resolve_config_path(None)
    paths = resolve_app_paths()
    records = list(load_baseline_manifest(LOCAL_BASELINE_MANIFEST).get("records") or [])
    prepared = []
    for entry in records:
        ctx = _prepare_record(entry)
        if not ctx or ctx.get("error"):
            print(f"{entry.get('record_id')}: SKIP {ctx.get('error') if ctx else '?'}")
            continue
        prepared.append(ctx)
        print(f"{ctx['record_id']}: ok")

    if not prepared:
        return 2

    local_baseline = aggregate_upload_clip_results([
        evaluate_uploaded_clip(
            paths,
            UploadClipInput(upload_file=c["upload_file"], frames=c["baseline_frames"], record_id=c["record_id"]),
        )
        for c in prepared if c.get("baseline_frames")
    ])

    # ===== 阶段一：膝踝阈值扫描 =====
    print("\n========== 阶段一：膝踝阈值扫描 ==========")
    knee_grid: list[dict[str, Any]] = []
    for thr in KNEE_ANKLE_SCAN_THRESHOLDS:
        gate = _knee_ankle_gate(thr)
        clips = [_eval_clip(paths, ctx, gate) for ctx in prepared]
        summary = aggregate_upload_clip_results(clips)
        row = _summary_row(summary, thr)
        knee_grid.append(row)
        print(
            f"  knee_ankle≤{thr}: TP={row.get('detected')} FP={row.get('false_alarms')} "
            f"missed={row.get('missed')} recall={row.get('recall')}"
        )

    threshold_selection = _select_optimal_threshold(knee_grid)
    optimal_thr = float(threshold_selection["selected_threshold"])
    print(f"\n>>> 选用膝踝阈值: {optimal_thr} ({threshold_selection['selection_criterion']})")

    threshold_report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "knee_ankle_threshold_grid": knee_grid,
        "threshold_selection": threshold_selection,
        "local_baseline": local_baseline,
    }
    OUT_THRESHOLD_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_THRESHOLD_MD.write_text(_build_threshold_md(threshold_report), encoding="utf-8")

    # ===== 阶段二：最佳膝踝阈 + 豁免网格 =====
    print(f"\n========== 阶段二：膝踝阈={optimal_thr} + 豁免 ==========")
    exempt_configs = _build_exempt_configs(optimal_thr)
    exempt_results: list[dict[str, Any]] = []
    clip_cache: dict[str, list[dict[str, Any]]] = {}

    for name, gate in exempt_configs:
        print(f"\n=== {name}: {_gate_label(gate)} ===")
        clip_results = [_eval_clip(paths, ctx, gate) for ctx in prepared]
        summary = aggregate_upload_clip_results(clip_results)
        clip_cache[name] = clip_results
        exempt_results.append({
            "name": name,
            "label": _gate_label(gate),
            "gate": {
                "feature": gate.feature,
                "max_threshold": gate.max_threshold,
                "wrist_exempt_max_threshold": gate.wrist_exempt_max_threshold,
                "upper_exempt_max_threshold": gate.upper_exempt_max_threshold,
            },
            "summary": summary,
        })
        print(f"  TP={summary.get('detected')} FP={summary.get('false_alarms')} "
              f"missed={summary.get('missed')} recall={summary.get('recall')}")

    knee_only = next(r for r in exempt_results if r["name"] == "knee_ankle_only")
    knee_only_summary = knee_only["summary"]

    best = None
    for row in exempt_results:
        s = row["summary"]
        rec = float(s.get("recall") or 0)
        base_rec = float(knee_only_summary.get("recall") or 0)
        if rec < base_rec - 0.001:
            continue
        score = (int(s.get("detected") or 0), -int(s.get("false_alarms") or 0), rec)
        if best is None or score > best[0]:
            best = (score, row)
    best_row = best[1] if best else knee_only

    squat_watch = []
    for watch in SQUAT_WATCH:
        ctx = next((c for c in prepared if c["upload_file"] == watch), None)
        if not ctx:
            continue
        idx = prepared.index(ctx)
        for row in (knee_only, best_row):
            clip_res = clip_cache[row["name"]][idx]
            squat_watch.append({
                "upload_file": watch,
                "label": row["label"],
                "missed_segments": (clip_res.get("diagnostics") or {}).get("missed_segments") or [],
            })

    base_m = int(knee_only_summary.get("missed") or 0)
    best_m = int(best_row["summary"].get("missed") or 0)
    base_fp = int(knee_only_summary.get("false_alarms") or 0)
    best_fp = int(best_row["summary"].get("false_alarms") or 0)
    best_tp = int(best_row["summary"].get("detected") or 0)

    recommendation = (
        f"膝踝最佳阈 **{optimal_thr}**；豁免推荐 **{best_row['label']}** — "
        f"TP={best_tp} FP={best_fp} FN={best_m} 召回={best_row['summary'].get('recall')}"
    )

    # 与膝踝@60历史对比
    hist_60_exempt = "145/349/11@92.95%"
    conclusion = (
        f"膝踝阈从 60 调整为 **{optimal_thr}**（召回≥92% 下 FP 最小）。"
        f"无豁免：TP={knee_only_summary.get('detected')} FP={base_fp} 漏报={base_m}。"
    )
    if best_m < base_m or best_tp > int(knee_only_summary.get("detected") or 0):
        conclusion += (
            f" 叠加豁免后 TP={best_tp} FP={best_fp} 漏报={best_m}。"
            f"历史膝踝@60+upper豁免100为 {hist_60_exempt}。"
        )
    else:
        conclusion += f" 豁免未进一步改善漏报；历史膝踝@60+upper豁免100为 {hist_60_exempt}。"

    full_report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "prepared_count": len(prepared),
        "params": {
            "pose_frame_interval": POSE_FRAME_INTERVAL,
            "alarm_min_consecutive_frames": ALARM_MIN,
            "alarm_cooldown_frames": ALARM_COOLDOWN,
            "gate_feature": GATE_FEATURE,
        },
        "local_baseline": local_baseline,
        "knee_ankle_threshold_grid": knee_grid,
        "threshold_selection": threshold_selection,
        "optimal_knee_ankle_threshold": optimal_thr,
        "knee_ankle_only_at_optimal": knee_only_summary,
        "exempt_results": exempt_results,
        "best_exempt": best_row,
        "squat_watch": squat_watch,
        "recommendation": recommendation,
        "conclusion": conclusion,
        "historical_reference": {
            "knee_ankle_60_no_exempt": {"tp": 143, "fp": 286, "missed": 13, "recall": 0.9167},
            "knee_ankle_60_upper_exempt_100": {"tp": 145, "fp": 349, "missed": 11, "recall": 0.9295},
        },
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(full_report, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_MD.write_text(_build_combined_md(full_report), encoding="utf-8")

    print(f"\n{recommendation}")
    print(f"结论: {conclusion}")
    print(f"阈值报告: {OUT_THRESHOLD_MD}")
    print(f"豁免报告: {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
