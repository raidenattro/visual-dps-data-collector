#!/usr/bin/env python3
"""站立姿态豁免实验：ankle_max@80 + triple90 + 非站立不 block。

参数对齐 manifest：pose_frame_interval=2, alarm_min=3, cooldown=0。

用法（项目根目录）:
  python scripts/data/validate_prefilter_stance_exempt28.py
"""

from __future__ import annotations

import itertools
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
from scripts.data.validate_prefilter_foot_ankle28 import SQUAT_WATCH_CLIPS
from scripts.data.validate_prefilter_joint_angle28 import (
    ALARM_COOLDOWN,
    ALARM_MIN,
    LOCAL_BASELINE_DIR,
    LOCAL_BASELINE_MANIFEST,
    POSE_FRAME_INTERVAL,
    _evaluate_frames,
    _false_alarm_frames,
    _gt_overlap_frames,
    _load_manifest_records,
    _prepare_record,
    _recompute_with_row_gate,
)
from scripts.data.validate_prefilter_multi_angle_logic28 import (
    _conds_met_and,
    _eval_combo,
    _multi_logic_gate_blocks,
    _speed_high,
)
from api.inference_eval_service import aggregate_upload_clip_results

OUT_JSON = ROOT / "docs/json/prefilter-stance-exempt-experiment.json"
OUT_MD = ROOT / "docs/prefilter-stance-exempt-speed-gate.md"

SPEED_FEATURE = "ankle_max_speed"
SPEED_THRESHOLD = 80.0
TRIPLE_AND_CONDS: list[tuple[str, float]] = [
    ("arm_torso_angle_max", 90.0),
    ("elbow_angle_mean", 150.0),
    ("wrist_elevation_angle_max", 60.0),
]

# 站立判定：特征 >= 阈 → 站立，门控可生效
KNEE_STANCE_THRESHOLDS = [120.0, 130.0, 140.0, 150.0]
# 上下半身整体夹角 ∠(肩,髋,踝)：蹲姿变小，行走时相对稳定
TORSO_LEG_STANCE_THRESHOLDS = [130.0, 140.0, 150.0, 160.0, 170.0]
LEG_SPAN_THRESHOLDS = [0.8, 1.0, 1.2, 1.5]

# 采用肩-髋-踝整体夹角作为站立判定（∠肩,髋,踝）
RECOMMENDED_STANCE_FEATURE = "torso_leg_angle_mean"
RECOMMENDED_STANCE_THRESHOLD = 160.0

STANCE_FEATURE_GRIDS: tuple[tuple[str, list[float], str], ...] = (
    ("torso_leg_angle_mean", TORSO_LEG_STANCE_THRESHOLDS, "gte"),
    ("torso_leg_angle_min", TORSO_LEG_STANCE_THRESHOLDS, "gte"),
    ("center_torso_leg_angle", TORSO_LEG_STANCE_THRESHOLDS, "gte"),
    ("knee_angle_mean", KNEE_STANCE_THRESHOLDS, "gte"),
    ("knee_angle_min", KNEE_STANCE_THRESHOLDS, "gte"),
    ("leg_span_ratio", LEG_SPAN_THRESHOLDS, "gte"),
)


def _is_standing(row: dict[str, Any], *, stance_feat: str, stance_thr: float, cmp: str) -> bool:
    """缺数据按站立处理（门控仍可生效，保守压误报）。"""
    v = _float_or_none(row.get(stance_feat))
    if v is None:
        return True
    if cmp == "gte":
        return v >= stance_thr
    return v >= stance_thr


def _stance_exempt_gate_blocks(
    row: dict[str, Any],
    *,
    speed_feature: str,
    speed_thr: float,
    triple_conds: list[tuple[str, float]],
    stance_feat: str,
    stance_thr: float,
    stance_cmp: str = "gte",
) -> bool:
    """下肢超速且未满足 triple90 且为站立姿态 → block。"""
    if not _speed_high(row, speed_feature=speed_feature, speed_thr=speed_thr):
        return False
    if _conds_met_and(row, triple_conds):
        return False
    if not _is_standing(row, stance_feat=stance_feat, stance_thr=stance_thr, cmp=stance_cmp):
        return False
    return True


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


