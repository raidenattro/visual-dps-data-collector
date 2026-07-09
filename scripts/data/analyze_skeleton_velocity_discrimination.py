#!/usr/bin/env python3
"""批量提取全骨骼速度特征并统计躯干/全身运动对误报的区分度。

正/负样本以 event_review.verified_true 合并的 ground truth 段为准。
段级重点对比：标真重叠碰撞段 vs 误报碰撞段（走过货位手腕短暂进框）。

用法（项目根目录）:
  python scripts/data/analyze_skeleton_velocity_discrimination.py
  python scripts/data/analyze_skeleton_velocity_discrimination.py --dry-run
  python scripts/data/analyze_skeleton_velocity_discrimination.py --skip-extract
  python scripts/data/analyze_skeleton_velocity_discrimination.py \\
    --out docs/skeleton-velocity-discrimination-rtmpose-m.md
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config_loader import parse_record_path_segments, resolve_app_paths, resolve_config_path
from pose_store import load_event_review, load_timeline

from api.accuracy_service import build_ground_truth_segments
from api.record_service import locate_record_by_id
from api.skeleton_features_service import extract_skeleton_features_for_record
from scripts.data.analyze_wrist_feature_discrimination import (
    DEFAULT_CAMERAS,
    DEFAULT_REVIEW_STATUS,
    DEFAULT_TAGS,
    DEFAULT_TIER,
    _build_false_alarm_by_frame,
    _collect_record_ids,
    _load_ground_truth_segments,
    _parse_csv_list,
    _parse_tags,
    _seg_overlaps_false_alarm,
    _seg_overlaps_gt,
    _stats,
    _threshold_scan,
)

TORSO_THRESHOLDS = [10, 20, 30, 40, 50, 60, 80, 100, 120, 150]
LOWER_THRESHOLDS = [10, 20, 30, 40, 50, 60, 80, 100, 120, 150]
RATIO_THRESHOLDS = [1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 8.0, 10.0]


def _threshold_scan_le(
    pos: list[float],
    neg: list[float],
    thresholds: list[float],
) -> list[dict[str, Any]]:
    """标真=正类：预测保留当 value <= threshold（低速度=停下拣货）。"""
    rows: list[dict[str, Any]] = []
    for t in thresholds:
        tp = sum(1 for x in pos if x <= t)
        fp = sum(1 for x in neg if x <= t)
        fn = len(pos) - tp
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        rows.append(
            {
                "threshold": t,
                "precision": round(prec, 4),
                "recall": round(rec, 4),
                "f1": round(f1, 4),
                "tp": tp,
                "fp": fp,
                "fn": fn,
            }
        )
    return rows


def _float_or_none(val: Any) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _seg_values(segs: list[dict[str, Any]], key: str) -> list[float]:
    out: list[float] = []
    for s in segs:
        v = _float_or_none(s.get(key))
        if v is not None:
            out.append(v)
    return out


@dataclass
class SkeletonPool:
    torso_gt_frame: list[float] = field(default_factory=list)
    torso_false_alarm_frame: list[float] = field(default_factory=list)
    torso_idle_frame: list[float] = field(default_factory=list)
    lower_gt_frame: list[float] = field(default_factory=list)
    lower_false_alarm_frame: list[float] = field(default_factory=list)
    lower_idle_frame: list[float] = field(default_factory=list)
    torso_p50_gt_seg: list[float] = field(default_factory=list)
    torso_p50_false_alarm_seg: list[float] = field(default_factory=list)
    torso_p50_other_seg: list[float] = field(default_factory=list)
    torso_max_gt_seg: list[float] = field(default_factory=list)
    torso_max_false_alarm_seg: list[float] = field(default_factory=list)
    torso_max_other_seg: list[float] = field(default_factory=list)
    lower_p50_gt_seg: list[float] = field(default_factory=list)
    lower_p50_false_alarm_seg: list[float] = field(default_factory=list)
    lower_p50_other_seg: list[float] = field(default_factory=list)
    body_p50_gt_seg: list[float] = field(default_factory=list)
    body_p50_false_alarm_seg: list[float] = field(default_factory=list)
    body_p50_other_seg: list[float] = field(default_factory=list)
    ratio_p50_gt_seg: list[float] = field(default_factory=list)
    ratio_p50_false_alarm_seg: list[float] = field(default_factory=list)
    ratio_p50_other_seg: list[float] = field(default_factory=list)
    records: int = 0
    gt_segments: int = 0
    segments: int = 0
    segments_gt_overlap: int = 0
    segments_false_alarm: int = 0


def _analyze_record(record_id: str) -> dict[str, Any] | None:
    try:
        import pyarrow.parquet as pq
    except ImportError:
        raise RuntimeError("缺少 pyarrow") from None

    loc = locate_record_by_id(record_id)
    if not loc:
        return None
    base = loc.path
    vel_path = base / "skeleton_velocity.parquet"
    seg_path = base / "skeleton_motion_segments.parquet"
    if not vel_path.is_file():
        return {"record_id": record_id, "error": "缺少 skeleton_velocity.parquet"}

    manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    vel = pq.read_table(vel_path).to_pylist()
    seg = pq.read_table(seg_path).to_pylist() if seg_path.is_file() else []
    tl = load_timeline(loc, include_events=True)

    gt_segments, review = _load_ground_truth_segments(loc)
    if not gt_segments:
        return {
            "record_id": record_id,
            "error": "无人工标真 ground truth（verified_true 为空或无法合并为段）",
        }

    gt_frame_set: set[int] = set()
    for gt in gt_segments:
        for fi in range(gt.frame_start, gt.frame_end + 1):
            gt_frame_set.add(fi)
    false_alarm_by_frame = _build_false_alarm_by_frame(tl, gt_segments)
    false_alarm_frame_set = set(false_alarm_by_frame.keys())

    seg_gt: list[dict[str, Any]] = []
    seg_false_alarm: list[dict[str, Any]] = []
    seg_other: list[dict[str, Any]] = []
    for s in seg:
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

    for r in vel:
        if not r.get("torso_velocity_valid"):
            continue
        fi = int(r.get("frame_idx") or 0)
        torso = _float_or_none(r.get("torso_speed"))
        lower = _float_or_none(r.get("lower_mean_speed"))
        if torso is None:
            continue
        if fi in gt_frame_set:
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

    return {
        "record_id": record_id,
        "clip": base.name,
        "camera_slug": slug,
        "gt_segments": len(gt_segments),
        "segments": len(seg),
        "segments_gt_overlap": len(seg_gt),
        "segments_false_alarm": len(seg_false_alarm),
        "review_status": str(review.get("status") or ""),
        "skeleton_features": manifest.get("skeleton_features"),
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
        "torso_speed_max_seg": {
            "gt_overlap_seg": _stats(_seg_values(seg_gt, "torso_speed_max")),
            "false_alarm_seg": _stats(_seg_values(seg_false_alarm, "torso_speed_max")),
            "other_seg": _stats(_seg_values(seg_other, "torso_speed_max")),
        },
        "lower_mean_speed_p50_seg": {
            "gt_overlap_seg": _stats(_seg_values(seg_gt, "lower_mean_speed_p50")),
            "false_alarm_seg": _stats(_seg_values(seg_false_alarm, "lower_mean_speed_p50")),
            "other_seg": _stats(_seg_values(seg_other, "lower_mean_speed_p50")),
        },
        "body_mean_speed_p50_seg": {
            "gt_overlap_seg": _stats(_seg_values(seg_gt, "body_mean_speed_p50")),
            "false_alarm_seg": _stats(_seg_values(seg_false_alarm, "body_mean_speed_p50")),
            "other_seg": _stats(_seg_values(seg_other, "body_mean_speed_p50")),
        },
        "wrist_torso_ratio_p50_seg": {
            "gt_overlap_seg": _stats(_seg_values(seg_gt, "wrist_torso_ratio_p50")),
            "false_alarm_seg": _stats(_seg_values(seg_false_alarm, "wrist_torso_ratio_p50")),
            "other_seg": _stats(_seg_values(seg_other, "wrist_torso_ratio_p50")),
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
            "segments": len(seg),
            "segments_gt_overlap": len(seg_gt),
            "segments_false_alarm": len(seg_false_alarm),
        },
    }


def _merge_pool(pool: SkeletonPool, part: dict[str, Any]) -> None:
    pool.torso_gt_frame.extend(part["torso_gt_frame"])
    pool.torso_false_alarm_frame.extend(part["torso_false_alarm_frame"])
    pool.torso_idle_frame.extend(part["torso_idle_frame"])
    pool.lower_gt_frame.extend(part["lower_gt_frame"])
    pool.lower_false_alarm_frame.extend(part["lower_false_alarm_frame"])
    pool.lower_idle_frame.extend(part["lower_idle_frame"])
    pool.torso_p50_gt_seg.extend(part["torso_p50_gt_seg"])
    pool.torso_p50_false_alarm_seg.extend(part["torso_p50_false_alarm_seg"])
    pool.torso_p50_other_seg.extend(part["torso_p50_other_seg"])
    pool.torso_max_gt_seg.extend(part["torso_max_gt_seg"])
    pool.torso_max_false_alarm_seg.extend(part["torso_max_false_alarm_seg"])
    pool.torso_max_other_seg.extend(part["torso_max_other_seg"])
    pool.lower_p50_gt_seg.extend(part["lower_p50_gt_seg"])
    pool.lower_p50_false_alarm_seg.extend(part["lower_p50_false_alarm_seg"])
    pool.lower_p50_other_seg.extend(part["lower_p50_other_seg"])
    pool.body_p50_gt_seg.extend(part["body_p50_gt_seg"])
    pool.body_p50_false_alarm_seg.extend(part["body_p50_false_alarm_seg"])
    pool.body_p50_other_seg.extend(part["body_p50_other_seg"])
    pool.ratio_p50_gt_seg.extend(part["ratio_p50_gt_seg"])
    pool.ratio_p50_false_alarm_seg.extend(part["ratio_p50_false_alarm_seg"])
    pool.ratio_p50_other_seg.extend(part["ratio_p50_other_seg"])
    pool.records += 1
    pool.gt_segments += int(part.get("gt_segments") or 0)
    pool.segments += int(part.get("segments") or 0)
    pool.segments_gt_overlap += int(part.get("segments_gt_overlap") or 0)
    pool.segments_false_alarm += int(part.get("segments_false_alarm") or 0)


def _pool_summary(pool: SkeletonPool) -> dict[str, Any]:
    return {
        "records": pool.records,
        "gt_segments": pool.gt_segments,
        "segments": pool.segments,
        "segments_gt_overlap": pool.segments_gt_overlap,
        "segments_false_alarm": pool.segments_false_alarm,
        "torso_speed_frame": {
            "gt": _stats(pool.torso_gt_frame),
            "false_alarm": _stats(pool.torso_false_alarm_frame),
            "idle": _stats(pool.torso_idle_frame),
        },
        "lower_mean_speed_frame": {
            "gt": _stats(pool.lower_gt_frame),
            "false_alarm": _stats(pool.lower_false_alarm_frame),
            "idle": _stats(pool.lower_idle_frame),
        },
        "torso_speed_p50_seg": {
            "gt_overlap_seg": _stats(pool.torso_p50_gt_seg),
            "false_alarm_seg": _stats(pool.torso_p50_false_alarm_seg),
            "other_seg": _stats(pool.torso_p50_other_seg),
        },
        "torso_speed_max_seg": {
            "gt_overlap_seg": _stats(pool.torso_max_gt_seg),
            "false_alarm_seg": _stats(pool.torso_max_false_alarm_seg),
            "other_seg": _stats(pool.torso_max_other_seg),
        },
        "lower_mean_speed_p50_seg": {
            "gt_overlap_seg": _stats(pool.lower_p50_gt_seg),
            "false_alarm_seg": _stats(pool.lower_p50_false_alarm_seg),
            "other_seg": _stats(pool.lower_p50_other_seg),
        },
        "body_mean_speed_p50_seg": {
            "gt_overlap_seg": _stats(pool.body_p50_gt_seg),
            "false_alarm_seg": _stats(pool.body_p50_false_alarm_seg),
            "other_seg": _stats(pool.body_p50_other_seg),
        },
        "wrist_torso_ratio_p50_seg": {
            "gt_overlap_seg": _stats(pool.ratio_p50_gt_seg),
            "false_alarm_seg": _stats(pool.ratio_p50_false_alarm_seg),
            "other_seg": _stats(pool.ratio_p50_other_seg),
        },
        "torso_p50_false_alarm_scan": _threshold_scan_le(
            pool.torso_p50_gt_seg,
            pool.torso_p50_false_alarm_seg,
            TORSO_THRESHOLDS,
        ),
        "lower_p50_false_alarm_scan": _threshold_scan_le(
            pool.lower_p50_gt_seg,
            pool.lower_p50_false_alarm_seg,
            LOWER_THRESHOLDS,
        ),
        "ratio_p50_false_alarm_scan": _threshold_scan(
            pool.ratio_p50_gt_seg,
            pool.ratio_p50_false_alarm_seg,
            RATIO_THRESHOLDS,
        ),
    }


def _best_f1(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    return max(rows, key=lambda r: r.get("f1") or 0)


def _md_stats_table(
    title: str,
    gt: dict[str, Any],
    false_alarm: dict[str, Any],
    other: dict[str, Any],
) -> list[str]:
    lines = [
        f"### {title}",
        "",
        "| 指标 | 标真重叠段 | 误报碰撞段 | 其他碰撞段 |",
        "|------|------------|------------|------------|",
    ]
    for key in ("n", "mean", "p50", "p90", "p95", "max"):
        lines.append(
            f"| {key} | {gt.get(key, '—')} | {false_alarm.get(key, '—')} | {other.get(key, '—')} |"
        )
    lines.append("")
    return lines


def _md_threshold_table_le(title: str, rows: list[dict[str, Any]], unit: str = "") -> list[str]:
    lines = [
        f"### {title}",
        "",
        f"| 上限阈值{unit} | 精确率 | 召回率 | F1 | TP | FP | FN |",
        "|------------|--------|--------|-----|----|----|-----|",
    ]
    for r in rows:
        lines.append(
            f"| ≤{r['threshold']} | {r['precision']:.2f} | {r['recall']:.2f} | {r['f1']:.2f} "
            f"| {r['tp']} | {r['fp']} | {r['fn']} |"
        )
    lines.append("")
    return lines


def _render_markdown(
    *,
    tier: str,
    tags: list[str],
    cameras: list[str],
    pool: dict[str, Any],
    by_camera: dict[str, dict[str, Any]],
    extract_log: list[dict[str, Any]],
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    torso_fp = pool["torso_speed_p50_seg"]["false_alarm_seg"]
    torso_gt = pool["torso_speed_p50_seg"]["gt_overlap_seg"]
    lower_fp = pool["lower_mean_speed_p50_seg"]["false_alarm_seg"]
    lower_gt = pool["lower_mean_speed_p50_seg"]["gt_overlap_seg"]
    torso_best = _best_f1(pool.get("torso_p50_false_alarm_scan") or [])
    lower_best = _best_f1(pool.get("lower_p50_false_alarm_scan") or [])
    ratio_best = _best_f1(pool.get("ratio_p50_false_alarm_scan") or [])

    lines = [
        "# RTMPose-M 全骨骼速度特征区分度分析",
        "",
        f"> 生成时间：{now}  ",
        f"> 模型层：`{tier}`  ",
        f"> 记录标签（同时满足）：{', '.join(tags)}  ",
        f"> 机位 slug：{', '.join(cameras)}  ",
        "> 目标：用躯干/下肢运动速度过滤「人走过货位、手腕短暂进框」误报  ",
        "> 正样本：标真重叠碰撞段（停下拣货）  ",
        "> 负样本：误报碰撞段（走过误报）  ",
        "",
        "## 1. 数据范围",
        "",
        f"- 符合条件记录数：**{pool['records']}**",
        f"- 碰撞段总数：**{pool['segments']}**（标真重叠 **{pool['segments_gt_overlap']}**，误报重叠 **{pool['segments_false_alarm']}**）",
        "",
        "### 各机位记录数",
        "",
        "| 机位 | 记录数 | 标真重叠段 | 误报碰撞段 |",
        "|------|--------|------------|------------|",
    ]
    for cam in cameras:
        c = by_camera.get(cam, {})
        lines.append(
            f"| {cam} | {c.get('records', 0)} | {c.get('segments_gt_overlap', 0)} | "
            f"{c.get('segments_false_alarm', 0)} |"
        )
    lines.extend(["", "### 提取日志", ""])
    for row in extract_log:
        rid = row.get("record_id", "")
        if row.get("status") == "ok":
            lines.append(
                f"- `{rid}`：速度 {row.get('velocity_count')} 行，运动段 {row.get('motion_segment_count')}"
            )
        elif row.get("status") == "skipped":
            lines.append(f"- `{rid}`：跳过（已存在）")
        else:
            lines.append(f"- `{rid}`：**失败** {row.get('error')}")

    lines.extend(
        [
            "",
            "## 2. 汇总结论",
            "",
            "| 特征 | 误报过滤价值 | 汇总观察 |",
            "|------|-------------|----------|",
            f"| 段级 torso_speed_p50 (px/s) | **待验证** | 标真 P50={torso_gt.get('p50', '—')} / 误报 P50={torso_fp.get('p50', '—')}；"
            f"标真 vs 误报最佳 F1≈{torso_best['f1'] if torso_best else '—'}（上限 ≤{torso_best['threshold'] if torso_best else '—'}） |",
            f"| 段级 lower_mean_speed_p50 | **待验证** | 标真 P50={lower_gt.get('p50', '—')} / 误报 P50={lower_fp.get('p50', '—')}；"
            f"最佳 F1≈{lower_best['f1'] if lower_best else '—'} |",
            f"| 段级 wrist_torso_ratio_p50 | **辅助** | 停下伸手拣货时手腕动、躯干静，比值偏高；最佳 F1≈{ratio_best['f1'] if ratio_best else '—'} |",
            "",
            "**推荐用法**：",
            "",
            "1. 主检测仍用手腕进框 + 连续帧门控。",
            "2. 二次过滤：段级 `torso_speed_p50 ≤ T` 或 `lower_mean_speed_p50 ≤ T` 与 Combo1（fc/dur/disp）AND 组合。",
            "3. 误报（走过）预期躯干/下肢速度更高；阈值需用本报告扫描结果标定。",
            "",
            "## 3. 段级运动统计",
            "",
        ]
    )
    lines.extend(
        _md_stats_table(
            "3.1 躯干速度中位数 torso_speed_p50 (px/s)",
            pool["torso_speed_p50_seg"]["gt_overlap_seg"],
            pool["torso_speed_p50_seg"]["false_alarm_seg"],
            pool["torso_speed_p50_seg"]["other_seg"],
        )
    )
    lines.extend(
        _md_stats_table(
            "3.2 下肢速度中位数 lower_mean_speed_p50 (px/s)",
            pool["lower_mean_speed_p50_seg"]["gt_overlap_seg"],
            pool["lower_mean_speed_p50_seg"]["false_alarm_seg"],
            pool["lower_mean_speed_p50_seg"]["other_seg"],
        )
    )
    lines.extend(
        _md_stats_table(
            "3.3 全身速度中位数 body_mean_speed_p50 (px/s)",
            pool["body_mean_speed_p50_seg"]["gt_overlap_seg"],
            pool["body_mean_speed_p50_seg"]["false_alarm_seg"],
            pool["body_mean_speed_p50_seg"]["other_seg"],
        )
    )
    lines.extend(
        _md_stats_table(
            "3.4 手腕/躯干速度比 wrist_torso_ratio_p50",
            pool["wrist_torso_ratio_p50_seg"]["gt_overlap_seg"],
            pool["wrist_torso_ratio_p50_seg"]["false_alarm_seg"],
            pool["wrist_torso_ratio_p50_seg"]["other_seg"],
        )
    )
    lines.extend(["## 4. 阈值扫描（标真段 vs 误报段）", ""])
    lines.extend(
        _md_threshold_table_le(
            "4.1 torso_speed_p50 上限（保留 speed ≤ 阈值）",
            pool.get("torso_p50_false_alarm_scan") or [],
            " px/s",
        )
    )
    lines.extend(
        _md_threshold_table_le(
            "4.2 lower_mean_speed_p50 上限",
            pool.get("lower_p50_false_alarm_scan") or [],
            " px/s",
        )
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="全骨骼速度特征区分度分析")
    parser.add_argument("--tier", default=DEFAULT_TIER)
    parser.add_argument("--tags", default=",".join(DEFAULT_TAGS))
    parser.add_argument("--cameras", default=",".join(DEFAULT_CAMERAS))
    parser.add_argument("--review-status", default=DEFAULT_REVIEW_STATUS)
    parser.add_argument("--skip-extract", action="store_true", help="不自动提取缺失特征")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--out",
        default=str(ROOT / "docs" / "skeleton-velocity-discrimination-rtmpose-m.md"),
    )
    args = parser.parse_args()

    resolve_config_path(None)
    tags = _parse_tags(args.tags)
    cameras = _parse_csv_list(args.cameras)
    camera_set = set(cameras)

    record_ids = _collect_record_ids(
        tier=args.tier,
        cameras=camera_set,
        tags=tags,
        review_status=args.review_status or None,
        has_verified=True,
    )
    if not record_ids:
        print("未找到符合条件记录")
        return 1

    if args.dry_run:
        print(f"将分析 {len(record_ids)} 条记录")
        for rid in record_ids:
            print(f"  {rid}")
        return 0

    extract_log: list[dict[str, Any]] = []
    per_record: list[dict[str, Any]] = []
    pool = SkeletonPool()
    by_camera: dict[str, dict[str, int]] = defaultdict(
        lambda: {"records": 0, "segments_gt_overlap": 0, "segments_false_alarm": 0}
    )

    for rid in record_ids:
        loc = locate_record_by_id(rid)
        if not loc:
            continue
        if not args.skip_extract:
            try:
                ext = extract_skeleton_features_for_record(loc, skip_if_exists=True)
                extract_log.append(ext)
            except Exception as exc:
                extract_log.append({"record_id": rid, "status": "error", "error": str(exc)})
                print(f"{rid}: 提取失败 {exc}")
                continue

        result = _analyze_record(rid)
        if not result or result.get("error"):
            print(f"{rid}: 分析跳过 {result.get('error') if result else '无结果'}")
            continue
        per_record.append({k: v for k, v in result.items() if k != "_pool"})
        _merge_pool(pool, result["_pool"])
        cam = str(result.get("camera_slug") or "")
        by_camera[cam]["records"] += 1
        by_camera[cam]["segments_gt_overlap"] += int(result.get("segments_gt_overlap") or 0)
        by_camera[cam]["segments_false_alarm"] += int(result.get("segments_false_alarm") or 0)
        print(f"{rid}: ok 标真段重叠 {result.get('segments_gt_overlap')} 误报段 {result.get('segments_false_alarm')}")

    if pool.records <= 0:
        print("无有效分析结果")
        return 2

    pool_summary = _pool_summary(pool)
    md = _render_markdown(
        tier=args.tier,
        tags=tags,
        cameras=cameras,
        pool=pool_summary,
        by_camera=by_camera,
        extract_log=extract_log,
    )
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    print(f"\n报告已写入: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
