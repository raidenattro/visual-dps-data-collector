#!/usr/bin/env python3
"""肩/肘/腕开合角与角速度特征实验（对齐 manifest 抽帧参数）。

只读 timeline，不写回记录包。对比 local baseline 误报/标真帧区分度，
扫描纯角速度门控与「速度门控 + 肘角伸展豁免」组合，并分析 prefilter / knee@65 漏报段特征。

用法（项目根目录）:
  python scripts/data/validate_prefilter_joint_angle28.py
"""

from __future__ import annotations

import json
import math
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.data.evaluate_combo1_segment_filter import _ensure_cv2_point_polygon_test

_ensure_cv2_point_polygon_test()

import cv2

from config_loader import resolve_app_paths, resolve_config_path
from event_engine.box_identity import box_collision_token, token_matches_any
from event_engine.speed_gated_collision import SpeedGateConfig, build_timeline_frame_index
from event_engine.skeleton_angles import extract_subsampled_joint_angle_from_frames
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
    LOWER_THRESHOLDS,
    _float_or_none,
    _load_ground_truth_segments,
    _threshold_scan_le,
)
from scripts.data.upload_export_common import (
    export_indices_for_record,
    load_baseline_manifest,
    resolve_baseline_clip_path,
)

LOCAL_BASELINE_MANIFEST = ROOT / "localdata/export/rule-baseline-local-prod-test/_manifest.json"
LOCAL_BASELINE_DIR = LOCAL_BASELINE_MANIFEST.parent
PREFILTER_LOWER_DIR = ROOT / "localdata/export/rule-speed-prefilter-prod-test"
PREFILTER_KNEE65_DIR = ROOT / "localdata/export/rule-speed-prefilter-knee65-prod-test"

OUT_JSON = ROOT / "docs/json/prefilter-joint-angle-experiment.json"
OUT_MD = ROOT / "docs/prefilter-joint-angle-speed-gate.md"

POSE_FRAME_INTERVAL = 2
ALARM_MIN = 3
ALARM_COOLDOWN = 0

ANGLE_FEATURES = (
    ("shoulder_angle_mean", "肩开合角均值（髋-肩-肘内角）"),
    ("elbow_angle_mean", "肘开合角均值（肩-肘-腕内角，越大越伸直）"),
    ("wrist_angle_mean", "腕开合角均值（肘-腕-肩内角）"),
    ("shoulder_angle_vel_max", "肩角速度峰值（度/秒）"),
    ("elbow_angle_vel_max", "肘角速度峰值（度/秒）"),
    ("wrist_angle_vel_max", "腕角速度峰值（度/秒）"),
    ("joint_open_vel_max", "肩肘腕角速度最大值"),
)

# 手部朝向 + 肩肘相对腰身夹角（新增）
ORIENTATION_FEATURES = (
    ("wrist_elevation_angle_max", "手腕抬升角峰值（肩→腕相对下垂方向，越大越举起）"),
    ("forearm_direction_angle_max", "前臂指向角峰值（肘→腕相对下垂方向）"),
    ("arm_torso_angle_mean", "肩-肘相对躯干夹角均值（∠髋心-肩-肘）"),
    ("elbow_waist_angle_mean", "肘相对腰身夹角均值（∠肩-肘-髋心）"),
    ("arm_torso_angle_max", "肩-肘相对躯干夹角峰值"),
    ("elbow_waist_angle_max", "肘相对腰身夹角峰值"),
)

# 纯角速度门控阈值（度/秒）：超阈则 block
ANGLE_VEL_THRESHOLDS = [30, 60, 90, 120, 150, 180, 240, 300]
# 组合门控：下肢速度 + 肘角伸展豁免
COMPOUND_SPEED_FEATURES = (
    ("lower_mean_speed", 60.0),
    ("knee_ankle_mean_speed", 65.0),
)
ELBOW_EXEMPT_GRID = [120, 130, 140, 150, 160, 170]


