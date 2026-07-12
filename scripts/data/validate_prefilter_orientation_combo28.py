#!/usr/bin/env python3
"""朝向特征阈值筛选 + 与 knee@65 / lower@60 组合门控实验。

参数对齐 manifest：pose_frame_interval=2, alarm_min=3, cooldown=0。
只读 timeline，不写回记录包。

用法（项目根目录）:
  python scripts/data/validate_prefilter_orientation_combo28.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.data.evaluate_combo1_segment_filter import _ensure_cv2_point_polygon_test

_ensure_cv2_point_polygon_test()

from config_loader import resolve_app_paths, resolve_config_path
from event_engine.speed_gated_collision import SpeedGateConfig, recompute_prefilter_upload_frames
from scripts.data.analyze_skeleton_velocity_discrimination import _float_or_none
from scripts.data.validate_prefilter_joint_angle28 import (
    ALARM_COOLDOWN,
    ALARM_MIN,
    LOCAL_BASELINE_DIR,
    POSE_FRAME_INTERVAL,
    PREFILTER_KNEE65_DIR,
    PREFILTER_LOWER_DIR,
    _evaluate_frames,
    _false_alarm_frames,
    _gt_overlap_frames,
    _load_manifest_records,
    _prepare_record,
    _recompute_with_row_gate,
)
from api.inference_eval_service import aggregate_upload_clip_results

OUT_JSON = ROOT / "docs/json/prefilter-orientation-combo-experiment.json"
OUT_MD = ROOT / "docs/prefilter-orientation-combo-speed-gate.md"

# 速度底座
SPEED_BASES = (
    ("knee_ankle_mean_speed", 65.0, "knee@65"),
    ("lower_mean_speed", 60.0, "lower@60"),
)

# 待筛选豁免特征及候选阈（基于 §4.2 区分度）
ORIENT_EXEMPT_GRIDS: dict[str, list[float]] = {
    "arm_torso_angle_max": [60, 65, 70, 75, 80, 85, 90, 95, 100, 110],
    "arm_torso_angle_mean": [45, 50, 55, 60, 65, 70, 75, 80, 85],
    "elbow_waist_angle_max": [95, 105, 115, 125, 135, 145],
    "wrist_elevation_angle_max": [35, 45, 55, 65, 75, 85],
}

# 双条件豁免：肩肘躯干 + 肘开合（压 FP）
DUAL_ARM_TORSO_GRID = [70, 75, 80, 85, 90]
DUAL_ELBOW_GRID = [130, 140, 150]


def _speed_gate_blocks(row: dict[str, Any], *, speed_feature: str, speed_thr: float) -> bool:
    speed = _float_or_none(row.get(speed_feature))
    if speed is None:
        return False
    return speed > speed_thr


def _orient_exempt_gate_blocks(
    row: dict[str, Any],
    *,
    speed_feature: str,
    speed_thr: float,
    orient_feature: str,
    orient_exempt_min: float,
) -> bool:
    """下肢超速且朝向未达豁免阈 → block。"""
    if not _speed_gate_blocks(row, speed_feature=speed_feature, speed_thr=speed_thr):
        return False
    orient = _float_or_none(row.get(orient_feature))
    if orient is not None and orient >= orient_exempt_min:
        return False
    return True


def _dual_exempt_gate_blocks(
    row: dict[str, Any],
    *,
    speed_feature: str,
    speed_thr: float,
    arm_torso_min: float,
    elbow_min: float,
) -> bool:
    if not _speed_gate_blocks(row, speed_feature=speed_feature, speed_thr=speed_thr):
        return False
    torso = _float_or_none(row.get("arm_torso_angle_max"))
    elbow = _float_or_none(row.get("elbow_angle_mean"))
    if torso is not None and elbow is not None:
        if torso >= arm_torso_min and elbow >= elbow_min:
            return False
    return True


def _eval_combo(
    paths,
    prepared: list[dict[str, Any]],
    *,
    gate_fn: Callable[[dict[str, Any]], bool],
    label: str,
) -> dict[str, Any]:
    clip_results = []
    for ctx in prepared:
        frames = _recompute_with_row_gate(ctx, gate_fn=gate_fn)
        clip_results.append(
            _evaluate_frames(paths, record_id=ctx["record_id"], upload_file=ctx["upload_file"], frames=frames)
        )
    summary = aggregate_upload_clip_results(clip_results)
    return {
        "label": label,
        "detected": summary.get("detected"),
        "missed": summary.get("missed"),
        "false_alarms": summary.get("false_alarms"),
        "recall": summary.get("recall"),
        "precision_proxy": summary.get("precision_proxy"),
    }


def _eval_pure_speed(paths, prepared: list[dict[str, Any]], *, speed_feature: str, speed_thr: float, label: str) -> dict[str, Any]:
    clip_results = []
    for ctx in prepared:
        frames = recompute_prefilter_upload_frames(
            ctx["timeline_frames"],
            ctx["export_indices"],
            ctx["boxes"],
            record_id=ctx["record_id"],
            speed_gate=SpeedGateConfig(feature=speed_feature, max_threshold=speed_thr, fail_open=True),
            infer_width=ctx["infer_w"],
            infer_height=ctx["infer_h"],
            video_fps=ctx["fps"],
            alarm_min_consecutive_frames=ALARM_MIN,
            alarm_cooldown_frames=ALARM_COOLDOWN,
        )
        clip_results.append(
            _evaluate_frames(paths, record_id=ctx["record_id"], upload_file=ctx["upload_file"], frames=frames)
        )
    summary = aggregate_upload_clip_results(clip_results)
    return {
        "label": label,
        "detected": summary.get("detected"),
        "missed": summary.get("missed"),
        "false_alarms": summary.get("false_alarms"),
        "recall": summary.get("recall"),
        "precision_proxy": summary.get("precision_proxy"),
    }


def _screen_orient_on_speed_frames(
    prepared: list[dict[str, Any]],
    *,
    speed_feature: str,
    speed_thr: float,
    orient_feature: str,
    thresholds: list[float],
) -> list[dict[str, Any]]:
    """在「会被速度门控」的帧上，扫描朝向豁免阈的标真/误报命中率。"""
    gt_hits: dict[float, int] = {t: 0 for t in thresholds}
    fa_hits: dict[float, int] = {t: 0 for t in thresholds}
    gt_total = 0
    fa_total = 0

    for ctx in prepared:
        gt_frames = _gt_overlap_frames(ctx.get("baseline_frames") or [], ctx["gt_segments"])
        fa_frames = _false_alarm_frames(ctx.get("baseline_frames") or [], ctx["gt_segments"])
        merged = ctx["merged_rows"]

        for frame_set, is_gt in ((gt_frames, True), (fa_frames, False)):
            for fi in frame_set:
                for (fidx, _tid), row in merged.items():
                    if fidx != fi:
                        continue
                    if not _speed_gate_blocks(row, speed_feature=speed_feature, speed_thr=speed_thr):
                        continue
                    orient = _float_or_none(row.get(orient_feature))
                    if orient is None:
                        continue
                    if is_gt:
                        gt_total += 1
                        for t in thresholds:
                            if orient >= t:
                                gt_hits[t] += 1
                    else:
                        fa_total += 1
                        for t in thresholds:
                            if orient >= t:
                                fa_hits[t] += 1

    rows: list[dict[str, Any]] = []
    for t in thresholds:
        gt_rate = round(gt_hits[t] / gt_total, 4) if gt_total else 0.0
        fa_rate = round(fa_hits[t] / fa_total, 4) if fa_total else 0.0
        rows.append({
            "orient_feature": orient_feature,
            "exempt_min": t,
            "gt_exempt_rate": gt_rate,
            "fa_exempt_rate": fa_rate,
            "separation": round(gt_rate - fa_rate, 4),
            "speed_feature": speed_feature,
            "speed_thr": speed_thr,
        })
    rows.sort(key=lambda r: (-float(r["separation"]), float(r["fa_exempt_rate"])))
    return rows


def _narrow_grid(grid: list[float], best: float | None) -> list[float]:
    """围绕筛选最优阈缩小全量评估范围。"""
    if best is None:
        return grid[:5]
    idx = min(range(len(grid)), key=lambda i: abs(grid[i] - best))
    lo, hi = max(0, idx - 2), min(len(grid), idx + 3)
    return sorted(set(grid[lo:hi]))


def _pick_best(
    combo_results: list[dict[str, Any]],
    *,
    ref_knee: dict[str, Any],
    ref_lower: dict[str, Any],
    max_fp_over_knee: int = 12,
) -> dict[str, Any]:
    """在 FP 不超过 knee@65+max_fp_over_knee 约束下，优先最小 FN，其次最小 FP。"""
    knee_fp = int(ref_knee.get("false_alarms") or 9999)
    knee_fn = int(ref_knee.get("missed") or 9999)
    fp_cap = knee_fp + max_fp_over_knee

    candidates = [
        r for r in combo_results
        if int(r.get("false_alarms") or 9999) <= fp_cap
    ]
    if not candidates:
        candidates = list(combo_results)

    candidates.sort(
        key=lambda r: (
            int(r.get("missed") or 9999),
            int(r.get("false_alarms") or 9999),
            -float(r.get("recall") or 0),
        )
    )
    best = candidates[0] if candidates else {}
    best["fp_cap"] = fp_cap
    best["ref_knee_fp"] = knee_fp
    best["ref_knee_fn"] = knee_fn
    best["ref_lower_fp"] = int(ref_lower.get("false_alarms") or 0)
    best["ref_lower_fn"] = int(ref_lower.get("missed") or 0)
    return best


def _build_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 朝向特征 + 速度门控组合实验",
        "",
        f"> 生成时间：{report.get('generated_at', '')}",
        "> 脚本：`scripts/data/validate_prefilter_orientation_combo28.py`",
        "",
        "## 1. 参数",
        "",
        "| 参数 | 值 |",
        "|------|-----|",
        f"| pose_frame_interval | {report['params']['pose_frame_interval']} |",
        f"| alarm_min_consecutive_frames | {report['params']['alarm_min_consecutive_frames']} |",
        f"| alarm_cooldown_frames | {report['params']['alarm_cooldown_frames']} |",
        "",
        "## 2. 实验逻辑",
        "",
        "门控：下肢速度超阈 **且** 朝向未达豁免阈 → 跳过手腕进框。",
        "",
        "豁免特征（取较大侧或均值）：",
        "- `arm_torso_angle_max`：∠(髋心, 肩, 肘) 峰值",
        "- `arm_torso_angle_mean`：肩-肘相对躯干均值",
        "- 双条件：`arm_torso_angle_max` **且** `elbow_angle_mean` 同时达标",
        "",
        "## 3. 帧级阈值筛选（速度已超阈子集）",
        "",
        "在会被速度门控的帧上，比较标真 vs 误报的「豁免命中率」=`P(orient≥阈)`。",
        "优选 **separation = 标真命中率 − 误报命中率** 最大的阈。",
        "",
    ]

    for block in report.get("threshold_screens") or []:
        sf = block.get("speed_label") or ""
        lines.append(f"### {sf}")
        lines.append("")
        lines.append("| 特征 | 豁免阈≥ | 标真豁免率 | 误报豁免率 | separation |")
        lines.append("|------|---------|------------|------------|------------|")
        for row in (block.get("top_rows") or [])[:8]:
            lines.append(
                f"| `{row.get('orient_feature')}` | {row.get('exempt_min')} | "
                f"{row.get('gt_exempt_rate')} | {row.get('fa_exempt_rate')} | {row.get('separation')} |"
        )
        lines.append("")

    lines.extend([
        "## 4. 参照方案",
        "",
        "| 方案 | TP | FP | FN | 召回率 |",
        "|------|-----|-----|-----|--------|",
    ])
    for row in report.get("references") or []:
        lines.append(
            f"| {row.get('label')} | {row.get('detected')} | {row.get('false_alarms')} | "
            f"{row.get('missed')} | {row.get('recall')} |"
        )

    lines.extend([
        "",
        "## 5. 组合门控全量评估（按 FP 约束优选）",
        "",
        f"优选准则：FP ≤ knee@65 + {report.get('fp_cap_delta', 12)}，最小化 FN，其次 FP。",
        "",
        "| 方案 | TP | FP | FN | 召回率 | ΔFP vs knee@65 | ΔFN vs knee@65 |",
        "|------|-----|-----|-----|--------|----------------|----------------|",
    ])
    knee_fp = int((report.get("references") or [{}])[2].get("false_alarms") or 298) if len(report.get("references") or []) > 2 else 298
    knee_fn = int((report.get("references") or [{}])[2].get("missed") or 12) if len(report.get("references") or []) > 2 else 12
    for row in report.get("combo_top") or []:
        dfp = int(row.get("false_alarms") or 0) - knee_fp
        dfn = int(row.get("missed") or 0) - knee_fn
        lines.append(
            f"| {row.get('label')} | {row.get('detected')} | {row.get('false_alarms')} | "
            f"{row.get('missed')} | {row.get('recall')} | {dfp:+d} | {dfn:+d} |"
        )

    best = report.get("best_combo") or {}
    lines.extend([
        "",
        f"**优选组合**：{best.get('label', '—')}",
        "",
        f"- TP={best.get('detected')} FP={best.get('false_alarms')} FN={best.get('missed')} recall={best.get('recall')}",
        f"- 相对 knee@65：ΔFP={int(best.get('false_alarms') or 0) - knee_fp:+d}，ΔFN={int(best.get('missed') or 0) - knee_fn:+d}",
        "",
        "## 6. 结论",
        "",
        report.get("conclusion", ""),
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    resolve_config_path(None)
    paths = resolve_app_paths()
    records = _load_manifest_records()
    prepared: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for entry in records:
        ctx = _prepare_record(entry)
        if not ctx or ctx.get("error"):
            rid = str(entry.get("record_id") or "")
            errors.append({"record_id": rid, "error": (ctx or {}).get("error") or "unknown"})
            print(f"{rid}: SKIP")
            continue
        prepared.append(ctx)
        print(f"{ctx['record_id']}: ok")

    if not prepared:
        return 2

    # --- 阶段一：帧级阈值筛选 ---
    threshold_screens: list[dict[str, Any]] = []
    screened_best_thresholds: dict[tuple[str, str, float], float] = {}

    for speed_feature, speed_thr, speed_label in SPEED_BASES:
        block_rows: list[dict[str, Any]] = []
        for orient_feature, grid in ORIENT_EXEMPT_GRIDS.items():
            rows = _screen_orient_on_speed_frames(
                prepared,
                speed_feature=speed_feature,
                speed_thr=speed_thr,
                orient_feature=orient_feature,
                thresholds=grid,
            )
            block_rows.extend(rows)
            if rows:
                top = rows[0]
                screened_best_thresholds[(speed_feature, orient_feature, speed_thr)] = float(top["exempt_min"])
                print(
                    f"  screen {speed_label} + {orient_feature}: best thr>={top['exempt_min']} "
                    f"sep={top['separation']} gt={top['gt_exempt_rate']} fa={top['fa_exempt_rate']}"
                )
        block_rows.sort(key=lambda r: (-float(r["separation"]), float(r["fa_exempt_rate"])))
        threshold_screens.append({
            "speed_label": speed_label,
            "speed_feature": speed_feature,
            "speed_thr": speed_thr,
            "top_rows": block_rows[:12],
        })

    # --- 参照 ---
    print("\n=== 参照方案 ===")
    ref_local = _eval_combo(
        paths, prepared,
        gate_fn=lambda r: False,
        label="local baseline（无门控）",
    )
    # baseline = no gate means never block - need all frames processed. gate_fn always False = never block, good.

    ref_lower = _eval_pure_speed(paths, prepared, speed_feature="lower_mean_speed", speed_thr=60.0, label="lower@60 prefilter")
    ref_knee = _eval_pure_speed(paths, prepared, speed_feature="knee_ankle_mean_speed", speed_thr=65.0, label="knee@65 prefilter")
    ref_knee_elbow = _eval_combo(
        paths, prepared,
        gate_fn=lambda r, sf="knee_ankle_mean_speed", st=65.0: _orient_exempt_gate_blocks(
            r, speed_feature=sf, speed_thr=st, orient_feature="elbow_angle_mean", orient_exempt_min=150.0
        ),
        label="knee@65 + 肘角豁免≥150（对照）",
    )

    references = [ref_local, ref_lower, ref_knee, ref_knee_elbow]
    for r in references:
        print(f"  {r['label']}: TP={r['detected']} FP={r['false_alarms']} FN={r['missed']} recall={r['recall']}")

    # --- 阶段二：组合全量评估 ---
    combo_results: list[dict[str, Any]] = []

    for speed_feature, speed_thr, speed_label in SPEED_BASES:
        for orient_feature, grid in ORIENT_EXEMPT_GRIDS.items():
            key = (speed_feature, orient_feature, speed_thr)
            best_thr = screened_best_thresholds.get(key)
            thr_set = _narrow_grid(grid, best_thr)
            for orient_thr in thr_set:
                label = f"{speed_label} + {orient_feature}≥{orient_thr}"
                res = _eval_combo(
                    paths, prepared,
                    gate_fn=lambda r, sf=speed_feature, st=speed_thr, of=orient_feature, ot=orient_thr: _orient_exempt_gate_blocks(
                        r, speed_feature=sf, speed_thr=st, orient_feature=of, orient_exempt_min=ot
                    ),
                    label=label,
                )
                res["speed_feature"] = speed_feature
                res["speed_thr"] = speed_thr
                res["orient_feature"] = orient_feature
                res["orient_exempt_min"] = orient_thr
                combo_results.append(res)

        for arm_thr in DUAL_ARM_TORSO_GRID:
            for elbow_thr in DUAL_ELBOW_GRID:
                label = f"{speed_label} + 躯干{arm_thr}° & 肘{elbow_thr}°"
                res = _eval_combo(
                    paths, prepared,
                    gate_fn=lambda r, sf=speed_feature, st=speed_thr, at=arm_thr, et=elbow_thr: _dual_exempt_gate_blocks(
                        r, speed_feature=sf, speed_thr=st, arm_torso_min=at, elbow_min=et
                    ),
                    label=label,
                )
                res["dual_arm_torso_min"] = arm_thr
                res["dual_elbow_min"] = elbow_thr
                res["speed_feature"] = speed_feature
                combo_results.append(res)

    combo_results.sort(
        key=lambda r: (
            int(r.get("missed") or 9999),
            int(r.get("false_alarms") or 9999),
            -float(r.get("recall") or 0),
        )
    )

    best = _pick_best(combo_results, ref_knee=ref_knee, ref_lower=ref_lower, max_fp_over_knee=12)
    combo_top = [r for r in combo_results if int(r.get("false_alarms") or 9999) <= int(best.get("fp_cap") or 9999)]
    combo_top.sort(
        key=lambda r: (
            int(r.get("missed") or 9999),
            int(r.get("false_alarms") or 9999),
            -float(r.get("recall") or 0),
        )
    )
    combo_top = combo_top[:15]

    # 结论
    b_fp = int(best.get("false_alarms") or 0)
    b_fn = int(best.get("missed") or 0)
    k_fp = int(ref_knee.get("false_alarms") or 0)
    k_fn = int(ref_knee.get("missed") or 0)
    l_fp = int(ref_lower.get("false_alarms") or 0)
    l_fn = int(ref_lower.get("missed") or 0)

    if b_fn < k_fn and b_fp <= k_fp + 12:
        conclusion = (
            f"优选 `{best.get('label')}`：相对 knee@65 漏报 {k_fn}→{b_fn}（少 {k_fn - b_fn} 段），"
            f"FP {k_fp}→{b_fp}（{b_fp - k_fp:+d}）。在 FP 约束内朝向豁免有效。"
        )
    elif b_fn <= k_fn and b_fp <= k_fp + 12:
        conclusion = (
            f"优选 `{best.get('label')}`：漏报不高于 knee@65（{b_fn} 段），FP={b_fp}。"
            "朝向特征对降漏报帮助有限，但双条件或更严阈可控制误报。"
        )
    else:
        conclusion = (
            f"在 FP≤knee@65+12 约束下，最优为 `{best.get('label')}`（FN={b_fn} FP={b_fp}）。"
            f"相对 knee@65（FN={k_fn} FP={k_fp}）/ lower@60（FN={l_fn} FP={l_fp}），"
            "单纯朝向豁免难以同时显著降漏报且压住 FP；建议采用 screened 最优阈的双条件组合。"
        )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "prepared_count": len(prepared),
        "errors": errors,
        "params": {
            "pose_frame_interval": POSE_FRAME_INTERVAL,
            "alarm_min_consecutive_frames": ALARM_MIN,
            "alarm_cooldown_frames": ALARM_COOLDOWN,
            "baseline_dir": str(LOCAL_BASELINE_DIR),
        },
        "threshold_screens": threshold_screens,
        "references": references,
        "combo_results": combo_results,
        "combo_top": combo_top,
        "best_combo": best,
        "fp_cap_delta": 12,
        "conclusion": conclusion,
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_MD.write_text(_build_markdown(report), encoding="utf-8")

    print(f"\n=== 优选 ===\n{best.get('label')}: TP={best.get('detected')} FP={b_fp} FN={b_fn}")
    print(f"结论: {conclusion}")
    print(f"JSON: {OUT_JSON}")
    print(f"MD:   {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
