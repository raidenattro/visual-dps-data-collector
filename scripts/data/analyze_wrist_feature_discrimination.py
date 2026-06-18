#!/usr/bin/env python3
"""批量提取手腕特征并统计：速度 / 碰撞段位移等对人工标真的区分度。

正/负样本以 event_review.verified_true 合并的 ground truth 段为准（范本货框
优先 confirmed_box_tokens，否则 box_tokens，与准确率评估一致）。默认筛选标签
（单人 + 无遮挡）及指定机位 slug。

用法（项目根目录）:
  python scripts/data/analyze_wrist_feature_discrimination.py
  python scripts/data/analyze_wrist_feature_discrimination.py --dry-run
  python scripts/data/analyze_wrist_feature_discrimination.py --skip-extract
  python scripts/data/analyze_wrist_feature_discrimination.py --out docs/wrist-features-discrimination-rtmpose-m.md
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
from event_engine.box_identity import token_matches_any
from pose_store import load_event_review, load_timeline
from record_index_store import list_record_summaries, maybe_sync_record_summaries
from record_tag_store import normalize_tag_name, record_ids_with_all_tags

from api.accuracy_service import GroundTruthSegment, build_ground_truth_segments
from api.record_service import locate_record_by_id
from api.wrist_features_service import extract_wrist_features_for_record

DEFAULT_CAMERAS = (
    "1-1-1",
    "1-2-1",
    "2-2-2",
    "2-3-1",
    "2-4-1",
    "2-5-1",
    "2-6-1",
    "2-7-2",
)
DEFAULT_TAGS = ("单人", "无遮挡")
DEFAULT_TIER = "rtmpose-m"
DEFAULT_REVIEW_STATUS = "completed"


def _pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    i = (len(xs) - 1) * p / 100.0
    lo, hi = int(math.floor(i)), int(math.ceil(i))
    if lo == hi:
        return xs[lo]
    return xs[lo] + (xs[hi] - xs[lo]) * (i - lo)


def _stats(xs: list[float]) -> dict[str, Any]:
    xs = [float(x) for x in xs if x is not None and math.isfinite(float(x))]
    if not xs:
        return {"n": 0}
    return {
        "n": len(xs),
        "mean": round(sum(xs) / len(xs), 2),
        "p50": round(_pct(xs, 50) or 0, 2),
        "p90": round(_pct(xs, 90) or 0, 2),
        "p95": round(_pct(xs, 95) or 0, 2),
        "max": round(max(xs), 2),
    }


def _parse_csv_list(raw: str) -> list[str]:
    return [p.strip() for p in str(raw or "").split(",") if p.strip()]


def _parse_tags(raw: str) -> list[str]:
    out: list[str] = []
    for part in _parse_csv_list(raw):
        name = normalize_tag_name(part)
        if name not in out:
            out.append(name)
    return out


def _collect_record_ids(
    *,
    tier: str,
    cameras: set[str],
    tags: list[str],
    review_status: str | None = DEFAULT_REVIEW_STATUS,
    has_verified: bool | None = True,
    sync_index: bool = True,
) -> list[str]:
    """与回放列表筛选一致：标签 + 机位 + 复核状态 + 有标真（record_index）。"""
    paths = resolve_app_paths()
    if sync_index:
        maybe_sync_record_summaries(paths, tier or None, force=False, offset=0)

    allowed = record_ids_with_all_tags(tags) if tags else None
    review_filter = str(review_status or "").strip().lower() or None

    items = list_record_summaries(
        pose_tier=tier or None,
        allowed_ids=allowed,
        review_status=review_filter,
        has_verified=has_verified,
    )

    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        rid = str(item.get("record_id") or "").strip()
        if not rid or rid in seen:
            continue
        slug = str(item.get("camera_slug") or "").strip()
        if not slug:
            _, slug, _ = parse_record_path_segments(rid)
        if cameras and slug not in cameras:
            continue
        if not locate_record_by_id(rid):
            continue
        seen.add(rid)
        out.append(rid)
    return sorted(out)


def _load_ground_truth_segments(locator) -> tuple[list[GroundTruthSegment], dict[str, Any]]:
    """加载人工复核标真并合并为 ground truth 时间段（与准确率评估一致）。"""
    review = load_event_review(locator)
    verified = [e for e in (review.get("verified_true") or []) if isinstance(e, dict)]
    return build_ground_truth_segments(verified), review


def _ranges_overlap(a0: int, a1: int, b0: int, b1: int) -> bool:
    return a0 <= b1 and b0 <= a1


def _seg_overlaps_gt(seg: dict[str, Any], gt_segments: list[GroundTruthSegment]) -> bool:
    """手腕碰撞段是否与某条人工标真段在时间与范本货框上重叠。"""
    a = int(seg.get("frame_enter") or 0)
    b = int(seg.get("frame_exit") or 0)
    tok = str(seg.get("box_token") or "").strip()
    if not tok or not gt_segments:
        return False
    for gt in gt_segments:
        if not _ranges_overlap(a, b, gt.frame_start, gt.frame_end):
            continue
        if token_matches_any(tok, list(gt.gt_tokens)):
            return True
    return False


def _frame_in_gt(fi: int, gt_segments: list[GroundTruthSegment]) -> bool:
    for gt in gt_segments:
        if gt.frame_start <= fi <= gt.frame_end:
            return True
    return False


def _build_false_alarm_by_frame(
    timeline: list[dict[str, Any]],
    gt_segments: list[GroundTruthSegment],
) -> dict[int, list[str]]:
    """误报告警帧：timeline alarm_collisions 未被任何标真段（时间+范本货框）覆盖。"""
    by_frame: dict[int, list[str]] = {}
    for row in timeline:
        fi = int(row.get("frame_idx") or 0)
        for raw in row.get("alarm_collisions") or []:
            token = str(raw).strip()
            if not token:
                continue
            covered = any(
                gt.frame_start <= fi <= gt.frame_end and token_matches_any(token, list(gt.gt_tokens))
                for gt in gt_segments
            )
            if not covered:
                by_frame.setdefault(fi, []).append(token)
    return by_frame


def _seg_overlaps_false_alarm(
    seg: dict[str, Any],
    false_alarm_by_frame: dict[int, list[str]],
) -> bool:
    """手腕碰撞段是否与误报告警在帧范围 + 货位 token 上重叠。"""
    if not false_alarm_by_frame:
        return False
    a = int(seg.get("frame_enter") or 0)
    b = int(seg.get("frame_exit") or 0)
    tok = str(seg.get("box_token") or "").strip()
    if not tok:
        return False
    for fi in range(a, b + 1):
        if token_matches_any(tok, false_alarm_by_frame.get(fi, ())):
            return True
    return False


def _threshold_scan(
    pos: list[float],
    neg: list[float],
    thresholds: list[float],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for t in thresholds:
        tp = sum(1 for x in pos if x >= t)
        fp = sum(1 for x in neg if x >= t)
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


@dataclass
class PoolAccumulator:
    speed_gt: list[float] = field(default_factory=list)
    speed_coll: list[float] = field(default_factory=list)
    speed_idle: list[float] = field(default_factory=list)
    speed_norm_gt: list[float] = field(default_factory=list)
    speed_norm_idle: list[float] = field(default_factory=list)
    disp_gt_seg: list[float] = field(default_factory=list)
    disp_false_alarm_seg: list[float] = field(default_factory=list)
    disp_other_seg: list[float] = field(default_factory=list)
    dur_gt_seg: list[float] = field(default_factory=list)
    dur_false_alarm_seg: list[float] = field(default_factory=list)
    dur_other_seg: list[float] = field(default_factory=list)
    fc_gt_seg: list[float] = field(default_factory=list)
    fc_false_alarm_seg: list[float] = field(default_factory=list)
    fc_other_seg: list[float] = field(default_factory=list)
    path_gt_seg: list[float] = field(default_factory=list)
    path_false_alarm_seg: list[float] = field(default_factory=list)
    path_other_seg: list[float] = field(default_factory=list)
    speed_false_alarm: list[float] = field(default_factory=list)
    records: int = 0
    gt_frames: int = 0
    false_alarm_frames: int = 0
    false_alarm_events: int = 0
    coll_frames: int = 0
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
    vel_path = base / "wrist_velocity.parquet"
    seg_path = base / "wrist_box_segments.parquet"
    if not vel_path.is_file():
        return {"record_id": record_id, "error": "缺少 wrist_velocity.parquet"}

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

    coll_frames: set[int] = set()
    gt_frame_set: set[int] = set()
    for gt in gt_segments:
        for fi in range(gt.frame_start, gt.frame_end + 1):
            gt_frame_set.add(fi)
    false_alarm_by_frame = _build_false_alarm_by_frame(tl, gt_segments)
    false_alarm_frame_set = set(false_alarm_by_frame.keys())
    false_alarm_events = sum(len(v) for v in false_alarm_by_frame.values())
    for row in tl:
        fi = int(row.get("frame_idx") or 0)
        colls = list(row.get("collisions") or [])
        if colls:
            coll_frames.add(fi)

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

    speed_gt: list[float] = []
    speed_false_alarm: list[float] = []
    speed_coll: list[float] = []
    speed_idle: list[float] = []
    norm_gt: list[float] = []
    norm_idle: list[float] = []

    for r in vel:
        if not r.get("velocity_valid"):
            continue
        fi = int(r.get("frame_idx") or 0)
        sp = float(r["speed"])
        sn = float(r.get("speed_norm") or 0)
        if fi in gt_frame_set:
            speed_gt.append(sp)
            norm_gt.append(sn)
        elif fi in false_alarm_frame_set:
            speed_false_alarm.append(sp)
        elif fi in coll_frames:
            speed_coll.append(sp)
        else:
            speed_idle.append(sp)
            norm_idle.append(sn)

    _, slug, _ = parse_record_path_segments(record_id)
    coll_cfg = manifest.get("collect_config") if isinstance(manifest.get("collect_config"), dict) else {}
    collision_cfg = manifest.get("collision") if isinstance(manifest.get("collision"), dict) else {}

    return {
        "record_id": record_id,
        "clip": base.name,
        "camera_slug": slug,
        "frame_count": int(manifest.get("frame_count") or 0),
        "fps": float(manifest.get("fps") or 15),
        "alarm_min_frames": int(collision_cfg.get("alarm_min_consecutive_frames") or coll_cfg.get("alarm_min_consecutive_frames") or 0),
        "gt_segments": len(gt_segments),
        "gt_frames": len(gt_frame_set),
        "false_alarm_frames": len(false_alarm_frame_set),
        "false_alarm_events": false_alarm_events,
        "coll_frames": len(coll_frames),
        "segments": len(seg),
        "segments_gt_overlap": len(seg_gt),
        "segments_false_alarm": len(seg_false_alarm),
        "review_status": str(review.get("status") or ""),
        "wrist_features": manifest.get("wrist_features"),
        "speed_px_s": {
            "gt": _stats(speed_gt),
            "false_alarm": _stats(speed_false_alarm),
            "collision_no_gt": _stats(speed_coll),
            "idle": _stats(speed_idle),
        },
        "speed_norm": {
            "gt": _stats(norm_gt),
            "idle": _stats(norm_idle),
        },
        "displacement": {
            "gt_overlap_seg": _stats([float(s["displacement"]) for s in seg_gt]),
            "false_alarm_seg": _stats([float(s["displacement"]) for s in seg_false_alarm]),
            "other_seg": _stats([float(s["displacement"]) for s in seg_other]),
        },
        "duration_sec": {
            "gt_overlap_seg": _stats([float(s["duration_sec"]) for s in seg_gt]),
            "false_alarm_seg": _stats([float(s["duration_sec"]) for s in seg_false_alarm]),
            "other_seg": _stats([float(s["duration_sec"]) for s in seg_other]),
        },
        "frame_count_seg": {
            "gt_overlap_seg": _stats([float(s["frame_count"]) for s in seg_gt]),
            "false_alarm_seg": _stats([float(s["frame_count"]) for s in seg_false_alarm]),
            "other_seg": _stats([float(s["frame_count"]) for s in seg_other]),
        },
        "path_length": {
            "gt_overlap_seg": _stats([float(s["path_length"]) for s in seg_gt]),
            "false_alarm_seg": _stats([float(s["path_length"]) for s in seg_false_alarm]),
            "other_seg": _stats([float(s["path_length"]) for s in seg_other]),
        },
        "speed_threshold_scan": _threshold_scan(
            speed_gt,
            speed_idle,
            [50, 80, 100, 150, 200, 250, 300, 400, 500],
        ),
        "displacement_threshold_scan": _threshold_scan(
            [float(s["displacement"]) for s in seg_gt],
            [float(s["displacement"]) for s in seg_other],
            [0, 5, 10, 15, 20, 25, 30, 40, 50],
        ),
        "displacement_false_alarm_threshold_scan": _threshold_scan(
            [float(s["displacement"]) for s in seg_gt],
            [float(s["displacement"]) for s in seg_false_alarm],
            [0, 5, 10, 15, 20, 25, 30, 40, 50],
        ),
        "frame_count_threshold_scan": _threshold_scan(
            [float(s["frame_count"]) for s in seg_gt],
            [float(s["frame_count"]) for s in seg_other],
            [1, 2, 3, 4, 5, 6, 8, 10, 15],
        ),
        "frame_count_false_alarm_threshold_scan": _threshold_scan(
            [float(s["frame_count"]) for s in seg_gt],
            [float(s["frame_count"]) for s in seg_false_alarm],
            [1, 2, 3, 4, 5, 6, 8, 10, 15],
        ),
        "_pool": {
            "speed_gt": speed_gt,
            "speed_false_alarm": speed_false_alarm,
            "speed_coll": speed_coll,
            "speed_idle": speed_idle,
            "speed_norm_gt": norm_gt,
            "speed_norm_idle": norm_idle,
            "disp_gt_seg": [float(s["displacement"]) for s in seg_gt],
            "disp_false_alarm_seg": [float(s["displacement"]) for s in seg_false_alarm],
            "disp_other_seg": [float(s["displacement"]) for s in seg_other],
            "dur_gt_seg": [float(s["duration_sec"]) for s in seg_gt],
            "dur_false_alarm_seg": [float(s["duration_sec"]) for s in seg_false_alarm],
            "dur_other_seg": [float(s["duration_sec"]) for s in seg_other],
            "fc_gt_seg": [float(s["frame_count"]) for s in seg_gt],
            "fc_false_alarm_seg": [float(s["frame_count"]) for s in seg_false_alarm],
            "fc_other_seg": [float(s["frame_count"]) for s in seg_other],
            "path_gt_seg": [float(s["path_length"]) for s in seg_gt],
            "path_false_alarm_seg": [float(s["path_length"]) for s in seg_false_alarm],
            "path_other_seg": [float(s["path_length"]) for s in seg_other],
            "gt_frames": len(gt_frame_set),
            "false_alarm_frames": len(false_alarm_frame_set),
            "false_alarm_events": false_alarm_events,
            "coll_frames": len(coll_frames),
            "gt_segments": len(gt_segments),
            "segments": len(seg),
            "segments_gt_overlap": len(seg_gt),
            "segments_false_alarm": len(seg_false_alarm),
        },
    }


def _merge_pool(pool: PoolAccumulator, part: dict[str, Any]) -> None:
    pool.speed_gt.extend(part["speed_gt"])
    pool.speed_false_alarm.extend(part["speed_false_alarm"])
    pool.speed_coll.extend(part["speed_coll"])
    pool.speed_idle.extend(part["speed_idle"])
    pool.speed_norm_gt.extend(part["speed_norm_gt"])
    pool.speed_norm_idle.extend(part["speed_norm_idle"])
    pool.disp_gt_seg.extend(part["disp_gt_seg"])
    pool.disp_false_alarm_seg.extend(part["disp_false_alarm_seg"])
    pool.disp_other_seg.extend(part["disp_other_seg"])
    pool.dur_gt_seg.extend(part["dur_gt_seg"])
    pool.dur_false_alarm_seg.extend(part["dur_false_alarm_seg"])
    pool.dur_other_seg.extend(part["dur_other_seg"])
    pool.fc_gt_seg.extend(part["fc_gt_seg"])
    pool.fc_false_alarm_seg.extend(part["fc_false_alarm_seg"])
    pool.fc_other_seg.extend(part["fc_other_seg"])
    pool.path_gt_seg.extend(part["path_gt_seg"])
    pool.path_false_alarm_seg.extend(part["path_false_alarm_seg"])
    pool.path_other_seg.extend(part["path_other_seg"])
    pool.records += 1
    pool.gt_frames += int(part.get("gt_frames") or 0)
    pool.false_alarm_frames += int(part.get("false_alarm_frames") or 0)
    pool.false_alarm_events += int(part.get("false_alarm_events") or 0)
    pool.coll_frames += int(part.get("coll_frames") or 0)
    pool.gt_segments += int(part.get("gt_segments") or 0)
    pool.segments += int(part.get("segments") or 0)
    pool.segments_gt_overlap += int(part.get("segments_gt_overlap") or 0)
    pool.segments_false_alarm += int(part.get("segments_false_alarm") or 0)


def _pool_summary(pool: PoolAccumulator) -> dict[str, Any]:
    return {
        "records": pool.records,
        "gt_frames": pool.gt_frames,
        "false_alarm_frames": pool.false_alarm_frames,
        "false_alarm_events": pool.false_alarm_events,
        "coll_frames": pool.coll_frames,
        "gt_segments": pool.gt_segments,
        "segments": pool.segments,
        "segments_gt_overlap": pool.segments_gt_overlap,
        "segments_false_alarm": pool.segments_false_alarm,
        "speed_px_s": {
            "gt": _stats(pool.speed_gt),
            "false_alarm": _stats(pool.speed_false_alarm),
            "collision_no_gt": _stats(pool.speed_coll),
            "idle": _stats(pool.speed_idle),
        },
        "speed_norm": {
            "gt": _stats(pool.speed_norm_gt),
            "idle": _stats(pool.speed_norm_idle),
        },
        "displacement": {
            "gt_overlap_seg": _stats(pool.disp_gt_seg),
            "false_alarm_seg": _stats(pool.disp_false_alarm_seg),
            "other_seg": _stats(pool.disp_other_seg),
        },
        "duration_sec": {
            "gt_overlap_seg": _stats(pool.dur_gt_seg),
            "false_alarm_seg": _stats(pool.dur_false_alarm_seg),
            "other_seg": _stats(pool.dur_other_seg),
        },
        "frame_count_seg": {
            "gt_overlap_seg": _stats(pool.fc_gt_seg),
            "false_alarm_seg": _stats(pool.fc_false_alarm_seg),
            "other_seg": _stats(pool.fc_other_seg),
        },
        "path_length": {
            "gt_overlap_seg": _stats(pool.path_gt_seg),
            "false_alarm_seg": _stats(pool.path_false_alarm_seg),
            "other_seg": _stats(pool.path_other_seg),
        },
        "speed_threshold_scan": _threshold_scan(
            pool.speed_gt,
            pool.speed_idle,
            [50, 80, 100, 150, 200, 250, 300, 400, 500],
        ),
        "displacement_threshold_scan": _threshold_scan(
            pool.disp_gt_seg,
            pool.disp_other_seg,
            [0, 5, 10, 15, 20, 25, 30, 40, 50],
        ),
        "displacement_false_alarm_threshold_scan": _threshold_scan(
            pool.disp_gt_seg,
            pool.disp_false_alarm_seg,
            [0, 5, 10, 15, 20, 25, 30, 40, 50],
        ),
        "frame_count_threshold_scan": _threshold_scan(
            pool.fc_gt_seg,
            pool.fc_other_seg,
            [1, 2, 3, 4, 5, 6, 8, 10, 15],
        ),
        "frame_count_false_alarm_threshold_scan": _threshold_scan(
            pool.fc_gt_seg,
            pool.fc_false_alarm_seg,
            [1, 2, 3, 4, 5, 6, 8, 10, 15],
        ),
        "duration_threshold_scan": _threshold_scan(
            pool.dur_gt_seg,
            pool.dur_other_seg,
            [0.07, 0.13, 0.2, 0.27, 0.33, 0.4, 0.53, 0.67, 1.0],
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


def _md_threshold_table(title: str, rows: list[dict[str, Any]], unit: str = "") -> list[str]:
    lines = [
        f"### {title}",
        "",
        f"| 阈值{unit} | 精确率 | 召回率 | F1 | TP | FP | FN |",
        "|---------|--------|--------|-----|----|----|-----|",
    ]
    for r in rows:
        lines.append(
            f"| {r['threshold']} | {r['precision']:.2f} | {r['recall']:.2f} | {r['f1']:.2f} "
            f"| {r['tp']} | {r['fp']} | {r['fn']} |"
        )
    lines.append("")
    return lines


def _render_markdown(
    *,
    tier: str,
    tags: list[str],
    cameras: list[str],
    review_status: str | None,
    has_verified: bool | None,
    per_record: list[dict[str, Any]],
    pool: dict[str, Any],
    by_camera: dict[str, dict[str, Any]],
    extract_log: list[dict[str, Any]],
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    review_line = (
        f"`{review_status}`（已复核）"
        if review_status == "completed"
        else (f"`{review_status}`" if review_status else "不限")
    )
    verified_line = "有标真（verified_count > 0）" if has_verified is True else (
        "无标真" if has_verified is False else "不限"
    )
    lines = [
        "# RTMPose-M 手腕速度 / 碰撞段特征区分度分析",
        "",
        f"> 生成时间：{now}  ",
        f"> 模型层：`{tier}`  ",
        f"> 记录标签（同时满足）：{', '.join(tags)}  ",
        f"> 复核状态：{review_line}  ",
        f"> 标真：{verified_line}  ",
        f"> 机位 slug：{', '.join(cameras)}  ",
        "> 正样本：手腕碰撞段与人工标真 ground truth 段在帧范围 + 范本货框上重叠  ",
        "> 范本货框：`confirmed_box_tokens` 优先，否则 `box_tokens`（与准确率评估一致）  ",
        "> 误报碰撞段：手腕碰撞段与误报告警（未被标真段覆盖的 `alarm_collisions`）在帧范围 + 货位 token 上重叠  ",
        "> 说明：筛选与回放「已保存记录」一致；标真段由 `verified_true` 按连续相同范本货框合并。",
        "",
        "## 1. 数据范围",
        "",
        f"- 符合条件记录数：**{pool['records']}**",
        f"- 汇总标真段：**{pool['gt_segments']}**",
        f"- 汇总标真时间段帧：**{pool['gt_frames']}**",
        f"- 汇总误报告警帧：**{pool['false_alarm_frames']}**（告警事件 **{pool['false_alarm_events']}** 次）",
        f"- 汇总碰撞占用帧：**{pool['coll_frames']}**",
        f"- 碰撞段总数：**{pool['segments']}**（标真重叠 **{pool['segments_gt_overlap']}**，误报重叠 **{pool['segments_false_alarm']}**）",
        "",
        "### 各机位记录数",
        "",
        "| 机位 | 记录数 | 标真段 | 碰撞段 | 标真重叠段 | 误报碰撞段 |",
        "|------|--------|--------|--------|------------|------------|",
    ]
    for cam in cameras:
        c = by_camera.get(cam, {})
        lines.append(
            f"| {cam} | {c.get('records', 0)} | {c.get('gt_segments', 0)} | "
            f"{c.get('segments', 0)} | {c.get('segments_gt_overlap', 0)} | "
            f"{c.get('segments_false_alarm', 0)} |"
        )
    lines.extend(["", "### 提取日志", ""])
    for row in extract_log:
        rid = row.get("record_id", "")
        if row.get("status") == "ok":
            lines.append(
                f"- `{rid}`：速度 {row.get('velocity_count')} 行，碰撞段 {row.get('segment_count')}，"
                f"货框 {row.get('box_count')}，标注 `{row.get('annotation_source') or row.get('annotation')}`"
            )
        elif row.get("status") == "skipped":
            lines.append(f"- `{rid}`：跳过（已存在）")
        else:
            lines.append(f"- `{rid}`：**失败** {row.get('error')}")
    lines.extend(["", "## 2. 汇总结论", ""])

    speed_best = _best_f1(pool.get("speed_threshold_scan") or [])
    disp_best = _best_f1(pool.get("displacement_threshold_scan") or [])
    disp_fp_best = _best_f1(pool.get("displacement_false_alarm_threshold_scan") or [])
    fc_best = _best_f1(pool.get("frame_count_threshold_scan") or [])
    fc_fp_best = _best_f1(pool.get("frame_count_false_alarm_threshold_scan") or [])
    dur_best = _best_f1(pool.get("duration_threshold_scan") or [])

    gt_sp = pool["speed_px_s"]["gt"]
    fa_sp = pool["speed_px_s"]["false_alarm"]
    idle_sp = pool["speed_px_s"]["idle"]
    disp_a = pool["displacement"]["gt_overlap_seg"]
    disp_fp = pool["displacement"]["false_alarm_seg"]
    disp_o = pool["displacement"]["other_seg"]
    fc_a = pool["frame_count_seg"]["gt_overlap_seg"]
    fc_fp = pool["frame_count_seg"]["false_alarm_seg"]
    fc_o = pool["frame_count_seg"]["other_seg"]

    lines.extend(
        [
            "| 特征 | 能否作主要识别依据 | 汇总观察 |",
            "|------|-------------------|----------|",
            f"| 瞬时速度 speed (px/s) | **弱** | 标真时间段 P50={gt_sp.get('p50', '—')} vs 误报帧 P50={fa_sp.get('p50', '—')} vs 无碰撞 P50={idle_sp.get('p50', '—')}；"
            f"最佳 F1≈{speed_best['f1'] if speed_best else '—'}（阈值 {speed_best['threshold'] if speed_best else '—'}） |",
            f"| 归一化速度 speed_norm | **弱** | 与 speed 类似，受姿态抖动与全身运动干扰 |",
            f"| 段位移 displacement (px) | **中等** | 标真 P50={disp_a.get('p50', '—')} / 误报 P50={disp_fp.get('p50', '—')} / 其他 P50={disp_o.get('p50', '—')}；"
            f"标真 vs 其他 F1≈{disp_best['f1'] if disp_best else '—'}，标真 vs 误报 F1≈{disp_fp_best['f1'] if disp_fp_best else '—'} |",
            f"| 段帧数 frame_count | **强** | 标真 P50={fc_a.get('p50', '—')} / 误报 P50={fc_fp.get('p50', '—')} / 其他 P50={fc_o.get('p50', '—')}；"
            f"标真 vs 其他 F1≈{fc_best['f1'] if fc_best else '—'}，标真 vs 误报 F1≈{fc_fp_best['f1'] if fc_fp_best else '—'} |",
            f"| 段时长 duration_sec | **强** | 与帧数等价（fps≈15）；最佳 F1≈{dur_best['f1'] if dur_best else '—'} |",
            f"| 段路径 path_length | **中等（辅助）** | 与 displacement 正相关，描述框内扫腕 |",
            "",
            "**推荐用法**：",
            "",
            "1. **主检测**仍依赖「手腕点进框 + 连续帧门控」（与准确率评估一致）。",
            "2. **二次过滤 / 质量分**：在碰撞段上优先使用 `frame_count ≥ 5` 或 `duration_sec ≥ 0.33`（约 5 帧@15fps），可再配合 `displacement ≥ 10~15 px`。",
            "3. **不建议**单独用瞬时速度阈值替代几何碰撞。",
            "",
            "## 3. 汇总统计",
            "",
            "### 3.1 帧级速度（px/s）",
            "",
            "| 场景 | n | mean | P50 | P90 | P95 |",
            "|------|---|------|-----|-----|-----|",
        ]
    )
    for label, key in (
        ("标真时间段内帧", "gt"),
        ("误报告警帧", "false_alarm"),
        ("碰撞未在标真段", "collision_no_gt"),
        ("无碰撞帧", "idle"),
    ):
        s = pool["speed_px_s"][key]
        lines.append(
            f"| {label} | {s.get('n', 0)} | {s.get('mean', '—')} | {s.get('p50', '—')} | "
            f"{s.get('p90', '—')} | {s.get('p95', '—')} |"
        )
    lines.append("")
    lines.extend(
        _md_stats_table(
            "3.2 段位移 displacement (px)",
            disp_a,
            disp_fp,
            disp_o,
        )
    )
    lines.extend(
        _md_stats_table(
            "3.3 段持续帧数 frame_count",
            fc_a,
            fc_fp,
            fc_o,
        )
    )
    lines.extend(
        _md_stats_table(
            "3.4 段时长 duration_sec",
            pool["duration_sec"]["gt_overlap_seg"],
            pool["duration_sec"]["false_alarm_seg"],
            pool["duration_sec"]["other_seg"],
        )
    )
    lines.extend(
        _md_stats_table(
            "3.5 段路径 path_length (px)",
            pool["path_length"]["gt_overlap_seg"],
            pool["path_length"]["false_alarm_seg"],
            pool["path_length"]["other_seg"],
        )
    )

    lines.append("## 4. 阈值扫描（汇总池）")
    lines.append("")
    lines.extend(_md_threshold_table("4.1 速度：标真时间段 vs 无碰撞帧", pool.get("speed_threshold_scan") or [], " px/s"))
    lines.extend(
        _md_threshold_table(
            "4.2 段位移：标真重叠段 vs 其他段",
            pool.get("displacement_threshold_scan") or [],
            " px",
        )
    )
    lines.extend(
        _md_threshold_table(
            "4.3 段位移：标真重叠段 vs 误报碰撞段",
            pool.get("displacement_false_alarm_threshold_scan") or [],
            " px",
        )
    )
    lines.extend(
        _md_threshold_table(
            "4.4 段帧数：标真重叠段 vs 其他段",
            pool.get("frame_count_threshold_scan") or [],
            " 帧",
        )
    )
    lines.extend(
        _md_threshold_table(
            "4.5 段帧数：标真重叠段 vs 误报碰撞段",
            pool.get("frame_count_false_alarm_threshold_scan") or [],
            " 帧",
        )
    )
    lines.extend(
        _md_threshold_table(
            "4.6 段时长：标真重叠段 vs 其他段",
            pool.get("duration_threshold_scan") or [],
            " s",
        )
    )

    lines.extend(["## 5. 分机位摘要", ""])
    for cam in cameras:
        c = by_camera.get(cam)
        if not c or not c.get("records"):
            continue
        lines.append(f"### {cam}（{c['records']} 条）")
        lines.append("")
        sa = c["speed_px_s"]["gt"]
        sfa = c["speed_px_s"]["false_alarm"]
        si = c["speed_px_s"]["idle"]
        da = c["displacement"]["gt_overlap_seg"]
        dfa = c["displacement"]["false_alarm_seg"]
        do = c["displacement"]["other_seg"]
        lines.append(
            f"- 速度：标真 P50={sa.get('p50', '—')}，误报帧 P50={sfa.get('p50', '—')}，空闲 P50={si.get('p50', '—')}"
        )
        lines.append(
            f"- 位移：标真 P50={da.get('p50', '—')}，误报段 P50={dfa.get('p50', '—')}，其他 P50={do.get('p50', '—')}"
        )
        lines.append(
            f"- 误报碰撞段：**{c.get('segments_false_alarm', 0)}** 条"
        )
        fb = _best_f1(c.get("frame_count_threshold_scan") or [])
        fbfp = _best_f1(c.get("frame_count_false_alarm_threshold_scan") or [])
        if fb:
            lines.append(
                f"- 段帧数标真 vs 其他 F1={fb['f1']:.2f}（≥{fb['threshold']} 帧）"
            )
        if fbfp:
            lines.append(
                f"- 段帧数标真 vs 误报 F1={fbfp['f1']:.2f}（≥{fbfp['threshold']} 帧）"
            )
        lines.append("")

    lines.extend(["## 6. 单条记录明细", ""])
    lines.append(
        "| 记录 | 机位 | 帧数 | 标真段 | 误报帧 | 碰撞段 | 标真重叠 | 误报重叠 | 位移 P50(标真/误报/其他) |"
    )
    lines.append(
        "|------|------|------|--------|--------|--------|----------|----------|--------------------------|"
    )
    for r in per_record:
        if r.get("error"):
            lines.append(
                f"| `{r.get('record_id', '')}` | — | — | — | — | — | — | — | 错误：{r['error']} |"
            )
            continue
        da = r["displacement"]["gt_overlap_seg"]
        dfa = r["displacement"]["false_alarm_seg"]
        do = r["displacement"]["other_seg"]
        lines.append(
            f"| `{r['clip']}` | {r['camera_slug']} | {r['frame_count']} | {r['gt_segments']} | "
            f"{r['false_alarm_frames']} | {r['segments']} | {r['segments_gt_overlap']} | "
            f"{r['segments_false_alarm']} | "
            f"{da.get('p50', '—')} / {dfa.get('p50', '—')} / {do.get('p50', '—')} |"
        )
    lines.extend(
        [
            "",
            "## 7. 方法说明",
            "",
            "- 手腕特征由 `scripts/data/extract_wrist_features.py` 写入；标注按机位 reflection **多货架合并**。",
            "- 人工标真段：`event_review.verified_true` 按 `frame_idx` 排序，连续相同范本货框合并为 `[frame_start, frame_end]`。",
            "- 范本货框：优先 `confirmed_box_tokens`，否则 `box_tokens`（与 `api/accuracy_service.py` 一致）。",
            "- 误报告警：`alarm_collisions` 中未被任何标真段（时间 + 范本货框）覆盖的帧（与准确率误报定义一致）。",
            "- 碰撞段：手腕进入某货框到离开的连续区间（与是否触发告警无关）。",
            "- 「标真重叠段」：碰撞段与标真段重叠且 `box_token` 与范本货框匹配。",
            "- 「误报碰撞段」：非标真重叠，但与误报告警在帧范围 + 货位 token 上重叠。",
            "- 「其他碰撞段」：既非标真重叠、也非误报重叠的几何碰撞（多为亚阈值短触）。",
            "- `person_track_id` 为后处理分配，速度统计包含所有有效 track。",
            "- 再跑本报告：`python scripts/data/analyze_wrist_feature_discrimination.py`",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="提取并分析手腕特征区分度")
    parser.add_argument("--tier", default=DEFAULT_TIER)
    parser.add_argument("--tags", default=",".join(DEFAULT_TAGS))
    parser.add_argument("--cameras", default=",".join(DEFAULT_CAMERAS))
    parser.add_argument(
        "--review-status",
        default=DEFAULT_REVIEW_STATUS,
        help="复核状态过滤（默认 completed=已复核；传空字符串表示不限）",
    )
    parser.add_argument(
        "--has-verified",
        choices=("yes", "no", "all"),
        default="yes",
        help="是否要求有标真范本（默认 yes，与回放「有标真」一致）",
    )
    parser.add_argument(
        "--no-sync-index",
        action="store_true",
        help="不刷新 record_index（默认首屏 sync 与 /api/records 一致）",
    )
    parser.add_argument("--skip-extract", action="store_true", help="跳过特征提取，仅分析已有 parquet")
    parser.add_argument("--skip-existing", action="store_true", help="提取时跳过已有特征文件")
    parser.add_argument(
        "--out",
        default=str(ROOT / "docs" / "wrist-features-discrimination-rtmpose-m.md"),
        help="Markdown 报告输出路径",
    )
    parser.add_argument("--json-out", default="", help="可选 JSON 明细输出")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    resolve_config_path(None)
    tags = _parse_tags(args.tags)
    cameras = _parse_csv_list(args.cameras)
    camera_set = set(cameras)
    review_status = str(args.review_status or "").strip() or None
    has_verified: bool | None
    if args.has_verified == "yes":
        has_verified = True
    elif args.has_verified == "no":
        has_verified = False
    else:
        has_verified = None

    record_ids = _collect_record_ids(
        tier=args.tier,
        cameras=camera_set,
        tags=tags,
        review_status=review_status,
        has_verified=has_verified,
        sync_index=not args.no_sync_index,
    )

    if not record_ids:
        print("未找到匹配记录", file=sys.stderr)
        return 1

    if args.dry_run:
        print(
            f"将处理 {len(record_ids)} 条记录（tier={args.tier}, tags={tags}, "
            f"review={review_status or 'all'}, verified={args.has_verified}）:"
        )
        for rid in record_ids:
            print(f"  {rid}")
        return 0

    extract_log: list[dict[str, Any]] = []
    if not args.skip_extract:
        for rid in record_ids:
            loc = locate_record_by_id(rid)
            if not loc:
                extract_log.append({"record_id": rid, "status": "error", "error": "记录不存在"})
                continue
            try:
                result = extract_wrist_features_for_record(
                    loc,
                    skip_if_exists=args.skip_existing,
                )
                extract_log.append({"record_id": rid, **result})
                st = result.get("status")
                if st == "skipped":
                    print(f"{rid}: skip")
                else:
                    print(
                        f"{rid}: ok seg={result.get('segment_count')} "
                        f"vel={result.get('velocity_count')} boxes={result.get('box_count')}"
                    )
            except Exception as exc:
                extract_log.append({"record_id": rid, "status": "error", "error": str(exc)})
                print(f"{rid}: fail {exc}")

    per_record: list[dict[str, Any]] = []
    pool = PoolAccumulator()
    by_camera_pool: dict[str, PoolAccumulator] = {c: PoolAccumulator() for c in cameras}

    for rid in record_ids:
        row = _analyze_record(rid)
        if not row:
            continue
        if row.get("error"):
            per_record.append(row)
            print(f"analyze {rid}: {row['error']}")
            continue
        part = row.pop("_pool")
        _merge_pool(pool, part)
        cam = row.get("camera_slug") or ""
        if cam in by_camera_pool:
            _merge_pool(by_camera_pool[cam], part)
        per_record.append(row)

    pool_summary = _pool_summary(pool)
    by_camera_summary = {cam: _pool_summary(p) for cam, p in by_camera_pool.items()}

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    md = _render_markdown(
        tier=args.tier,
        tags=tags,
        cameras=cameras,
        review_status=review_status,
        has_verified=has_verified,
        per_record=per_record,
        pool=pool_summary,
        by_camera=by_camera_summary,
        extract_log=extract_log,
    )
    out_path.write_text(md, encoding="utf-8")
    print(f"\n报告已写入: {out_path}")

    if args.json_out:
        json_path = Path(args.json_out)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "tier": args.tier,
            "tags": tags,
            "cameras": cameras,
            "review_status": review_status,
            "has_verified": has_verified,
            "pool": pool_summary,
            "by_camera": by_camera_summary,
            "records": per_record,
            "extract_log": extract_log,
        }
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON 已写入: {json_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
