#!/usr/bin/env python3
"""alarm_min=5 下碰撞段 displacement × disp/fc 散点图（误报 / 漏报 / 正确检测）。

每个点 = 一条手腕碰撞段，坐标为 (displacement, displacement/frame_count)。
分类与 evaluate_combo1 一致：内存重算 alarm_min=5 告警，标真段检出与否决定 TP/FN，
误报告警重叠段为 FP。

用法（项目根目录）:
  python scripts/data/plot_alarm_min_disp_fc_scatter.py
  python scripts/data/plot_alarm_min_disp_fc_scatter.py --out docs/alarm-min/alarm-min5-disp-fc-scatter-rtmpose-m

SVG 输出至 docs/view/；Markdown 输出至 docs/alarm-min/；JSON 输出至 docs/json/。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from event_engine.cv2_shim import ensure_cv2_point_polygon_test

ensure_cv2_point_polygon_test()

from scripts.data.report_paths import DOCS_JSON_DIR, DOCS_VIEW_DIR, resolve_docs_json

from config_loader import parse_record_path_segments, resolve_config_path
from scripts.data.eval_dataset import (
    DEFAULT_CAMERAS,
    DEFAULT_REVIEW_STATUS,
    DEFAULT_TAGS,
    DEFAULT_TIER,
    collect_record_ids,
    parse_csv_list,
    parse_tags,
    seg_overlaps_false_alarm,
    seg_overlaps_gt,
)
from scripts.data.evaluate_combo1_segment_filter import (
    ComboRule,
    _build_segments,
    _false_alarm_by_frame_from_alarms,
    _infer_size_from_frames,
    _simulate_alarms,
    filter_alarms_by_combo,
)

from api.accuracy_service import GroundTruthSegment, build_ground_truth_segments, evaluate_segments
from config_loader import resolve_app_paths
from event_engine.annotation_boxes import load_scaled_boxes
from pose_store import load_all_frames, load_event_review, load_manifest
from api.accuracy_service import resolve_annotation_for_accuracy_record
from api.record_service import locate_record_by_id
from event_engine.box_identity import token_matches_any

CATEGORY_TP = "正确检测"
CATEGORY_FN = "漏报"
CATEGORY_FP = "误报"

CATEGORY_COLORS = {
    CATEGORY_TP: "#2ca02c",
    CATEGORY_FN: "#ff7f0e",
    CATEGORY_FP: "#d62728",
}

REF_DPF = 2.5
# combo4 单条件：仅 disp/fc ≤ 2.5
DPF_FILTER_RULE = ComboRule(combo_id=4, min_frames=1, min_duration=0.0, max_disp_per_frame=REF_DPF)


@dataclass
class ScatterPoint:
    record_id: str
    clip: str
    camera_slug: str
    category: str
    displacement: float
    disp_per_frame: float
    frame_count: int
    duration_sec: float
    box_token: str
    frame_enter: int
    frame_exit: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "clip": self.clip,
            "camera_slug": self.camera_slug,
            "category": self.category,
            "displacement": round(self.displacement, 2),
            "disp_per_frame": round(self.disp_per_frame, 4),
            "frame_count": self.frame_count,
            "duration_sec": round(self.duration_sec, 4),
            "box_token": self.box_token,
            "frame_enter": self.frame_enter,
            "frame_exit": self.frame_exit,
        }


def _ranges_overlap(a0: int, a1: int, b0: int, b1: int) -> bool:
    return a0 <= b1 and b0 <= a1


def _overlapping_gt_segments(
    seg: dict[str, Any], gt_segments: list[GroundTruthSegment]
) -> list[GroundTruthSegment]:
    a = int(seg.get("frame_enter") or 0)
    b = int(seg.get("frame_exit") or 0)
    tok = str(seg.get("box_token") or "").strip()
    if not tok:
        return []
    out: list[GroundTruthSegment] = []
    for gt in gt_segments:
        if not _ranges_overlap(a, b, gt.frame_start, gt.frame_end):
            continue
        if token_matches_any(tok, list(gt.gt_tokens)):
            out.append(gt)
    return out


def _classify_segment(
    seg: dict[str, Any],
    gt_segments: list[GroundTruthSegment],
    gt_detected: dict[tuple[int, int, str], bool],
    false_alarm_by_frame: dict[int, list[str]],
) -> str | None:
    overlaps = _overlapping_gt_segments(seg, gt_segments)
    if overlaps:
        for gt in overlaps:
            if gt_detected.get(_gt_key(gt), False):
                return CATEGORY_TP
        return CATEGORY_FN
    if seg_overlaps_false_alarm(seg, false_alarm_by_frame):
        return CATEGORY_FP
    return None


def _gt_key(gt: GroundTruthSegment) -> tuple[int, int, str]:
    return (gt.frame_start, gt.frame_end, ",".join(sorted(gt.gt_tokens)))


def _gt_detected_map(
    gt_segments: list[GroundTruthSegment], alarms: list[tuple[int, str]]
) -> dict[tuple[int, int, str], bool]:
    metrics = evaluate_segments(gt_segments, alarms)
    details = metrics.get("segment_details") or []
    out: dict[tuple[int, int, str], bool] = {}
    for gt, row in zip(gt_segments, details):
        out[_gt_key(gt)] = bool(row.get("detected"))
    return out


def _aggregate_system(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {}
    g = sum(int(r["gt_segments"]) for r in rows)
    d = sum(int(r["detected"]) for r in rows)
    missed = sum(int(r["missed"]) for r in rows)
    fa = sum(int(r["false_alarms"]) for r in rows)
    return {
        "gt_segments": g,
        "detected": d,
        "missed": missed,
        "false_alarms": fa,
        "recall": round(d / g, 4) if g else None,
    }


def _collect_points_for_record(
    record_id: str,
    *,
    alarm_min: int,
    alarm_cooldown: int,
    tier: str,
) -> tuple[list[ScatterPoint], dict[str, Any] | None]:
    paths = resolve_app_paths()
    locator = locate_record_by_id(record_id)
    if not locator:
        return [], None

    review = load_event_review(locator)
    verified = review.get("verified_true") if isinstance(review.get("verified_true"), list) else []
    gt_segments = build_ground_truth_segments([e for e in verified if isinstance(e, dict)])
    if not gt_segments:
        return [], None

    manifest = load_manifest(locator)
    frames = load_all_frames(locator)
    if not frames:
        return [], None

    ann_path = resolve_annotation_for_accuracy_record(paths, locator, pose_tier=tier)
    if not ann_path or not ann_path.is_file():
        return [], None

    infer_w, infer_h = _infer_size_from_frames(frames, manifest)
    boxes = load_scaled_boxes(ann_path, infer_w, infer_h)
    if not boxes:
        return [], None

    fps = float(manifest.get("fps") or 15.0)
    if fps <= 0:
        fps = 15.0

    segments = _build_segments(frames, boxes, fps=fps)
    raw_alarms = _simulate_alarms(
        frames, boxes, alarm_min=alarm_min, alarm_cooldown=alarm_cooldown, fps=fps
    )
    false_alarm_bf = _false_alarm_by_frame_from_alarms(raw_alarms, gt_segments)
    gt_detected = _gt_detected_map(gt_segments, raw_alarms)
    base_metrics = evaluate_segments(gt_segments, raw_alarms)
    filtered_alarms, _ = filter_alarms_by_combo(raw_alarms, segments, DPF_FILTER_RULE)
    filt_metrics = evaluate_segments(gt_segments, filtered_alarms)
    sys_row = {
        "record_id": record_id,
        "gt_segments": int(base_metrics["gt_segments"]),
        "detected": int(base_metrics["detected"]),
        "missed": int(base_metrics["missed"]),
        "false_alarms": int(base_metrics["false_alarms"]),
        "filtered_detected": int(filt_metrics["detected"]),
        "filtered_missed": int(filt_metrics["missed"]),
        "filtered_false_alarms": int(filt_metrics["false_alarms"]),
        "raw_alarm_count": len(raw_alarms),
        "filtered_alarm_count": len(filtered_alarms),
    }
    _, slug, _ = parse_record_path_segments(record_id)

    points: list[ScatterPoint] = []
    for seg in segments:
        cat = _classify_segment(seg, gt_segments, gt_detected, false_alarm_bf)
        if cat is None:
            continue
        fc = int(seg.get("frame_count") or 0)
        if fc <= 0:
            continue
        disp = float(seg.get("displacement") or 0.0)
        dpf = disp / fc
        if not math.isfinite(dpf):
            continue
        points.append(
            ScatterPoint(
                record_id=record_id,
                clip=locator.path.name,
                camera_slug=slug,
                category=cat,
                displacement=disp,
                disp_per_frame=dpf,
                frame_count=fc,
                duration_sec=float(seg.get("duration_sec") or 0.0),
                box_token=str(seg.get("box_token") or ""),
                frame_enter=int(seg.get("frame_enter") or 0),
                frame_exit=int(seg.get("frame_exit") or 0),
            )
        )
    return points, sys_row


def _svg_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _render_svg(
    points: list[ScatterPoint],
    *,
    alarm_min: int = 5,
    baseline_sys: dict[str, Any] | None = None,
    filtered_sys: dict[str, Any] | None = None,
    width: int = 960,
    height: int = 640,
    margin: tuple[int, int, int, int] = (72, 48, 56, 64),
) -> str:
    left, top, right, bottom = margin
    plot_w = width - left - right
    plot_h = height - top - bottom

    xs = [p.displacement for p in points]
    ys = [p.disp_per_frame for p in points]
    x_max = max(xs) * 1.05 if xs else 100.0
    y_max = max(max(ys) * 1.08, REF_DPF * 1.4) if ys else 6.0
    x_max = max(x_max, 10.0)
    y_max = max(y_max, REF_DPF * 1.2)

    def sx(x: float) -> float:
        return left + (x / x_max) * plot_w

    def sy(y: float) -> float:
        return top + plot_h - (y / y_max) * plot_h

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="Segoe UI, sans-serif">',
        '<rect width="100%" height="100%" fill="#fafafa"/>',
        f'<text x="{width/2:.1f}" y="28" text-anchor="middle" font-size="16" font-weight="600">'
        f"alarm_min={alarm_min} 碰撞段：displacement × disp/fc</text>",
        f'<text x="{width/2:.1f}" y="48" text-anchor="middle" font-size="12" fill="#555">'
        f"绿=正确检测  橙=漏报  红=误报  |  虚线 disp/fc={REF_DPF}</text>",
        # 坐标轴
        f'<line x1="{left}" y1="{top+plot_h}" x2="{left+plot_w}" y2="{top+plot_h}" stroke="#333"/>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top+plot_h}" stroke="#333"/>',
    ]

    # 网格与刻度
    x_ticks = 5
    for i in range(x_ticks + 1):
        xv = x_max * i / x_ticks
        px = sx(xv)
        lines.append(
            f'<line x1="{px:.1f}" y1="{top}" x2="{px:.1f}" y2="{top+plot_h}" stroke="#e0e0e0"/>'
        )
        lines.append(
            f'<text x="{px:.1f}" y="{top+plot_h+20}" text-anchor="middle" font-size="11">{xv:.0f}</text>'
        )

    y_ticks = 5
    for i in range(y_ticks + 1):
        yv = y_max * i / y_ticks
        py = sy(yv)
        lines.append(
            f'<line x1="{left}" y1="{py:.1f}" x2="{left+plot_w}" y2="{py:.1f}" stroke="#e0e0e0"/>'
        )
        lines.append(
            f'<text x="{left-8}" y="{py+4:.1f}" text-anchor="end" font-size="11">{yv:.1f}</text>'
        )

    lines.append(
        f'<text x="{left+plot_w/2:.1f}" y="{height-18}" text-anchor="middle" font-size="13">'
        f"displacement (px)</text>"
    )
    lines.append(
        f'<text x="18" y="{top+plot_h/2:.1f}" text-anchor="middle" font-size="13" '
        f'transform="rotate(-90 18 {top+plot_h/2:.1f})">disp / frame_count</text>'
    )

    # 参考线 disp/fc = 2.5
    ref_y = sy(REF_DPF)
    lines.append(
        f'<line x1="{left}" y1="{ref_y:.1f}" x2="{left+plot_w}" y2="{ref_y:.1f}" '
        f'stroke="#1f77b4" stroke-width="1.5" stroke-dasharray="6,4" opacity="0.85"/>'
    )
    lines.append(
        f'<text x="{left+plot_w-4}" y="{ref_y-6:.1f}" text-anchor="end" font-size="11" fill="#1f77b4">'
        f"dpf={REF_DPF}</text>"
    )

    # 散点（FP 先画，TP 最后，避免被盖住）
    order = {CATEGORY_FP: 0, CATEGORY_FN: 1, CATEGORY_TP: 2}
    for p in sorted(points, key=lambda x: order.get(x.category, 9)):
        color = CATEGORY_COLORS.get(p.category, "#888")
        cx, cy = sx(p.displacement), sy(p.disp_per_frame)
        tip = (
            f"{p.category}\\n{p.clip}\\n机位 {p.camera_slug}\\n"
            f"disp={p.displacement:.1f} px  dpf={p.disp_per_frame:.3f}\\n"
            f"fc={p.frame_count}  dur={p.duration_sec:.3f}s\\n"
            f"帧 {p.frame_enter}-{p.frame_exit}  token={p.box_token}"
        )
        lines.append(
            f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="4.5" fill="{color}" '
            f'fill-opacity="0.72" stroke="#fff" stroke-width="0.6">'
            f"<title>{_svg_escape(tip)}</title></circle>"
        )

    # 右上角：dpf 过滤统计 + 图例
    legend_top = top + 12
    if baseline_sys and filtered_sys:
        base_fp = int(baseline_sys.get("false_alarms") or 0)
        filt_fp = int(filtered_sys.get("false_alarms") or 0)
        fp_reduced = base_fp - filt_fp
        base_recall = baseline_sys.get("recall")
        filt_recall = filtered_sys.get("recall")
        box_w, box_h = 212, 58
        box_x = left + plot_w - box_w - 4
        box_y = top + 6
        lines.append(
            f'<rect x="{box_x:.1f}" y="{box_y:.1f}" width="{box_w}" height="{box_h}" '
            f'fill="#fff" fill-opacity="0.93" stroke="#bbb" rx="5"/>'
        )
        lines.append(
            f'<text x="{box_x + 10:.1f}" y="{box_y + 18:.1f}" font-size="12" font-weight="600">'
            f"dpf≤{REF_DPF} 过滤误报</text>"
        )
        lines.append(
            f'<text x="{box_x + 10:.1f}" y="{box_y + 34:.1f}" font-size="11" fill="#333">'
            f"FP {base_fp} → {filt_fp}  (−{fp_reduced})</text>"
        )
        if base_recall is not None and filt_recall is not None:
            lines.append(
                f'<text x="{box_x + 10:.1f}" y="{box_y + 50:.1f}" font-size="10" fill="#666">'
                f"召回 {base_recall:.1%} → {filt_recall:.1%}</text>"
            )
        legend_top = box_y + box_h + 10

    lx, ly = left + plot_w - 150, legend_top
    for i, (cat, color) in enumerate(CATEGORY_COLORS.items()):
        yy = ly + i * 20
        n = sum(1 for p in points if p.category == cat)
        lines.append(f'<circle cx="{lx}" cy="{yy}" r="5" fill="{color}"/>')
        lines.append(
            f'<text x="{lx+12}" y="{yy+4}" font-size="12">{cat} ({n})</text>'
        )

    lines.append("</svg>")
    return "\n".join(lines)


def _render_markdown(
    points: list[ScatterPoint],
    *,
    record_ids: list[str],
    tags: list[str],
    cameras: list[str],
    alarm_min: int,
    base_stem: str,
    baseline_sys: dict[str, Any],
    filtered_sys: dict[str, Any],
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    counts = {cat: sum(1 for p in points if p.category == cat) for cat in CATEGORY_COLORS}
    svg_name = f"../view/{Path(base_stem).name}.svg"

    def stats_for(cat: str, key: str) -> str:
        vals = [getattr(p, key) for p in points if p.category == cat]
        if not vals:
            return "—"
        vals = sorted(vals)
        p50 = vals[len(vals) // 2]
        return f"P50={p50:.2f}"

    fp_points = [p for p in points if p.category == CATEGORY_FP]
    fp_seg_blocked = sum(1 for p in fp_points if p.disp_per_frame > REF_DPF)
    fp_seg_pass = len(fp_points) - fp_seg_blocked

    base_fp = int(baseline_sys.get("false_alarms") or 0)
    filt_fp = int(filtered_sys.get("false_alarms") or 0)
    fp_reduced = base_fp - filt_fp
    base_fn = int(baseline_sys.get("missed") or 0)
    filt_fn = int(filtered_sys.get("missed") or 0)
    base_recall = baseline_sys.get("recall")
    filt_recall = filtered_sys.get("recall")

    lines = [
        f"# RTMPose-M alarm_min={alarm_min} 碰撞段 displacement × disp/fc 分布",
        "",
        f"> 生成时间：{now}  ",
        f"> 样本：**{len(record_ids)}** 条（{', '.join(tags)} · 已复核 · 有标真）  ",
        f"> 机位：{', '.join(cameras)}  ",
        f"> 门控：`alarm_min={alarm_min}` 内存重算告警；**未**施加 disp/fc 段过滤  ",
        f"> 每个点 = 一条手腕碰撞段，悬停 SVG 查看明细；全量点数据见 `json/{Path(base_stem).name}.json`  ",
        "",
        "## 分类说明",
        "",
        "| 类别 | 含义 | 段数 | displacement | disp/fc |",
        "|------|------|------|--------------|---------|",
    ]
    for cat in (CATEGORY_TP, CATEGORY_FN, CATEGORY_FP):
        lines.append(
            f"| {cat} | "
            + {
                CATEGORY_TP: "与**已检出**标真段重叠的碰撞段",
                CATEGORY_FN: "与**漏报**标真段重叠的碰撞段",
                CATEGORY_FP: f"与 alarm_min={alarm_min} **误报告警**重叠、且不优先归标真段",
            }[cat]
            + f" | {counts[cat]} | {stats_for(cat, 'displacement')} | {stats_for(cat, 'disp_per_frame')} |"
        )

    lines.extend(
        [
            "",
            f"## dpf≤{REF_DPF} 段过滤（combo4 单条件）",
            "",
            "在散点图同一 `alarm_min` 基线上，对候选告警施加 **仅** `displacement/frame_count ≤ "
            f"{REF_DPF}` 的段级确认（与 combo4 一致）。",
            "",
            "### 系统级（标真段 vs 告警）",
            "",
            "| 指标 | alarm_min 基线 | + dpf≤2.5 | 变化 |",
            "|------|----------------|------------|------|",
            f"| 误报 FP | {base_fp} | {filt_fp} | **-{fp_reduced}** |",
            f"| 漏报 FN | {base_fn} | {filt_fn} | {'+' if filt_fn - base_fn >= 0 else ''}{filt_fn - base_fn} |",
            f"| 召回 recall | {base_recall:.1%} | {filt_recall:.1%} | "
            f"{(filt_recall or 0) - (base_recall or 0):+.1%} |"
            if base_recall is not None and filt_recall is not None
            else f"| 召回 recall | — | — | — |",
            "",
            "### 误报碰撞段（散点图红点）",
            "",
            f"- 误报重叠碰撞段 **{len(fp_points)}** 条；其中 `disp/fc > {REF_DPF}`：**{fp_seg_blocked}** 条"
            f"（段级不通过，关联告警易被抑制）",
            f"- `disp/fc ≤ {REF_DPF}` 仍通过：**{fp_seg_pass}** 条（段过滤后仍可能留下误报）",
            f"- **系统级误报减少 {fp_reduced} 次**（{base_fp} → {filt_fp}）",
            "",
        ]
    )

    lines.extend(
        [
            "",
            "## 散点图",
            "",
            f"![displacement × disp/fc]({svg_name})",
            "",
            f"参考虚线：`disp/fc = {REF_DPF}`（combo4 单条件阈值）。",
            "",
            "## 读图提示",
            "",
            "- 横轴 displacement、纵轴 disp/fc；`frame_count`/`duration` 在 tooltip 中给出，与 alarm_min 门控时长相关。",
            "- 若误报（红）大量落在虚线下方，说明仅 dpf≤2.5 即可区分；漏报（橙）若在虚线上方，提高 dpf 门槛会加剧漏报。",
            "- 脚本：`scripts/data/plot_alarm_min_disp_fc_scatter.py`",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="alarm_min=5 displacement×disp/fc 散点图")
    parser.add_argument("--tier", default=DEFAULT_TIER)
    parser.add_argument("--tags", default=",".join(DEFAULT_TAGS))
    parser.add_argument("--cameras", default=",".join(DEFAULT_CAMERAS))
    parser.add_argument("--alarm-min", type=int, default=5)
    parser.add_argument("--cooldown", type=int, default=6)
    parser.add_argument(
        "--out",
        default=str(ROOT / "docs" / "alarm-min" / "alarm-min5-disp-fc-scatter-rtmpose-m"),
        help="Markdown 输出路径前缀（不含扩展名）；SVG 写入 docs/view/；JSON 写入 docs/json/",
    )
    parser.add_argument(
        "--view-dir",
        default=str(DOCS_VIEW_DIR),
        help="SVG 输出目录（默认 docs/view）",
    )
    parser.add_argument("--json-out", default="", help="JSON 输出路径（默认 docs/json/{报告名}.json）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    resolve_config_path(None)
    tags = parse_tags(args.tags)
    cameras = parse_csv_list(args.cameras)
    record_ids = collect_record_ids(
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
        print(f"将处理 {len(record_ids)} 条记录")
        return 0

    all_points: list[ScatterPoint] = []
    sys_rows: list[dict[str, Any]] = []
    for rid in record_ids:
        pts, sys_row = _collect_points_for_record(
            rid, alarm_min=args.alarm_min, alarm_cooldown=args.cooldown, tier=args.tier
        )
        all_points.extend(pts)
        if sys_row is not None:
            sys_rows.append(sys_row)
        print(f"{rid}: {len(pts)} 点")

    if not all_points:
        print("无有效散点", file=sys.stderr)
        return 1

    base = Path(args.out)
    base.parent.mkdir(parents=True, exist_ok=True)
    stem = str(base.with_suffix(""))
    asset_name = base.name

    view_dir = Path(args.view_dir)
    view_dir.mkdir(parents=True, exist_ok=True)

    baseline_sys = _aggregate_system(sys_rows)
    filtered_sys = {
        "gt_segments": sum(int(r["gt_segments"]) for r in sys_rows),
        "detected": sum(int(r["filtered_detected"]) for r in sys_rows),
        "missed": sum(int(r["filtered_missed"]) for r in sys_rows),
        "false_alarms": sum(int(r["filtered_false_alarms"]) for r in sys_rows),
    }
    g = filtered_sys["gt_segments"]
    d = filtered_sys["detected"]
    filtered_sys["recall"] = round(d / g, 4) if g else None

    svg = _render_svg(
        all_points,
        alarm_min=args.alarm_min,
        baseline_sys=baseline_sys,
        filtered_sys=filtered_sys,
    )
    svg_path = view_dir / f"{asset_name}.svg"
    svg_path.write_text(svg, encoding="utf-8")

    md_path = base.with_suffix(".md")
    md_path.write_text(
        _render_markdown(
            all_points,
            record_ids=record_ids,
            tags=tags,
            cameras=cameras,
            alarm_min=args.alarm_min,
            base_stem=stem,
            baseline_sys=baseline_sys,
            filtered_sys=filtered_sys,
        ),
        encoding="utf-8",
    )

    json_path = resolve_docs_json(md_path, args.json_out)
    DOCS_JSON_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "alarm_min": args.alarm_min,
        "ref_disp_per_frame": REF_DPF,
        "record_ids": record_ids,
        "points": [p.to_dict() for p in all_points],
        "counts": {cat: sum(1 for p in all_points if p.category == cat) for cat in CATEGORY_COLORS},
        "baseline_system": baseline_sys,
        "dpf_filtered_system": filtered_sys,
        "dpf_filter_rule": {
            "max_disp_per_frame": REF_DPF,
            "min_frames": DPF_FILTER_RULE.min_frames,
            "min_duration": DPF_FILTER_RULE.min_duration,
        },
        "fp_segments_blocked_by_dpf": sum(
            1 for p in all_points if p.category == CATEGORY_FP and p.disp_per_frame > REF_DPF
        ),
        "per_record_system": sys_rows,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nSVG:  {svg_path}")
    print(f"MD:   {md_path}")
    print(f"JSON: {json_path}")
    for cat in (CATEGORY_TP, CATEGORY_FN, CATEGORY_FP):
        print(f"  {cat}: {payload['counts'][cat]}")
    fp_reduced = int(baseline_sys.get("false_alarms") or 0) - int(filtered_sys.get("false_alarms") or 0)
    print(
        f"  dpf≤{REF_DPF} 过滤误报: {baseline_sys.get('false_alarms')} → "
        f"{filtered_sys.get('false_alarms')} (-{fp_reduced})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