def _gate_blocked_frames(
    ctx: dict[str, Any],
    *,
    gate_fn,
    frame_set: set[int],
) -> list[int]:
    merged = ctx.get("merged_rows") or {}
    blocked: list[int] = []
    for (fi, _tid), row in merged.items():
        if int(fi) not in frame_set:
            continue
        if gate_fn(row):
            blocked.append(int(fi))
    return sorted(set(blocked))


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

    print(f"准备 {len(prepared)} 条记录，扫描姿态豁免网格…")

    refs: dict[str, Any] = {}
    for label, gate_fn in (
        ("baseline_no_gate", lambda _r: False),
        ("ankle_max80_triple90", _triple90_only_blocks),
    ):
        clip_results = []
        for ctx in prepared:
            frames = _recompute_with_row_gate(ctx, gate_fn=gate_fn)
            clip_results.append(
                _evaluate_frames(paths, record_id=ctx["record_id"], upload_file=ctx["upload_file"], frames=frames)
            )
        refs[label] = aggregate_upload_clip_results(clip_results)

    triple_fn = _triple90_only_blocks
    triple_summary = refs.get("ankle_max80_triple90") or {}
    baseline_summary = refs.get("baseline_no_gate") or {}
    target_fn = int(baseline_summary.get("missed") or 9)
    fp_cap = 310

    grid_results: list[dict[str, Any]] = []

    for stance_feat, thresholds, cmp in STANCE_FEATURE_GRIDS:
        for stance_thr in thresholds:
            label = (
                f"ankle_max@80 + triple90 + standing({stance_feat}>={stance_thr})"
            )
            gate_fn = lambda row, sf=stance_feat, st=stance_thr, c=cmp: _stance_exempt_gate_blocks(
                row,
                speed_feature=SPEED_FEATURE,
                speed_thr=SPEED_THRESHOLD,
                triple_conds=TRIPLE_AND_CONDS,
                stance_feat=sf,
                stance_thr=st,
                stance_cmp=c,
            )
            clip_results = []
            for ctx in prepared:
                frames = _recompute_with_row_gate(ctx, gate_fn=gate_fn)
                clip_results.append(
                    _evaluate_frames(
                        paths,
                        record_id=ctx["record_id"],
                        upload_file=ctx["upload_file"],
                        frames=frames,
                    )
                )
            summary = aggregate_upload_clip_results(clip_results)
            grid_results.append({
                "label": label,
                "stance_feature": stance_feat,
                "stance_threshold": stance_thr,
                "stance_cmp": cmp,
                "detected": summary.get("detected"),
                "missed": summary.get("missed"),
                "false_alarms": summary.get("false_alarms"),
                "recall": summary.get("recall"),
                "precision_proxy": summary.get("precision_proxy"),
                "delta_fn_vs_triple90": int(summary.get("missed") or 0) - int(triple_summary.get("missed") or 0),
                "delta_fp_vs_triple90": int(summary.get("false_alarms") or 0) - int(triple_summary.get("false_alarms") or 0),
            })

    grid_results.sort(key=_sort_key)

    fn_target = [r for r in grid_results if int(r.get("missed") or 99) <= target_fn]
    fn_target.sort(key=lambda r: (int(r.get("false_alarms") or 9999), -float(r.get("recall") or 0)))
    fp_bounded = [r for r in grid_results if int(r.get("false_alarms") or 9999) <= fp_cap]
    fp_bounded.sort(key=_sort_key)

    best_fn = fn_target[0] if fn_target else grid_results[0]
    best_fp = fp_bounded[0] if fp_bounded else grid_results[0]
    best_adopted = next(
        (
            r
            for r in grid_results
            if r.get("stance_feature") == RECOMMENDED_STANCE_FEATURE
            and float(r.get("stance_threshold") or 0) == RECOMMENDED_STANCE_THRESHOLD
        ),
        best_fn,
    )

    squat_watch: list[dict[str, Any]] = []
    best_gate_fn = lambda row, b=best_adopted: _stance_exempt_gate_blocks(
        row,
        speed_feature=SPEED_FEATURE,
        speed_thr=SPEED_THRESHOLD,
        triple_conds=TRIPLE_AND_CONDS,
        stance_feat=str(b.get("stance_feature")),
        stance_thr=float(b.get("stance_threshold") or 140),
        stance_cmp=str(b.get("stance_cmp") or "gte"),
    )
    for watch_file in SQUAT_WATCH_CLIPS:
        ctx = next((c for c in prepared if c.get("upload_file") == watch_file), None)
        if not ctx:
            continue
        gt_frames = _gt_overlap_frames(ctx.get("baseline_frames") or [], ctx["gt_segments"])
        squat_watch.append({
            "upload_file": watch_file,
            "triple90_blocked": _gate_blocked_frames(ctx, gate_fn=triple_fn, frame_set=gt_frames),
            "stance_exempt_blocked": _gate_blocked_frames(ctx, gate_fn=best_gate_fn, frame_set=gt_frames),
        })

    if int(best_adopted.get("missed") or 99) <= target_fn:
        conclusion = (
            f"采用肩髋踝 `{best_adopted.get('label')}`：FN {triple_summary.get('missed')}→{best_adopted.get('missed')}，"
            f"FP {triple_summary.get('false_alarms')}→{best_adopted.get('false_alarms')}。"
        )
    else:
        conclusion = (
            f"肩髋踝推荐 `{best_adopted.get('label')}`（FN={best_adopted.get('missed')} FP={best_adopted.get('false_alarms')}）；"
            f"FP 约束下优选 `{best_fp.get('label')}`。"
        )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "prepared_count": len(prepared),
        "params": {
            "pose_frame_interval": POSE_FRAME_INTERVAL,
            "alarm_min_consecutive_frames": ALARM_MIN,
            "alarm_cooldown_frames": ALARM_COOLDOWN,
            "speed_feature": SPEED_FEATURE,
            "speed_threshold": SPEED_THRESHOLD,
            "triple_and": TRIPLE_AND_CONDS,
        },
        "references": refs,
        "target_fn_baseline": target_fn,
        "grid_results": grid_results,
        "fn_target_hits": fn_target[:15],
        "fp_bounded_top": fp_bounded[:15],
        "best_fn": best_fn,
        "best_fp_bounded": best_fp,
        "best_adopted": best_adopted,
        "recommended_stance": {
            "feature": RECOMMENDED_STANCE_FEATURE,
            "threshold": RECOMMENDED_STANCE_THRESHOLD,
            "description": "∠(肩,髋,踝) 上下半身整体夹角",
        },
        "squat_watch": squat_watch,
        "conclusion": conclusion,
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_MD.write_text(_build_md(report), encoding="utf-8")

    print(f"\n参照 ankle_max80+triple90: TP={triple_summary.get('detected')} "
          f"FP={triple_summary.get('false_alarms')} FN={triple_summary.get('missed')}")
    print(f"采用肩髋踝: {best_adopted.get('label')}")
    print(f"  TP={best_adopted.get('detected')} FP={best_adopted.get('false_alarms')} FN={best_adopted.get('missed')}")
    print(f"结论: {conclusion}")
    print(f"MD: {OUT_MD}")
    return 0


