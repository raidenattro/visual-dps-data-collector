#!/usr/bin/env python3
"""蹲/站姿态代理特征筛查：标真漏报帧 vs 误报帧 vs squat_watch。

用法（项目根目录）:
  python scripts/data/screen_prefilter_stance_proxy28.py
"""

from __future__ import annotations

import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.data.evaluate_combo1_segment_filter import _ensure_cv2_point_polygon_test

_ensure_cv2_point_polygon_test()

from scripts.data.analyze_skeleton_velocity_discrimination import _float_or_none
from scripts.data.validate_prefilter_foot_ankle28 import SQUAT_WATCH_CLIPS
from scripts.data.validate_prefilter_joint_angle28 import (
    LOCAL_BASELINE_MANIFEST,
    _false_alarm_frames,
    _gt_overlap_frames,
    _load_manifest_records,
    _prepare_record,
)

OUT_JSON = ROOT / "docs/json/prefilter-stance-proxy-screen.json"
OUT_MD = ROOT / "docs/prefilter-stance-proxy-screen.md"

SPEED_FEATURE = "ankle_max_speed"
SPEED_THRESHOLD = 80.0
TRIPLE_CONDS = (
    ("arm_torso_angle_max", 90.0),
    ("elbow_angle_mean", 150.0),
    ("wrist_elevation_angle_max", 60.0),
)

STANCE_FEATURES = (
    "torso_leg_angle_mean",
    "torso_leg_angle_min",
    "torso_leg_angle_max",
    "center_torso_leg_angle",
    "left_torso_leg_angle",
    "right_torso_leg_angle",
    "knee_angle_mean",
    "knee_angle_min",
    "knee_angle_max",
    "leg_span_ratio",
    "hip_knee_ankle_vertical_ratio",
    "elbow_waist_angle_max",
    "arm_torso_angle_max",
    "wrist_elevation_angle_max",
    "ankle_max_speed",
    "torso_speed",
)

FOCUS_SEGMENTS = (
    ("clip_0013_start_00-42-48_rtmpose_m.json", 159, 169),
    ("clip_0020_start_00-48-44_rtmpose_m.json", 79, 82),
    ("clip_0013_start_00-29-53_rtmpose_m.json", 2244, 2254),
)


def _triple_met(row: dict[str, Any]) -> bool:
    for feat, thr in TRIPLE_CONDS:
        v = _float_or_none(row.get(feat))
        if v is None or v < thr:
            return False
    return True


def _would_block_triple90(row: dict[str, Any]) -> bool:
    speed = _float_or_none(row.get(SPEED_FEATURE))
    if speed is None or speed <= SPEED_THRESHOLD:
        return False
    return not _triple_met(row)


def _p50(vals: list[float]) -> float | None:
    if not vals:
        return None
    return float(statistics.median(vals))


def _collect_rows(
    prepared: list[dict[str, Any]],
    frame_set: set[int],
    upload_file: str,
) -> list[dict[str, Any]]:
    ctx = next((c for c in prepared if c.get("upload_file") == upload_file), None)
    if not ctx:
        return []
    merged = ctx.get("merged_rows") or {}
    out: list[dict[str, Any]] = []
    for (fi, _tid), row in merged.items():
        if int(fi) not in frame_set:
            continue
        out.append(dict(row))
    return out


def _feature_stats(rows: list[dict[str, Any]], feat: str) -> dict[str, Any]:
    vals = [_float_or_none(r.get(feat)) for r in rows]
    valid = [v for v in vals if v is not None]
    if not valid:
        return {"count": 0}
    return {
        "count": len(valid),
        "p50": round(_p50(valid), 4),
        "min": round(min(valid), 4),
        "max": round(max(valid), 4),
    }


