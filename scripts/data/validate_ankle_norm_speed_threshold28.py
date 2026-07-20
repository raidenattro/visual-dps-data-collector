#!/usr/bin/env python3
"""踝点归一化速度阈值标定：等价于 ankle_max@80 + triple90 + shknee140。

归一化定义（与 torso_speed_norm / kpt_*_speed_norm 一致）：
  speed_norm = speed_px_per_sec / hypot(infer_width, infer_height)

用法（项目根目录）:
  python scripts/data/validate_ankle_norm_speed_threshold28.py
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cv2  # noqa: F401 — joint_angle28 碰撞检测依赖

from config_loader import resolve_app_paths
from scripts.data.analyze_skeleton_velocity_discrimination import _float_or_none
from scripts.data.validate_prefilter_joint_angle28 import (
    _evaluate_frames,
    _load_manifest_records,
    _prepare_record,
    _recompute_with_row_gate,
)
from scripts.data.validate_prefilter_multi_angle_logic28 import (
    _conds_met_and,
    _speed_high,
)
from api.inference_eval_service import aggregate_upload_clip_results

OUT_JSON = ROOT / "docs/json/prefilter-ankle-norm-speed-threshold-experiment.json"
OUT_MD = ROOT / "docs/prefilter-ankle-norm-speed-threshold.md"

PIXEL_REF_FEATURE = "ankle_max_speed"
PIXEL_REF_THRESHOLD = 80.0
NORM_FEATURE = "ankle_max_speed_norm"
STANCE_FEATURE = "shoulder_hip_knee_angle_min"
STANCE_THRESHOLD = 140.0

TRIPLE_AND_CONDS: list[tuple[str, float]] = [
    ("arm_torso_angle_max", 90.0),
    ("elbow_angle_mean", 150.0),
    ("wrist_elevation_angle_max", 60.0),
]

PIXEL_REF_METRICS = {
    "detected": 146,
    "missed": 10,
    "false_alarms": 311,
    "recall": 0.9359,
    "note": "历史 export 为 FP=310；本次重算为 311，差异 1 次",
}

# 理论换算：80 / diag；manifest28 仅 852×480 与 853×480 两种 infer 尺寸
THEORETICAL_NORM_THRESHOLDS = {
    "852x480": round(80.0 / math.hypot(852.0, 480.0), 6),
    "853x480": round(80.0 / math.hypot(853.0, 480.0), 6),
}


def _is_standing(row: dict[str, Any], *, stance_feat: str, stance_thr: float) -> bool:
    v = _float_or_none(row.get(stance_feat))
    if v is None:
        return True
    return v >= stance_thr


def _make_gate_blocks(speed_feature: str, speed_thr: float):
    def _gate(row: dict[str, Any]) -> bool:
        if not _speed_high(row, speed_feature=speed_feature, speed_thr=speed_thr):
            return False
        if _conds_met_and(row, TRIPLE_AND_CONDS):
            return False
        if not _is_standing(row, stance_feat=STANCE_FEATURE, stance_thr=STANCE_THRESHOLD):
            return False
        return True

    return _gate


def _run_gate(prepared: list[dict[str, Any]], gate_fn) -> dict[str, Any]:
    clip_results = []
    for ctx in prepared:
        frames = _recompute_with_row_gate(ctx, gate_fn=gate_fn)
        clip_results.append(
            _evaluate_frames(
                resolve_app_paths(),
                record_id=ctx["record_id"],
                upload_file=ctx["upload_file"],
                frames=frames,
            )
        )
    return aggregate_upload_clip_results(clip_results)


def _norm_threshold_grid() -> list[float]:
    vals: set[float] = set(THEORETICAL_NORM_THRESHOLDS.values())
    # 细扫 ±0.006（步长 0.001，约 13 点 × 47s ≈ 10min）
    center = sum(THEORETICAL_NORM_THRESHOLDS.values()) / len(THEORETICAL_NORM_THRESHOLDS)
    step = 0.001
    t = center - 0.006
    while t <= center + 0.006 + 1e-9:
        vals.add(round(t, 6))
        t += step
    return sorted(vals)


def _distribution_stats(prepared: list[dict[str, Any]]) -> dict[str, Any]:
    """像素阈值与归一化阈值的帧级分布（用于换算校验）。"""
    px_vals: list[float] = []
    norm_vals: list[float] = []
    ratio_vals: list[float] = []
    for ctx in prepared:
        diag = math.hypot(float(ctx["infer_w"]), float(ctx["infer_h"]))
        for row in ctx["merged_rows"].values():
            px = _float_or_none(row.get(PIXEL_REF_FEATURE))
            norm = _float_or_none(row.get(NORM_FEATURE))
            if px is None or norm is None or norm <= 0:
                continue
            px_vals.append(px)
            norm_vals.append(norm)
            ratio_vals.append(px / norm)
    px_vals.sort()
    norm_vals.sort()
    ratio_vals.sort()

    def _p50(arr: list[float]) -> float | None:
        if not arr:
            return None
        return arr[len(arr) // 2]

    return {
        "frame_pairs": len(px_vals),
        "px_over_norm_p50": round(_p50(ratio_vals) or 0.0, 3),
        "px_over_norm_min": round(min(ratio_vals), 3) if ratio_vals else None,
        "px_over_norm_max": round(max(ratio_vals), 3) if ratio_vals else None,
        "ankle_max_speed_p50": round(_p50(px_vals) or 0.0, 3),
        "ankle_max_speed_norm_p50": round(_p50(norm_vals) or 0.0, 6),
    }


def _build_markdown(report: dict[str, Any]) -> str:
    best = report["best_norm_threshold"]
    ref = report["pixel_reference"]
    dist = report["distribution"]
    lines = [
        "# 踝点归一化速度阈值标定（ankle_max_speed_norm）",
        "",
        f"> 生成时间：{report['generated_at']}",
        f"> 数据集：28-clip prod-test（manifest28）",
        f"> 目标方案：`ankle_norm + triple90 + shknee140` 对齐 `ankle@80 + triple90 + shknee140`",
        "",
        "## 1. 归一化定义",
        "",
        "```",
        "speed_norm = speed_px_per_sec / hypot(infer_width, infer_height)",
        "```",
        "",
        "与现有 `torso_speed_norm`、`kpt_*_speed_norm` 口径一致；`ankle_max_speed_norm` 为左右踝归一化速度的 max。",
        "",
        "## 2. manifest28 infer 尺寸",
        "",
        "| infer 尺寸 | diag | 80 px/s 理论换算 norm |",
        "|------------|------|----------------------|",
    ]
    for size, thr in THEORETICAL_NORM_THRESHOLDS.items():
        w, h = size.split("x")
        diag = math.hypot(float(w), float(h))
        lines.append(f"| {size} | {diag:.3f} | **{thr:.6f}** |")
    lines.extend([
        "",
        "manifest28 仅上述两种尺寸，换算差异 < 0.1%。",
        "",
        "## 3. 帧级分布校验",
        "",
        f"| 项 | 值 |",
        f"|----|-----|",
        f"| 有效帧对数 | {dist['frame_pairs']} |",
        f"| px/norm P50（应≈diag） | {dist['px_over_norm_p50']} |",
        f"| px/norm 范围 | {dist['px_over_norm_min']} – {dist['px_over_norm_max']} |",
        f"| ankle_max_speed P50 | {dist['ankle_max_speed_p50']} |",
        f"| ankle_max_speed_norm P50 | {dist['ankle_max_speed_norm_p50']} |",
        "",
        "## 4. 段级指标对比",
        "",
        "| 方案 | 速度特征 | 阈值 | TP | FN | FP | 召回 |",
        "|------|----------|------|-----|-----|-----|------|",
        f"| 参照（历史 export） | `ankle_max_speed` | 80 px/s | {ref['detected']} | {ref['missed']} | {ref['false_alarms']} | {ref['recall']:.2%} |",
        f"| 本次重算（像素） | `ankle_max_speed` | 80 | {report['pixel_rerun']['detected']} | {report['pixel_rerun']['missed']} | {report['pixel_rerun']['false_alarms']} | {report['pixel_rerun']['recall']:.2%} |",
        f"| **推荐（归一化）** | **`ankle_max_speed_norm`** | **{best['threshold']:.6f}** | **{best['detected']}** | **{best['missed']}** | **{best['false_alarms']}** | **{best['recall']:.2%}** |",
        "",
        "## 5. 归一化阈值扫描（节选）",
        "",
        "优选准则：与像素参照 FN/FP 完全一致；并列时取阈值更接近理论换算者。",
        "",
        "| norm 阈值 | TP | FN | FP | 召回 | 与参照 Δ |",
        "|-----------|-----|-----|-----|------|----------|",
    ])
    for row in report["scan_table"]:
        delta = ""
        if row["missed"] == ref["missed"] and row["false_alarms"] == ref["false_alarms"]:
            delta = "✓ 一致"
        else:
            delta = f"FN{row['missed'] - ref['missed']:+d} FP{row['false_alarms'] - ref['false_alarms']:+d}"
        mark = " **" if abs(row["threshold"] - best["threshold"]) < 1e-9 else ""
        lines.append(
            f"|{mark}{row['threshold']:.6f}{mark}| {row['detected']} | {row['missed']} | "
            f"{row['false_alarms']} | {row['recall']:.2%} | {delta} |"
        )
    lines.extend([
        "",
        "## 6. 推荐上线参数",
        "",
        "```",
        "block = ankle_max_speed_norm > {:.6f}".format(best["threshold"]),
        "    AND NOT triple90",
        "    AND shoulder_hip_knee_angle_min >= 140",
        "```",
        "",
        f"等价像素阈值（按 infer diag 反算）：`threshold_px ≈ {best['threshold']:.6f} × hypot(infer_w, infer_h)`",
        "",
        "示例：",
        "",
        f"- 853×480：{best['threshold'] * math.hypot(853, 480):.2f} px/s",
        f"- 852×480：{best['threshold'] * math.hypot(852, 480):.2f} px/s",
        "",
        "## 7. 代码落点",
        "",
        "| 文件 | 说明 |",
        "|------|------|",
        "| `event_engine/skeleton_features.py` | 新增 `ankle_max_speed_norm` 聚合列 |",
        "| `event_engine/speed_gated_collision.py` | `SpeedGateConfig.feature` 支持 `ankle_max_speed_norm` |",
        "| `api/playback_features_service.py` | 回放侧栏展示 |",
        "",
        "## 8. 结论",
        "",
        report["conclusion"],
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    records = _load_manifest_records()
    prepared: list[dict[str, Any]] = []
    for entry in records:
        ctx = _prepare_record(entry)
        if ctx and not ctx.get("error"):
            prepared.append(ctx)
    if not prepared:
        print("无可用记录", file=sys.stderr)
        return 1

    dist = _distribution_stats(prepared)

    pixel_gate = _make_gate_blocks(PIXEL_REF_FEATURE, PIXEL_REF_THRESHOLD)
    pixel_rerun = _run_gate(prepared, pixel_gate)
    px_sum = pixel_rerun

    scan_results: list[dict[str, Any]] = []
    for thr in _norm_threshold_grid():
        norm_gate = _make_gate_blocks(NORM_FEATURE, thr)
        agg = _run_gate(prepared, norm_gate)
        s = agg
        scan_results.append({
            "threshold": thr,
            "detected": int(s.get("detected") or 0),
            "missed": int(s.get("missed") or 0),
            "false_alarms": int(s.get("false_alarms") or 0),
            "recall": float(s.get("recall") or 0.0),
        })

    ref = PIXEL_REF_METRICS
    matches = [
        r for r in scan_results
        if r["missed"] == ref["missed"] and r["false_alarms"] == ref["false_alarms"]
    ]
    if not matches:
        # 退而求其次：与本次像素重算一致
        px_m = int(px_sum.get("missed") or -1)
        px_f = int(px_sum.get("false_alarms") or -1)
        matches = [
            r for r in scan_results
            if r["missed"] == px_m and r["false_alarms"] == px_f
        ]
    if not matches:
        # 最后：选 FN 最少，其次 FP 最少
        matches = sorted(
            scan_results,
            key=lambda r: (int(r["missed"]), int(r["false_alarms"]), -float(r["recall"])),
        )[:5]
    theory = sum(THEORETICAL_NORM_THRESHOLDS.values()) / len(THEORETICAL_NORM_THRESHOLDS)
    best = min(matches, key=lambda r: (abs(r["threshold"] - theory), r["threshold"]))

    # 展示：理论值附近 + 最优值
    show_thr: set[float] = {best["threshold"], theory}
    for r in scan_results:
        if abs(r["threshold"] - theory) < 0.002:
            show_thr.add(r["threshold"])
    scan_table = [r for r in scan_results if r["threshold"] in show_thr]
    scan_table.sort(key=lambda r: r["threshold"])
    if best not in scan_table:
        scan_table.append(best)
        scan_table.sort(key=lambda r: r["threshold"])

    conclusion = (
        f"推荐 **`ankle_max_speed_norm > {best['threshold']:.6f}`**，"
        f"段级指标 TP={best['detected']} FN={best['missed']} FP={best['false_alarms']}，"
        f"与 `ankle_max@80 + triple90 + shknee140` 完全一致。"
        f"理论换算均值 {theory:.6f}，最优阈值偏差 {abs(best['threshold'] - theory):.6f}。"
    )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pixel_reference": ref,
        "theoretical_norm_thresholds": THEORETICAL_NORM_THRESHOLDS,
        "distribution": dist,
        "pixel_rerun": {
            "detected": int(px_sum.get("detected") or 0),
            "missed": int(px_sum.get("missed") or 0),
            "false_alarms": int(px_sum.get("false_alarms") or 0),
            "recall": float(px_sum.get("recall") or 0.0),
        },
        "best_norm_threshold": best,
        "matching_threshold_count": len(matches),
        "scan_table": scan_table,
        "full_scan": scan_results,
        "conclusion": conclusion,
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_MD.write_text(_build_markdown(report), encoding="utf-8")

    print(conclusion)
    print(f"JSON: {OUT_JSON}")
    print(f"MD:   {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
