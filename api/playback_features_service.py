"""回放按帧骨骼特征：速度 + 关节角 + 手腕速度（实时计算，与前置门控实验字段对齐）。"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

from event_engine.skeleton_angles import extract_subsampled_joint_angle_from_frames
from event_engine.skeleton_features import (
    MAX_VELOCITY_GAP_FRAMES,
    MEDIAN_FILTER_WINDOW,
    extract_subsampled_velocity_from_frames,
)
from pose_store import RecordLocator, load_frames_range, load_manifest

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
)

# 门控预览默认阈（ankle_max@80 + triple90）
GATE_PREVIEW = {
    "speed_feature": "ankle_max_speed",
    "speed_threshold": 80.0,
    "angle_exempt": (
        ("arm_torso_angle_max", 90.0),
        ("elbow_angle_mean", 150.0),
        ("wrist_elevation_angle_max", 60.0),
    ),
}


def _float_or_none(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not (f == f):  # NaN
        return None
    return f


def _pick_keys(row: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in keys:
        val = row.get(k)
        if val is None:
            out[k] = None
        elif isinstance(val, (int, float)):
            out[k] = val
        else:
            out[k] = val
    return out


def _gate_preview(row: dict[str, Any]) -> dict[str, Any]:
    feat = GATE_PREVIEW["speed_feature"]
    thr = float(GATE_PREVIEW["speed_threshold"])
    speed = _float_or_none(row.get(feat))
    speed_high = speed is not None and speed > thr
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
    would_block = speed_high and not triple_met
    return {
        "speed_feature": feat,
        "speed_threshold": thr,
        "speed_value": speed,
        "speed_high": speed_high,
        "triple_and_met": triple_met,
        "angle_exempt_detail": exempt_detail,
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


def _frame_indices_from_rows(frames: list[dict[str, Any]]) -> set[int]:
    out: set[int] = set()
    for fr in frames:
        if not isinstance(fr, dict):
            continue
        fi = int(fr.get("frame_idx") or fr.get("source_frame_idx") or 0)
        if fi > 0:
            out.add(fi)
    return out


# 进程内 LRU：避免同帧重复重算阻塞视频/骨架请求
_FRAME_FEATURE_CACHE: OrderedDict[tuple[str, int, bool], dict[str, Any]] = OrderedDict()
_CACHE_MAX_ENTRIES = 384


def _cache_get(key: tuple[str, int, bool]) -> dict[str, Any] | None:
    hit = _FRAME_FEATURE_CACHE.get(key)
    if hit is not None:
        _FRAME_FEATURE_CACHE.move_to_end(key)
    return hit


def _cache_put(key: tuple[str, int, bool], value: dict[str, Any]) -> None:
    _FRAME_FEATURE_CACHE[key] = value
    _FRAME_FEATURE_CACHE.move_to_end(key)
    while len(_FRAME_FEATURE_CACHE) > _CACHE_MAX_ENTRIES:
        _FRAME_FEATURE_CACHE.popitem(last=False)


def clear_playback_features_cache(record_id: str | None = None) -> None:
    """切换记录时可按需清理缓存。"""
    if not record_id:
        _FRAME_FEATURE_CACHE.clear()
        return
    rid = str(record_id).strip()
    for key in list(_FRAME_FEATURE_CACHE.keys()):
        if key[0] == rid:
            del _FRAME_FEATURE_CACHE[key]


def load_playback_features_for_frame(
    locator: RecordLocator,
    *,
    frame_idx: int,
    include_wrist: bool = False,
) -> dict[str, Any]:
    """按帧返回所有人体 track 的速度与角度特征（带进程内 LRU 缓存）。"""
    fi = int(frame_idx)
    if fi <= 0:
        return {
            "available": False,
            "record_id": locator.record_id,
            "hint": "frame_idx 无效",
        }

    cache_key = (locator.record_id, fi, bool(include_wrist))
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    result = _compute_playback_features_for_frame(
        locator,
        frame_idx=fi,
        include_wrist=include_wrist,
    )
    if result.get("available"):
        _cache_put(cache_key, result)
    return result


def _compute_playback_features_for_frame(
    locator: RecordLocator,
    *,
    frame_idx: int,
    include_wrist: bool,
) -> dict[str, Any]:
    fi = int(frame_idx)

    # 速度差分需要历史帧：中值窗口 + 间隔断开
    lookback = max(12, MAX_VELOCITY_GAP_FRAMES + MEDIAN_FILTER_WINDOW + 4)
    lo = max(1, fi - lookback)
    try:
        frames = load_frames_range(locator, from_frame_idx=lo, to_frame_idx=fi)
    except (OSError, RuntimeError, ValueError) as exc:
        return {
            "available": False,
            "record_id": locator.record_id,
            "frame_idx": fi,
            "error": str(exc),
        }

    if not frames:
        return {
            "available": False,
            "record_id": locator.record_id,
            "frame_idx": fi,
            "hint": "该帧附近无骨架数据",
        }

    manifest = load_manifest(locator)
    infer_w, infer_h = _infer_size_from_frames(frames, manifest)
    fps = _video_fps(manifest)
    indices = _frame_indices_from_rows(frames)

    velocity_rows = extract_subsampled_velocity_from_frames(
        frames,
        indices,
        infer_width=infer_w,
        infer_height=infer_h,
        video_fps=fps,
    )
    angle_rows = extract_subsampled_joint_angle_from_frames(
        frames,
        indices,
        video_fps=fps,
    )
    merged = _merge_velocity_angle(velocity_rows, angle_rows)

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
            "gate_preview": _gate_preview(full_row),
        })

    return {
        "available": True,
        "record_id": locator.record_id,
        "frame_idx": fi,
        "infer_width": infer_w,
        "infer_height": infer_h,
        "person_count": len(persons),
        "persons": persons,
        "gate_preview_defaults": {
            "speed_feature": GATE_PREVIEW["speed_feature"],
            "speed_threshold": GATE_PREVIEW["speed_threshold"],
            "angle_exempt": [
                {"feature": f, "min_threshold": t}
                for f, t in GATE_PREVIEW["angle_exempt"]
            ],
        },
    }