def main() -> int:
    records = _load_manifest_records()
    prepared: list[dict[str, Any]] = []
    for entry in records:
        ctx = _prepare_record(entry)
        if ctx and not ctx.get("error"):
            prepared.append(ctx)

    gt_pool: list[dict[str, Any]] = []
    fa_pool: list[dict[str, Any]] = []
    blocked_gt_pool: list[dict[str, Any]] = []
    blocked_fa_pool: list[dict[str, Any]] = []

    for ctx in prepared:
        merged = ctx.get("merged_rows") or {}
        gt_frames = _gt_overlap_frames(ctx.get("baseline_frames") or [], ctx["gt_segments"])
        fa_frames = _false_alarm_frames(ctx.get("baseline_frames") or [], ctx["gt_segments"])
        for (_fi, _tid), row in merged.items():
            fi = int(row.get("frame_idx") or 0)
            if fi in gt_frames:
                gt_pool.append(row)
                if _would_block_triple90(row):
                    blocked_gt_pool.append(row)
            if fi in fa_frames:
                fa_pool.append(row)
                if _would_block_triple90(row):
                    blocked_fa_pool.append(row)

    feature_compare: dict[str, Any] = {}
    for feat in STANCE_FEATURES:
        feature_compare[feat] = {
            "gt_all": _feature_stats(gt_pool, feat),
            "fa_all": _feature_stats(fa_pool, feat),
            "gt_blocked_triple90": _feature_stats(blocked_gt_pool, feat),
            "fa_blocked_triple90": _feature_stats(blocked_fa_pool, feat),
        }

    squat_watch: list[dict[str, Any]] = []
    for watch_file in SQUAT_WATCH_CLIPS:
        ctx = next((c for c in prepared if c.get("upload_file") == watch_file), None)
        if not ctx:
            continue
        gt_frames = _gt_overlap_frames(ctx.get("baseline_frames") or [], ctx["gt_segments"])
        merged = ctx.get("merged_rows") or {}
        blocked_frames = []
        for (fi, _tid), row in merged.items():
            if int(fi) not in gt_frames:
                continue
            if _would_block_triple90(row):
                blocked_frames.append(int(fi))
        seg_stats = {}
        for feat in STANCE_FEATURES:
            rows = _collect_rows(prepared, gt_frames, watch_file)
            seg_stats[feat] = _feature_stats(rows, feat)
        squat_watch.append({
            "upload_file": watch_file,
            "gt_frame_count": len(gt_frames),
            "triple90_blocked_frames": sorted(set(blocked_frames)),
            "feature_stats_gt": seg_stats,
        })

    focus_segments: list[dict[str, Any]] = []
    for upload_file, lo, hi in FOCUS_SEGMENTS:
        frame_set = set(range(lo, hi + 1))
        rows = _collect_rows(prepared, frame_set, upload_file)
        stats = {feat: _feature_stats(rows, feat) for feat in STANCE_FEATURES}
        blocked = sum(1 for r in rows if _would_block_triple90(r))
        focus_segments.append({
            "upload_file": upload_file,
            "frame_range": [lo, hi],
            "row_count": len(rows),
            "triple90_blocked_rows": blocked,
            "feature_stats": stats,
        })

    knee_gt = [_float_or_none(r.get("knee_angle_mean")) for r in blocked_gt_pool]
    knee_fa = [_float_or_none(r.get("knee_angle_mean")) for r in blocked_fa_pool]
    knee_gt = [v for v in knee_gt if v is not None]
    knee_fa = [v for v in knee_fa if v is not None]
    torso_gt = [_float_or_none(r.get("torso_leg_angle_mean")) for r in blocked_gt_pool]
    torso_fa = [_float_or_none(r.get("torso_leg_angle_mean")) for r in blocked_fa_pool]
    torso_gt = [v for v in torso_gt if v is not None]
    torso_fa = [v for v in torso_fa if v is not None]
    torso_min_gt = [_float_or_none(r.get("torso_leg_angle_min")) for r in blocked_gt_pool]
    torso_min_fa = [_float_or_none(r.get("torso_leg_angle_min")) for r in blocked_fa_pool]
    torso_min_gt = [v for v in torso_min_gt if v is not None]
    torso_min_fa = [v for v in torso_min_fa if v is not None]

    conclusion_parts = []
    if torso_gt and torso_fa:
        tg, tf = _p50(torso_gt), _p50(torso_fa)
        if tg is not None and tf is not None:
            conclusion_parts.append(
                f"上下半身夹角 torso_leg_mean：标真 blocked P50={tg:.1f}° vs 误报 {tf:.1f}°。"
            )
    if torso_min_gt and torso_min_fa:
        tg, tf = _p50(torso_min_gt), _p50(torso_min_fa)
        if tg is not None and tf is not None:
            conclusion_parts.append(
                f"torso_leg_min：标真 blocked P50={tg:.1f}° vs 误报 {tf:.1f}°（蹲取可看 min）。"
            )
    if knee_gt and knee_fa:
        gt_p50 = _p50(knee_gt)
        fa_p50 = _p50(knee_fa)
        if gt_p50 is not None and fa_p50 is not None:
            if gt_p50 < fa_p50 - 10:
                conclusion_parts.append(
                    f"被 triple90 门控的标真帧膝角 P50={gt_p50:.1f}° 低于误报 {fa_p50:.1f}°，"
                    "膝角可用于非站立豁免。"
                )
            else:
                conclusion_parts.append(
                    f"膝角区分度有限（标真 blocked P50={gt_p50:.1f}° vs 误报 {fa_p50:.1f}°）。"
                )
    else:
        conclusion_parts.append("膝角样本不足。")
    conclusion_parts.append(
        "俯视 2D 下上下半身夹角与膝角均难区分蹲取/行走；门控网格见 validate_prefilter_stance_exempt28。"
    )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "prepared_count": len(prepared),
        "gate_base": f"{SPEED_FEATURE}@{SPEED_THRESHOLD} + triple90",
        "feature_compare": feature_compare,
        "squat_watch": squat_watch,
        "focus_segments": focus_segments,
        "blocked_counts": {
            "gt_frames": len(gt_pool),
            "fa_frames": len(fa_pool),
            "gt_blocked_triple90": len(blocked_gt_pool),
            "fa_blocked_triple90": len(blocked_fa_pool),
        },
        "conclusion": " ".join(conclusion_parts),
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_MD.write_text(_build_md(report), encoding="utf-8")
    print(report["conclusion"])
    print(f"JSON: {OUT_JSON}")
    print(f"MD: {OUT_MD}")
    return 0