def _build_md(report: dict[str, Any]) -> str:
    refs = report.get("references") or {}
    triple = refs.get("ankle_max80_triple90") or {}
    base = refs.get("baseline_no_gate") or {}
    lines = [
        "# 站立姿态门控豁免实验（ankle_max@80 + triple90）",
        "",
        f"- 生成时间: {report.get('generated_at')}",
        f"- 记录数: {report.get('prepared_count')}",
        "",
        "## 1. 参照",
        "",
        "| 方案 | TP | FP | FN | 召回 |",
        "|------|-----|-----|-----|------|",
        f"| baseline（无门控） | {base.get('detected')} | {base.get('false_alarms')} | {base.get('missed')} | {base.get('recall')} |",
        f"| ankle_max@80 + triple90 | {triple.get('detected')} | {triple.get('false_alarms')} | {triple.get('missed')} | {triple.get('recall')} |",
        "",
        "## 2. 门控逻辑",
        "",
        "```",
        "block = ankle_max_speed > 80",
        "    AND NOT triple90(arm_torso≥90 AND elbow≥150 AND wrist_elev≥60)",
        "    AND is_standing(stance_feature >= threshold)",
        "```",
        "",
        "非站立（蹲/蹲取）时即使超速也不 block。",
        "",
        "## 3. 网格 Top（FN 优先）",
        "",
        "| 方案 | TP | FP | FN | 召回 | ΔFN | ΔFP |",
        "|------|-----|-----|-----|------|-----|-----|",
    ]
    for r in (report.get("grid_results") or [])[:20]:
        lines.append(
            f"| {r.get('label')} | {r.get('detected')} | {r.get('false_alarms')} | "
            f"{r.get('missed')} | {r.get('recall')} | {r.get('delta_fn_vs_triple90')} | {r.get('delta_fp_vs_triple90')} |"
        )
    best = report.get("best_adopted") or report.get("best_fn") or {}
    rec = report.get("recommended_stance") or {}
    lines.extend([
        "",
        "## 4. 推荐组合（肩-髋-踝）",
        "",
        f"- **{best.get('label')}**",
        f"- 站立特征：`{rec.get('feature')}` ≥ {rec.get('threshold')}（{rec.get('description')}）",
        f"- TP={best.get('detected')} FP={best.get('false_alarms')} FN={best.get('missed')} recall={best.get('recall')}",
        "",
        "## 5. squat_watch 门控帧",
        "",
        "| clip | triple90 blocked | stance exempt blocked |",
        "|------|------------------|----------------------|",
    ])
    for w in report.get("squat_watch") or []:
        lines.append(
            f"| {w.get('upload_file')} | {len(w.get('triple90_blocked') or [])} | "
            f"{len(w.get('stance_exempt_blocked') or [])} |"
        )
    lines.extend(["", "## 6. 结论", "", str(report.get("conclusion") or ""), ""])
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
