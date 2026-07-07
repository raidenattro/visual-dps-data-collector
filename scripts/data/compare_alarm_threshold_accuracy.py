#!/usr/bin/env python3
"""对比不同 alarm_min_consecutive_frames 下的漏报/误报（不重写 timeline）。

在固定 28 条优质样本上，用同一套骨架 + 标注在内存中重算告警，
与人工 verified_true 范本对比（规则同 api/accuracy_service.py）。

用法（项目根目录）:
  python scripts/data/compare_alarm_threshold_accuracy.py
  python scripts/data/compare_alarm_threshold_accuracy.py --dry-run
  python scripts/data/compare_alarm_threshold_accuracy.py \\
    --baseline-min 5 --experiment-min 22 --cooldown 6 \\
    --out docs/alarm-min/alarm-min-22-accuracy-comparison-rtmpose-m.md
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from event_engine.cv2_shim import ensure_cv2_point_polygon_test

ensure_cv2_point_polygon_test()

from config_loader import parse_record_path_segments, resolve_app_paths, resolve_config_path
from event_engine.annotation_boxes import load_scaled_boxes
from event_engine.box_identity import canonical_box_token
from event_engine.collision_sim import (
    filter_pose_inference_frames,
    simulate_alarms_from_frames,
    stored_pose_frame_interval,
)
from pose_store import load_all_frames, load_event_review, load_manifest

from api.accuracy_service import (
    build_ground_truth_segments,
    evaluate_segments,
    resolve_annotation_for_accuracy_record,
)
from api.record_service import locate_record_by_id
from scripts.data.report_paths import DOCS_JSON_DIR, resolve_docs_json
from scripts.data.eval_dataset import (
    DEFAULT_CAMERAS,
    DEFAULT_REVIEW_STATUS,
    DEFAULT_TAGS,
    DEFAULT_TIER,
    collect_record_ids,
    parse_csv_list,
    parse_tags,
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


def _simulate_alarms(
    locator,
    annotation_path: Path,
    *,
    alarm_min: int,
    alarm_cooldown: int,
    pose_frame_interval: int = 1,
) -> list[tuple[int, str]]:
    """内存重算告警，不写回磁盘。"""
    manifest = load_manifest(locator)
    all_frames = load_all_frames(locator)
    if not all_frames:
        return []

    infer_w, infer_h = _infer_size_from_frames(all_frames, manifest)
    boxes = load_scaled_boxes(annotation_path, infer_w, infer_h)
    if not boxes:
        return []

    fps = float(manifest.get("fps") or 15.0)
    if fps <= 0:
        fps = 15.0

    stored = stored_pose_frame_interval(manifest)
    frames = filter_pose_inference_frames(
        all_frames,
        pose_frame_interval,
        stored_interval=stored,
    )

    return simulate_alarms_from_frames(
        frames,
        boxes,
        alarm_min_consecutive_frames=alarm_min,
        alarm_cooldown_frames=alarm_cooldown,
        video_fps=fps,
        probe_mode="wrist",
    )


def _alarms_from_stored_timeline(frames: list[dict[str, Any]]) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for fr in frames:
        if not isinstance(fr, dict):
            continue
        fi = int(fr.get("frame_idx") or 0)
        for raw in fr.get("alarm_collisions") or []:
            token = canonical_box_token(str(raw).strip())
            if token:
                out.append((fi, token))
    return out


def _precision_proxy(detected: int, false_alarms: int) -> float | None:
    denom = detected + false_alarms
    if denom <= 0:
        return None
    return round(detected / denom, 4)


def _evaluate_record(
    record_id: str,
    *,
    alarm_min: int,
    alarm_cooldown: int,
    pose_frame_interval: int = 1,
    use_stored: bool = False,
) -> dict[str, Any]:
    paths = resolve_app_paths()
    locator = locate_record_by_id(record_id)
    if not locator:
        return {"record_id": record_id, "status": "error", "error": "记录不存在"}

    review = load_event_review(locator)
    verified = review.get("verified_true") if isinstance(review.get("verified_true"), list) else []
    segments = build_ground_truth_segments([e for e in verified if isinstance(e, dict)])
    if not segments:
        return {"record_id": record_id, "status": "skipped", "error": "无有效标真段"}

    manifest = load_manifest(locator)
    frames = load_all_frames(locator)

    if use_stored:
        alarms = _alarms_from_stored_timeline(frames)
        cfg_label = "stored"
        stored_collision = manifest.get("collision") if isinstance(manifest.get("collision"), dict) else {}
        alarm_min_eff = int(stored_collision.get("alarm_min_consecutive_frames") or 0)
    else:
        ann_path = resolve_annotation_for_accuracy_record(paths, locator, pose_tier=DEFAULT_TIER)
        if not ann_path or not ann_path.is_file():
            return {"record_id": record_id, "status": "error", "error": "无标注 JSON"}
        alarms = _simulate_alarms(
            locator,
            ann_path,
            alarm_min=alarm_min,
            alarm_cooldown=alarm_cooldown,
            pose_frame_interval=pose_frame_interval,
        )
        cfg_label = f"min={alarm_min}"
        alarm_min_eff = alarm_min

    metrics = evaluate_segments(segments, alarms)
    _, slug, _ = parse_record_path_segments(record_id)

    return {
        "record_id": record_id,
        "clip": locator.path.name,
        "camera_slug": slug,
        "status": "ok",
        "config": cfg_label,
        "alarm_min": alarm_min_eff,
        "gt_segments": metrics["gt_segments"],
        "detected": metrics["detected"],
        "missed": metrics["missed"],
        "false_alarms": metrics["false_alarms"],
        "recall": metrics["recall"],
        "miss_rate": metrics["miss_rate"],
        "alarm_count": len(alarms),
        "precision_proxy": _precision_proxy(metrics["detected"], metrics["false_alarms"]),
        "missed_segments": [s for s in metrics["segment_details"] if not s.get("detected")][:5],
        "false_alarm_samples": metrics["false_alarm_samples"][:5],
    }


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
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


def _delta(baseline: dict[str, Any], experiment: dict[str, Any], key: str) -> str:
    a = baseline.get(key)
    b = experiment.get(key)
    if a is None or b is None:
        return "—"
    if isinstance(a, float) and isinstance(b, float):
        diff = b - a
        sign = "+" if diff > 0 else ""
        return f"{sign}{diff:.4f}"
    if isinstance(a, int) and isinstance(b, int):
        diff = b - a
        sign = "+" if diff > 0 else ""
        return f"{sign}{diff}"
    return "—"


def _render_markdown(
    *,
    record_ids: list[str],
    baseline_min: int,
    experiment_min: int,
    cooldown: int,
    baseline_rows: list[dict[str, Any]],
    experiment_rows: list[dict[str, Any]],
    stored_rows: list[dict[str, Any]],
    tags: list[str],
    cameras: list[str],
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    base_agg = _aggregate(baseline_rows)
    exp_agg = _aggregate(experiment_rows)
    stored_agg = _aggregate(stored_rows)

    lines = [
        "# RTMPose-M 告警门控对比：alarm_min=5 vs 22",
        "",
        f"> 生成时间：{now}  ",
        f"> 样本：**{len(record_ids)}** 条（标签 {', '.join(tags)} · 已复核 · 有标真）  ",
        f"> 机位：{', '.join(cameras)}  ",
        f"> 对比：`alarm_min_consecutive_frames={baseline_min}` vs `{experiment_min}`，`alarm_cooldown_frames={cooldown}`  ",
        "> 方法：同一 skeleton + reflection 合并标注，**内存重算**告警（不修改落盘 timeline）  ",
        "> 评估规则与 `api/accuracy_service.py` 一致：标真段内出现匹配告警=检出，否则漏报；未覆盖告警=误报",
        "",
        "## 1. 汇总对比",
        "",
        "| 指标 | 模拟 min=5 | 模拟 min=22 | 变化 (22−5) | 当前落盘 timeline |",
        "|------|------------|-------------|-------------|-------------------|",
    ]

    def row_metric(label: str, key: str, fmt=lambda x: x):
        lines.append(
            f"| {label} | {fmt(base_agg.get(key))} | {fmt(exp_agg.get(key))} | "
            f"{_delta(base_agg, exp_agg, key)} | {fmt(stored_agg.get(key))} |"
        )

    row_metric("记录数", "records")
    row_metric("标真段数", "gt_segments")
    row_metric("检出段（TP）", "detected")
    row_metric("漏报段（FN）", "missed")
    row_metric("误报次数（FP）", "false_alarms")
    row_metric("召回率 recall", "recall", lambda x: f"{x:.2%}" if x is not None else "—")
    row_metric("漏报率 miss_rate", "miss_rate", lambda x: f"{x:.2%}" if x is not None else "—")
    row_metric("精确率代理¹", "precision_proxy", lambda x: f"{x:.2%}" if x is not None else "—")
    row_metric("告警事件总数", "alarm_count")

    lines.extend(
        [
            "",
            "¹ **精确率代理** = 检出段 / (检出段 + 误报次数)，仅作误报/检出权衡参考，非标准目标检测 Precision。",
            "",
            "### 解读",
            "",
        ]
    )

    fa_delta = int(exp_agg.get("false_alarms") or 0) - int(base_agg.get("false_alarms") or 0)
    miss_delta = int(exp_agg.get("missed") or 0) - int(base_agg.get("missed") or 0)
    rec_b = base_agg.get("recall")
    rec_e = exp_agg.get("recall")

    if fa_delta < 0:
        lines.append(
            f"- 将连续帧门控从 **5 提到 22**，误报由 **{base_agg['false_alarms']}** 降至 **{exp_agg['false_alarms']}**"
            f"（{fa_delta}），有利于压制短促擦框误报。"
        )
    elif fa_delta > 0:
        lines.append(f"- 误报反而增加 {fa_delta}，需检查个别机位长接触或标注边界。")
    else:
        lines.append("- 误报总数不变。")

    if miss_delta > 0:
        lines.append(
            f"- 漏报段由 **{base_agg['missed']}** 增至 **{exp_agg['missed']}**（+{miss_delta}）；"
            f"召回 {rec_b:.1%} → {rec_e:.1%}。" if rec_b is not None and rec_e is not None else
            f"- 漏报段增加 {miss_delta}。"
        )
    elif miss_delta < 0:
        lines.append(f"- 漏报减少 {-miss_delta}（意外改善，可能原 min=5 告警与标真帧对齐更严）。")
    else:
        lines.append("- 漏报段数不变。")

    lines.extend(["", "## 2. 分机位对比", ""])
    by_cam: dict[str, dict[str, dict[str, Any]]] = {}
    for label, rows in (
        ("baseline", baseline_rows),
        ("experiment", experiment_rows),
    ):
        for r in rows:
            if r.get("status") != "ok":
                continue
            cam = str(r.get("camera_slug") or "")
            by_cam.setdefault(cam, {})[label] = r

    lines.append("| 机位 | 标真段 | 漏报@5 | 漏报@22 | 误报@5 | 误报@22 | 召回@5 | 召回@22 |")
    lines.append("|------|--------|--------|---------|--------|---------|--------|---------|")
    for cam in cameras:
        b = by_cam.get(cam, {}).get("baseline", {})
        e = by_cam.get(cam, {}).get("experiment", {})
        if not b and not e:
            continue
        # per-camera sum from records in rows - aggregate manually
        pass

    # per-camera aggregate from rows
    cam_stats: dict[str, dict[str, dict[str, int]]] = {}
    for label, rows in (("b", baseline_rows), ("e", experiment_rows)):
        for r in rows:
            if r.get("status") != "ok":
                continue
            cam = str(r.get("camera_slug") or "")
            bucket = cam_stats.setdefault(cam, {"b": {}, "e": {}})
            for k in ("gt_segments", "missed", "false_alarms", "detected"):
                bucket[label][k] = bucket[label].get(k, 0) + int(r.get(k) or 0)

    for cam in cameras:
        s = cam_stats.get(cam)
        if not s:
            continue
        bg = s.get("b", {})
        eg = s.get("e", {})
        g = bg.get("gt_segments") or eg.get("gt_segments") or 0
        rec_b = bg.get("detected", 0) / g if g else None
        rec_e = eg.get("detected", 0) / g if g else None
        lines.append(
            f"| {cam} | {g} | {bg.get('missed', 0)} | {eg.get('missed', 0)} | "
            f"{bg.get('false_alarms', 0)} | {eg.get('false_alarms', 0)} | "
            f"{rec_b:.0%} | {rec_e:.0%} |" if g else f"| {cam} | — |"
        )

    lines.extend(["", "## 3. 单条记录明细", ""])
    lines.append("| 记录 | 机位 | 标真段 | 漏报@5 | 漏报@22 | 误报@5 | 误报@22 | 告警数@5 | 告警数@22 |")
    lines.append("|------|------|--------|--------|---------|--------|---------|----------|-----------|")

    exp_by_id = {r["record_id"]: r for r in experiment_rows if r.get("status") == "ok"}
    for b in baseline_rows:
        if b.get("status") != "ok":
            continue
        e = exp_by_id.get(b["record_id"], {})
        lines.append(
            f"| `{b.get('clip', '')}` | {b.get('camera_slug', '')} | {b.get('gt_segments', 0)} | "
            f"{b.get('missed', 0)} | {e.get('missed', 0)} | {b.get('false_alarms', 0)} | {e.get('false_alarms', 0)} | "
            f"{b.get('alarm_count', 0)} | {e.get('alarm_count', 0)} |"
        )

    # records with increased misses
    worsened = []
    for b in baseline_rows:
        if b.get("status") != "ok":
            continue
        e = exp_by_id.get(b["record_id"])
        if not e:
            continue
        if int(e.get("missed") or 0) > int(b.get("missed") or 0):
            worsened.append((b, e))
    if worsened:
        lines.extend(["", "## 4. min=22 新增漏报记录", ""])
        for b, e in worsened:
            lines.append(f"### `{b.get('clip')}`")
            lines.append(f"- 漏报 {b.get('missed')} → {e.get('missed')}")
            for seg in e.get("missed_segments") or []:
                lines.append(
                    f"  - 帧 {seg.get('frame_start')}–{seg.get('frame_end')} "
                    f"范本 `{','.join(seg.get('gt_tokens') or [])}`"
                )
            lines.append("")

    lines.extend(
        [
            "## 5. 方法说明",
            "",
            "- 脚本：`scripts/data/compare_alarm_threshold_accuracy.py`",
            "- 标真来源：`event_review.verified_true`（优先 confirmed_box_tokens）",
            "- 告警重算：`CollisionProcessor` + reflection 合并标注",
            "- 「当前落盘」列为读取 `timeline.parquet` 已有 `alarm_collisions`（采集时参数可能为 3/5/6）",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="对比 alarm_min 门控下的漏报/误报")
    parser.add_argument("--tier", default=DEFAULT_TIER)
    parser.add_argument("--tags", default=",".join(DEFAULT_TAGS))
    parser.add_argument("--cameras", default=",".join(DEFAULT_CAMERAS))
    parser.add_argument("--baseline-min", type=int, default=5)
    parser.add_argument("--experiment-min", type=int, default=22)
    parser.add_argument("--cooldown", type=int, default=6)
    parser.add_argument("--pose-frame-interval", type=int, default=1)
    parser.add_argument(
        "--out",
        default=str(ROOT / "docs" / "alarm-min" / "alarm-min-22-accuracy-comparison-rtmpose-m.md"),
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
        print(f"将评估 {len(record_ids)} 条记录")
        for rid in record_ids:
            print(f"  {rid}")
        return 0

    baseline_rows: list[dict[str, Any]] = []
    experiment_rows: list[dict[str, Any]] = []
    stored_rows: list[dict[str, Any]] = []

    for rid in record_ids:
        b = _evaluate_record(
            rid,
            alarm_min=args.baseline_min,
            alarm_cooldown=args.cooldown,
            pose_frame_interval=args.pose_frame_interval,
        )
        e = _evaluate_record(
            rid,
            alarm_min=args.experiment_min,
            alarm_cooldown=args.cooldown,
            pose_frame_interval=args.pose_frame_interval,
        )
        s = _evaluate_record(rid, alarm_min=0, alarm_cooldown=args.cooldown, use_stored=True)
        baseline_rows.append(b)
        experiment_rows.append(e)
        stored_rows.append(s)
        if b.get("status") == "ok" and e.get("status") == "ok":
            print(
                f"{rid.split('/')[-1]}: miss {b['missed']}→{e['missed']} "
                f"fa {b['false_alarms']}→{e['false_alarms']}"
            )
        else:
            print(f"{rid}: {b.get('status')} / {e.get('status')} {b.get('error', '')}")

    md = _render_markdown(
        record_ids=record_ids,
        baseline_min=args.baseline_min,
        experiment_min=args.experiment_min,
        cooldown=args.cooldown,
        baseline_rows=baseline_rows,
        experiment_rows=experiment_rows,
        stored_rows=stored_rows,
        tags=tags,
        cameras=cameras,
    )
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    print(f"\n报告: {out_path}")

    json_path = resolve_docs_json(args.out, args.json_out)
    DOCS_JSON_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "record_ids": record_ids,
        "baseline_min": args.baseline_min,
        "experiment_min": args.experiment_min,
        "cooldown": args.cooldown,
        "baseline": {"aggregate": _aggregate(baseline_rows), "records": baseline_rows},
        "experiment": {"aggregate": _aggregate(experiment_rows), "records": experiment_rows},
        "stored": {"aggregate": _aggregate(stored_rows), "records": stored_rows},
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"JSON: {json_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
