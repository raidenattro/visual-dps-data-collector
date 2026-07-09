"""全骨骼速度特征提取（17 点 + 躯干/全身聚合 + 碰撞段运动统计）。"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any

from event_engine.wrist_features import (
    assign_person_tracks_to_frames,
    extract_collision_segment_rows,
)
from event_engine.wrist_hits import WRIST_KPT_SCORE_MIN
from model_assets import COCO17_KEYPOINT_NAMES

SKELETON_FEATURE_SCHEMA = 1
KPT_COUNT = len(COCO17_KEYPOINT_NAMES)

# 上肢：肩/肘/腕
UPPER_KPT_INDICES = (5, 6, 7, 8, 9, 10)
# 下肢：髋/膝/踝
LOWER_KPT_INDICES = (11, 12, 13, 14, 15, 16)
# 躯干锚点：肩优先，否则髋
TORSO_SHOULDER_INDICES = (5, 6)
TORSO_HIP_INDICES = (11, 12)
WRIST_INDICES = (9, 10)
ELBOW_INDICES = (7, 8)

# 跨帧间隔超过此值则断开速度差分
MAX_VELOCITY_GAP_FRAMES = 2
# 位置中值滤波窗口
MEDIAN_FILTER_WINDOW = 3
RATIO_EPS = 1e-3


@dataclass
class _KptState:
    frame_idx: int = 0
    timestamp_sec: float = 0.0
    x: float = 0.0
    y: float = 0.0
    score: float = 0.0


@dataclass
class _TrackKptBuffer:
    """单 track × 单关键点：位置历史 + 上一帧滤波坐标。"""
    positions: list[tuple[float, float, float, int, float]] = field(default_factory=list)
    prev_filtered: tuple[float, float] | None = None
    prev_meta: _KptState | None = None


def _read_kpt(person: dict[str, Any], kpt_idx: int) -> tuple[float, float, float] | None:
    keypoints = person.get("keypoints") or []
    if kpt_idx >= len(keypoints):
        return None
    kp = keypoints[kpt_idx]
    if not isinstance(kp, (list, tuple)) or len(kp) < 2:
        return None
    score = float(kp[2]) if len(kp) > 2 else 0.0
    if score < WRIST_KPT_SCORE_MIN:
        return None
    return float(kp[0]), float(kp[1]), score


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return float(statistics.median(values))


def _filtered_xy(buffer: _TrackKptBuffer) -> tuple[float, float] | None:
    """对最近 MEDIAN_FILTER_WINDOW 帧坐标做中值滤波。"""
    recent = buffer.positions[-MEDIAN_FILTER_WINDOW:]
    if not recent:
        return None
    xs = [p[0] for p in recent]
    ys = [p[1] for p in recent]
    mx = _median(xs)
    my = _median(ys)
    if mx is None or my is None:
        return None
    return mx, my


def _compute_speed(
    x: float,
    y: float,
    prev: _KptState,
    *,
    frame_idx: int,
    ts: float,
    diag: float,
) -> dict[str, Any]:
    dt = ts - prev.timestamp_sec
    if dt <= 0:
        dt = (frame_idx - prev.frame_idx) / 25.0
    if dt <= 0:
        return {
            "vx": None,
            "vy": None,
            "speed": None,
            "speed_norm": None,
            "velocity_valid": False,
        }
    vx = (x - prev.x) / dt
    vy = (y - prev.y) / dt
    speed = math.hypot(vx, vy)
    return {
        "vx": round(vx, 3),
        "vy": round(vy, 3),
        "speed": round(speed, 3),
        "speed_norm": round(speed / diag, 6),
        "velocity_valid": True,
    }


def _torso_xy(person: dict[str, Any]) -> tuple[float, float, float] | None:
    """躯干锚点：双肩中心，不可用时用双髋中心。"""
    shoulders: list[tuple[float, float, float]] = []
    for idx in TORSO_SHOULDER_INDICES:
        pt = _read_kpt(person, idx)
        if pt is not None:
            shoulders.append(pt)
    if len(shoulders) >= 2:
        xs = [p[0] for p in shoulders]
        ys = [p[1] for p in shoulders]
        scores = [p[2] for p in shoulders]
        return sum(xs) / 2.0, sum(ys) / 2.0, min(scores)
    if len(shoulders) == 1:
        return shoulders[0]

    hips: list[tuple[float, float, float]] = []
    for idx in TORSO_HIP_INDICES:
        pt = _read_kpt(person, idx)
        if pt is not None:
            hips.append(pt)
    if len(hips) >= 2:
        xs = [p[0] for p in hips]
        ys = [p[1] for p in hips]
        scores = [p[2] for p in hips]
        return sum(xs) / 2.0, sum(ys) / 2.0, min(scores)
    if len(hips) == 1:
        return hips[0]
    return None


def _mean_of_speeds(speeds: list[float | None]) -> float | None:
    valid = [float(s) for s in speeds if s is not None and math.isfinite(float(s))]
    if not valid:
        return None
    return sum(valid) / len(valid)


def _max_of_speeds(speeds: list[float | None]) -> float | None:
    valid = [float(s) for s in speeds if s is not None and math.isfinite(float(s))]
    if not valid:
        return None
    return max(valid)


def extract_keypoint_velocity_rows(
    frames: list[dict[str, Any]],
    *,
    infer_width: int,
    infer_height: int,
    video_fps: float = 25.0,
) -> list[dict[str, Any]]:
    """每帧 × 每人 × 17 关键点速度（宽表，含聚合列）。"""
    diag = math.hypot(max(1, infer_width), max(1, infer_height))
    fps = max(1.0, float(video_fps))
    buffers: dict[tuple[int, int], _TrackKptBuffer] = {}
    torso_buffers: dict[int, _TrackKptBuffer] = {}
    rows: list[dict[str, Any]] = []

    for fr in sorted(frames, key=lambda f: int(f.get("frame_idx") or 0)):
        if not isinstance(fr, dict):
            continue
        frame_idx = int(fr.get("frame_idx") or 0)
        source_frame_idx = int(fr.get("source_frame_idx") or frame_idx)
        ts = float(fr.get("timestamp_sec") or 0.0)
        if ts <= 0 and frame_idx > 0:
            ts = frame_idx / fps

        for person in fr.get("persons") or []:
            if not isinstance(person, dict):
                continue
            person_id = int(person.get("person_id") if person.get("person_id") is not None else -1)
            track_id = int(person.get("person_track_id") or 0)

            row: dict[str, Any] = {
                "frame_idx": frame_idx,
                "source_frame_idx": source_frame_idx,
                "timestamp_sec": ts,
                "person_id": person_id,
                "person_track_id": track_id,
            }

            kpt_speeds: dict[int, float | None] = {}
            for kpt_idx in range(KPT_COUNT):
                key = (track_id, kpt_idx)
                buf = buffers.setdefault(key, _TrackKptBuffer())
                pt = _read_kpt(person, kpt_idx)
                prefix = f"kpt_{kpt_idx}"
                row[f"{prefix}_x"] = None
                row[f"{prefix}_y"] = None
                row[f"{prefix}_score"] = None
                row[f"{prefix}_vx"] = None
                row[f"{prefix}_vy"] = None
                row[f"{prefix}_speed"] = None
                row[f"{prefix}_speed_norm"] = None
                row[f"{prefix}_velocity_valid"] = False

                if pt is None:
                    kpt_speeds[kpt_idx] = None
                    continue

                x, y, score = pt
                row[f"{prefix}_x"] = round(x, 2)
                row[f"{prefix}_y"] = round(y, 2)
                row[f"{prefix}_score"] = round(score, 4)

                if buf.prev_meta is not None:
                    gap = frame_idx - buf.prev_meta.frame_idx
                    if gap > MAX_VELOCITY_GAP_FRAMES:
                        buf.prev_filtered = None
                        buf.prev_meta = None

                buf.positions.append((x, y, score, frame_idx, ts))
                filtered = _filtered_xy(buf)
                if filtered is None:
                    kpt_speeds[kpt_idx] = None
                    continue

                fx, fy = filtered
                if buf.prev_filtered is not None and buf.prev_meta is not None:
                    prev_state = _KptState(
                        frame_idx=buf.prev_meta.frame_idx,
                        timestamp_sec=buf.prev_meta.timestamp_sec,
                        x=buf.prev_filtered[0],
                        y=buf.prev_filtered[1],
                    )
                    vel = _compute_speed(
                        fx,
                        fy,
                        prev_state,
                        frame_idx=frame_idx,
                        ts=ts,
                        diag=diag,
                    )
                    row.update({
                        f"{prefix}_vx": vel["vx"],
                        f"{prefix}_vy": vel["vy"],
                        f"{prefix}_speed": vel["speed"],
                        f"{prefix}_speed_norm": vel["speed_norm"],
                        f"{prefix}_velocity_valid": vel["velocity_valid"],
                    })
                    kpt_speeds[kpt_idx] = vel["speed"] if vel["velocity_valid"] else None
                else:
                    kpt_speeds[kpt_idx] = None

                buf.prev_filtered = (fx, fy)
                buf.prev_meta = _KptState(frame_idx=frame_idx, timestamp_sec=ts, x=fx, y=fy, score=score)

            # 躯干速度（独立缓冲）
            torso_buf = torso_buffers.setdefault(track_id, _TrackKptBuffer())
            torso_pt = _torso_xy(person)
            torso_speed: float | None = None
            row["torso_x"] = None
            row["torso_y"] = None
            row["torso_speed"] = None
            row["torso_speed_norm"] = None
            row["torso_velocity_valid"] = False

            if torso_pt is not None:
                tx, ty, tscore = torso_pt
                row["torso_x"] = round(tx, 2)
                row["torso_y"] = round(ty, 2)
                if torso_buf.prev_meta is not None:
                    gap = frame_idx - torso_buf.prev_meta.frame_idx
                    if gap > MAX_VELOCITY_GAP_FRAMES:
                        torso_buf.prev_filtered = None
                        torso_buf.prev_meta = None

                torso_buf.positions.append((tx, ty, tscore, frame_idx, ts))
                t_filtered = _filtered_xy(torso_buf)
                if t_filtered is not None:
                    tfx, tfy = t_filtered
                    if torso_buf.prev_filtered is not None and torso_buf.prev_meta is not None:
                        prev_state = _KptState(
                            frame_idx=torso_buf.prev_meta.frame_idx,
                            timestamp_sec=torso_buf.prev_meta.timestamp_sec,
                            x=torso_buf.prev_filtered[0],
                            y=torso_buf.prev_filtered[1],
                        )
                        vel = _compute_speed(
                            tfx,
                            tfy,
                            prev_state,
                            frame_idx=frame_idx,
                            ts=ts,
                            diag=diag,
                        )
                        torso_speed = vel["speed"] if vel["velocity_valid"] else None
                        row["torso_speed"] = vel["speed"]
                        row["torso_speed_norm"] = vel["speed_norm"]
                        row["torso_velocity_valid"] = vel["velocity_valid"]
                    torso_buf.prev_filtered = (tfx, tfy)
                    torso_buf.prev_meta = _KptState(
                        frame_idx=frame_idx,
                        timestamp_sec=ts,
                        x=tfx,
                        y=tfy,
                        score=tscore,
                    )

            upper_speeds = [kpt_speeds.get(i) for i in UPPER_KPT_INDICES]
            lower_speeds = [kpt_speeds.get(i) for i in LOWER_KPT_INDICES]
            wrist_speeds = [kpt_speeds.get(i) for i in WRIST_INDICES]
            elbow_speeds = [kpt_speeds.get(i) for i in ELBOW_INDICES]
            all_speeds = [kpt_speeds.get(i) for i in range(KPT_COUNT)]

            body_mean = _mean_of_speeds(all_speeds)
            body_max = _max_of_speeds(all_speeds)
            upper_mean = _mean_of_speeds(upper_speeds)
            lower_mean = _mean_of_speeds(lower_speeds)
            wrist_max = _max_of_speeds(wrist_speeds)
            elbow_max = _max_of_speeds(elbow_speeds)

            row["body_mean_speed"] = round(body_mean, 3) if body_mean is not None else None
            row["body_max_speed"] = round(body_max, 3) if body_max is not None else None
            row["upper_mean_speed"] = round(upper_mean, 3) if upper_mean is not None else None
            row["lower_mean_speed"] = round(lower_mean, 3) if lower_mean is not None else None
            row["wrist_max_speed"] = round(wrist_max, 3) if wrist_max is not None else None
            row["elbow_max_speed"] = round(elbow_max, 3) if elbow_max is not None else None

            if wrist_max is not None and torso_speed is not None:
                row["wrist_torso_ratio"] = round(wrist_max / (torso_speed + RATIO_EPS), 4)
            else:
                row["wrist_torso_ratio"] = None

            rows.append(row)

    return rows


def extract_aggregate_velocity_rows(
    frames: list[dict[str, Any]],
    *,
    infer_width: int,
    infer_height: int,
    video_fps: float = 25.0,
) -> list[dict[str, Any]]:
    """帧级聚合速度（不含 17 点明细列，体积更小）。"""
    full = extract_keypoint_velocity_rows(
        frames,
        infer_width=infer_width,
        infer_height=infer_height,
        video_fps=video_fps,
    )
    aggregate_keys = (
        "frame_idx",
        "source_frame_idx",
        "timestamp_sec",
        "person_id",
        "person_track_id",
        "torso_x",
        "torso_y",
        "torso_speed",
        "torso_speed_norm",
        "torso_velocity_valid",
        "body_mean_speed",
        "body_max_speed",
        "upper_mean_speed",
        "lower_mean_speed",
        "wrist_max_speed",
        "elbow_max_speed",
        "wrist_torso_ratio",
    )
    kpt_speed_keys = [f"kpt_{i}_speed" for i in range(KPT_COUNT)]
    out: list[dict[str, Any]] = []
    for row in full:
        slim = {k: row.get(k) for k in aggregate_keys}
        for k in kpt_speed_keys:
            slim[k] = row.get(k)
        out.append(slim)
    return out


def filter_frames_to_indices(
    frames: list[dict[str, Any]],
    frame_indices: set[int] | list[int],
) -> list[dict[str, Any]]:
    """按 frame_idx 子集过滤并保持时间顺序（用于抽帧速度差分）。"""
    wanted = set(int(x) for x in frame_indices)
    out = [
        fr for fr in sorted(frames, key=lambda f: int(f.get("frame_idx") or 0))
        if isinstance(fr, dict) and int(fr.get("frame_idx") or 0) in wanted
    ]
    return out


def extract_subsampled_velocity_from_frames(
    frames: list[dict[str, Any]],
    export_frame_indices: set[int] | list[int],
    *,
    infer_width: int,
    infer_height: int,
    video_fps: float = 25.0,
) -> list[dict[str, Any]]:
    """仅在导出抽帧序列上计算聚合速度（贴合 pose_frame_interval 现场环境）。"""
    subsampled = filter_frames_to_indices(frames, export_frame_indices)
    if not subsampled:
        return []
    tracked = assign_person_tracks_to_frames(subsampled, video_fps=video_fps)
    return extract_aggregate_velocity_rows(
        tracked,
        infer_width=infer_width,
        infer_height=infer_height,
        video_fps=video_fps,
    )


def _percentile(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    i = (len(xs) - 1) * p / 100.0
    lo, hi = int(math.floor(i)), int(math.ceil(i))
    if lo == hi:
        return xs[lo]
    return xs[lo] + (xs[hi] - xs[lo]) * (i - lo)


def enrich_collision_segments_with_motion(
    segments: list[dict[str, Any]],
    velocity_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """为碰撞段附加段内运动速度统计。"""
    by_frame_track: dict[tuple[int, int], dict[str, Any]] = {}
    for row in velocity_rows:
        if not isinstance(row, dict):
            continue
        fi = int(row.get("frame_idx") or 0)
        tid = int(row.get("person_track_id") or 0)
        by_frame_track[(fi, tid)] = row

    enriched: list[dict[str, Any]] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        out = dict(seg)
        track_id = int(seg.get("person_track_id") or 0)
        a = int(seg.get("frame_enter") or 0)
        b = int(seg.get("frame_exit") or 0)
        seg_frames = max(0, b - a + 1)

        torso_speeds: list[float] = []
        lower_speeds: list[float] = []
        body_mean_speeds: list[float] = []
        ratio_speeds: list[float] = []

        for fi in range(a, b + 1):
            row = by_frame_track.get((fi, track_id))
            if not row:
                continue
            if row.get("torso_velocity_valid") and row.get("torso_speed") is not None:
                torso_speeds.append(float(row["torso_speed"]))
            if row.get("lower_mean_speed") is not None:
                lower_speeds.append(float(row["lower_mean_speed"]))
            if row.get("body_mean_speed") is not None:
                body_mean_speeds.append(float(row["body_mean_speed"]))
            if row.get("wrist_torso_ratio") is not None:
                ratio_speeds.append(float(row["wrist_torso_ratio"]))

        valid_count = len(torso_speeds)
        out["motion_frame_count"] = valid_count
        out["motion_valid_ratio"] = round(valid_count / seg_frames, 4) if seg_frames > 0 else 0.0
        out["torso_speed_p50"] = round(_percentile(torso_speeds, 50), 3) if torso_speeds else None
        out["torso_speed_max"] = round(max(torso_speeds), 3) if torso_speeds else None
        out["lower_mean_speed_p50"] = (
            round(_percentile(lower_speeds, 50), 3) if lower_speeds else None
        )
        out["body_mean_speed_p50"] = (
            round(_percentile(body_mean_speeds, 50), 3) if body_mean_speeds else None
        )
        out["wrist_torso_ratio_p50"] = (
            round(_percentile(ratio_speeds, 50), 4) if ratio_speeds else None
        )
        enriched.append(out)

    return enriched


def extract_skeleton_features_from_frames(
    frames: list[dict[str, Any]],
    boxes: list[dict[str, Any]],
    *,
    infer_width: int,
    infer_height: int,
    video_fps: float = 25.0,
    max_gap_frames: int = 1,
    include_keypoint_detail: bool = True,
) -> dict[str, Any]:
    """从已加载帧数据提取全骨骼速度与碰撞段运动特征。"""
    tracked = assign_person_tracks_to_frames(frames, video_fps=video_fps)
    if include_keypoint_detail:
        velocity_rows = extract_keypoint_velocity_rows(
            tracked,
            infer_width=infer_width,
            infer_height=infer_height,
            video_fps=video_fps,
        )
    else:
        velocity_rows = extract_aggregate_velocity_rows(
            tracked,
            infer_width=infer_width,
            infer_height=infer_height,
            video_fps=video_fps,
        )
    segment_rows = extract_collision_segment_rows(
        tracked,
        boxes,
        max_gap_frames=max_gap_frames,
    )
    motion_segment_rows = enrich_collision_segments_with_motion(segment_rows, velocity_rows)
    return {
        "schema": SKELETON_FEATURE_SCHEMA,
        "velocity_rows": velocity_rows,
        "segment_rows": segment_rows,
        "motion_segment_rows": motion_segment_rows,
        "velocity_count": len(velocity_rows),
        "segment_count": len(segment_rows),
        "motion_segment_count": len(motion_segment_rows),
    }
