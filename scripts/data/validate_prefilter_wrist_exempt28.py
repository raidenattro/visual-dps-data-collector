#!/usr/bin/env python3
"""前置门控 + 手腕/上肢低速豁免实验。

假设：取货时手腕需在货框内停留，手腕/肘/肩速度较慢；走过时全身均快。
逻辑：下肢 lower_mean_speed > 60 时，若 wrist_max 或 upper_mean 低于豁免阈值，仍做手腕进框检测。

参数：pose_frame_interval=2, alarm_min=3, cooldown=0

用法:
  python scripts/data/validate_prefilter_wrist_exempt28.py
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

from config_loader import resolve_app_paths, resolve_config_path
from event_engine.box_identity import token_matches_any
from event_engine.speed_gated_collision import SpeedGateConfig, recompute_prefilter_upload_frames
from event_engine.skeleton_features import extract_subsampled_velocity_from_frames
from pose_store import load_all_frames, load_manifest

from api.accuracy_service import GroundTruthSegment
from api.inference_eval_service import (
    UploadClipInput,
    aggregate_upload_clip_results,
    evaluate_uploaded_clip,
    load_inference_json_file,
)
from api.record_service import locate_record_by_id
from api.wrist_features_service import _infer_size_from_frames, _load_boxes_for_wrist_features, _video_fps
from scripts.data.analyze_skeleton_velocity_discrimination import (
    _float_or_none,
    _load_ground_truth_segments,
)
from scripts.data.upload_export_common import (
    export_indices_for_record,
    load_baseline_manifest,
    resolve_baseline_clip_path,
)

LOCAL_BASELINE_MANIFEST = ROOT / "localdata/export/rule-baseline-local-prod-test/_manifest.json"
LOCAL_BASELINE_DIR = LOCAL_BASELINE_MANIFEST.parent
OUT_JSON = ROOT / "docs/prefilter-wrist-exempt-experiment.json"
OUT_MD = ROOT / "docs/prefilter-wrist-exempt-speed-gate.md"

POSE_FRAME_INTERVAL = 2
ALARM_MIN = 3
ALARM_COOLDOWN = 0
LOWER_THRESHOLD = 60.0
EXEMPT_THRESHOLDS = [30, 40, 50, 60, 80, 100]

SQUAT_WATCH = (
    "clip_0009_start_00-37-59_rtmpose_m.json",
    "clip_0013_start_00-42-48_rtmpose_m.json",
    "clip_0020_start_00-48-44_rtmpose_m.json",
    "clip_0013_start_00-29-53_rtmpose_m.json",
)


def _prepare_record(entry: dict[str, Any]) -> dict[str, Any] | None:
    record_id = str(entry.get("record_id") or "").strip()
    loc = locate_record_by_id(record_id)
    if not loc:
        return {"record_id": record_id, "error": "记录不存在"}
    timeline_frames = load_all_frames(loc)
    if not timeline_frames:
        return {"record_id": record_id, "error": "无帧数据"}
    manifest = load_manifest(loc)
    infer_w, infer_h = _infer_size_from_frames(timeline_frames, manifest)
    fps = _video_fps(manifest)
    boxes, _, _ = _load_boxes_for_wrist_features(loc, manifest, infer_w=infer_w, infer_h=infer_h)
    if not boxes:
        return {"record_id": record_id, "error": "无货框标注"}
    export_indices = export_indices_for_record(
        entry, baseline_dir=LOCAL_BASELINE_DIR, timeline_frames=timeline_frames,
        pose_frame_interval=POSE_FRAME_INTERVAL,
    )
    if not export_indices:
        return {"record_id": record_id, "error": "无导出抽帧索引"}
    gt_segments, _ = _load_ground_truth_segments(loc)
    if not gt_segments:
        return {"record_id": record_id, "error": "无标真"}
    clip_path = resolve_baseline_clip_path(LOCAL_BASELINE_DIR, entry)
    baseline_frames = load_inference_json_file(clip_path) if clip_path else []
    velocity_rows = extract_subsampled_velocity_from_frames(
        timeline_frames, export_indices, infer_width=infer_w, infer_height=infer_h, video_fps=fps,
    )
    return {
        "record_id": record_id,
        "timeline_frames": timeline_frames,
        "boxes": boxes,
        "infer_w": infer_w,
        "infer_h": infer_h,
        "fps": fps,
        "export_indices": export_indices,
        "gt_segments": gt_segments,
        "baseline_frames": baseline_frames,
        "velocity_rows": velocity_rows,
        "upload_file": str(entry.get("file") or f"{entry.get('clip_name')}.json"),
    }


def _gt_overlap_frames(frames, gt_segments) -> set[int]:
    out: set[int] = set()
    for fr in frames:
        fi = int(fr.get("frame_idx") or 0)
        for gt in gt_segments:
            if gt.frame_start <= fi <= gt.frame_end:
                out.add(fi)
                break
    return out


def _false_alarm_frames(frames, gt_segments) -> set[int]:
    out: set[int] = set()
    for fr in frames:
        if not fr.get("is_picking"):
            continue
        fi = int(fr.get("frame_idx") or 0)
        tokens = list(fr.get("rule_alarm_collisions") or fr.get("rule_collisions") or [])
        covered = False
        for gt in gt_segments:
            if fi < gt.frame_start or fi > gt.frame_end:
                continue
            if not tokens or token_matches_any(str(tokens[0]), list(gt.gt_tokens)):
                covered = True
                break
        if not covered:
            out.add(fi)
    return out


def _speeds_at_frames(rows, frame_set: set[int], key: str) -> list[float]:
    vals = []
    for row in rows:
        fi = int(row.get("frame_idx") or 0)
        if fi in frame_set:
            v = _float_or_none(row.get(key))
            if v is not None:
                vals.append(v)
    return vals


def _p50(vals: list[float]) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    return s[len(s) // 2]


def _eval_clip(paths, ctx, gate: SpeedGateConfig) -> dict[str, Any]:
    frames = recompute_prefilter_upload_frames(
        ctx["timeline_frames"], ctx["export_indices"], ctx["boxes"],
        record_id=ctx["record_id"], speed_gate=gate,
        infer_width=ctx["infer_w"], infer_height=ctx["infer_h"], video_fps=ctx["fps"],
        alarm_min_consecutive_frames=ALARM_MIN, alarm_cooldown_frames=ALARM_COOLDOWN,
    )
    return evaluate_uploaded_clip(
        paths, UploadClipInput(upload_file=ctx["upload_file"], frames=frames, record_id=ctx["record_id"]),
    )


def _gate_label(gate: SpeedGateConfig) -> str:
    parts = [f"lower≤{gate.max_threshold}"]
    if gate.wrist_exempt_max_threshold is not None:
        parts.append(f"wrist豁免≤{gate.wrist_exempt_max_threshold}")
    if gate.upper_exempt_max_threshold is not None:
        parts.append(f"upper豁免≤{gate.upper_exempt_max_threshold}")
    return " + ".join(parts) if len(parts) > 1 else f"lower>{gate.max_threshold}（无豁免）"


def _build_configs() -> list[tuple[str, SpeedGateConfig]]:
    configs: list[tuple[str, SpeedGateConfig]] = []
    base = SpeedGateConfig(feature="lower_mean_speed", max_threshold=LOWER_THRESHOLD, fail_open=True)
    configs.append(("baseline_lower_only", base))
    for thr in EXEMPT_THRESHOLDS:
        configs.append((
            f"wrist_exempt_{thr}",
            SpeedGateConfig(
                feature="lower_mean_speed", max_threshold=LOWER_THRESHOLD, fail_open=True,
                wrist_exempt_max_threshold=thr,
            ),
        ))
    for thr in EXEMPT_THRESHOLDS:
        configs.append((
            f"upper_exempt_{thr}",
            SpeedGateConfig(
                feature="lower_mean_speed", max_threshold=LOWER_THRESHOLD, fail_open=True,
                upper_exempt_max_threshold=thr,
            ),
        ))
    for thr in [40, 50, 60, 80]:
        configs.append((
            f"both_exempt_{thr}",
            SpeedGateConfig(
                feature="lower_mean_speed", max_threshold=LOWER_THRESHOLD, fail_open=True,
                wrist_exempt_max_threshold=thr,
                upper_exempt_max_threshold=thr,
            ),
        ))
    return configs


def _build_md(report: dict[str, Any]) -> str:
    lines = [
        "# 前置门控 + 手腕/上肢低速豁免实验",
        "",
        f"> 生成时间：{report.get('generated_at', '')}",
        "> 脚本：`scripts/data/validate_prefilter_wrist_exempt28.py`",
        "",
        "## 1. 假设与逻辑",
        "",
        "取货时手腕需在货框内**停留一小段**，此时手腕速度较慢；肩、肘往往也较慢（伸手取货姿态）。",
        "走过误报时下肢与上肢通常**都在移动**（全身高速）。",
        "",
        "**复合门控**（在现有前置过滤上叠加）：",
        "",
        "```",
        "若 lower_mean_speed ≤ 60        → 不 block（下肢慢，正常检测）",
        "若 lower_mean_speed > 60        → 下肢快，进一步判断：",
        "    若 wrist_max_speed ≤ 豁免阈   → 不 block（手腕停留，像取货）",
        "    或 upper_mean_speed ≤ 豁免阈  → 不 block（肩肘腕整体慢）",
        "    否则                          → block（全身快，像走过）",
        "```",
        "",
        "- `wrist_max_speed`：左右腕速度最大值",
        "- `upper_mean_speed`：肩(5,6)+肘(7,8)+腕(9,10) 六点算术平均",
        "- 下肢门控特征仍为 `lower_mean_speed`（髋+膝+踝），阈值 60",
        "",
        "## 2. 参数",
        "",
        "| 参数 | 值 |",
        "|------|-----|",
        f"| pose_frame_interval | {report['params']['pose_frame_interval']} |",
        f"| alarm_min | {report['params']['alarm_min_consecutive_frames']} |",
        f"| cooldown | {report['params']['alarm_cooldown_frames']} |",
        f"| lower 门控阈值 | {report['params']['lower_threshold']} |",
        "",
        "## 3. 帧级速度分布（标真重叠帧 vs baseline 误报帧）",
        "",
        "| 特征 | 标真 P50 | 误报 P50 | 解读 |",
        "|------|----------|----------|------|",
    ]
    disc = report.get("limb_discrimination") or {}
    for key, label in (
        ("wrist_max_speed", "手腕最大速度"),
        ("upper_mean_speed", "肩肘腕平均"),
        ("lower_mean_speed", "下肢平均（对照）"),
    ):
        row = disc.get(key) or {}
        gt_p = row.get("gt_p50")
        fa_p = row.get("fa_p50")
        hint = ""
        if gt_p is not None and fa_p is not None:
            hint = "标真更慢" if gt_p < fa_p else "区分度弱"
        lines.append(f"| `{key}` | {gt_p} | {fa_p} | {hint} |")

    lines.extend([
        "",
        "## 4. 方案对比（相对 rule-baseline-local-prod-test）",
        "",
        "| 方案 | TP | FP | FN段 | 召回率 | vs baseline |",
        "|------|-----|-----|------|--------|-------------|",
    ])
    baseline_row = report.get("baseline_lower_only") or {}
    for row in report.get("results") or []:
        name = row.get("name", "")
        s = row.get("summary") or {}
        delta = ""
        if name != "baseline_lower_only":
            d_tp = int(s.get("detected") or 0) - int(baseline_row.get("detected") or 0)
            d_fp = int(s.get("false_alarms") or 0) - int(baseline_row.get("false_alarms") or 0)
            d_fn = int(s.get("missed") or 0) - int(baseline_row.get("missed") or 0)
            delta = f"TP{d_tp:+d} FP{d_fp:+d} FN{d_fn:+d}"
        lines.append(
            f"| {row.get('label', name)} | {s.get('detected')} | {s.get('false_alarms')} | "
            f"{s.get('missed')} | {s.get('recall')} | {delta} |"
        )

    lines.extend([
        "",
        f"local baseline（无过滤）：TP={report.get('local_baseline', {}).get('detected')} "
        f"FP={report.get('local_baseline', {}).get('false_alarms')} "
        f"recall={report.get('local_baseline', {}).get('recall')}",
        "",
        "## 5. 推荐方案",
        "",
        report.get("recommendation", ""),
        "",
        "## 6. 下蹲/漏报重点 clip",
        "",
        "| clip | 方案 | 漏报段 |",
        "|------|------|--------|",
    ])
    for item in report.get("squat_watch") or []:
        missed = "; ".join(
            f"{s.get('frame_start')}-{s.get('frame_end')}"
            for s in (item.get("missed_segments") or [])
        ) or "—"
        lines.append(f"| `{item.get('upload_file')}` | {item.get('label')} | {missed} |")

    lines.extend(["", "## 7. 结论", "", report.get("conclusion", ""), ""])
    return "\n".join(lines)


def main() -> int:
    resolve_config_path(None)
    paths = resolve_app_paths()
    records = list(load_baseline_manifest(LOCAL_BASELINE_MANIFEST).get("records") or [])
    prepared = []
    for entry in records:
        ctx = _prepare_record(entry)
        if not ctx or ctx.get("error"):
            print(f"{entry.get('record_id')}: SKIP {ctx.get('error') if ctx else '?'}")
            continue
        prepared.append(ctx)
        print(f"{ctx['record_id']}: ok")

    if not prepared:
        return 2

    # 帧级上肢/手腕分布
    gt_wrist, fa_wrist = [], []
    gt_upper, fa_upper = [], []
    gt_lower, fa_lower = [], []
    for ctx in prepared:
        gt_f = _gt_overlap_frames(ctx["baseline_frames"], ctx["gt_segments"])
        fa_f = _false_alarm_frames(ctx["baseline_frames"], ctx["gt_segments"])
        rows = ctx["velocity_rows"]
        gt_wrist.extend(_speeds_at_frames(rows, gt_f, "wrist_max_speed"))
        fa_wrist.extend(_speeds_at_frames(rows, fa_f, "wrist_max_speed"))
        gt_upper.extend(_speeds_at_frames(rows, gt_f, "upper_mean_speed"))
        fa_upper.extend(_speeds_at_frames(rows, fa_f, "upper_mean_speed"))
        gt_lower.extend(_speeds_at_frames(rows, gt_f, "lower_mean_speed"))
        fa_lower.extend(_speeds_at_frames(rows, fa_f, "lower_mean_speed"))

    limb_discrimination = {
        "wrist_max_speed": {"gt_p50": _p50(gt_wrist), "fa_p50": _p50(fa_wrist), "gt_n": len(gt_wrist), "fa_n": len(fa_wrist)},
        "upper_mean_speed": {"gt_p50": _p50(gt_upper), "fa_p50": _p50(fa_upper), "gt_n": len(gt_upper), "fa_n": len(fa_upper)},
        "lower_mean_speed": {"gt_p50": _p50(gt_lower), "fa_p50": _p50(fa_lower), "gt_n": len(gt_lower), "fa_n": len(fa_lower)},
    }

    local_baseline_clips = [
        evaluate_uploaded_clip(
            paths,
            UploadClipInput(upload_file=c["upload_file"], frames=c["baseline_frames"], record_id=c["record_id"]),
        )
        for c in prepared if c.get("baseline_frames")
    ]
    local_baseline = aggregate_upload_clip_results(local_baseline_clips)

    configs = _build_configs()
    results: list[dict[str, Any]] = []
    clip_cache: dict[str, list[dict[str, Any]]] = {}

    for name, gate in configs:
        print(f"\n=== {name}: {_gate_label(gate)} ===")
        clip_results = []
        for ctx in prepared:
            clip_results.append(_eval_clip(paths, ctx, gate))
        summary = aggregate_upload_clip_results(clip_results)
        clip_cache[name] = clip_results
        results.append({
            "name": name,
            "label": _gate_label(gate),
            "gate": {
                "feature": gate.feature,
                "max_threshold": gate.max_threshold,
                "wrist_exempt_max_threshold": gate.wrist_exempt_max_threshold,
                "upper_exempt_max_threshold": gate.upper_exempt_max_threshold,
            },
            "summary": summary,
        })
        print(f"  TP={summary.get('detected')} FP={summary.get('false_alarms')} "
              f"missed={summary.get('missed')} recall={summary.get('recall')}")

    baseline_lower_only = next(r["summary"] for r in results if r["name"] == "baseline_lower_only")

    # 选推荐：召回 >= baseline_lower 且 FP 最小；若召回更高优先
    best = None
    for row in results:
        s = row["summary"]
        rec = float(s.get("recall") or 0)
        base_rec = float(baseline_lower_only.get("recall") or 0)
        if rec < base_rec - 0.001:
            continue
        fp = int(s.get("false_alarms") or 0)
        tp = int(s.get("detected") or 0)
        score = (tp, -fp, rec)
        if best is None or score > best[0]:
            best = (score, row)

    best_row = best[1] if best else results[0]
    recommendation = (
        f"推荐：**{best_row['label']}** — "
        f"TP={best_row['summary'].get('detected')} FP={best_row['summary'].get('false_alarms')} "
        f"召回={best_row['summary'].get('recall')}"
    )

    # 重点 clip：baseline vs best
    squat_watch = []
    for watch in SQUAT_WATCH:
        ctx = next((c for c in prepared if c["upload_file"] == watch), None)
        if not ctx:
            continue
        for row in (next(r for r in results if r["name"] == "baseline_lower_only"), best_row):
            clips = clip_cache[row["name"]]
            clip_res = next(c for c in clips if c.get("upload_file") == watch or watch in str(c.get("record_id", "")))
            if not clip_res:
                idx = prepared.index(ctx)
                clip_res = clips[idx]
            squat_watch.append({
                "upload_file": watch,
                "label": row["label"],
                "missed_segments": (clip_res.get("diagnostics") or {}).get("missed_segments") or [],
            })

    base_missed = int(baseline_lower_only.get("missed") or 0)
    best_missed = int(best_row["summary"].get("missed") or 0)
    best_fp = int(best_row["summary"].get("false_alarms") or 0)
    base_fp = int(baseline_lower_only.get("false_alarms") or 0)
    if best_missed < base_missed:
        conclusion = (
            f"手腕/上肢豁免有效：漏报 {base_missed}→{best_missed}，"
            f"FP {base_fp}→{best_fp}。取货停留时上肢低速可作为下肢高速的豁免条件。"
        )
    elif best_fp < base_fp and best_missed == base_missed:
        conclusion = (
            f"漏报不变，FP 略降（{base_fp}→{best_fp}），对下蹲漏报帮助有限。"
        )
    else:
        conclusion = (
            f"上肢豁免未能减少漏报（{base_missed} 段），且 FP {base_fp}→{best_fp}。"
            "标真帧上肢速度在蹲起阶段未必持续低于误报，需结合水平速度或其他条件。"
        )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "prepared_count": len(prepared),
        "params": {
            "pose_frame_interval": POSE_FRAME_INTERVAL,
            "alarm_min_consecutive_frames": ALARM_MIN,
            "alarm_cooldown_frames": ALARM_COOLDOWN,
            "lower_threshold": LOWER_THRESHOLD,
        },
        "limb_discrimination": limb_discrimination,
        "local_baseline": local_baseline,
        "baseline_lower_only": baseline_lower_only,
        "results": results,
        "recommendation": recommendation,
        "squat_watch": squat_watch,
        "conclusion": conclusion,
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_MD.write_text(_build_md(report), encoding="utf-8")

    print(f"\n{recommendation}")
    print(f"结论: {conclusion}")
    print(f"MD: {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
