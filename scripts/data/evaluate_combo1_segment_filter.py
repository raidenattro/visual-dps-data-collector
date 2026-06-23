#!/usr/bin/env python3
"""评估段特征组合过滤：min=5 候选告警 + 段级 AND 规则确认。

组合1（默认）:
  frame_count >= 4, duration_sec >= 0.20, displacement/frame_count <= 2.5
组合2:
  frame_count >= 4, duration_sec >= 0.20, displacement/frame_count <= 3.0
组合3（组合1 + 总位移上限）:
  frame_count >= 4, duration_sec >= 0.20, displacement/frame_count <= 2.5, displacement <= 10

用法（项目根目录）:
  python scripts/data/evaluate_combo1_segment_filter.py
  python scripts/data/evaluate_combo1_segment_filter.py --combo-id 2 --max-disp-per-frame 3.0 \\
    --out docs/combo2-segment-filter-rtmpose-m.md
  python scripts/data/evaluate_combo1_segment_filter.py --combo-id 3 \\
    --out docs/combo3-segment-filter-rtmpose-m.md
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _ensure_cv2_point_polygon_test() -> None:
    existing = sys.modules.get("cv2")
    if existing is not None and hasattr(existing, "pointPolygonTest"):
        return

    def _ray_point_in_contour(x: float, y: float, contour) -> bool:
        try:
            import numpy as np

            arr = np.asarray(contour, dtype=float)
            if arr.ndim == 3:
                arr = arr.reshape(-1, 2)
            elif arr.ndim == 2 and arr.shape[1] != 2:
                arr = arr.reshape(-1, 2)
            poly = [(float(px), float(py)) for px, py in arr]
        except Exception:
            poly = []
            for pt in contour or []:
                if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                    poly.append((float(pt[0]), float(pt[1])))
        if len(poly) < 3:
            return False
        inside = False
        n = len(poly)
        for i in range(n):
            x1, y1 = poly[i]
            x2, y2 = poly[(i + 1) % n]
            if ((y1 > y) != (y2 > y)) and (x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-12) + x1):
                inside = not inside
        return inside

    class _Cv2Shim:
        @staticmethod
        def pointPolygonTest(contour, pt, measure_dist):  # noqa: N802
            x, y = float(pt[0]), float(pt[1])
            return 1.0 if _ray_point_in_contour(x, y, contour) else -1.0

    sys.modules["cv2"] = _Cv2Shim()


_ensure_cv2_point_polygon_test()

from config_loader import parse_record_path_segments, resolve_app_paths, resolve_config_path
from event_engine.annotation_boxes import load_scaled_boxes
from event_engine.box_identity import canonical_box_token
from event_engine.collision import CollisionProcessor
from event_engine.wrist_features import assign_person_tracks_to_frames, extract_collision_segment_rows
from pose_store import load_all_frames, load_event_review, load_manifest

from api.accuracy_service import (
    GroundTruthSegment,
    build_ground_truth_segments,
    evaluate_segments,
    resolve_annotation_for_accuracy_record,
)
from api.record_service import locate_record_by_id
from scripts.data.analyze_wrist_feature_discrimination import (
    DEFAULT_CAMERAS,
    DEFAULT_REVIEW_STATUS,
    DEFAULT_TAGS,
    DEFAULT_TIER,
    _build_false_alarm_by_frame,
    _collect_record_ids,
    _parse_csv_list,
    _parse_tags,
    _seg_overlaps_false_alarm,
    _seg_overlaps_gt,
)

COMBO1_MIN_FRAMES = 4
COMBO1_MIN_DURATION = 0.20
COMBO1_MAX_DISP_PER_FRAME = 2.5
COMBO2_MAX_DISP_PER_FRAME = 3.0
COMBO3_MAX_DISPLACEMENT = 10.0


@dataclass(frozen=True)
class ComboRule:
    combo_id: int
    min_frames: int
    min_duration: float
    max_disp_per_frame: float
    max_displacement: float | None = None

    @property
    def label(self) -> str:
        return f"组合{self.combo_id}"


def combo_rule_from_args(args: argparse.Namespace) -> ComboRule:
    combo_id = int(args.combo_id)
    max_dpf = float(args.max_disp_per_frame)
    max_disp: float | None = float(args.max_displacement) if float(args.max_displacement) > 0 else None
    if combo_id == 2 and args.max_disp_per_frame == COMBO1_MAX_DISP_PER_FRAME:
        max_dpf = COMBO2_MAX_DISP_PER_FRAME
    if combo_id == 3:
        max_dpf = COMBO1_MAX_DISP_PER_FRAME
        max_disp = max_disp if max_disp is not None else COMBO3_MAX_DISPLACEMENT
    return ComboRule(
        combo_id=combo_id,
        min_frames=int(args.min_frames),
        min_duration=float(args.min_duration),
        max_disp_per_frame=max_dpf,
        max_displacement=max_disp,
    )


def _infer_size_from_frames(frames: list[dict[str, Any]], manifest: dict[str, Any]) -> tuple[int, int]:
    infer_w = int(manifest.get("infer_width") or 0)
    infer_h = int(manifest.get("infer_height") or 0)
    if infer_w > 0 and infer_h > 0:
        return infer_w, infer_h
    for fr in frames:
        if not isinstance(fr, dict):
            continue
        w = int(fr.get("infer_width") or 0)
        h = int(fr.get("infer_height") or 0)
        if w > 0 and h > 0:
            return w, h
    return 640, 480


def segment_combo_pass(seg: dict[str, Any], rule: ComboRule) -> bool:
    fc = int(seg.get("frame_count") or 0)
    dur = float(seg.get("duration_sec") or 0.0)
    disp = float(seg.get("displacement") or 0.0)
    if fc < rule.min_frames:
        return False
    if dur < rule.min_duration:
        return False
    if rule.max_displacement is not None and disp > rule.max_displacement:
        return False
    return (disp / fc) <= rule.max_disp_per_frame


def segment_combo_detail(seg: dict[str, Any], rule: ComboRule) -> dict[str, Any]:
    fc = int(seg.get("frame_count") or 0)
    dur = float(seg.get("duration_sec") or 0.0)
    disp = float(seg.get("displacement") or 0.0)
    dpf = disp / fc if fc > 0 else float("inf")
    pass_disp_cap = rule.max_displacement is None or disp <= rule.max_displacement
    return {
        "frame_count": fc,
        "duration_sec": round(dur, 4),
        "displacement": round(disp, 2),
        "disp_per_frame": round(dpf, 4),
        "pass_disp_cap": pass_disp_cap,
        "pass_all": segment_combo_pass(seg, rule),
    }


def _simulate_alarms(
    frames: list[dict[str, Any]],
    boxes: list[dict[str, Any]],
    *,
    alarm_min: int,
    alarm_cooldown: int,
    fps: float,
) -> list[tuple[int, str]]:
    processor = CollisionProcessor(
        boxes,
        alarm_min_consecutive_frames=max(1, int(alarm_min)),
        alarm_cooldown_frames=max(1, int(alarm_cooldown)),
        video_fps=fps,
    )
    out: list[tuple[int, str]] = []
    for fr in frames:
        if not isinstance(fr, dict):
            continue
        idx = int(fr.get("source_frame_idx") or fr.get("frame_idx") or 0)
        event = processor.process({"frame_idx": idx, "persons": fr.get("persons") or []})
        for raw in event.get("alarm_collisions") or []:
            token = canonical_box_token(str(raw).strip())
            if token:
                out.append((idx, token))
    return out


def _build_segments(
    frames: list[dict[str, Any]],
    boxes: list[dict[str, Any]],
    *,
    fps: float,
    max_gap_frames: int = 1,
) -> list[dict[str, Any]]:
    tracked = assign_person_tracks_to_frames(frames, video_fps=fps)
    return extract_collision_segment_rows(tracked, boxes, max_gap_frames=max_gap_frames)


def _alarms_to_timeline_rows(alarms: list[tuple[int, str]]) -> list[dict[str, Any]]:
    by_frame: dict[int, list[str]] = {}
    for fi, tok in alarms:
        by_frame.setdefault(fi, []).append(tok)
    return [{"frame_idx": fi, "alarm_collisions": toks} for fi, toks in sorted(by_frame.items())]


def _false_alarm_by_frame_from_alarms(
    alarms: list[tuple[int, str]],
    gt_segments: list[GroundTruthSegment],
) -> dict[int, list[str]]:
    return _build_false_alarm_by_frame(_alarms_to_timeline_rows(alarms), gt_segments)


def _segments_for_alarm(
    fi: int,
    token: str,
    segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for seg in segments:
        tok = canonical_box_token(str(seg.get("box_token") or ""))
        if tok != token:
            continue
        a = int(seg.get("frame_enter") or 0)
        b = int(seg.get("frame_exit") or 0)
        if a <= fi <= b:
            out.append(seg)
    return out


def filter_alarms_by_combo(
    alarms: list[tuple[int, str]],
    segments: list[dict[str, Any]],
    rule: ComboRule,
) -> tuple[list[tuple[int, str]], list[dict[str, Any]]]:
    kept: list[tuple[int, str]] = []
    dropped: list[dict[str, Any]] = []
    for fi, token in alarms:
        cands = _segments_for_alarm(fi, token, segments)
        if any(segment_combo_pass(s, rule) for s in cands):
            kept.append((fi, token))
        else:
            dropped.append(
                {
                    "frame_idx": fi,
                    "box_token": token,
                    "candidate_segments": len(cands),
                    "segments": [segment_combo_detail(s, rule) for s in cands[:3]],
                }
            )
    return kept, dropped


def _precision_proxy(detected: int, false_alarms: int) -> float | None:
    denom = detected + false_alarms
    if denom <= 0:
        return None
    return round(detected / denom, 4)


def _aggregate_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in rows if r.get("status") == "ok"]
    totals = {
        "records": len(ok),
        "gt_segments": sum(int(r.get("gt_segments") or 0) for r in ok),
        "detected": sum(int(r.get("detected") or 0) for r in ok),
        "missed": sum(int(r.get("missed") or 0) for r in ok),
        "false_alarms": sum(int(r.get("false_alarms") or 0) for r in ok),
        "alarm_count": sum(int(r.get("alarm_count") or 0) for r in ok),
    }
    g = totals["gt_segments"]
    d = totals["detected"]
    totals["recall"] = round(d / g, 4) if g else None
    totals["miss_rate"] = round(totals["missed"] / g, 4) if g else None
    totals["precision_proxy"] = _precision_proxy(d, totals["false_alarms"])
    return totals


def _segment_pool_stats(
    segments: list[dict[str, Any]],
    gt_segments: list[GroundTruthSegment],
    false_alarm_by_frame: dict[int, list[str]],
    rule: ComboRule,
) -> dict[str, Any]:
    gt_list: list[dict[str, Any]] = []
    fa_list: list[dict[str, Any]] = []
    other_list: list[dict[str, Any]] = []
    for seg in segments:
        if _seg_overlaps_gt(seg, gt_segments):
            gt_list.append(seg)
        elif _seg_overlaps_false_alarm(seg, false_alarm_by_frame):
            fa_list.append(seg)
        else:
            other_list.append(seg)

    def bucket(lst: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(lst)
        passed = sum(1 for s in lst if segment_combo_pass(s, rule))
        return {
            "n": n,
            "pass": passed,
            "fail": n - passed,
            "pass_rate": round(passed / n, 4) if n else None,
        }

    return {
        "gt_overlap": bucket(gt_list),
        "false_alarm_overlap": bucket(fa_list),
        "other": bucket(other_list),
    }


def _evaluate_record(
    record_id: str,
    *,
    alarm_min: int,
    alarm_cooldown: int,
    combo_rule: ComboRule | None = None,
    apply_combo: bool = False,
) -> dict[str, Any]:
    paths = resolve_app_paths()
    locator = locate_record_by_id(record_id)
    if not locator:
        return {"record_id": record_id, "status": "error", "error": "记录不存在"}

    review = load_event_review(locator)
    verified = review.get("verified_true") if isinstance(review.get("verified_true"), list) else []
    gt_segments = build_ground_truth_segments([e for e in verified if isinstance(e, dict)])
    if not gt_segments:
        return {"record_id": record_id, "status": "skipped", "error": "无有效标真段"}

    manifest = load_manifest(locator)
    frames = load_all_frames(locator)
    if not frames:
        return {"record_id": record_id, "status": "error", "error": "无帧数据"}

    ann_path = resolve_annotation_for_accuracy_record(paths, locator, pose_tier=DEFAULT_TIER)
    if not ann_path or not ann_path.is_file():
        return {"record_id": record_id, "status": "error", "error": "无标注 JSON"}

    infer_w, infer_h = _infer_size_from_frames(frames, manifest)
    boxes = load_scaled_boxes(ann_path, infer_w, infer_h)
    if not boxes:
        return {"record_id": record_id, "status": "error", "error": "标注无有效货框"}

    fps = float(manifest.get("fps") or 15.0)
    if fps <= 0:
        fps = 15.0

    segments = _build_segments(frames, boxes, fps=fps)
    raw_alarms = _simulate_alarms(
        frames, boxes, alarm_min=alarm_min, alarm_cooldown=alarm_cooldown, fps=fps
    )
    false_alarm_bf = _false_alarm_by_frame_from_alarms(raw_alarms, gt_segments)
    rule = combo_rule or ComboRule(1, COMBO1_MIN_FRAMES, COMBO1_MIN_DURATION, COMBO1_MAX_DISP_PER_FRAME)
    seg_stats = _segment_pool_stats(segments, gt_segments, false_alarm_bf, rule)

    alarms = raw_alarms
    dropped_alarms: list[dict[str, Any]] = []
    if apply_combo:
        alarms, dropped_alarms = filter_alarms_by_combo(raw_alarms, segments, rule)

    metrics = evaluate_segments(gt_segments, alarms)
    _, slug, _ = parse_record_path_segments(record_id)

    return {
        "record_id": record_id,
        "clip": locator.path.name,
        "camera_slug": slug,
        "status": "ok",
        "alarm_min": alarm_min,
        "combo_rule": {
            "combo_id": rule.combo_id,
            "min_frames": rule.min_frames,
            "min_duration": rule.min_duration,
            "max_disp_per_frame": rule.max_disp_per_frame,
            "max_displacement": rule.max_displacement,
        },
        "apply_combo": apply_combo,
        "gt_segments": metrics["gt_segments"],
        "detected": metrics["detected"],
        "missed": metrics["missed"],
        "false_alarms": metrics["false_alarms"],
        "recall": metrics["recall"],
        "miss_rate": metrics["miss_rate"],
        "alarm_count": len(alarms),
        "raw_alarm_count": len(raw_alarms),
        "alarms_dropped_by_combo": len(dropped_alarms),
        "precision_proxy": _precision_proxy(metrics["detected"], metrics["false_alarms"]),
        "segment_stats": seg_stats,
        "missed_segments": [s for s in metrics["segment_details"] if not s.get("detected")][:5],
        "dropped_alarm_samples": dropped_alarms[:5],
    }


def _delta(a: dict[str, Any], b: dict[str, Any], key: str) -> str:
    va, vb = a.get(key), b.get(key)
    if va is None or vb is None:
        return "—"
    if isinstance(va, float) and isinstance(vb, float):
        diff = vb - va
        sign = "+" if diff > 0 else ""
        return f"{sign}{diff:.4f}"
    if isinstance(va, int) and isinstance(vb, int):
        diff = vb - va
        sign = "+" if diff > 0 else ""
        return f"{sign}{diff}"
    return "—"


def _render_markdown(
    *,
    record_ids: list[str],
    baseline_rows: list[dict[str, Any]],
    combo_rows: list[dict[str, Any]],
    min22_rows: list[dict[str, Any]],
    combo_rule: ComboRule,
    combo1_ref_rows: list[dict[str, Any]] | None,
    tags: list[str],
    cameras: list[str],
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    base = _aggregate_metrics(baseline_rows)
    combo = _aggregate_metrics(combo_rows)
    m22 = _aggregate_metrics(min22_rows)
    c1 = _aggregate_metrics(combo1_ref_rows) if combo1_ref_rows else None

    pool_gt_n = pool_gt_pass = pool_fa_n = pool_fa_pass = pool_fa_fail = pool_gt_fail = 0
    for r in combo_rows:
        if r.get("status") != "ok":
            continue
        ss = r.get("segment_stats") or {}
        g = ss.get("gt_overlap") or {}
        f = ss.get("false_alarm_overlap") or {}
        pool_gt_n += int(g.get("n") or 0)
        pool_gt_pass += int(g.get("pass") or 0)
        pool_fa_n += int(f.get("n") or 0)
        pool_fa_pass += int(f.get("pass") or 0)
        pool_fa_fail += int(f.get("fail") or 0)
        pool_gt_fail += int(g.get("fail") or 0)

    combo_label = combo_rule.label
    ref_col = f"组合1 参考" if c1 else "min=22 参考"
    ref_metrics = c1 if c1 else m22

    lines = [
        f"# RTMPose-M {combo_label} 段特征过滤评估",
        "",
        f"> 生成时间：{now}  ",
        f"> 样本：**{len(record_ids)}** 条（{', '.join(tags)} · 已复核 · 有标真）  ",
        f"> 机位：{', '.join(cameras)}  ",
        "> 基线：`alarm_min=5`，`cooldown=6`，内存重算告警  ",
        f"> 实验：基线告警 + **{combo_label}** 段级确认  ",
        f"> 参考：{'组合1（disp/fc≤2.5）' if c1 else '`alarm_min=22`（单阈值）'}  ",
        "",
        f"## {combo_label} 规则",
        "",
        "| 条件 | 阈值 |",
        "|------|------|",
        f"| `frame_count` | ≥ {combo_rule.min_frames} |",
        f"| `duration_sec` | ≥ {combo_rule.min_duration} |",
        f"| `displacement / frame_count` | ≤ {combo_rule.max_disp_per_frame} |",
    ]
    if combo_rule.max_displacement is not None:
        lines.append(f"| `displacement`（总位移） | ≤ {combo_rule.max_displacement} |")
    n_conds = 3 + (1 if combo_rule.max_displacement is not None else 0)
    lines.extend(
        [
            "",
            f"{n_conds} 项 **同时满足**（AND）；段来自碰撞段提取（`max_gap_frames=1`）。",
            "",
            "## 1. 系统级准确率（标真段 vs 告警）",
            "",
            f"| 指标 | min=5 基线 | min=5 + {combo_label} | 变化 | {ref_col} |",
            "|------|------------|---------------|------|-------------|",
        ]
    )

    def row(label: str, key: str, fmt=lambda x: x):
        lines.append(
            f"| {label} | {fmt(base.get(key))} | {fmt(combo.get(key))} | "
            f"{_delta(base, combo, key)} | {fmt(ref_metrics.get(key))} |"
        )

    row("标真段数", "gt_segments")
    row("检出段（TP）", "detected")
    row("漏报段（FN）", "missed")
    row("误报次数（FP）", "false_alarms")
    row("召回率 recall", "recall", lambda x: f"{x:.2%}" if x is not None else "—")
    row("漏报率 miss_rate", "miss_rate", lambda x: f"{x:.2%}" if x is not None else "—")
    row("精确率代理¹", "precision_proxy", lambda x: f"{x:.2%}" if x is not None else "—")
    row("告警事件数", "alarm_count")

    lines.extend(["", "¹ 精确率代理 = 检出段 / (检出段 + 误报次数)。", "", "### 解读", ""])

    fa_d = int(combo.get("false_alarms") or 0) - int(base.get("false_alarms") or 0)
    miss_d = int(combo.get("missed") or 0) - int(base.get("missed") or 0)
    if fa_d < 0:
        lines.append(
            f"- {combo_label} 在 min=5 基线上将误报由 **{base['false_alarms']}** 降至 **{combo['false_alarms']}**（{fa_d}）。"
        )
    else:
        lines.append(f"- 误报变化：{fa_d}。")
    if c1 and combo_rule.combo_id in (2, 3):
        fa_c1 = int(combo.get("false_alarms") or 0) - int(c1.get("false_alarms") or 0)
        miss_c1 = int(combo.get("missed") or 0) - int(c1.get("missed") or 0)
        lines.append(
            f"- 相对组合1：误报 {c1['false_alarms']} → {combo['false_alarms']}（{fa_c1}）；"
            f"漏报 {c1['missed']} → {combo['missed']}（{'+' if miss_c1 >= 0 else ''}{miss_c1}）。"
        )
    if miss_d > 0:
        lines.append(
            f"- 漏报由 **{base['missed']}** 增至 **{combo['missed']}**（+{miss_d}）；"
            f"召回 {base.get('recall', 0):.1%} → {combo.get('recall', 0):.1%}。"
        )
    elif miss_d < 0:
        lines.append(f"- 漏报减少 {-miss_d}。")
    else:
        lines.append("- 漏报段数不变。")

    if pool_fa_n:
        lines.extend(
            [
                "",
                f"## 2. 段级{combo_label} 通过率（相对基线 min=5 误报段）",
                "",
                f"| 段类型 | 段数 | {combo_label} 通过 | {combo_label} 不通过 | 通过率 |",
                "|--------|------|------------|--------------|--------|",
                f"| 标真重叠段 | {pool_gt_n} | {pool_gt_pass} | {pool_gt_fail} | {pool_gt_pass / pool_gt_n:.1%} |",
                f"| 误报重叠段 | {pool_fa_n} | {pool_fa_pass} | {pool_fa_fail} | {pool_fa_pass / pool_fa_n:.1%} |",
                "",
                f"- 误报碰撞段被抑制：**{pool_fa_fail} / {pool_fa_n}**（{pool_fa_fail / pool_fa_n:.1%}）",
                f"- 标真碰撞段被保留：**{pool_gt_pass} / {pool_gt_n}**（{pool_gt_pass / pool_gt_n:.1%}）",
                f"- 标真碰撞段误杀：**{pool_gt_fail}**",
                f"- 误报碰撞段漏网：**{pool_fa_pass}**",
            ]
        )

    lines.extend(
        [
            "",
            f"## 3. 分机位对比（系统级）",
            "",
            f"| 机位 | 标真段 | 漏报@5 | 漏报@{combo_label} | 误报@5 | 误报@{combo_label} | 召回@5 | 召回@{combo_label} |",
            "|------|--------|--------|------------|--------|------------|--------|------------|",
        ]
    )

    by_cam: dict[str, dict[str, dict[str, int]]] = {}
    for label, rows in (("b", baseline_rows), ("c", combo_rows)):
        for r in rows:
            if r.get("status") != "ok":
                continue
            cam = str(r.get("camera_slug") or "")
            bucket = by_cam.setdefault(cam, {"b": {}, "c": {}})
            for k in ("gt_segments", "missed", "false_alarms", "detected"):
                bucket[label][k] = bucket[label].get(k, 0) + int(r.get(k) or 0)

    for cam in cameras:
        s = by_cam.get(cam)
        if not s:
            continue
        b, c = s.get("b", {}), s.get("c", {})
        g = b.get("gt_segments") or c.get("gt_segments") or 0
        rec_b = b.get("detected", 0) / g if g else 0
        rec_c = c.get("detected", 0) / g if g else 0
        lines.append(
            f"| {cam} | {g} | {b.get('missed', 0)} | {c.get('missed', 0)} | "
            f"{b.get('false_alarms', 0)} | {c.get('false_alarms', 0)} | "
            f"{rec_b:.0%} | {rec_c:.0%} |"
        )

    lines.extend(["", "## 4. 单条记录明细", ""])
    lines.append(
        f"| 记录 | 机位 | 标真段 | 漏报@5 | 漏报@{combo_label} | 误报@5 | 误报@{combo_label} | "
        f"告警@5 | 告警@{combo_label} | 剔除 |"
    )
    lines.append("|------|------|--------|--------|------------|--------|------------|----------|------------|-----------|")

    combo_by_id = {r["record_id"]: r for r in combo_rows if r.get("status") == "ok"}
    for b in baseline_rows:
        if b.get("status") != "ok":
            continue
        c = combo_by_id.get(b["record_id"], {})
        lines.append(
            f"| `{b.get('clip', '')}` | {b.get('camera_slug', '')} | {b.get('gt_segments', 0)} | "
            f"{b.get('missed', 0)} | {c.get('missed', 0)} | {b.get('false_alarms', 0)} | {c.get('false_alarms', 0)} | "
            f"{b.get('alarm_count', 0)} | {c.get('alarm_count', 0)} | {c.get('alarms_dropped_by_combo', 0)} |"
        )

    lines.extend(
        [
            "",
            "## 5. 方法说明",
            "",
            "- 脚本：`scripts/data/evaluate_combo1_segment_filter.py`",
            f"- {combo_label}：告警帧须落在同 token 碰撞段上，且该段最终属性通过三项 AND",
            "- 评估规则与 `api/accuracy_service.py` 一致",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="评估段特征组合过滤")
    parser.add_argument("--tier", default=DEFAULT_TIER)
    parser.add_argument("--tags", default=",".join(DEFAULT_TAGS))
    parser.add_argument("--cameras", default=",".join(DEFAULT_CAMERAS))
    parser.add_argument("--alarm-min", type=int, default=5)
    parser.add_argument("--alarm-min-ref", type=int, default=22)
    parser.add_argument("--cooldown", type=int, default=6)
    parser.add_argument("--combo-id", type=int, default=1, choices=(1, 2, 3))
    parser.add_argument("--min-frames", type=int, default=COMBO1_MIN_FRAMES)
    parser.add_argument("--min-duration", type=float, default=COMBO1_MIN_DURATION)
    parser.add_argument("--max-disp-per-frame", type=float, default=COMBO1_MAX_DISP_PER_FRAME)
    parser.add_argument(
        "--max-displacement",
        type=float,
        default=0.0,
        help="段总位移上限 px；combo3 默认 10，0 表示不启用",
    )
    parser.add_argument(
        "--out",
        default="",
        help="默认 combo1/2/3 → docs/comboN-segment-filter-rtmpose-m.md",
    )
    parser.add_argument("--json-out", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    combo_rule = combo_rule_from_args(args)
    if not args.out:
        args.out = str(ROOT / "docs" / f"combo{combo_rule.combo_id}-segment-filter-rtmpose-m.md")

    combo1_rule = ComboRule(
        1, COMBO1_MIN_FRAMES, COMBO1_MIN_DURATION, COMBO1_MAX_DISP_PER_FRAME, None
    )

    resolve_config_path(None)
    tags = _parse_tags(args.tags)
    cameras = _parse_csv_list(args.cameras)
    record_ids = _collect_record_ids(
        tier=args.tier,
        cameras=set(cameras),
        tags=tags,
        review_status=DEFAULT_REVIEW_STATUS,
        has_verified=True,
    )
    if not record_ids:
        print("未找到匹配记录", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"将评估 {len(record_ids)} 条记录")
        for rid in record_ids:
            print(f"  {rid}")
        return 0

    baseline_rows: list[dict[str, Any]] = []
    combo_rows: list[dict[str, Any]] = []
    min22_rows: list[dict[str, Any]] = []
    combo1_ref_rows: list[dict[str, Any]] | None = None
    if combo_rule.combo_id in (2, 3):
        combo1_ref_rows = []

    for rid in record_ids:
        b = _evaluate_record(
            rid, alarm_min=args.alarm_min, alarm_cooldown=args.cooldown, apply_combo=False
        )
        c = _evaluate_record(
            rid,
            alarm_min=args.alarm_min,
            alarm_cooldown=args.cooldown,
            combo_rule=combo_rule,
            apply_combo=True,
        )
        m = _evaluate_record(
            rid, alarm_min=args.alarm_min_ref, alarm_cooldown=args.cooldown, apply_combo=False
        )
        baseline_rows.append(b)
        combo_rows.append(c)
        min22_rows.append(m)
        if combo1_ref_rows is not None:
            c1 = _evaluate_record(
                rid,
                alarm_min=args.alarm_min,
                alarm_cooldown=args.cooldown,
                combo_rule=combo1_rule,
                apply_combo=True,
            )
            combo1_ref_rows.append(c1)
        if b.get("status") == "ok" and c.get("status") == "ok":
            print(
                f"{rid.split('/')[-1]}: miss {b['missed']}→{c['missed']} "
                f"fa {b['false_alarms']}→{c['false_alarms']} "
                f"dropped {c.get('alarms_dropped_by_combo', 0)}"
            )

    json_out = args.json_out or str(Path(args.out).with_suffix(".json"))
    md = _render_markdown(
        record_ids=record_ids,
        baseline_rows=baseline_rows,
        combo_rows=combo_rows,
        min22_rows=min22_rows,
        combo_rule=combo_rule,
        combo1_ref_rows=combo1_ref_rows,
        tags=tags,
        cameras=cameras,
    )
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    print(f"\n报告: {out_path}")

    payload = {
        "combo_rule": {
            "combo_id": combo_rule.combo_id,
            "min_frames": combo_rule.min_frames,
            "min_duration": combo_rule.min_duration,
            "max_disp_per_frame": combo_rule.max_disp_per_frame,
            "max_displacement": combo_rule.max_displacement,
        },
        "record_ids": record_ids,
        "baseline": {"aggregate": _aggregate_metrics(baseline_rows), "records": baseline_rows},
        "combo_filtered": {"aggregate": _aggregate_metrics(combo_rows), "records": combo_rows},
        "min22_ref": {"aggregate": _aggregate_metrics(min22_rows), "records": min22_rows},
    }
    if combo1_ref_rows is not None:
        payload["combo1_ref"] = {
            "aggregate": _aggregate_metrics(combo1_ref_rows),
            "records": combo1_ref_rows,
        }
    jp = Path(json_out)
    jp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"JSON: {jp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
