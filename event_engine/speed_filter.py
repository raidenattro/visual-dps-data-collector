"""段级速度上限过滤（纯速度，不含 Combo1）。"""

from __future__ import annotations

from typing import Any

from event_engine.box_identity import canonical_box_token
from event_engine.skeleton_features import (
    assign_person_tracks_to_frames,
    enrich_collision_segments_with_motion,
    extract_collision_segment_rows,
    extract_subsampled_velocity_from_frames,
)
from pose_store import RecordLocator, load_all_frames, load_manifest

from api.inference_eval_service import extract_picking_alarms_from_frames
from api.wrist_features_service import _infer_size_from_frames, _load_boxes_for_wrist_features, _video_fps

# 保守过滤默认参数（rule-baseline-prod-test 28 条验证）
DEFAULT_SPEED_FEATURE = "lower_mean_speed_p50"
DEFAULT_SPEED_THRESHOLD = 60.0


def export_frame_indices(upload_frames: list[dict[str, Any]]) -> set[int]:
    """从上传 clip JSON 提取导出抽帧 frame_idx 集合。"""
    return {int(fr.get("frame_idx") or 0) for fr in upload_frames if int(fr.get("frame_idx") or 0) > 0}


def segments_for_alarm(
    fi: int,
    token: str,
    segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """告警帧匹配同 box_token 且 frame 落在段内的碰撞段。"""
    out: list[dict[str, Any]] = []
    want = canonical_box_token(str(token or "").strip())
    for seg in segments:
        tok = canonical_box_token(str(seg.get("box_token") or ""))
        if tok != want:
            continue
        a = int(seg.get("frame_enter") or 0)
        b = int(seg.get("frame_exit") or 0)
        if a <= fi <= b:
            out.append(seg)
    return out


def segment_speed_pass(
    seg: dict[str, Any],
    *,
    feature: str,
    max_threshold: float,
) -> bool:
    """段级速度不超过阈值则保留（低速度=停下拣货，高速度=走过误报）。"""
    val = seg.get(feature)
    if val is None:
        return True
    try:
        return float(val) <= float(max_threshold)
    except (TypeError, ValueError):
        return True


def filter_alarms_by_speed_only(
    alarms: list[tuple[int, str]],
    segments: list[dict[str, Any]],
    *,
    feature: str,
    max_threshold: float,
) -> tuple[list[tuple[int, str]], list[dict[str, Any]]]:
    """仅用段级速度上限过滤告警列表。"""
    kept: list[tuple[int, str]] = []
    dropped: list[dict[str, Any]] = []
    for fi, token in alarms:
        cands = segments_for_alarm(fi, token, segments)
        if not cands:
            kept.append((fi, token))
            continue
        if any(segment_speed_pass(s, feature=feature, max_threshold=max_threshold) for s in cands):
            kept.append((fi, token))
        else:
            dropped.append({
                "frame_idx": fi,
                "box_token": token,
                "feature": feature,
                "max_threshold": max_threshold,
                "candidate_segments": len(cands),
            })
    return kept, dropped


def build_motion_segments_for_upload(
    locator: RecordLocator,
    upload_frames: list[dict[str, Any]],
    *,
    max_gap_frames: int = 1,
) -> list[dict[str, Any]]:
    """基于上传抽帧与本地 pose，构建带运动统计的碰撞段列表。"""
    export_indices = export_frame_indices(upload_frames)
    if not export_indices:
        return []

    manifest = load_manifest(locator)
    all_frames = load_all_frames(locator)
    if not all_frames:
        return []

    infer_w, infer_h = _infer_size_from_frames(all_frames, manifest)
    fps = _video_fps(manifest)
    boxes, _, _ = _load_boxes_for_wrist_features(
        locator, manifest, infer_w=infer_w, infer_h=infer_h
    )
    if not boxes:
        return []

    velocity_rows = extract_subsampled_velocity_from_frames(
        all_frames,
        export_indices,
        infer_width=infer_w,
        infer_height=infer_h,
        video_fps=fps,
    )
    subsampled_frames = [
        fr for fr in sorted(all_frames, key=lambda f: int(f.get("frame_idx") or 0))
        if isinstance(fr, dict) and int(fr.get("frame_idx") or 0) in export_indices
    ]
    tracked = assign_person_tracks_to_frames(subsampled_frames, video_fps=fps)
    collision_segments = extract_collision_segment_rows(tracked, boxes, max_gap_frames=max_gap_frames)
    return enrich_collision_segments_with_motion(collision_segments, velocity_rows)


def _alarm_kept(fi: int, token: str, kept_set: set[tuple[int, str]]) -> bool:
    return (fi, canonical_box_token(str(token or "").strip())) in kept_set


def apply_speed_filter_to_upload_frames(
    upload_frames: list[dict[str, Any]],
    kept_alarms: list[tuple[int, str]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """按保留告警集合回写 clip JSON 帧（仅改 is_picking / rule_alarm_collisions）。"""
    kept_set = {
        (int(fi), canonical_box_token(str(tok or "").strip()))
        for fi, tok in kept_alarms
    }
    out: list[dict[str, Any]] = []
    stats = {
        "picking_before": 0,
        "picking_after": 0,
        "alarms_before": 0,
        "alarms_after": 0,
        "alarms_dropped": 0,
    }

    for fr in upload_frames:
        if not isinstance(fr, dict):
            continue
        row = dict(fr)
        fi = int(row.get("frame_idx") or 0)
        if not row.get("is_picking"):
            out.append(row)
            continue

        stats["picking_before"] += 1
        alarm_raw = list(row.get("rule_alarm_collisions") or [])
        if alarm_raw:
            kept_raw: list[str] = []
            for raw in alarm_raw:
                canon = canonical_box_token(str(raw).strip())
                stats["alarms_before"] += 1
                if _alarm_kept(fi, canon, kept_set):
                    kept_raw.append(str(raw))
                    stats["alarms_after"] += 1
                else:
                    stats["alarms_dropped"] += 1
            row["rule_alarm_collisions"] = kept_raw
            row["is_picking"] = bool(kept_raw)
        else:
            # 有 is_picking 但无货框 token
            if _alarm_kept(fi, "", kept_set):
                stats["alarms_after"] += 1
            else:
                row["is_picking"] = False
                stats["alarms_dropped"] += 1

        if row.get("is_picking"):
            stats["picking_after"] += 1
        out.append(row)

    return out, stats


def filter_upload_frames_by_speed(
    upload_frames: list[dict[str, Any]],
    motion_segments: list[dict[str, Any]],
    *,
    feature: str = DEFAULT_SPEED_FEATURE,
    max_threshold: float = DEFAULT_SPEED_THRESHOLD,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """对上传 clip 帧应用速度过滤，返回新帧列表与统计。"""
    baseline_alarms = extract_picking_alarms_from_frames(upload_frames)
    kept, dropped = filter_alarms_by_speed_only(
        baseline_alarms,
        motion_segments,
        feature=feature,
        max_threshold=max_threshold,
    )
    filtered_frames, stats = apply_speed_filter_to_upload_frames(upload_frames, kept)
    meta = {
        "feature": feature,
        "max_threshold": max_threshold,
        "alarms_baseline": len(baseline_alarms),
        "alarms_kept": len(kept),
        "alarms_dropped_count": len(dropped),
        "dropped_samples": dropped[:20],
        **stats,
    }
    return filtered_frames, meta