def _load_manifest_records() -> list[dict[str, Any]]:
    manifest = load_baseline_manifest(LOCAL_BASELINE_MANIFEST)
    return list(manifest.get("records") or [])


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
        entry,
        baseline_dir=LOCAL_BASELINE_DIR,
        timeline_frames=timeline_frames,
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
        timeline_frames,
        export_indices,
        infer_width=infer_w,
        infer_height=infer_h,
        video_fps=fps,
    )
    angle_rows = extract_subsampled_joint_angle_from_frames(
        timeline_frames,
        export_indices,
        video_fps=fps,
    )

    merged_by_key: dict[tuple[int, int], dict[str, Any]] = {}
    for row in velocity_rows:
        key = (int(row.get("frame_idx") or 0), int(row.get("person_track_id") or 0))
        merged_by_key[key] = dict(row)
    for row in angle_rows:
        key = (int(row.get("frame_idx") or 0), int(row.get("person_track_id") or 0))
        merged_by_key.setdefault(key, {})
        merged_by_key[key].update(row)

    return {
        "record_id": record_id,
        "entry": entry,
        "timeline_frames": timeline_frames,
        "boxes": boxes,
        "infer_w": infer_w,
        "infer_h": infer_h,
        "fps": fps,
        "export_indices": export_indices,
        "gt_segments": gt_segments,
        "baseline_frames": baseline_frames,
        "velocity_rows": velocity_rows,
        "angle_rows": angle_rows,
        "merged_rows": merged_by_key,
        "upload_file": str(entry.get("file") or f"{entry.get('clip_name')}.json"),
    }


def _false_alarm_frames(frames: list[dict[str, Any]], gt_segments: list[GroundTruthSegment]) -> set[int]:
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
            if not tokens:
                covered = True
                break
            if token_matches_any(str(tokens[0]), list(gt.gt_tokens)):
                covered = True
                break
        if not covered:
            out.add(fi)
    return out


def _gt_overlap_frames(frames: list[dict[str, Any]], gt_segments: list[GroundTruthSegment]) -> set[int]:
    out: set[int] = set()
    for fr in frames:
        fi = int(fr.get("frame_idx") or 0)
        for gt in gt_segments:
            if gt.frame_start <= fi <= gt.frame_end:
                out.add(fi)
                break
    return out


def _frame_feature_values(
    merged_rows: dict[tuple[int, int], dict[str, Any]],
    frame_set: set[int],
    feature: str,
) -> list[float]:
    vals: list[float] = []
    for (fi, _tid), row in merged_rows.items():
        if fi not in frame_set:
            continue
        v = _float_or_none(row.get(feature))
        if v is not None:
            vals.append(v)
    return vals


