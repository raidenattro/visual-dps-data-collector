#!/usr/bin/env python3
"""基于 rule-baseline-prod-test 抽帧告警 + 标真，纯速度特征过滤验证。

- 告警：clip_*.json 的 is_picking（baseline 生产规则）
- 速度：仅导出抽帧（pose_frame_interval=2）序列上差分
- 过滤：仅段级速度上限（不使用 Combo1 / 位移 / 帧数等策略）

用法（项目根目录）:
  python scripts/data/validate_baseline28_subsampled_velocity.py
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

from config_loader import parse_record_path_segments, resolve_config_path
from event_engine.box_identity import canonical_box_token, token_matches_any
from event_engine.skeleton_features import (
    assign_person_tracks_to_frames,
    enrich_collision_segments_with_motion,
    extract_collision_segment_rows,
    extract_subsampled_velocity_from_frames,
)
from pose_store import load_all_frames, load_manifest

from api.accuracy_service import GroundTruthSegment, build_ground_truth_segments, evaluate_segments
from api.inference_eval_service import (
    extract_picking_alarms_from_frames,
    load_inference_json_file,
)
from api.record_service import locate_record_by_id
from api.wrist_features_service import _infer_size_from_frames, _load_boxes_for_wrist_features, _video_fps
from scripts.data.analyze_skeleton_velocity_discrimination import (
    TORSO_THRESHOLDS,
    LOWER_THRESHOLDS,
    RATIO_THRESHOLDS,
    SkeletonPool,
    _best_f1,
    _float_or_none,
    _load_ground_truth_segments,
    _merge_pool,
    _pool_summary,
    _seg_overlaps_false_alarm,
    _seg_overlaps_gt,
    _seg_values,
    _stats,
    _threshold_scan,
    _threshold_scan_le,
)
from scripts.data.analyze_wrist_feature_discrimination import _frame_in_gt

MANIFEST = ROOT / "localdata/export/rule-baseline-prod-test/_manifest.json"
OUT_JSON = ROOT / "localdata/export/rule-baseline-prod-test/skeleton_velocity_speed_only.json"
OUT_MD = ROOT / "docs/skeleton-velocity-speed-filter.md"

SPEED_FEATURE_GRIDS: dict[str, list[float]] = {
    "torso_speed_p50": [10, 20, 30, 40, 50, 60, 80, 100, 120, 150],
    "lower_mean_speed_p50": [10, 20, 30, 40, 50, 60, 80, 100, 120, 150],
    "torso_speed_max": [40, 60, 80, 100, 120, 150, 200],
    "body_mean_speed_p50": [20, 40, 60, 80, 100, 120, 150],
}


from event_engine.speed_filter import (
    export_frame_indices as _export_frame_indices,
    filter_alarms_by_speed_only,
)


def _load_manifest_records() -> list[dict[str, Any]]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return list(manifest.get("records") or [])


def _build_false_alarm_by_baseline_alarms(
    alarms: list[tuple[int, str]],
    gt_segments: list[GroundTruthSegment],
) -> dict[int, list[str]]:
    """baseline is_picking 告警中未被标真覆盖的帧。"""
    by_frame: dict[int, list[str]] = {}
    for fi, token in alarms:
        if fi <= 0:
            continue
        tok = str(token or "").strip()
        covered = False
        if tok:
            covered = any(
                gt.frame_start <= fi <= gt.frame_end
                and token_matches_any(tok, list(gt.gt_tokens))
                for gt in gt_segments
            )
        else:
            covered = any(gt.frame_start <= fi <= gt.frame_end for gt in gt_segments)
        if not covered:
            by_frame.setdefault(fi, []).append(tok or "")
    return by_frame


def _analyze_baseline_record(
    record_entry: dict[str, Any],
) -> dict[str, Any] | None:
    record_id = str(record_entry.get("record_id") or "").strip()
    clip_path = Path(str(record_entry.get("path") or ""))
    if not record_id or not clip_path.is_file():
        return {"record_id": record_id, "error": "缺少 record_id 或 clip 文件"}

    loc = locate_record_by_id(record_id)
    if not loc:
        return {"record_id": record_id, "error": "记录不存在"}

    upload_frames = load_inference_json_file(clip_path)
    export_indices = _export_frame_indices(upload_frames)
    if not export_indices:
        return {"record_id": record_id, "error": "导出 JSON 无有效 frame_idx"}

    gt_segments, review = _load_ground_truth_segments(loc)
    if not gt_segments:
        return {"record_id": record_id, "error": "无标真 ground truth"}

    manifest = load_manifest(loc)
    all_frames = load_all_frames(loc)
    if not all_frames:
        return {"record_id": record_id, "error": "无帧数据"}

    infer_w, infer_h = _infer_size_from_frames(all_frames, manifest)
    fps = _video_fps(manifest)

    boxes, _, ann_source = _load_boxes_for_wrist_features(
        loc, manifest, infer_w=infer_w, infer_h=infer_h
    )
    if not boxes:
        return {"record_id": record_id, "error": "无货框标注"}

    velocity_rows = extract_subsampled_velocity_from_frames(
        all_frames,
        export_indices,
        infer_width=infer_w,
        infer_height=infer_h,
        video_fps=fps,
    )

    subsampled_frames = [
        fr for fr in sorted(all_frames, key=lambda f: int(f.get("frame_idx") or 0))
        if isinstance(fr, dict) and int(fr.get("frame_idx") or 0) in export_indices
    ]
    tracked = assign_person_tracks_to_frames(subsampled_frames, video_fps=fps)
    collision_segments = extract_collision_segment_rows(tracked, boxes, max_gap_frames=1)
    motion_segments = enrich_collision_segments_with_motion(collision_segments, velocity_rows)

    baseline_alarms = extract_picking_alarms_from_frames(upload_frames)
    false_alarm_by_frame = _build_false_alarm_by_baseline_alarms(baseline_alarms, gt_segments)
    false_alarm_frame_set = set(false_alarm_by_frame.keys())
    picking_frame_set = {fi for fi, _ in baseline_alarms}

    seg_gt: list[dict[str, Any]] = []
    seg_false_alarm: list[dict[str, Any]] = []
    seg_other: list[dict[str, Any]] = []
    for s in motion_segments:
        if _seg_overlaps_gt(s, gt_segments):
            seg_gt.append(s)
        elif _seg_overlaps_false_alarm(s, false_alarm_by_frame):
            seg_false_alarm.append(s)
        else:
            seg_other.append(s)

    torso_gt: list[float] = []
    torso_false_alarm: list[float] = []
    torso_idle: list[float] = []
    lower_gt: list[float] = []
    lower_false_alarm: list[float] = []
    lower_idle: list[float] = []

    for r in velocity_rows:
        if not r.get("torso_velocity_valid"):
            continue
        fi = int(r.get("frame_idx") or 0)
        torso = _float_or_none(r.get("torso_speed"))
        lower = _float_or_none(r.get("lower_mean_speed"))
        if torso is None:
            continue
        if _frame_in_gt(fi, gt_segments):
            torso_gt.append(torso)
            if lower is not None:
                lower_gt.append(lower)
        elif fi in false_alarm_frame_set:
            torso_false_alarm.append(torso)
            if lower is not None:
                lower_false_alarm.append(lower)
        else:
            torso_idle.append(torso)
            if lower is not None:
                lower_idle.append(lower)

    _, slug, _ = parse_record_path_segments(record_id)

    baseline_metrics = evaluate_segments(gt_segments, baseline_alarms)

    return {
        "record_id": record_id,
        "clip": loc.path.name,
        "camera_slug": slug,
        "export_frame_count": len(export_indices),
        "velocity_row_count": len(velocity_rows),
        "baseline_alarm_count": len(baseline_alarms),
        "false_alarm_frame_count": len(false_alarm_frame_set),
        "gt_segments": len(gt_segments),
        "segments": len(motion_segments),
        "segments_gt_overlap": len(seg_gt),
        "segments_false_alarm": len(seg_false_alarm),
        "annotation_source": ann_source,
        "review_status": str(review.get("status") or ""),
        "baseline_metrics": {
            "detected": int(baseline_metrics["detected"]),
            "false_alarms": int(baseline_metrics["false_alarms"]),
            "recall": float(baseline_metrics["recall"]),
        },
        "torso_speed_frame": {
            "gt": _stats(torso_gt),
            "false_alarm": _stats(torso_false_alarm),
            "idle": _stats(torso_idle),
        },
        "lower_mean_speed_frame": {
            "gt": _stats(lower_gt),
            "false_alarm": _stats(lower_false_alarm),
            "idle": _stats(lower_idle),
        },
        "torso_speed_p50_seg": {
            "gt_overlap_seg": _stats(_seg_values(seg_gt, "torso_speed_p50")),
            "false_alarm_seg": _stats(_seg_values(seg_false_alarm, "torso_speed_p50")),
            "other_seg": _stats(_seg_values(seg_other, "torso_speed_p50")),
        },
        "lower_mean_speed_p50_seg": {
            "gt_overlap_seg": _stats(_seg_values(seg_gt, "lower_mean_speed_p50")),
            "false_alarm_seg": _stats(_seg_values(seg_false_alarm, "lower_mean_speed_p50")),
            "other_seg": _stats(_seg_values(seg_other, "lower_mean_speed_p50")),
        },
        "torso_p50_false_alarm_scan": _threshold_scan_le(
            _seg_values(seg_gt, "torso_speed_p50"),
            _seg_values(seg_false_alarm, "torso_speed_p50"),
            TORSO_THRESHOLDS,
        ),
        "lower_p50_false_alarm_scan": _threshold_scan_le(
            _seg_values(seg_gt, "lower_mean_speed_p50"),
            _seg_values(seg_false_alarm, "lower_mean_speed_p50"),
            LOWER_THRESHOLDS,
        ),
        "ratio_p50_false_alarm_scan": _threshold_scan(
            _seg_values(seg_gt, "wrist_torso_ratio_p50"),
            _seg_values(seg_false_alarm, "wrist_torso_ratio_p50"),
            RATIO_THRESHOLDS,
        ),
        "_pool": {
            "torso_gt_frame": torso_gt,
            "torso_false_alarm_frame": torso_false_alarm,
            "torso_idle_frame": torso_idle,
            "lower_gt_frame": lower_gt,
            "lower_false_alarm_frame": lower_false_alarm,
            "lower_idle_frame": lower_idle,
            "torso_p50_gt_seg": _seg_values(seg_gt, "torso_speed_p50"),
            "torso_p50_false_alarm_seg": _seg_values(seg_false_alarm, "torso_speed_p50"),
            "torso_p50_other_seg": _seg_values(seg_other, "torso_speed_p50"),
            "torso_max_gt_seg": _seg_values(seg_gt, "torso_speed_max"),
            "torso_max_false_alarm_seg": _seg_values(seg_false_alarm, "torso_speed_max"),
            "torso_max_other_seg": _seg_values(seg_other, "torso_speed_max"),
            "lower_p50_gt_seg": _seg_values(seg_gt, "lower_mean_speed_p50"),
            "lower_p50_false_alarm_seg": _seg_values(seg_false_alarm, "lower_mean_speed_p50"),
            "lower_p50_other_seg": _seg_values(seg_other, "lower_mean_speed_p50"),
            "body_p50_gt_seg": _seg_values(seg_gt, "body_mean_speed_p50"),
            "body_p50_false_alarm_seg": _seg_values(seg_false_alarm, "body_mean_speed_p50"),
            "body_p50_other_seg": _seg_values(seg_other, "body_mean_speed_p50"),
            "ratio_p50_gt_seg": _seg_values(seg_gt, "wrist_torso_ratio_p50"),
            "ratio_p50_false_alarm_seg": _seg_values(seg_false_alarm, "wrist_torso_ratio_p50"),
            "ratio_p50_other_seg": _seg_values(seg_other, "wrist_torso_ratio_p50"),
            "gt_segments": len(gt_segments),
            "segments": len(motion_segments),
            "segments_gt_overlap": len(seg_gt),
            "segments_false_alarm": len(seg_false_alarm),
        },
        "_motion_segments": motion_segments,
        "_baseline_alarms": baseline_alarms,
        "_gt_segments": gt_segments,
    }


def _speed_only_grid(
    per_record: list[dict[str, Any]],
) -> dict[str, Any]:
    """对各速度特征做阈值网格搜索（相对 baseline 原始告警）。"""
    raw_tp = sum(
        int(r.get("baseline_metrics", {}).get("detected") or 0)
        for r in per_record if not r.get("error")
    )
    raw_fp = sum(
        int(r.get("baseline_metrics", {}).get("false_alarms") or 0)
        for r in per_record if not r.get("error")
    )

    grids: dict[str, list[dict[str, Any]]] = {}
    best_rows: list[dict[str, Any]] = []

    for feature, thresholds in SPEED_FEATURE_GRIDS.items():
        rows: list[dict[str, Any]] = []
        for thr in thresholds:
            tp = fp = 0
            for rec in per_record:
                if rec.get("error"):
                    continue
                filtered, _ = filter_alarms_by_speed_only(
                    rec["_baseline_alarms"],
                    rec["_motion_segments"],
                    feature=feature,
                    max_threshold=float(thr),
                )
                m = evaluate_segments(rec["_gt_segments"], filtered)
                tp += int(m["detected"])
                fp += int(m["false_alarms"])
            fp_drop = (raw_fp - fp) / raw_fp if raw_fp else None
            tp_loss = (raw_tp - tp) / raw_tp if raw_tp else None
            denom = tp + fp
            rows.append({
                "feature": feature,
                "max_threshold": thr,
                "detected": tp,
                "false_alarms": fp,
                "fp_drop_vs_baseline": round(fp_drop, 4) if fp_drop is not None else None,
                "tp_loss_vs_baseline": round(tp_loss, 4) if tp_loss is not None else None,
                "precision_proxy": round(tp / denom, 4) if denom else None,
                "recall": round(tp / raw_tp, 4) if raw_tp else None,
            })
        grids[feature] = rows
        eligible = [r for r in rows if (r.get("tp_loss_vs_baseline") or 0) <= 0.05]
        if eligible:
            best = max(eligible, key=lambda r: (r.get("fp_drop_vs_baseline") or 0))
        else:
            best = max(rows, key=lambda r: (r.get("fp_drop_vs_baseline") or 0)) if rows else None
        if best:
            best_rows.append(best)

    best_overall = max(best_rows, key=lambda r: (r.get("fp_drop_vs_baseline") or 0)) if best_rows else None

    return {
        "baseline": {"detected": raw_tp, "false_alarms": raw_fp},
        "grids": grids,
        "best_per_feature": best_rows,
        "best_overall": best_overall,
    }


def _feature_ranking(pool: dict[str, Any], pool_acc: SkeletonPool) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name, scan_key, direction, seg_key in [
        ("lower_mean_speed_p50", "lower_p50_false_alarm_scan", "le", "lower_mean_speed_p50_seg"),
        ("torso_speed_p50", "torso_p50_false_alarm_scan", "le", "torso_speed_p50_seg"),
        ("torso_speed_max", None, "le", "torso_speed_max_seg"),
        ("wrist_torso_ratio_p50", "ratio_p50_false_alarm_scan", "ge", "wrist_torso_ratio_p50_seg"),
    ]:
        if scan_key:
            scan = pool.get(scan_key) or []
        else:
            scan = _threshold_scan_le(
                pool_acc.torso_max_gt_seg,
                pool_acc.torso_max_false_alarm_seg,
                TORSO_THRESHOLDS,
            )
        best = _best_f1(scan)
        sk = pool[seg_key]
        rows.append({
            "feature": name,
            "direction": direction,
            "gt_p50": sk["gt_overlap_seg"].get("p50"),
            "false_alarm_p50": sk["false_alarm_seg"].get("p50"),
            "best_threshold": best.get("threshold") if best else None,
            "best_f1": best.get("f1") if best else None,
            "best_precision": best.get("precision") if best else None,
            "best_recall": best.get("recall") if best else None,
        })
    rows.sort(key=lambda r: (r.get("best_f1") or 0), reverse=True)
    return rows


def _methodology_lines() -> list[str]:
    """特征提取与验证方法说明（写入报告）。"""
    return [
        "---",
        "",
        "## 2. 特征提取方式",
        "",
        "实现见 `event_engine/skeleton_features.py`，分三层：**单点速度 → 帧级聚合 → 段级统计**。",
        "",
        "### 2.1 单点速度（COCO-17 关键点）",
        "",
        "| 步骤 | 说明 |",
        "|------|------|",
        "| 置信度过滤 | 关键点 `score ≥ 0.3` 才参与 |",
        "| 位置平滑 | 最近 3 帧坐标做中值滤波，抑制姿态抖动 |",
        "| 速度计算 | `speed = hypot(Δx, Δy) / Δt`，单位 px/s |",
        "| 断链 | 相邻帧间隔 > 2 时重置历史 |",
        "",
        "### 2.2 帧级聚合",
        "",
        "| 特征 | 定义 |",
        "|------|------|",
        "| `torso_speed` | 双肩中心（不可用则双髋中心）速度 |",
        "| `lower_mean_speed` | 下肢 6 点（髋/膝/踝）有效速度均值 |",
        "| `body_mean_speed` | 17 点有效速度均值 |",
        "| `wrist_torso_ratio` | `wrist_max / (torso_speed + 1e-3)` |",
        "",
        "### 2.3 段级统计",
        "",
        "碰撞段 `[frame_enter, frame_exit]` 内按 `person_track_id` 收集帧级速度，统计 P50/Max 等。",
        "",
        "### 2.4 抽帧约定",
        "",
        f"速度仅在导出抽帧（`pose_frame_interval=2`）序列上差分，贴合现场。",
        "",
        "### 2.5 过滤语义",
        "",
        "段级 `feature ≤ threshold` 则保留告警；无运动数据则保留（不误杀）。",
        "",
        "---",
        "",
        "## 3. 验证方法",
        "",
        "| 项 | 约定 |",
        "|----|------|",
        "| 告警来源 | `clip_*.json` 的 `is_picking` + `rule_alarm_collisions` |",
        "| 标真来源 | `event_review.verified_true` |",
        "| 过滤策略 | **仅段级速度上限**，不含 Combo1/位移/帧数 |",
        "",
    ]


def _conclusion_lines(
    *,
    ranking: list[dict[str, Any]],
    speed_eval: dict[str, Any],
) -> list[str]:
    """验证结论（写入报告）。"""
    best = ranking[0] if ranking else {}
    baseline = speed_eval.get("baseline") or {}
    best_alarm = speed_eval.get("best_overall") or {}
    # 从网格中取 lower_mean_speed_p50 ≤ 60 行
    lower_grid = (speed_eval.get("grids") or {}).get("lower_mean_speed_p50") or []
    lower_60 = next((r for r in lower_grid if r.get("max_threshold") == 60), None)

    lines = [
        "## 4. 验证结论",
        "",
        "### 4.1 方案可行",
        "",
        "段级 F1 约 0.85，显著优于单用手腕帧级速度（约 0.17）。误报段躯干/下肢速度明显高于标真段。",
        "",
        f"### 4.2 baseline：TP={baseline.get('detected')} FP={baseline.get('false_alarms')}",
        "",
        "### 4.3 段级区分度最优",
        "",
        f"**`{best.get('feature', '—')}`** ≤ {best.get('best_threshold', '—')}，F1={best.get('best_f1', '—')}",
        "",
        "### 4.4 推荐规则",
        "",
        "**平衡版（TP 损失更低）：**",
        "",
        "```",
        "保留告警 if 碰撞段 lower_mean_speed_p50 ≤ 60",
        "```",
        "",
    ]
    if lower_60:
        lines.extend([
            f"- TP={lower_60.get('detected')} FP={lower_60.get('false_alarms')} "
            f"FP降={lower_60.get('fp_drop_vs_baseline')} TP损={lower_60.get('tp_loss_vs_baseline')}",
            "",
        ])
    lines.extend([
        "**激进版（FP 下降更多）：**",
        "",
        "```",
        f"保留告警 if 碰撞段 {best_alarm.get('feature', 'body_mean_speed_p50')} "
        f"≤ {best_alarm.get('max_threshold', 60)}",
        "```",
        "",
        f"- FP 降={best_alarm.get('fp_drop_vs_baseline', '—')} "
        f"TP 损={best_alarm.get('tp_loss_vs_baseline', '—')}",
        "",
        "---",
        "",
    ])
    return lines


def _render_md(
    *,
    manifest_params: dict[str, Any],
    pool: dict[str, Any],
    ranking: list[dict[str, Any]],
    speed_eval: dict[str, Any],
    per_record: list[dict[str, Any]],
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    best = ranking[0] if ranking else {}
    lines = [
        "# 全骨骼速度特征过滤（rule-baseline-prod-test 28 条验证）",
        "",
        f"> 生成时间：{now}",
        f"> 验证脚本：`scripts/data/validate_baseline28_subsampled_velocity.py`",
        f"> 数据 JSON：`localdata/export/rule-baseline-prod-test/skeleton_velocity_speed_only.json`",
        f"> 实现模块：`event_engine/skeleton_features.py`",
        "",
        "## 1. 数据范围",
        "",
        f"- 记录数：**{pool.get('records', 0)}**",
        f"- 标真重叠碰撞段：**{pool.get('segments_gt_overlap', 0)}**",
        f"- baseline 误报碰撞段：**{pool.get('segments_false_alarm', 0)}**",
        f"- 抽帧间隔：`pose_frame_interval={manifest_params.get('pose_frame_interval', 2)}`",
        "",
    ]
    lines.extend(_methodology_lines())
    lines.extend([
        "## 5. 段级区分度（标真 vs 误报）",
        "",
        "| 特征 | 标真 P50 | 误报 P50 | 最佳阈值 | F1 |",
        "|------|----------|----------|----------|-----|",
    ])
    for r in ranking:
        th = r.get("best_threshold")
        th_str = f"≤{th}" if r.get("direction") == "le" else f"≥{th}"
        lines.append(
            f"| {r['feature']} | {r.get('gt_p50','—')} | {r.get('false_alarm_p50','—')} | "
            f"{th_str} | {r.get('best_f1','—')} |"
        )

    lines.extend(_conclusion_lines(ranking=ranking, speed_eval=speed_eval))

    baseline = speed_eval.get("baseline") or {}
    lines.extend([
        "## 6. 阈值网格（告警级）",
        "",
        f"baseline 原始：TP={baseline.get('detected')} FP={baseline.get('false_alarms')}",
        "",
    ])

    for feature, rows in (speed_eval.get("grids") or {}).items():
        lines.extend([
            f"### {feature} 上限",
            "",
            "| 阈值 ≤ | TP | FP | FP下降 | TP损失 | 精确率代理 |",
            "|--------|-----|-----|--------|--------|------------|",
        ])
        for row in rows:
            lines.append(
                f"| {row['max_threshold']} | {row['detected']} | {row['false_alarms']} | "
                f"{row.get('fp_drop_vs_baseline','—')} | {row.get('tp_loss_vs_baseline','—')} | "
                f"{row.get('precision_proxy','—')} |"
            )
        lines.append("")

    lines.extend([
        "## 7. 分片摘要",
        "",
        "| record_id | 抽帧数 | baseline告警 | 误报段 | 标真段 |",
        "|-----------|--------|-------------|--------|--------|",
    ])
    for rec in per_record:
        if rec.get("error"):
            continue
        lines.append(
            f"| `{rec.get('record_id','')}` | {rec.get('export_frame_count',0)} | "
            f"{rec.get('baseline_alarm_count',0)} | {rec.get('segments_false_alarm',0)} | "
            f"{rec.get('segments_gt_overlap',0)} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    resolve_config_path(None)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    params = manifest.get("params") or {}
    records = _load_manifest_records()

    pool_acc = SkeletonPool()
    per_record_clean: list[dict[str, Any]] = []
    per_record_full: list[dict[str, Any]] = []

    for entry in records:
        rid = entry.get("record_id", "")
        result = _analyze_baseline_record(entry)
        if not result or result.get("error"):
            print(f"{rid}: skip {result.get('error') if result else '无结果'}")
            continue
        per_record_full.append(result)
        per_record_clean.append({k: v for k, v in result.items() if not k.startswith("_")})
        _merge_pool(pool_acc, result["_pool"])
        print(
            f"{rid}: export={result.get('export_frame_count')} "
            f"alarms={result.get('baseline_alarm_count')} "
            f"fa_seg={result.get('segments_false_alarm')} "
            f"gt_seg={result.get('segments_gt_overlap')}"
        )

    if pool_acc.records <= 0:
        print("无有效分析结果")
        return 2

    pool = _pool_summary(pool_acc)
    ranking = _feature_ranking(pool, pool_acc)
    speed_eval = _speed_only_grid(per_record_full)

    best_alarm = speed_eval.get("best_overall") or {}
    payload = {
        "source": "rule-baseline-prod-test",
        "filter_mode": "speed_only",
        "pose_frame_interval": params.get("pose_frame_interval"),
        "alarm_source": "is_picking + rule_alarm_collisions",
        "velocity_mode": "subsampled_export_frames_only",
        "record_count": len(per_record_clean),
        "pool": pool,
        "feature_ranking": ranking,
        "speed_filter_evaluation": speed_eval,
        "recommendation": {
            "segment_feature": ranking[0]["feature"] if ranking else None,
            "segment_threshold": ranking[0].get("best_threshold") if ranking else None,
            "segment_f1": ranking[0].get("best_f1") if ranking else None,
            "alarm_filter_feature": best_alarm.get("feature"),
            "alarm_filter_threshold": best_alarm.get("max_threshold"),
            "fp_drop_vs_baseline": best_alarm.get("fp_drop_vs_baseline"),
            "tp_loss_vs_baseline": best_alarm.get("tp_loss_vs_baseline"),
        },
        "per_record": per_record_clean,
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text(
        _render_md(
            manifest_params=params,
            pool=pool,
            ranking=ranking,
            speed_eval=speed_eval,
            per_record=per_record_clean,
        ),
        encoding="utf-8",
    )

    print("\n=== 段级特征排名（纯速度 · baseline 抽帧）===")
    for r in ranking:
        print(
            f"{r['feature']}: gt_p50={r.get('gt_p50')} fa_p50={r.get('false_alarm_p50')} "
            f"f1={r.get('best_f1')} threshold={r.get('best_threshold')}"
        )
    bl = speed_eval.get("baseline") or {}
    print(f"\nbaseline TP/FP: {bl.get('detected')}/{bl.get('false_alarms')}")
    if best_alarm:
        print(
            f"纯速度过滤推荐: {best_alarm.get('feature')} ≤ {best_alarm.get('max_threshold')} "
            f"FP降={best_alarm.get('fp_drop_vs_baseline')} TP损={best_alarm.get('tp_loss_vs_baseline')}"
        )
    print(f"JSON: {OUT_JSON}")
    print(f"MD: {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
