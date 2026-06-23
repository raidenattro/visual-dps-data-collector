#!/usr/bin/env python3
"""alarm_min=5 下碰撞段 displacement × disp/fc 散点图（误报 / 漏报 / 正确检测）。

每个点 = 一条手腕碰撞段，坐标为 (displacement, displacement/frame_count)。
分类与 evaluate_combo1 一致：内存重算 alarm_min=5 告警，标真段检出与否决定 TP/FN，
误报告警重叠段为 FP。

用法（项目根目录）:
  python scripts/data/plot_alarm_min_disp_fc_scatter.py
  python scripts/data/plot_alarm_min_disp_fc_scatter.py --out docs/alarm-min5-disp-fc-scatter-rtmpose-m

SVG / HTML 输出至 docs/view/；Markdown / JSON 输出至 docs/。
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
DOCS_VIEW_DIR = ROOT / "docs" / "view"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 触发 evaluate_combo1 内 cv2 shim
import scripts.data.evaluate_combo1_segment_filter  # noqa: F401

from config_loader import parse_record_path_segments, resolve_config_path
from scripts.data.analyze_wrist_feature_discrimination import (
    DEFAULT_CAMERAS,
    DEFAULT_REVIEW_STATUS,
    DEFAULT_TAGS,
    DEFAULT_TIER,
    _collect_record_ids,
    _parse_csv_list,
    _parse_tags,
    _seg_overlaps_false_alarm,
    _seg_overlaps_gt,
)
from scripts.data.evaluate_combo1_segment_filter import (
    _build_segments,
    _false_alarm_by_frame_from_alarms,
    _infer_size_from_frames,
    _simulate_alarms,
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
    if _seg_overlaps_false_alarm(seg, false_alarm_by_frame):
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


def _collect_points_for_record(
    record_id: str,
    *,
    alarm_min: int,
    alarm_cooldown: int,
    tier: str,
) -> list[ScatterPoint]:
    paths = resolve_app_paths()
    locator = locate_record_by_id(record_id)
    if not locator:
        return []

    review = load_event_review(locator)
    verified = review.get("verified_true") if isinstance(review.get("verified_true"), list) else []
    gt_segments = build_ground_truth_segments([e for e in verified if isinstance(e, dict)])
    if not gt_segments:
        return []

    manifest = load_manifest(locator)
    frames = load_all_frames(locator)
    if not frames:
        return []

    ann_path = resolve_annotation_for_accuracy_record(paths, locator, pose_tier=tier)
    if not ann_path or not ann_path.is_file():
        return []

    infer_w, infer_h = _infer_size_from_frames(frames, manifest)
    boxes = load_scaled_boxes(ann_path, infer_w, infer_h)
    if not boxes:
        return []

    fps = float(manifest.get("fps") or 15.0)
    if fps <= 0:
        fps = 15.0

    segments = _build_segments(frames, boxes, fps=fps)
    raw_alarms = _simulate_alarms(
        frames, boxes, alarm_min=alarm_min, alarm_cooldown=alarm_cooldown, fps=fps
    )
    false_alarm_bf = _false_alarm_by_frame_from_alarms(raw_alarms, gt_segments)
    gt_detected = _gt_detected_map(gt_segments, raw_alarms)
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
    return points


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

    # 图例
    lx, ly = left + plot_w - 150, top + 12
    for i, (cat, color) in enumerate(CATEGORY_COLORS.items()):
        yy = ly + i * 20
        n = sum(1 for p in points if p.category == cat)
        lines.append(f'<circle cx="{lx}" cy="{yy}" r="5" fill="{color}"/>')
        lines.append(
            f'<text x="{lx+12}" y="{yy+4}" font-size="12">{cat} ({n})</text>'
        )

    lines.append("</svg>")
    return "\n".join(lines)


def _render_html(svg: str, points: list[ScatterPoint], title: str) -> str:
    rows = []
    for p in points:
        d = p.to_dict()
        rows.append(
            "<tr>"
            f"<td>{d['category']}</td>"
            f"<td>{d['clip']}</td>"
            f"<td>{d['camera_slug']}</td>"
            f"<td>{d['displacement']}</td>"
            f"<td>{d['disp_per_frame']}</td>"
            f"<td>{d['frame_count']}</td>"
            f"<td>{d['duration_sec']}</td>"
            f"<td>{d['frame_enter']}-{d['frame_exit']}</td>"
            f"<td>{d['box_token']}</td>"
            "</tr>"
        )
    table = "\n".join(rows)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <title>{title}</title>
  <style>
    body {{ font-family: Segoe UI, sans-serif; margin: 24px; background: #f5f5f5; }}
    h1 {{ font-size: 1.25rem; }}
    .chart {{ background: #fff; padding: 12px; border-radius: 8px; box-shadow: 0 1px 4px #0001; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 12px; margin-top: 20px; background: #fff; }}
    th, td {{ border: 1px solid #ddd; padding: 6px 8px; text-align: left; }}
    th {{ background: #eee; position: sticky; top: 0; }}
    tr:nth-child(even) {{ background: #fafafa; }}
    .tp {{ color: #2ca02c; }} .fn {{ color: #ff7f0e; }} .fp {{ color: #d62728; }}
  </style>
</head>
<body>
  <h1>{title}</h1>
  <p>悬停圆点查看 displacement / disp/fc；下表为全部 {len(points)} 个碰撞段明细。</p>
  <div class="chart">{svg}</div>
  <table>
    <thead>
      <tr>
        <th>类别</th><th>clip</th><th>机位</th>
        <th>displacement</th><th>disp/fc</th><th>frame_count</th><th>duration_sec</th>
        <th>帧范围</th><th>box_token</th>
      </tr>
    </thead>
    <tbody>
{table}
    </tbody>
  </table>
</body>
</html>
"""


