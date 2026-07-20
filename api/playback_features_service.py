"""回放按帧骨骼特征：与 export 同路径（export 抽帧 + export 帧号）。"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

from event_engine.export_frame_utils import (
    DEFAULT_EXPORT_POSE_FRAME_INTERVAL,
    default_baseline_export_dir,
    resolve_export_indices,
)
from event_engine.skeleton_angles import extract_subsampled_joint_angle_from_frames
from event_engine.skeleton_features import (
    extract_subsampled_velocity_from_frames,
    filter_frames_to_indices,
)
from event_engine.wrist_features import assign_person_tracks_to_frames
from pose_store import RecordLocator, load_all_frames, load_manifest

from api.wrist_features_service import (
    _infer_size_from_frames,
    _video_fps,
    load_wrist_features_payload,
)

# 回放侧栏展示字段（与实验/门控命名一致）
VELOCITY_KEYS = (
    "torso_speed",
    "body_mean_speed",
    "body_max_speed",
    "upper_mean_speed",
    "lower_mean_speed",
    "knee_ankle_mean_speed",
    "ankle_mean_speed",
    "ankle_max_speed",
    "ankle_max_speed_norm",
    "wrist_max_speed",
    "elbow_max_speed",
    "wrist_torso_ratio",
)

ANGLE_KEYS = (
    "arm_torso_angle_max",
    "arm_torso_angle_mean",
    "elbow_angle_mean",
    "elbow_angle_max",
    "elbow_angle_min",
    "wrist_elevation_angle_max",
    "wrist_elevation_angle_mean",
    "forearm_direction_angle_max",
    "forearm_direction_angle_mean",
    "elbow_waist_angle_max",
    "elbow_waist_angle_mean",
    "shoulder_angle_mean",
    "joint_open_vel_max",
    "elbow_angle_vel_max",
    "torso_leg_angle_mean",
    "torso_leg_angle_min",
    "torso_leg_angle_max",
    "center_torso_leg_angle",
    "left_torso_leg_angle",
    "right_torso_leg_angle",
    "knee_angle_mean",
    "knee_angle_min",
    "knee_angle_max",
    "left_knee_angle",
    "right_knee_angle",
    "leg_span_ratio",
    "hip_knee_ankle_vertical_ratio",
)

GATE_PREVIEW = {
    "speed_feature": "ankle_max_speed",
    "speed_threshold": 80.0,
    "angle_exempt": (
        ("arm_torso_angle_max", 90.0),
        ("elbow_angle_mean", 150.0),
        ("wrist_elevation_angle_max", 60.0),
    ),
    "stance_feature": "torso_leg_angle_mean",
    "stance_threshold": 160.0,
}

# 回放侧栏展示多组 stance（与 export 实验命名一致）
STANCE_PREVIEW_CONFIGS: tuple[tuple[str, str, float], ...] = (
    ("stance160", "torso_leg_angle_mean", 160.0),
    ("stance120", "knee_angle_mean", 120.0),
)

KPT_LEFT_WRIST = 9
KPT_RIGHT_WRIST = 10
WRIST_KPT_SCORE_MIN = 0.3


def _float_or_none(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not (f == f):
        return None
    return f


def _pick_keys(row: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in keys:
        out[k] = row.get(k)
    return out


def _kpt_score(person: dict[str, Any], idx: int) -> float | None:
    kpts = person.get("keypoints") or []
    if idx >= len(kpts):
        return None
    kp = kpts[idx]
    if not isinstance(kp, (list, tuple)) or len(kp) < 3:
        return None
    if kp[0] is None or kp[1] is None:
        return None
    return _float_or_none(kp[2])


def _wrist_confidence_for_person(person: dict[str, Any]) -> dict[str, Any]:
    left = _kpt_score(person, KPT_LEFT_WRIST)
    right = _kpt_score(person, KPT_RIGHT_WRIST)
    thr = WRIST_KPT_SCORE_MIN
    return {
        "left_wrist": left,
        "right_wrist": right,
        "score_min": thr,
        "left_valid": left is not None and left >= thr,
        "right_valid": right is not None and right >= thr,
    }


def _build_wrist_confidence_map(
    timeline_frames: list[dict[str, Any]],
    export_indices: set[int],
    *,
    video_fps: float,
) -> dict[tuple[int, int], dict[str, Any]]:
    """(frame_idx, person_track_id) -> wrist_confidence；track 与速度特征同源。"""
    subsampled = filter_frames_to_indices(timeline_frames, export_indices)
    if not subsampled:
        return {}
    tracked = assign_person_tracks_to_frames(subsampled, video_fps=video_fps)
    out: dict[tuple[int, int], dict[str, Any]] = {}
    for fr in tracked:
        if not isinstance(fr, dict):
            continue
        fi = int(fr.get("frame_idx") or 0)
        if fi <= 0:
            continue
        for person in fr.get("persons") or []:
            if not isinstance(person, dict):
                continue
            track_id = int(person.get("person_track_id") or 0)
            if track_id <= 0:
                continue
            out[(fi, track_id)] = _wrist_confidence_for_person(person)
    return out


def _is_standing(row: dict[str, Any], *, feature: str | None = None, threshold: float | None = None) -> bool:
    feat = feature or GATE_PREVIEW.get("stance_feature")
    thr = float(threshold if threshold is not None else (GATE_PREVIEW.get("stance_threshold") or 0))
    if not feat:
        return True
    v = _float_or_none(row.get(feat))
    if v is None:
        return True
    return v >= thr


def _stance_preview_item(
    row: dict[str, Any],
    *,
    label: str,
    feature: str,
    threshold: float,
) -> dict[str, Any]:
    val = _float_or_none(row.get(feature))
    return {
        "label": label,
        "stance_feature": feature,
        "stance_threshold": threshold,
        "stance_value": val,
        "is_standing": _is_standing(row, feature=feature, threshold=threshold),
    }


def _gate_preview(row: dict[str, Any]) -> dict[str, Any]:
    speed_feat = GATE_PREVIEW["speed_feature"]
    speed_thr = float(GATE_PREVIEW["speed_threshold"])
    speed = _float_or_none(row.get(speed_feat))
    speed_high = speed is not None and speed > speed_thr
    exempt_hits = 0
    exempt_detail: list[dict[str, Any]] = []
    for angle_key, min_thr in GATE_PREVIEW["angle_exempt"]:
        v = _float_or_none(row.get(angle_key))
        ok = v is not None and v >= min_thr
        if ok:
            exempt_hits += 1
        exempt_detail.append({
            "feature": angle_key,
            "min_threshold": min_thr,
            "value": v,
            "met": ok,
        })
    triple_met = exempt_hits == len(GATE_PREVIEW["angle_exempt"])
    stance_previews = [
        _stance_preview_item(row, label=label, feature=stance_feat, threshold=stance_thr)
        for label, stance_feat, stance_thr in STANCE_PREVIEW_CONFIGS
    ]
    primary = stance_previews[0] if stance_previews else {}
    stance_feat = primary.get("stance_feature")
    stance_thr = float(primary.get("stance_threshold") or 0)
    stance_val = primary.get("stance_value")
    standing = bool(primary.get("is_standing"))
    would_block = speed_high and not triple_met and standing
    return {
        "speed_feature": speed_feat,
        "speed_threshold": speed_thr,
        "speed_value": speed,
        "speed_high": speed_high,
        "triple_and_met": triple_met,
        "angle_exempt_detail": exempt_detail,
        "stance_feature": stance_feat,
        "stance_threshold": stance_thr,
        "stance_value": stance_val,
        "is_standing": standing,
        "stance_previews": stance_previews,
        "would_block_collision": would_block,
    }


def _merge_velocity_angle(
    velocity_rows: list[dict[str, Any]],
    angle_rows: list[dict[str, Any]],
) -> dict[tuple[int, int], dict[str, Any]]:
    merged: dict[tuple[int, int], dict[str, Any]] = {}
    for row in velocity_rows:
        if not isinstance(row, dict):
            continue
        key = (int(row.get("frame_idx") or 0), int(row.get("person_track_id") or 0))
        merged[key] = dict(row)
    for row in angle_rows:
        if not isinstance(row, dict):
            continue
        key = (int(row.get("frame_idx") or 0), int(row.get("person_track_id") or 0))
        merged.setdefault(key, {})
        merged[key].update(row)
    return merged


# 按记录缓存 export 路径全量特征（与 export 脚本一次算全 timeline 一致）
_RECORD_EXPORT_CACHE: OrderedDict[str, dict[str, Any]] = OrderedDict()
_RECORD_CACHE_MAX = 12

# 按帧响应缓存
_FRAME_FEATURE_CACHE: OrderedDict[tuple[str, int, bool], dict[str, Any]] = OrderedDict()
_FRAME_CACHE_MAX_ENTRIES = 384


def _record_cache_get(record_id: str) -> dict[str, Any] | None:
    hit = _RECORD_EXPORT_CACHE.get(record_id)
    if hit is not None:
        _RECORD_EXPORT_CACHE.move_to_end(record_id)
    return hit


def _record_cache_put(record_id: str, value: dict[str, Any]) -> None:
    _RECORD_EXPORT_CACHE[record_id] = value
    _RECORD_EXPORT_CACHE.move_to_end(record_id)
    while len(_RECORD_EXPORT_CACHE) > _RECORD_CACHE_MAX:
        _RECORD_EXPORT_CACHE.popitem(last=False)


def _frame_cache_get(key: tuple[str, int, bool]) -> dict[str, Any] | None:
    hit = _FRAME_FEATURE_CACHE.get(key)
    if hit is not None:
        _FRAME_FEATURE_CACHE.move_to_end(key)
    return hit


def _frame_cache_put(key: tuple[str, int, bool], value: dict[str, Any]) -> None:
    _FRAME_FEATURE_CACHE[key] = value
    _FRAME_FEATURE_CACHE.move_to_end(key)
    while len(_FRAME_FEATURE_CACHE) > _FRAME_CACHE_MAX_ENTRIES:
        _FRAME_FEATURE_CACHE.popitem(last=False)


def clear_playback_features_cache(record_id: str | None = None) -> None:
    if not record_id:
        _FRAME_FEATURE_CACHE.clear()
        _RECORD_EXPORT_CACHE.clear()
        return
    rid = str(record_id).strip()
    for key in list(_FRAME_FEATURE_CACHE.keys()):
        if key[0] == rid:
            del _FRAME_FEATURE_CACHE[key]
    _RECORD_EXPORT_CACHE.pop(rid, None)


def _build_record_export_context(locator: RecordLocator) -> dict[str, Any]:
    cached = _record_cache_get(locator.record_id)
    if cached is not None:
        return cached

    timeline_frames = load_all_frames(locator)
    if not timeline_frames:
        ctx = {"available": False, "hint": "记录无帧数据"}
        _record_cache_put(locator.record_id, ctx)
        return ctx

    manifest = load_manifest(locator)
    infer_w, infer_h = _infer_size_from_frames(timeline_frames, manifest)
    fps = _video_fps(manifest)

    # 回放特征对齐 export：优先 baseline clip；无 baseline 时用实验默认 interval=2
    default_interval = DEFAULT_EXPORT_POSE_FRAME_INTERVAL
    baseline_dir = default_baseline_export_dir()
    export_indices, indices_source, pose_interval = resolve_export_indices(
        locator.record_id,
        timeline_frames,
        pose_frame_interval=default_interval,
        baseline_dir=baseline_dir,
    )
    if not export_indices:
        ctx = {"available": False, "hint": "无法确定 export 抽帧索引"}
        _record_cache_put(locator.record_id, ctx)
        return ctx

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
    merged = _merge_velocity_angle(velocity_rows, angle_rows)
    wrist_conf_map = _build_wrist_confidence_map(
        timeline_frames,
        export_indices,
        video_fps=fps,
    )

    ctx = {
        "available": True,
        "merged": merged,
        "wrist_conf_map": wrist_conf_map,
        "export_indices": export_indices,
        "export_indices_source": indices_source,
        "pose_frame_interval": pose_interval,
        "infer_width": infer_w,
        "infer_height": infer_h,
        "fps": fps,
    }
    _record_cache_put(locator.record_id, ctx)
    return ctx


def load_playback_features_for_frame(
    locator: RecordLocator,
    *,
    frame_idx: int,
    include_wrist: bool = False,
) -> dict[str, Any]:
    """按 export 帧号返回特征（与 export_prefilter 同路径）。"""
    fi = int(frame_idx)
    if fi <= 0:
        return {
            "available": False,
            "record_id": locator.record_id,
            "hint": "frame_idx 无效",
        }

    cache_key = (locator.record_id, fi, bool(include_wrist))
    cached = _frame_cache_get(cache_key)
    if cached is not None:
        return cached

    result = _compute_playback_features_for_frame(
        locator,
        frame_idx=fi,
        include_wrist=include_wrist,
    )
    if result.get("available") or result.get("hint"):
        _frame_cache_put(cache_key, result)
    return result


def _compute_playback_features_for_frame(
    locator: RecordLocator,
    *,
    frame_idx: int,
    include_wrist: bool,
) -> dict[str, Any]:
    fi = int(frame_idx)
    ctx = _build_record_export_context(locator)
    if not ctx.get("available"):
        return {
            "available": False,
            "record_id": locator.record_id,
            "frame_idx": fi,
            "hint": ctx.get("hint") or "特征不可用",
        }

    export_indices: set[int] = ctx["export_indices"]
    merged: dict[tuple[int, int], dict[str, Any]] = ctx["merged"]
    is_export_frame = fi in export_indices

    if not is_export_frame:
        nearest = min(export_indices, key=lambda x: abs(x - fi)) if export_indices else None
        return {
            "available": False,
            "record_id": locator.record_id,
            "frame_idx": fi,
            "export_frame_idx": fi,
            "is_export_frame": False,
            "pose_frame_interval": ctx.get("pose_frame_interval"),
            "export_indices_source": ctx.get("export_indices_source"),
            "hint": (
                f"帧 {fi} 非 export 抽帧（pose_frame_interval={ctx.get('pose_frame_interval')}）；"
                f"最近 export 帧={nearest}"
            ),
        }

    wrist_by_track: dict[int, list[dict[str, Any]]] = {}
    if include_wrist:
        try:
            wrist_payload = load_wrist_features_payload(locator, frame_idx=fi)
            if wrist_payload.get("available"):
                for wr in wrist_payload.get("frame_velocity") or []:
                    if not isinstance(wr, dict):
                        continue
                    tid = int(wr.get("person_track_id") or 0)
                    wrist_by_track.setdefault(tid, []).append(wr)
        except (OSError, RuntimeError):
            pass

    wrist_conf_map: dict[tuple[int, int], dict[str, Any]] = ctx.get("wrist_conf_map") or {}

    persons: list[dict[str, Any]] = []
    for (row_fi, track_id), row in sorted(merged.items(), key=lambda x: x[0][1]):
        if row_fi != fi or track_id <= 0:
            continue
        full_row = dict(row)
        persons.append({
            "person_id": row.get("person_id"),
            "person_track_id": track_id,
            "label_x": _float_or_none(row.get("torso_x")),
            "label_y": _float_or_none(row.get("torso_y")),
            "velocity": _pick_keys(full_row, VELOCITY_KEYS),
            "angles": _pick_keys(full_row, ANGLE_KEYS),
            "wrists": wrist_by_track.get(track_id) or [],
            "wrist_confidence": wrist_conf_map.get((fi, track_id))
            or {"left_wrist": None, "right_wrist": None, "score_min": WRIST_KPT_SCORE_MIN},
            "gate_preview": _gate_preview(full_row),
        })

    if not persons:
        return {
            "available": False,
            "record_id": locator.record_id,
            "frame_idx": fi,
            "export_frame_idx": fi,
            "is_export_frame": True,
            "hint": f"export 帧 {fi} 无有效人体特征",
        }

    return {
        "available": True,
        "record_id": locator.record_id,
        "frame_idx": fi,
        "export_frame_idx": fi,
        "is_export_frame": True,
        "pose_frame_interval": ctx.get("pose_frame_interval"),
        "export_indices_source": ctx.get("export_indices_source"),
        "infer_width": ctx.get("infer_width"),
        "infer_height": ctx.get("infer_height"),
        "person_count": len(persons),
        "persons": persons,
        "gate_preview_defaults": {
            "speed_feature": GATE_PREVIEW["speed_feature"],
            "speed_threshold": GATE_PREVIEW["speed_threshold"],
            "angle_exempt": [
                {"feature": f, "min_threshold": t}
                for f, t in GATE_PREVIEW["angle_exempt"]
            ],
            "stance_feature": GATE_PREVIEW.get("stance_feature"),
            "stance_threshold": GATE_PREVIEW.get("stance_threshold"),
            "stance_previews": [
                {"label": label, "stance_feature": feat, "stance_threshold": thr}
                for label, feat, thr in STANCE_PREVIEW_CONFIGS
            ],
        },
    }