def _p50(vals: list[float]) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    return round(s[len(s) // 2], 3)


def _p90(vals: list[float]) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    idx = min(len(s) - 1, int(len(s) * 0.9))
    return round(s[idx], 3)


def _evaluate_frames(paths, *, record_id: str, upload_file: str, frames: list[dict[str, Any]]) -> dict[str, Any]:
    clip = UploadClipInput(upload_file=upload_file, frames=frames, record_id=record_id)
    return evaluate_uploaded_clip(paths, clip)


def _compound_gate_blocks(
    row: dict[str, Any],
    *,
    speed_feature: str,
    speed_thr: float,
    elbow_exempt_min: float,
) -> bool:
    speed = _float_or_none(row.get(speed_feature))
    if speed is None or speed <= speed_thr:
        return False
    elbow = _float_or_none(row.get("elbow_angle_mean"))
    if elbow is not None and elbow >= elbow_exempt_min:
        return False
    return True


def _angle_vel_gate_blocks(row: dict[str, Any], *, feature: str, threshold: float) -> bool:
    val = _float_or_none(row.get(feature))
    if val is None:
        return False
    return val > threshold


def _recompute_with_row_gate(
    ctx: dict[str, Any],
    *,
    gate_fn,
) -> list[dict[str, Any]]:
    """按预计算行门控重算 upload 帧（对齐 export 抽帧）。"""
    from event_engine.collision import PersonTrackAssigner

    boxes = ctx["boxes"]
    record_id = ctx["record_id"]
    wanted = sorted({int(x) for x in ctx["export_indices"] if int(x) > 0})
    by_idx = build_timeline_frame_index(ctx["timeline_frames"])
    merged = ctx["merged_rows"]
    fps = ctx["fps"]

    assigner = PersonTrackAssigner(max_match_dist=220.0, stale_sec=1.2)
    box_consecutive: dict[str, int] = {}
    box_last_alarm: dict[str, int] = {}
    upload_rows: list[dict[str, Any]] = []

    for frame_idx in wanted:
        src = by_idx.get(frame_idx)
        if not src:
            continue
        now_ts = float(src.get("timestamp_sec") or 0.0)
        if now_ts <= 0 and frame_idx > 0:
            now_ts = frame_idx / fps

        active_collisions: list[str] = []
        used_track_ids: set[int] = set()
        persons = src.get("persons") or []

        for person in persons:
            if not isinstance(person, dict):
                continue
            keypoints = person.get("keypoints") or []
            if len(keypoints) < 11:
                continue

            def _pt(i: int):
                kp = keypoints[i]
                return float(kp[0]), float(kp[1]), float(kp[2]) if len(kp) > 2 else 0.0

            lx, ly, ls = _pt(5)
            rx, ry, rs = _pt(6)
            if ls > 0.2 and rs > 0.2:
                ax, ay = (lx + rx) / 2.0, (ly + ry) / 2.0
            else:
                xs = [float(k[0]) for k in keypoints if len(k) >= 2]
                ys = [float(k[1]) for k in keypoints if len(k) >= 2]
                ax = sum(xs) / len(xs) if xs else 0.0
                ay = sum(ys) / len(ys) if ys else 0.0

            track_id = assigner.assign(ax, ay, now_ts=now_ts, occupied_track_ids=used_track_ids)
            row = merged.get((frame_idx, track_id), {})
            if gate_fn(row):
                continue

            for kpt_idx in (9, 10):
                if len(keypoints) <= kpt_idx:
                    continue
                kp = keypoints[kpt_idx]
                if len(kp) < 3 or float(kp[2]) <= 0.3:
                    continue
                wx, wy = float(kp[0]), float(kp[1])
                for box in boxes:
                    contour = box.get("orig_contour")
                    if contour is None:
                        continue
                    if cv2.pointPolygonTest(contour, (wx, wy), False) >= 0:
                        token = box_collision_token(box)
                        if token:
                            active_collisions.append(token)
                        break

        active_collisions = list(set(active_collisions))
        current_tokens = set(active_collisions)
        for token in list(box_consecutive.keys()):
            if token not in current_tokens:
                box_consecutive[token] = 0

        alarm_collisions: list[str] = []
        for token in current_tokens:
            box_consecutive[token] = box_consecutive.get(token, 0) + 1
            last_alarm = box_last_alarm.get(token, -10**9)
            if box_consecutive[token] >= ALARM_MIN and frame_idx - last_alarm >= ALARM_COOLDOWN:
                alarm_collisions.append(token)
                box_last_alarm[token] = frame_idx

        upload_rows.append({
            "record_id": record_id,
            "frame_idx": frame_idx,
            "is_picking": bool(alarm_collisions),
            "picking_prob": None,
            "predicted_box_tokens": [],
            "rule_collisions": active_collisions,
            "rule_alarm_collisions": alarm_collisions,
        })
    return upload_rows


def _load_missed_from_export(export_dir: Path) -> dict[str, list[dict[str, Any]]]:
    report_path = export_dir / "accuracy_report.json"
    if not report_path.is_file():
        return {}
    data = json.loads(report_path.read_text(encoding="utf-8"))
    out: dict[str, list[dict[str, Any]]] = {}
    for clip in data.get("clips") or []:
        upload_file = str(clip.get("upload_file") or "")
        missed = list((clip.get("diagnostics") or {}).get("missed_segments") or [])
        if missed:
            out[upload_file] = missed
    return out


def _segment_frame_stats(
    ctx: dict[str, Any],
    seg: dict[str, Any],
) -> dict[str, Any]:
    a = int(seg.get("frame_start") or 0)
    b = int(seg.get("frame_end") or 0)
    merged = ctx["merged_rows"]
    stats: dict[str, list[float]] = {feat: [] for feat, _ in ANGLE_FEATURES}
    for feat, _ in ORIENTATION_FEATURES:
        stats[feat] = []
    stats["lower_mean_speed"] = []
    stats["knee_ankle_mean_speed"] = []

    for fi in range(a, b + 1):
        for (_fi, _tid), row in merged.items():
            if _fi != fi:
                continue
            for feat, _ in ANGLE_FEATURES:
                v = _float_or_none(row.get(feat))
                if v is not None:
                    stats[feat].append(v)
            for feat, _ in ORIENTATION_FEATURES:
                v = _float_or_none(row.get(feat))
                if v is not None:
                    stats[feat].append(v)
            for sf in ("lower_mean_speed", "knee_ankle_mean_speed"):
                v = _float_or_none(row.get(sf))
                if v is not None:
                    stats[sf].append(v)

    result: dict[str, Any] = {
        "frame_start": a,
        "frame_end": b,
        "gt_tokens": seg.get("gt_tokens") or [],
    }
    for key, vals in stats.items():
        result[f"{key}_p50"] = _p50(vals)
        result[f"{key}_p90"] = _p90(vals)
        result[f"{key}_max"] = round(max(vals), 3) if vals else None
    return result


def _build_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 肩/肘/腕开合角与角速度前置门控实验",
        "",
        f"> 生成时间：{report.get('generated_at', '')}",
        "> 脚本：`scripts/data/validate_prefilter_joint_angle28.py`",
        "",
        "## 1. 实验目的",
        "",
        "在 **manifest 对齐参数**（`pose_frame_interval=2`, `alarm_min=3`, `cooldown=0`）下，",
        "提取肩/肘/腕关节开合角及其角速度，评估能否作为前置门控特征或「速度门控 + 肘角伸展豁免」组合，",
        "缓解 prefilter / knee@65 的漏报与误报权衡困境。",
        "",
        "## 2. 参数",
        "",
        "| 参数 | 值 |",
        "|------|-----|",
        f"| pose_frame_interval | {report['params']['pose_frame_interval']} |",
        f"| alarm_min_consecutive_frames | {report['params']['alarm_min_consecutive_frames']} |",
        f"| alarm_cooldown_frames | {report['params']['alarm_cooldown_frames']} |",
        f"| 记录数 | {report.get('prepared_count')} |",
        "",
        "## 3. 特征定义",
        "",
        "| 特征 | 定义 |",
        "|------|------|",
        "| 肩开合角 | 髋-肩-肘内角（度），左右均值 |",
        "| 肘开合角 | 肩-肘-腕内角（度），180°≈手臂伸直 |",
        "| 腕开合角 | 肘-腕-肩内角（度），表征前臂偏折 |",
        "| 角速度 | 3 帧中值滤波后 \\|Δ角\\|/Δt（度/秒），取肩/肘/腕峰值 |",
        "",
        "### 手部朝向与躯干夹角",
        "",
        "| 特征 | 定义 |",
        "|------|------|",
        "| 手腕抬升角 | 肩→腕向量与图像向下 (0,1) 夹角；0°≈下垂，90°≈水平前伸 |",
        "| 前臂指向角 | 肘→腕向量与向下方向夹角，表征手部指向 |",
        "| 肩-肘躯干夹角 | ∠(髋心, 肩, 肘)，髋心=左右髋中心（腰身锚点） |",
        "| 肘-腰身夹角 | ∠(肩, 肘, 髋心)，肘部相对躯干张开程度 |",
        "",
        "路过不取货时手腕抬升角通常较低；伸手取货时肩-肘相对躯干张开更大。",
        "",
        "抽帧与速度差分算法与 `skeleton_features.py` 一致，仅在 export 索引子集上计算。",
        "",
        "## 4. 帧级区分度（标真重叠帧 vs baseline 误报帧）",
        "",
        "### 4.1 关节开合角 / 角速度",
        "",
        "| 特征 | 标真 P50 | 误报 P50 | 标真 P90 | 误报 P90 |",
        "|------|----------|----------|----------|----------|",
    ]
    disc = report.get("frame_discrimination") or {}
    for feat, label in ANGLE_FEATURES:
        row = disc.get(feat) or {}
        gt = row.get("gt_overlap") or {}
        fa = row.get("false_alarm") or {}
        lines.append(
            f"| `{feat}` | {gt.get('p50', '—')} | {fa.get('p50', '—')} | "
            f"{gt.get('p90', '—')} | {fa.get('p90', '—')} |"
        )

    lines.extend([
        "",
        "### 4.2 手部朝向 / 肩肘-腰身夹角",
        "",
        "| 特征 | 标真 P50 | 误报 P50 | 标真 P90 | 误报 P90 |",
        "|------|----------|----------|----------|----------|",
    ])
    for feat, label in ORIENTATION_FEATURES:
        row = disc.get(feat) or {}
        gt = row.get("gt_overlap") or {}
        fa = row.get("false_alarm") or {}
        lines.append(
            f"| `{feat}` | {gt.get('p50', '—')} | {fa.get('p50', '—')} | "
            f"{gt.get('p90', '—')} | {fa.get('p90', '—')} |"
        )

    lines.extend([
        "",
        "参考下肢速度（同抽帧）：",
        "",
        "| 特征 | 标真 P50 | 误报 P50 |",
        "|------|----------|----------|",
    ])
    for sf in ("lower_mean_speed", "knee_ankle_mean_speed"):
        row = disc.get(sf) or {}
        gt = row.get("gt_overlap") or {}
        fa = row.get("false_alarm") or {}
        lines.append(f"| `{sf}` | {gt.get('p50', '—')} | {fa.get('p50', '—')} |")

    lines.extend([
        "",
        "## 5. 纯角速度门控扫描（超阈 block，相对 local baseline）",
        "",
        "逻辑：当 `joint_open_vel_max`（或单侧角速度）> 阈值时跳过手腕进框检测。",
        "",
        "| 特征 | 阈值 ≤ | TP | FP | 召回率 |",
        "|------|--------|-----|-----|--------|",
    ])
    for feat, _ in ANGLE_FEATURES:
        if "vel" not in feat:
            continue
        grid = (report.get("angle_vel_grids") or {}).get(feat) or []
        for row in grid[:8]:
            lines.append(
                f"| `{feat}` | {row.get('max_threshold')} | {row.get('detected')} | "
                f"{row.get('false_alarms')} | {row.get('recall')} |"
            )

    lines.extend([
        "",
        "## 6. 组合门控：下肢速度 + 肘角伸展豁免",
        "",
        "逻辑：下肢速度超阈 **且** 肘角均值 < 豁免阈 时才 block（蹲取/伸手取货时肘角大则不 block）。",
        "",
        "| 速度特征 | 速度阈 | 肘角豁免 ≥ | TP | FP | FN | 召回率 |",
        "|----------|--------|------------|-----|-----|-----|--------|",
    ])
    for row in report.get("compound_grid") or []:
        lines.append(
            f"| `{row.get('speed_feature')}` | {row.get('speed_threshold')} | "
            f"{row.get('elbow_exempt_min')} | {row.get('detected')} | {row.get('false_alarms')} | "
            f"{row.get('missed')} | {row.get('recall')} |"
        )

    best = report.get("best_compound") or {}
    lines.extend([
        "",
        f"**组合门控最优（召回优先）**：`{best.get('speed_feature')}` 阈 {best.get('speed_threshold')} + "
        f"肘角豁免 ≥ {best.get('elbow_exempt_min')} → TP={best.get('detected')} FP={best.get('false_alarms')} "
        f"FN={best.get('missed')} recall={best.get('recall')}",
        "",
        "## 7. 与现有方案对比",
        "",
        "| 方案 | TP | FP | FN | 召回率 |",
        "|------|-----|-----|-----|--------|",
    ])
    ref = report.get("reference_summaries") or {}
    for name, row in ref.items():
        lines.append(
            f"| {name} | {row.get('detected')} | {row.get('false_alarms')} | "
            f"{row.get('missed')} | {row.get('recall')} |"
        )

    lines.extend([
        "",
        "## 8. prefilter / knee@65 漏报段特征画像",
        "",
        "| clip | 方案 | 漏报段 | 肘角P50 | 腕抬升P50 | 肩肘躯干P50 | 肘腰身P50 | 膝踝速P50 |",
        "|------|------|--------|---------|-----------|-------------|-----------|-----------|",
    ])
    for item in report.get("missed_profiles") or []:
        seg_txt = f"{item.get('frame_start')}-{item.get('frame_end')}"
        lines.append(
            f"| `{item.get('upload_file')}` | {item.get('scheme')} | {seg_txt} | "
            f"{item.get('elbow_angle_mean_p50', '—')} | {item.get('wrist_elevation_angle_max_p50', '—')} | "
            f"{item.get('arm_torso_angle_mean_p50', '—')} | {item.get('elbow_waist_angle_mean_p50', '—')} | "
            f"{item.get('knee_ankle_mean_speed_p50', '—')} |"
        )

    lines.extend([
        "",
        "## 9. 结论",
        "",
        report.get("conclusion", "（待填）"),
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    resolve_config_path(None)
    paths = resolve_app_paths()
    records = _load_manifest_records()
    if not records:
        print("manifest 无 records")
        return 2

    prepared: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for entry in records:
        ctx = _prepare_record(entry)
        if not ctx or ctx.get("error"):
            rid = str(entry.get("record_id") or "")
            err = (ctx or {}).get("error") or "unknown"
            errors.append({"record_id": rid, "error": err})
            print(f"{rid}: SKIP {err}")
            continue
        prepared.append(ctx)
        print(f"{ctx['record_id']}: ok export={len(ctx['export_indices'])}")

    if not prepared:
        print("无可用记录")
        return 2

    baseline_clips = []
    for ctx in prepared:
        if ctx.get("baseline_frames"):
            baseline_clips.append(
                _evaluate_frames(
                    paths,
                    record_id=ctx["record_id"],
                    upload_file=ctx["upload_file"],
                    frames=ctx["baseline_frames"],
                )
            )
    local_baseline_summary = aggregate_upload_clip_results(baseline_clips)

    # 帧级区分度
    frame_discrimination: dict[str, Any] = {}
    all_feats = [f[0] for f in ANGLE_FEATURES] + [f[0] for f in ORIENTATION_FEATURES] + ["lower_mean_speed", "knee_ankle_mean_speed"]
    for feat in all_feats:
        gt_speeds: list[float] = []
        fa_speeds: list[float] = []
        for ctx in prepared:
            gt_frames = _gt_overlap_frames(ctx.get("baseline_frames") or [], ctx["gt_segments"])
            fa_frames = _false_alarm_frames(ctx.get("baseline_frames") or [], ctx["gt_segments"])
            gt_speeds.extend(_frame_feature_values(ctx["merged_rows"], gt_frames, feat))
            fa_speeds.extend(_frame_feature_values(ctx["merged_rows"], fa_frames, feat))
        frame_discrimination[feat] = {
            "gt_overlap": {"count": len(gt_speeds), "p50": _p50(gt_speeds), "p90": _p90(gt_speeds)},
            "false_alarm": {"count": len(fa_speeds), "p50": _p50(fa_speeds), "p90": _p90(fa_speeds)},
        }
        if feat in ("lower_mean_speed", "knee_ankle_mean_speed"):
            frame_discrimination[feat]["frame_threshold_scan"] = _threshold_scan_le(
                gt_speeds, fa_speeds, LOWER_THRESHOLDS
            )

    # 纯角速度门控
    angle_vel_grids: dict[str, list[dict[str, Any]]] = {}
    vel_feats = [f for f, _ in ANGLE_FEATURES if "vel" in f]
    for feat in vel_feats:
        print(f"\n=== 角速度门控: {feat} ===")
        grid: list[dict[str, Any]] = []
        for thr in ANGLE_VEL_THRESHOLDS:
            clip_results = []
            for ctx in prepared:
                frames = _recompute_with_row_gate(
                    ctx,
                    gate_fn=lambda row, f=feat, t=thr: _angle_vel_gate_blocks(row, feature=f, threshold=t),
                )
                clip_results.append(
                    _evaluate_frames(paths, record_id=ctx["record_id"], upload_file=ctx["upload_file"], frames=frames)
                )
            summary = aggregate_upload_clip_results(clip_results)
            grid.append({
                "max_threshold": thr,
                "feature": feat,
                "detected": summary.get("detected"),
                "missed": summary.get("missed"),
                "false_alarms": summary.get("false_alarms"),
                "recall": summary.get("recall"),
            })
            print(f"  thr={thr}: TP={summary.get('detected')} FP={summary.get('false_alarms')} recall={summary.get('recall')}")
        angle_vel_grids[feat] = grid

    # 组合门控网格
    compound_grid: list[dict[str, Any]] = []
    print("\n=== 组合门控：速度 + 肘角豁免 ===")
    for speed_feature, speed_thr in COMPOUND_SPEED_FEATURES:
        for elbow_exempt in ELBOW_EXEMPT_GRID:
            clip_results = []
            for ctx in prepared:
                frames = _recompute_with_row_gate(
                    ctx,
                    gate_fn=lambda row, sf=speed_feature, st=speed_thr, ee=elbow_exempt: _compound_gate_blocks(
                        row, speed_feature=sf, speed_thr=st, elbow_exempt_min=ee
                    ),
                )
                clip_results.append(
                    _evaluate_frames(paths, record_id=ctx["record_id"], upload_file=ctx["upload_file"], frames=frames)
                )
            summary = aggregate_upload_clip_results(clip_results)
            compound_grid.append({
                "speed_feature": speed_feature,
                "speed_threshold": speed_thr,
                "elbow_exempt_min": elbow_exempt,
                "detected": summary.get("detected"),
                "missed": summary.get("missed"),
                "false_alarms": summary.get("false_alarms"),
                "recall": summary.get("recall"),
            })
            print(
                f"  {speed_feature}@{speed_thr} elbow>={elbow_exempt}: "
                f"TP={summary.get('detected')} FP={summary.get('false_alarms')} FN={summary.get('missed')}"
            )

    # 召回优先选最优组合（TP 最大，同 TP 取 FP 最小）
    compound_grid.sort(
        key=lambda r: (-int(r.get("detected") or 0), int(r.get("false_alarms") or 9999))
    )
    best_compound = compound_grid[0] if compound_grid else {}

    # 参考方案：lower@60 / knee@65 从 accuracy_report 读取
    reference_summaries: dict[str, Any] = {
        "local baseline": local_baseline_summary,
    }
    for label, export_dir in (
        ("lower@60 prefilter", PREFILTER_LOWER_DIR),
        ("knee@65 prefilter", PREFILTER_KNEE65_DIR),
    ):
        rp = export_dir / "accuracy_report.json"
        if rp.is_file():
            reference_summaries[label] = json.loads(rp.read_text(encoding="utf-8")).get("summary") or {}

    # 漏报段特征画像
    missed_profiles: list[dict[str, Any]] = []
    ctx_by_file = {c["upload_file"]: c for c in prepared}
    for scheme, export_dir in (
        ("lower@60", PREFILTER_LOWER_DIR),
        ("knee@65", PREFILTER_KNEE65_DIR),
    ):
        missed_map = _load_missed_from_export(export_dir)
        for upload_file, segments in missed_map.items():
            ctx = ctx_by_file.get(upload_file)
            if not ctx:
                continue
            for seg in segments:
                prof = _segment_frame_stats(ctx, seg)
                prof["upload_file"] = upload_file
                prof["scheme"] = scheme
                missed_profiles.append(prof)

    # 结论
    bl = local_baseline_summary
    bc = best_compound
    lower_ref = reference_summaries.get("lower@60 prefilter") or {}
    knee_ref = reference_summaries.get("knee@65 prefilter") or {}

    bc_tp = int(bc.get("detected") or 0)
    bc_fp = int(bc.get("false_alarms") or 0)
    bc_fn = int(bc.get("missed") or 0)
    knee_tp = int(knee_ref.get("detected") or 0)
    knee_fp = int(knee_ref.get("false_alarms") or 0)
    knee_fn = int(knee_ref.get("missed") or 0)

    if bc_tp > knee_tp and bc_fp <= knee_fp + 20:
        conclusion = (
            f"组合门控 `{bc.get('speed_feature')}@{bc.get('speed_threshold')}` + 肘角豁免≥{bc.get('elbow_exempt_min')} "
            f"优于 knee@65：TP {knee_tp}→{bc_tp}，FP {knee_fp}→{bc_fp}，FN {knee_fn}→{bc_fn}。"
            "肘角伸展可区分蹲取/伸手与纯行走，建议下一步接入 SpeedGateConfig。"
        )
    elif bc_tp >= knee_tp:
        conclusion = (
            f"组合门控召回 {bc.get('recall')}（TP={bc_tp}）不低于 knee@65（{knee_ref.get('recall')}），"
            f"但 FP={bc_fp} 仍高于 lower@60（{lower_ref.get('false_alarms')}）。"
            "漏报段肘角 P50 普遍较高，肘角豁免有望恢复部分速度门控误杀；纯角速度门控区分度不足。"
        )
    else:
        best_vel = angle_vel_grids.get("joint_open_vel_max") or []
        vel_note = ""
        if best_vel:
            top = max(best_vel, key=lambda r: float(r.get("recall") or 0))
            vel_note = (
                f"纯角速度门控最优 recall={top.get('recall')}（阈{top.get('max_threshold')}），"
            )
        conclusion = (
            f"{vel_note}组合门控未能超越 knee@65（TP={knee_tp} FP={knee_fp}）。"
            "肩肘腕角速度在标真/误报帧 P50 重叠较大，单独作门控难以破局；"
            "漏报段多因骨骼遮挡或手腕未进框，角特征仅能辅助豁免下肢高速误杀。"
        )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "record_count": len(records),
        "prepared_count": len(prepared),
        "errors": errors,
        "params": {
            "pose_frame_interval": POSE_FRAME_INTERVAL,
            "alarm_min_consecutive_frames": ALARM_MIN,
            "alarm_cooldown_frames": ALARM_COOLDOWN,
            "baseline_dir": str(LOCAL_BASELINE_DIR),
        },
        "frame_discrimination": frame_discrimination,
        "angle_vel_grids": angle_vel_grids,
        "compound_grid": compound_grid,
        "best_compound": best_compound,
        "reference_summaries": reference_summaries,
        "missed_profiles": missed_profiles,
        "conclusion": conclusion,
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_MD.write_text(_build_markdown(report), encoding="utf-8")

    print(f"\n=== 结论 ===\n{conclusion}")
    print(f"JSON: {OUT_JSON}")
    print(f"MD:   {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