def _render_markdown(
    points: list[ScatterPoint],
    *,
    record_ids: list[str],
    tags: list[str],
    cameras: list[str],
    alarm_min: int,
    base_stem: str,
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    counts = {cat: sum(1 for p in points if p.category == cat) for cat in CATEGORY_COLORS}
    svg_name = f"view/{Path(base_stem).name}.svg"
    html_name = f"view/{Path(base_stem).name}.html"

    def stats_for(cat: str, key: str) -> str:
        vals = [getattr(p, key) for p in points if p.category == cat]
        if not vals:
            return "—"
        vals = sorted(vals)
        p50 = vals[len(vals) // 2]
        return f"P50={p50:.2f}"

    lines = [
        f"# RTMPose-M alarm_min={alarm_min} 碰撞段 displacement × disp/fc 分布",
        "",
        f"> 生成时间：{now}  ",
        f"> 样本：**{len(record_ids)}** 条（{', '.join(tags)} · 已复核 · 有标真）  ",
        f"> 机位：{', '.join(cameras)}  ",
        f"> 门控：`alarm_min={alarm_min}` 内存重算告警；**未**施加 disp/fc 段过滤  ",
        f"> 每个点 = 一条手腕碰撞段，悬停 SVG 或打开 HTML 表查看明细  ",
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
                CATEGORY_FP: "与 min=5 **误报告警**重叠、且不优先归标真段",
            }[cat]
            + f" | {counts[cat]} | {stats_for(cat, 'displacement')} | {stats_for(cat, 'disp_per_frame')} |"
        )

    lines.extend(
        [
            "",
            "## 散点图",
            "",
            f"![displacement × disp/fc]({svg_name})",
            "",
            f"交互版（含全表）：[{html_name}]({html_name})",
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
        default=str(ROOT / "docs" / "alarm-min5-disp-fc-scatter-rtmpose-m"),
        help="Markdown / JSON 输出路径前缀（不含扩展名）；SVG / HTML 写入 docs/view/",
    )
    parser.add_argument(
        "--view-dir",
        default=str(DOCS_VIEW_DIR),
        help="SVG / HTML 输出目录（默认 docs/view）",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

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
        print(f"将处理 {len(record_ids)} 条记录")
        return 0

    all_points: list[ScatterPoint] = []
    for rid in record_ids:
        pts = _collect_points_for_record(
            rid, alarm_min=args.alarm_min, alarm_cooldown=args.cooldown, tier=args.tier
        )
        all_points.extend(pts)
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

    svg = _render_svg(all_points, alarm_min=args.alarm_min)
    svg_path = view_dir / f"{asset_name}.svg"
    svg_path.write_text(svg, encoding="utf-8")

    title = f"alarm_min={args.alarm_min} displacement × disp/fc（RTMPose-M）"
    html_path = view_dir / f"{asset_name}.html"
    html_path.write_text(_render_html(svg, all_points, title), encoding="utf-8")

    md_path = base.with_suffix(".md")
    md_path.write_text(
        _render_markdown(
            all_points,
            record_ids=record_ids,
            tags=tags,
            cameras=cameras,
            alarm_min=args.alarm_min,
            base_stem=stem,
        ),
        encoding="utf-8",
    )

    json_path = base.with_suffix(".json")
    payload = {
        "alarm_min": args.alarm_min,
        "ref_disp_per_frame": REF_DPF,
        "record_ids": record_ids,
        "points": [p.to_dict() for p in all_points],
        "counts": {cat: sum(1 for p in all_points if p.category == cat) for cat in CATEGORY_COLORS},
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nSVG:  {svg_path}")
    print(f"HTML: {html_path}")
    print(f"MD:   {md_path}")
    print(f"JSON: {json_path}")
    for cat in (CATEGORY_TP, CATEGORY_FN, CATEGORY_FP):
        print(f"  {cat}: {payload['counts'][cat]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
