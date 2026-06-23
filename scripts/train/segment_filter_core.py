"""段特征过滤：记录加载与系统级评估（无框空间特征）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config_loader import parse_record_path_segments, resolve_app_paths
from event_engine.annotation_boxes import load_scaled_boxes
from pose_store import load_all_frames, load_event_review, load_manifest

from api.accuracy_service import build_ground_truth_segments, evaluate_segments, resolve_annotation_for_accuracy_record
from api.record_service import locate_record_by_id
from scripts.data.evaluate_combo1_segment_filter import (
    ComboRule,
    _build_segments,
    _false_alarm_by_frame_from_alarms,
    _infer_size_from_frames,
    _simulate_alarms,
    filter_alarms_by_combo,
)

DEFAULT_TIER = "rtmpose-m"


@dataclass
class RecordBundle:
    record_id: str
    clip: str
    camera_slug: str
    gt_segments: list[Any]
    segments: list[dict[str, Any]]
    raw_alarms: list[tuple[int, str]]


def rule_from_tuple(
    min_frames: int,
    min_duration: float,
    max_disp_per_frame: float,
    max_displacement: float | None = None,
) -> ComboRule:
    return ComboRule(
        combo_id=0,
        min_frames=int(min_frames),
        min_duration=float(min_duration),
        max_disp_per_frame=float(max_disp_per_frame),
        max_displacement=max_displacement,
    )


def combo1_rule() -> ComboRule:
    return rule_from_tuple(4, 0.20, 2.5, None)


def load_record_bundle(
    record_id: str,
    *,
    alarm_min: int = 5,
    alarm_cooldown: int = 6,
    tier: str = DEFAULT_TIER,
) -> RecordBundle | None:
    paths = resolve_app_paths()
    locator = locate_record_by_id(record_id)
    if not locator:
        return None

    review = load_event_review(locator)
    verified = review.get("verified_true") if isinstance(review.get("verified_true"), list) else []
    gt_segments = build_ground_truth_segments([e for e in verified if isinstance(e, dict)])
    if not gt_segments:
        return None

    manifest = load_manifest(locator)
    frames = load_all_frames(locator)
    if not frames:
        return None

    ann_path = resolve_annotation_for_accuracy_record(paths, locator, pose_tier=tier)
    if not ann_path or not ann_path.is_file():
        return None

    infer_w, infer_h = _infer_size_from_frames(frames, manifest)
    boxes = load_scaled_boxes(ann_path, infer_w, infer_h)
    if not boxes:
        return None

    fps = float(manifest.get("fps") or 15.0)
    if fps <= 0:
        fps = 15.0

    segments = _build_segments(frames, boxes, fps=fps)
    raw_alarms = _simulate_alarms(
        frames, boxes, alarm_min=alarm_min, alarm_cooldown=alarm_cooldown, fps=fps
    )
    _, slug, _ = parse_record_path_segments(record_id)
    return RecordBundle(
        record_id=record_id,
        clip=locator.path.name,
        camera_slug=slug,
        gt_segments=gt_segments,
        segments=segments,
        raw_alarms=raw_alarms,
    )


def evaluate_bundle(
    bundle: RecordBundle,
    rule: ComboRule | None,
    *,
    apply_filter: bool,
) -> dict[str, Any]:
    alarms = bundle.raw_alarms
    if apply_filter and rule is not None:
        alarms, _ = filter_alarms_by_combo(bundle.raw_alarms, bundle.segments, rule)
    metrics = evaluate_segments(bundle.gt_segments, alarms)
    detected = int(metrics["detected"])
    false_alarms = int(metrics["false_alarms"])
    denom = detected + false_alarms
    return {
        "record_id": bundle.record_id,
        "clip": bundle.clip,
        "camera_slug": bundle.camera_slug,
        "gt_segments": int(metrics["gt_segments"]),
        "detected": detected,
        "missed": int(metrics["missed"]),
        "false_alarms": false_alarms,
        "recall": float(metrics["recall"]),
        "miss_rate": float(metrics["miss_rate"]),
        "alarm_count": len(alarms),
        "raw_alarm_count": len(bundle.raw_alarms),
        "precision_proxy": round(detected / denom, 4) if denom else None,
    }


def aggregate_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {}
    g = sum(int(r["gt_segments"]) for r in rows)
    d = sum(int(r["detected"]) for r in rows)
    missed = sum(int(r["missed"]) for r in rows)
    fa = sum(int(r["false_alarms"]) for r in rows)
    alarms = sum(int(r["alarm_count"]) for r in rows)
    denom = d + fa
    return {
        "records": len(rows),
        "gt_segments": g,
        "detected": d,
        "missed": missed,
        "false_alarms": fa,
        "alarm_count": alarms,
        "recall": round(d / g, 4) if g else None,
        "miss_rate": round(missed / g, 4) if g else None,
        "precision_proxy": round(d / denom, 4) if denom else None,
    }


def rule_to_dict(rule: ComboRule) -> dict[str, Any]:
    return {
        "min_frames": rule.min_frames,
        "min_duration": rule.min_duration,
        "max_disp_per_frame": rule.max_disp_per_frame,
        "max_displacement": rule.max_displacement,
    }


def rule_label(rule: ComboRule) -> str:
    parts = [
        f"fc≥{rule.min_frames}",
        f"dur≥{rule.min_duration}",
        f"dpf≤{rule.max_disp_per_frame}",
    ]
    if rule.max_displacement is not None:
        parts.append(f"disp≤{rule.max_displacement}")
    return ", ".join(parts)
