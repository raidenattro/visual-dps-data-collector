#!/usr/bin/env python3
"""肩髋膝站立特征替代 torso160 网格实验（28 clip）。

规则：ankle_max@80 + triple90 AND + 站立时 block。
站立判定：∠(肩,髋,膝) >= 阈值（顶点在髋）。

用法（项目根目录）:
  python scripts/data/validate_prefilter_shoulder_hip_knee_stance28.py
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
    _multi_logic_gate_blocks,
    _speed_high,
)
from api.inference_eval_service import aggregate_upload_clip_results

OUT_JSON = ROOT / "docs/json/prefilter-shoulder-hip-knee-stance-experiment.json"
OUT_MD = ROOT / "docs/prefilter-ankle80-triple90-shknee-stance-experiment.md"

SPEED_FEATURE = "ankle_max_speed"
SPEED_THRESHOLD = 80.0
TRIPLE_AND_CONDS: list[tuple[str, float]] = [
    ("arm_torso_angle_max", 90.0),
    ("elbow_angle_mean", 150.0),
    ("wrist_elevation_angle_max", 60.0),
]

# ∠(肩,髋,膝)：站立时髋伸展角较大，蹲姿变小
SHKNEE_THRESHOLDS = [100.0, 110.0, 120.0, 130.0, 140.0, 150.0, 160.0, 170.0, 180.0]

STANCE_FEATURE_GRIDS: tuple[tuple[str, list[float]], ...] = (
    ("shoulder_hip_knee_angle_mean", SHKNEE_THRESHOLDS),
    ("shoulder_hip_knee_angle_min", SHKNEE_THRESHOLDS),
    ("center_shoulder_hip_knee_angle", SHKNEE_THRESHOLDS),
)

TORSO160_REF = {
    "label": "ankle_max@80 + triple90 + torso_leg_angle_mean>=160",
    "stance_feature": "torso_leg_angle_mean",
    "stance_threshold": 160.0,
    "detected": 146,
    "missed": 10,
    "false_alarms": 311,
    "recall": 0.9359,
}


def _is_standing(row: dict[str, Any], *, stance_feat: str, stance_thr: float) -> bool:
    v = _float_or_none(row.get(stance_feat))
    if v is None:
        return True
    return v >= stance_thr


def _stance_exempt_gate_blocks(
    row: dict[str, Any],
    *,
    stance_feat: str,
    stance_thr: float,
) -> bool:
    if not _speed_high(row, speed_feature=SPEED_FEATURE, speed_thr=SPEED_THRESHOLD):
        return False
    if _conds_met_and(row, TRIPLE_AND_CONDS):
        return False
    if not _is_standing(row, stance_feat=stance_feat, stance_thr=stance_thr):
        return False
    return True


def _torso160_gate_blocks(row: dict[str, Any]) -> bool:
    return _stance_exempt_gate_blocks(
        row, stance_feat="torso_leg_angle_mean", stance_thr=160.0
    )


def _triple90_only_blocks(row: dict[str, Any]) -> bool:
    return _multi_logic_gate_blocks(
        row,
        speed_feature=SPEED_FEATURE,
        speed_thr=SPEED_THRESHOLD,
        conds=TRIPLE_AND_CONDS,
        logic="and",
    )


def _sort_key(r: dict[str, Any]) -> tuple:
    return (
        int(r.get("missed") or 99),
        int(r.get("false_alarms") or 9999),
        -float(r.get("recall") or 0),
    )


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


def main() -> int:
    paths = resolve_app_paths()
    records = _load_manifest_records()
    prepared: list[dict[str, Any]] = []
    for entry in records:
        ctx = _prepare_record(entry)
        if ctx and not ctx.get("error"):
            prepared.append(ctx)
    if not prepared:
        print("无可用记录")
        return 2

    print(f"准备 {len(prepared)} 条记录，扫描肩髋膝站立网格…")

    triple_summary = _run_gate(prepared, _triple90_only_blocks)
    torso160_summary = _run_gate(prepared, _torso160_gate_blocks)

    target_fn = int(torso160_summary.get("missed") or TORSO160_REF["missed"])
    target_fp = int(torso160_summary.get("false_alarms") or TORSO160_REF["false_alarms"])

    grid_results: list[dict[str, Any]] = []
    for stance_feat, thresholds in STANCE_FEATURE_GRIDS:
        for stance_thr in thresholds:
            label = f"ankle_max@80 + triple90 + {stance_feat}>={stance_thr:.0f}"
            gate_fn = lambda row, sf=stance_feat, st=stance_thr: _stance_exempt_gate_blocks(
                row, stance_feat=sf, stance_thr=st
            )
            summary = _run_gate(prepared, gate_fn)
            grid_results.append({
                "label": label,
                "stance_feature": stance_feat,
                "stance_threshold": stance_thr,
                "detected": summary.get("detected"),
                "missed": summary.get("missed"),
                "false_alarms": summary.get("false_alarms"),
                "recall": summary.get("recall"),
                "precision_proxy": summary.get("precision_proxy"),
                "delta_fn_vs_torso160": int(summary.get("missed") or 0) - target_fn,
                "delta_fp_vs_torso160": int(summary.get("false_alarms") or 0) - target_fp,
            })

    grid_results.sort(key=_sort_key)

    fn_bounded = [r for r in grid_results if int(r.get("missed") or 99) <= target_fn]
    fn_bounded.sort(key=lambda r: (
        abs(int(r.get("false_alarms") or 0) - target_fp),
        int(r.get("false_alarms") or 9999),
    ))
    best_match = fn_bounded[0] if fn_bounded else grid_results[0]
    best_fp = min(grid_results, key=lambda r: (int(r.get("missed") or 99), int(r.get("false_alarms") or 9999)))

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "clips": len(prepared),
        "refs": {
            "triple90_only": triple_summary,
            "torso160": torso160_summary,
        },
        "grid": grid_results,
        "best_fn_bounded_match_torso160": best_match,
        "best_overall_fp": best_fp,
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 肩髋膝站立特征替代 torso160 实验",
        "",
        f"生成时间（UTC）: {payload['generated_at']}",
        "",
        "## 1. 背景",
        "",
        "原规则 `ankle_max@80 + triple90 + torso_leg_angle_mean>=160` 用 ∠(肩,髋,踝) 判定站立。",
        "本实验用 **∠(肩,髋,膝)**（顶点在髋，肩-髋-膝内角）替代 torso，",
        "在相同 ankle@80 + triple90 前提下扫描阈值，看能否复现 torso160 的 FN/FP 水平。",
        "",
        "## 2. 特征定义",
        "",
        "| 特征 | 几何含义 |",
        "|------|----------|",
        "| `shoulder_hip_knee_angle_mean` | 左右肩-髋-膝及中心角均值 |",
        "| `shoulder_hip_knee_angle_min` | 上述角度最小值 |",
        "| `center_shoulder_hip_knee_angle` | 肩心-髋心-膝心夹角 |",
        "",
        "站立时髋伸展角较大；蹲姿/弯腰取货时该角变小，与 torso_leg 类似但不含踝点。",
        "",
        "## 3. 参考基线（28 clip 重算）",
        "",
        "| 规则 | TP | FN | FP | 召回 |",
        "|------|----|----|-----|------|",
        f"| triple90 only | {triple_summary.get('detected')} | {triple_summary.get('missed')} | {triple_summary.get('false_alarms')} | {float(triple_summary.get('recall') or 0):.2%} |",
        f"| torso160（对照） | {torso160_summary.get('detected')} | {torso160_summary.get('missed')} | {torso160_summary.get('false_alarms')} | {float(torso160_summary.get('recall') or 0):.2%} |",
        "",
        "## 4. 网格扫描结果（FN≤torso160 且 FP 最接近）",
        "",
        f"**推荐**: `{best_match['label']}`",
        "",
        f"| 指标 | torso160 | 推荐肩髋膝 | Δ |",
        f"|------|----------|------------|---|",
        f"| TP | {torso160_summary.get('detected')} | {best_match.get('detected')} | {int(best_match.get('detected') or 0) - int(torso160_summary.get('detected') or 0):+d} |",
        f"| FN | {target_fn} | {best_match.get('missed')} | {best_match.get('delta_fn_vs_torso160'):+d} |",
        f"| FP | {target_fp} | {best_match.get('false_alarms')} | {best_match.get('delta_fp_vs_torso160'):+d} |",
        f"| 召回 | {float(torso160_summary.get('recall') or 0):.2%} | {float(best_match.get('recall') or 0):.2%} | |",
        "",
        "## 5. Top 15（按 FN、FP 排序）",
        "",
        "| 规则 | TP | FN | FP | 召回 | ΔFN | ΔFP |",
        "|------|----|----|-----|------|-----|-----|",
    ]
    for r in grid_results[:15]:
        lines.append(
            f"| {r['label']} | {r.get('detected')} | {r.get('missed')} | {r.get('false_alarms')} | "
            f"{float(r.get('recall') or 0):.2%} | {r.get('delta_fn_vs_torso160'):+d} | {r.get('delta_fp_vs_torso160'):+d} |"
        )

    lines.extend([
        "",
        "## 6. 结论",
        "",
    ])

    fn_ok = int(best_match.get("missed") or 99) <= target_fn
    fp_delta = int(best_match.get("delta_fp_vs_torso160") or 0)
    if fn_ok and abs(fp_delta) <= 15:
        lines.append(
            f"肩髋膝 `{best_match['stance_feature']}>={best_match['stance_threshold']:.0f}` "
            f"可近似替代 torso160（FN≤{target_fn}，FP 差 {fp_delta:+d}）。"
        )
    elif fn_ok:
        lines.append(
            f"在 FN≤{target_fn} 约束下，最优为 `{best_match['label']}`，"
            f"但 FP 较 torso160 {fp_delta:+d}，需权衡。"
        )
    else:
        lines.append("当前阈值网格内未能达到 torso160 的 FN 水平，建议扩大阈值或组合特征。")

    lines.extend([
        "",
        f"完整 JSON: `{OUT_JSON.relative_to(ROOT).as_posix()}`",
        "",
    ])

    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {OUT_MD}")
    print(f"Best match: {best_match['label']}  FN={best_match.get('missed')}  FP={best_match.get('false_alarms')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
