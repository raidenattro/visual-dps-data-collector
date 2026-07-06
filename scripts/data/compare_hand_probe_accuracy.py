#!/usr/bin/env python3
"""对比手腕 baseline vs 手臂延长探针的准确率（内存重算，不写 timeline）。

模拟现场参数：pose_frame_interval、alarm_min、alarm_cooldown；与 verified_true 标真段对比。

用法（项目根目录）:
  python scripts/data/compare_hand_probe_accuracy.py --dry-run
  python scripts/data/compare_hand_probe_accuracy.py
  python scripts/data/compare_hand_probe_accuracy.py \\
    --pose-frame-interval 2 --alarm-min 3 --cooldown 0 --extension-ratio 0.4 \\
    --out docs/hand-probe-ab-prod-params-rtmpose-m.md
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
from event_engine.collision_sim import (
    filter_pose_inference_frames,
    simulate_alarms_from_frames,
    stored_pose_frame_interval,
)
from event_engine.wrist_hits import ProbeMode
from pose_store import load_all_frames, load_event_review, load_manifest

from api.accuracy_service import (
    build_ground_truth_segments,
    evaluate_segments,
    resolve_annotation_for_accuracy_record,
)
from api.record_service import locate_record_by_id
from scripts.data.eval_dataset import (
    DEFAULT_CAMERAS,
    DEFAULT_REVIEW_STATUS,
    DEFAULT_TAGS,
    DEFAULT_TIER,
    collect_record_ids,
    parse_csv_list,
    parse_tags,
)
from scripts.data.report_paths import DOCS_JSON_DIR, resolve_docs_json


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


def _simulate_record_alarms(
    locator,
    annotation_path: Path,
    *,
    pose_frame_interval: int,
    alarm_min: int,
    alarm_cooldown: int,
    probe_mode: ProbeMode,
    extension_ratio: float,
) -> tuple[list[tuple[int, str]], dict[str, Any]]:
    manifest = load_manifest(locator)
    all_frames = load_all_frames(locator)
    meta: dict[str, Any] = {
        "frame_count_all": len(all_frames),
        "stored_interval": stored_pose_frame_interval(manifest),
    }
    if not all_frames:
        return [], meta

    infer_w, infer_h = _infer_size_from_frames(all_frames, manifest)
    boxes = load_scaled_boxes(annotation_path, infer_w, infer_h)
    if not boxes:
        return [], meta

    fps = float(manifest.get("fps") or 15.0)
    if fps <= 0:
        fps = 15.0

    frames = filter_pose_inference_frames(
        all_frames,
        pose_frame_interval,
        stored_interval=meta["stored_interval"],
    )
    meta["frame_count_used"] = len(frames)

    alarms = simulate_alarms_from_frames(
        frames,
        boxes,
        alarm_min_consecutive_frames=alarm_min,
        alarm_cooldown_frames=alarm_cooldown,
        video_fps=fps,
        probe_mode=probe_mode,
        extension_ratio=extension_ratio,
    )
    return alarms, meta


def _precision_proxy(detected: int, false_alarms: int) -> float | None:
    denom = detected + false_alarms
    if denom <= 0:
        return None
    return round(detected / denom, 4)


def _evaluate_record(
    record_id: str,
    *,
    pose_frame_interval: int,
    alarm_min: int,
    alarm_cooldown: int,
    probe_mode: ProbeMode,
    extension_ratio: float,
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

    ann_path = resolve_annotation_for_accuracy_record(paths, locator, pose_tier=DEFAULT_TIER)
    if not ann_path or not ann_path.is_file():
        return {"record_id": record_id, "status": "error", "error": "无标注 JSON"}

    alarms, sim_meta = _simulate_record_alarms(
        locator,
        ann_path,
        pose_frame_interval=pose_frame_interval,
        alarm_min=alarm_min,
        alarm_cooldown=alarm_cooldown,
        probe_mode=probe_mode,
        extension_ratio=extension_ratio,
    )
    metrics = evaluate_segments(segments, alarms)
    _, slug, _ = parse_record_path_segments(record_id)

    return {
        "record_id": record_id,
        "clip": locator.path.name,
        "camera_slug": slug,
        "status": "ok",
        "probe_mode": probe_mode,
        "extension_ratio": extension_ratio if probe_mode == "hand_extended" else None,
        "pose_frame_interval": pose_frame_interval,
        "stored_interval": sim_meta.get("stored_interval"),
        "frame_count_all": sim_meta.get("frame_count_all"),
        "frame_count_used": sim_meta.get("frame_count_used"),
        "alarm_min": alarm_min,
        "alarm_cooldown": alarm_cooldown,
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
    params: dict[str, Any],
    baseline_rows: list[dict[str, Any]],
    experiment_rows: list[dict[str, Any]],
    tags: list[str],
    cameras: list[str],
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    base_agg = _aggregate(baseline_rows)
    exp_agg = _aggregate(experiment_rows)

    lines = [
        "# RTMPose-M 手腕 vs 手臂延长探针准确率对比",
        "",
        f"> 生成时间：{now}  ",
        f"> 样本：**{len(record_ids)}** 条（标签 {', '.join(tags)} · 已复核 · 有标真）  ",
        f"> 机位：{', '.join(cameras)}  ",
        f"> 现场模拟：`pose_frame_interval={params['pose_frame_interval']}`，"
        f"`alarm_min={params['alarm_min']}`，`alarm_cooldown={params['alarm_cooldown']}`  ",
        f"> Baseline：探针 **wrist（手腕）**  ",
        f"> 实验组：探针 **hand_extended（延长 α={params['extension_ratio']}）**  ",
        "> 方法：同一 skeleton + reflection 标注，**内存重算**（不写 timeline）  ",
        "> 评估：标真段内匹配 `alarm_collisions` = 检出；未覆盖告警 = 误报",
        "",
        "## 1. 汇总对比",
        "",
        "| 指标 | 手腕 baseline | 手臂延长 | 变化 (延长−手腕) |",
        "|------|---------------|----------|------------------|",
    ]

    def row_metric(label: str, key: str, fmt=lambda x: x):
        lines.append(
            f"| {label} | {fmt(base_agg.get(key))} | {fmt(exp_agg.get(key))} | "
            f"{_delta(base_agg, exp_agg, key)} |"
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

    lines.extend([
        "",
        "¹ **精确率代理** = 检出段 / (检出段 + 误报次数)。",
        "",
        "## 2. 分机位对比",
        "",
        "| 机位 | 标真段 | 漏报@手腕 | 漏报@延长 | 误报@手腕 | 误报@延长 | 召回@手腕 | 召回@延长 |",
        "|------|--------|-----------|-----------|-----------|-----------|-----------|-----------|",
    ])

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
    lines.append("| 记录 | 机位 | 采集间隔→使用帧 | 标真段 | 漏报@手腕 | 漏报@延长 | 误报@手腕 | 误报@延长 |")
    lines.append("|------|------|-----------------|--------|-----------|-----------|-----------|-----------|")

    exp_by_id = {r["record_id"]: r for r in experiment_rows if r.get("status") == "ok"}
    for b in baseline_rows:
        if b.get("status") != "ok":
            continue
        e = exp_by_id.get(b["record_id"], {})
        interval_note = (
            f"{b.get('stored_interval', '?')}→{b.get('frame_count_used', '?')}"
            f"/{b.get('frame_count_all', '?')}"
        )
        lines.append(
            f"| `{b.get('clip', '')}` | {b.get('camera_slug', '')} | {interval_note} | "
            f"{b.get('gt_segments', 0)} | {b.get('missed', 0)} | {e.get('missed', 0)} | "
            f"{b.get('false_alarms', 0)} | {e.get('false_alarms', 0)} |"
        )

    rescued: list[tuple[dict, dict]] = []
    worsened: list[tuple[dict, dict]] = []
    for b in baseline_rows:
        if b.get("status") != "ok":
            continue
        e = exp_by_id.get(b["record_id"])
        if not e:
            continue
        if int(e.get("detected") or 0) > int(b.get("detected") or 0):
            rescued.append((b, e))
        if int(e.get("false_alarms") or 0) > int(b.get("false_alarms") or 0):
            worsened.append((b, e))

    if rescued:
        lines.extend(["", "## 4. 延长探针救回检出（相对手腕）", ""])
        for b, e in rescued:
            lines.append(f"- `{b.get('clip')}`：检出 {b.get('detected')} → {e.get('detected')}")

    if worsened:
        lines.extend(["", "## 5. 延长探针新增误报（相对手腕）", ""])
        for b, e in worsened:
            lines.append(
                f"- `{b.get('clip')}`：误报 {b.get('false_alarms')} → {e.get('false_alarms')}"
            )

    lines.extend([
        "",
        "## 方法说明",
        "",
        "- 脚本：`scripts/data/compare_hand_probe_accuracy.py`",
        "- 抽帧：`filter_pose_inference_frames`（与 `collect_core` 源帧对齐）",
        "- 延长：`P_sim = P_wrist + α × (P_wrist − P_elbow)`，无效时 fallback 手腕",
        "- `alarm_cooldown_frames=0` 表示无冷却（允许连续推理帧重复告警）",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="手腕 vs 手臂延长探针准确率对比（内存重算）")
    parser.add_argument("--tier", default=DEFAULT_TIER)
    parser.add_argument("--tags", default=",".join(DEFAULT_TAGS))
    parser.add_argument("--cameras", default=",".join(DEFAULT_CAMERAS))
    parser.add_argument("--pose-frame-interval", type=int, default=2)
    parser.add_argument("--alarm-min", type=int, default=3)
    parser.add_argument("--cooldown", type=int, default=0)
    parser.add_argument("--extension-ratio", type=float, default=0.4)
    parser.add_argument(
        "--out",
        default=str(ROOT / "docs" / "hand-probe-ab-prod-params-rtmpose-m.md"),
    )
    parser.add_argument("--json-out", default="")
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
        print(
            f"参数: interval={args.pose_frame_interval}, min={args.alarm_min}, "
            f"cooldown={args.cooldown}, extension={args.extension_ratio}"
        )
        for rid in record_ids:
            print(f"  {rid}")
        return 0

    baseline_rows: list[dict[str, Any]] = []
    experiment_rows: list[dict[str, Any]] = []

    for rid in record_ids:
        common = {
            "pose_frame_interval": args.pose_frame_interval,
            "alarm_min": args.alarm_min,
            "alarm_cooldown": args.cooldown,
            "extension_ratio": args.extension_ratio,
        }
        b = _evaluate_record(rid, probe_mode="wrist", **common)
        e = _evaluate_record(rid, probe_mode="hand_extended", **common)
        baseline_rows.append(b)
        experiment_rows.append(e)
        if b.get("status") == "ok" and e.get("status") == "ok":
            print(
                f"{rid.split('/')[-1]}: miss {b['missed']}→{e['missed']} "
                f"fa {b['false_alarms']}→{e['false_alarms']} "
                f"frames {b.get('frame_count_used')}/{b.get('frame_count_all')}"
            )
        else:
            print(f"{rid}: {b.get('status')} / {e.get('status')} {b.get('error', '')}")

    params = {
        "pose_frame_interval": args.pose_frame_interval,
        "alarm_min": args.alarm_min,
        "alarm_cooldown": args.cooldown,
        "extension_ratio": args.extension_ratio,
    }
    md = _render_markdown(
        record_ids=record_ids,
        params=params,
        baseline_rows=baseline_rows,
        experiment_rows=experiment_rows,
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
        "params": params,
        "baseline": {"aggregate": _aggregate(baseline_rows), "records": baseline_rows},
        "hand_extended": {"aggregate": _aggregate(experiment_rows), "records": experiment_rows},
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"JSON: {json_path}")

    base_agg = _aggregate(baseline_rows)
    exp_agg = _aggregate(experiment_rows)
    print(
        f"\n汇总: recall {base_agg.get('recall')} → {exp_agg.get('recall')} | "
        f"miss {base_agg.get('missed')} → {exp_agg.get('missed')} | "
        f"fa {base_agg.get('false_alarms')} → {exp_agg.get('false_alarms')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