def _build_md(report: dict[str, Any]) -> str:
    lines = [
        "# 蹲/站姿态代理特征筛查",
        "",
        f"- 生成时间: {report.get('generated_at')}",
        f"- 门控底座: {report.get('gate_base')}",
        f"- 记录数: {report.get('prepared_count')}",
        "",
        "## 1. 帧级统计（triple90 门控命中）",
        "",
        "| 集合 | 帧数 |",
        "|------|------|",
    ]
    bc = report.get("blocked_counts") or {}
    lines.append(f"| 标真全帧 | {bc.get('gt_frames')} |")
    lines.append(f"| 误报全帧 | {bc.get('fa_frames')} |")
    lines.append(f"| 标真被 triple90 block | {bc.get('gt_blocked_triple90')} |")
    lines.append(f"| 误报被 triple90 block | {bc.get('fa_blocked_triple90')} |")
    lines.extend(["", "## 2. 特征 P50 对比", "", "| 特征 | 标真 blocked | 误报 blocked |", "|------|-------------|-------------|"])
    for feat in STANCE_FEATURES:
        fc = (report.get("feature_compare") or {}).get(feat) or {}
        gt_b = fc.get("gt_blocked_triple90") or {}
        fa_b = fc.get("fa_blocked_triple90") or {}
        lines.append(f"| {feat} | {gt_b.get('p50', '—')} | {fa_b.get('p50', '—')} |")
    lines.extend(["", "## 3. 重点漏报段", ""])
    for seg in report.get("focus_segments") or []:
        lines.append(f"### {seg.get('upload_file')} {seg.get('frame_range')}")
        lines.append(f"- triple90 blocked 行数: {seg.get('triple90_blocked_rows')}/{seg.get('row_count')}")
        ks = (seg.get("feature_stats") or {}).get("knee_angle_mean") or {}
        ls = (seg.get("feature_stats") or {}).get("leg_span_ratio") or {}
        lines.append(f"- knee_angle_mean P50: {ks.get('p50', '—')}")
        lines.append(f"- leg_span_ratio P50: {ls.get('p50', '—')}")
        lines.append("")
    lines.extend(["## 4. 结论", "", str(report.get("conclusion") or ""), ""])
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
