"""手腕速度与碰撞段（手腕进/出货框）特征提取。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from event_engine.collision import PersonTrackAssigner
from event_engine.wrist_hits import WRIST_KEYPOINTS, person_wrist_hits, read_wrist_point

FEATURE_SCHEMA = 1


@dataclass
class _OpenSegment:
    segment_id: int
    person_id: int
    person_track_id: int
    wrist: str
    box_token: str
    frame_enter: int
    source_frame_enter: int
    ts_enter: float
    x_enter: float
    y_enter: float
    enter_score: float
    last_frame: int
    source_last_frame: int
    ts_last: float
    x_last: float
    y_last: float
    exit_score: float
    path_length: float = 0.0
    frame_count: int = 1
    had_alarm: bool = False
    gap_frames: int = 0


def _person_anchor(person: dict[str, Any]) -> tuple[float, float]:
    """与 CollisionProcessor 一致：肩中心或全身均值。"""
    keypoints = person.get("keypoints") or []
    if len(keypoints) < 11:
        xs = [float(k[0]) for k in keypoints if isinstance(k, (list, tuple)) and len(k) >= 2]
        ys = [float(k[1]) for k in keypoints if isinstance(k, (list, tuple)) and len(k) >= 2]
        return (
            sum(xs) / len(xs) if xs else 0.0,
            sum(ys) / len(ys) if ys else 0.0,
        )

    def _pt(i: int) -> tuple[float, float, float]:
        kp = keypoints[i]
        return (
            float(kp[0]),
            float(kp[1]),
            float(kp[2]) if len(kp) > 2 else 0.0,
        )

    lx, ly, ls = _pt(5)
    rx, ry, rs = _pt(6)
    if ls > 0.2 and rs > 0.2:
        return (lx + rx) / 2.0, (ly + ry) / 2.0
    xs = [float(k[0]) for k in keypoints if isinstance(k, (list, tuple)) and len(k) >= 2]
    ys = [float(k[1]) for k in keypoints if isinstance(k, (list, tuple)) and len(k) >= 2]
    return (
        sum(xs) / len(xs) if xs else 0.0,
        sum(ys) / len(ys) if ys else 0.0,
    )


def assign_person_tracks_to_frames(
    frames: list[dict[str, Any]],
    *,
    video_fps: float = 25.0,
) -> list[dict[str, Any]]:
    """对已存骨架逐帧分配 person_track_id（无需重跑模型）。"""
    fps = max(1.0, float(video_fps))
    assigner = PersonTrackAssigner(max_match_dist=220.0, stale_sec=1.2)
    out: list[dict[str, Any]] = []

    for fr in sorted(frames, key=lambda f: int(f.get("frame_idx") or 0)):
        if not isinstance(fr, dict):
            continue
        frame_idx = int(fr.get("frame_idx") or fr.get("source_frame_idx") or 0)
        now_ts = float(fr.get("timestamp_sec") or 0.0)
        if now_ts <= 0 and frame_idx > 0:
            now_ts = frame_idx / fps

        persons = fr.get("persons") or []
        tracked: list[dict[str, Any]] = []
        used: set[int] = set()
        for person in persons:
            if not isinstance(person, dict):
                continue
            ax, ay = _person_anchor(person)
            track_id = assigner.assign(ax, ay, now_ts=now_ts, occupied_track_ids=used)
            row = dict(person)
            row["person_track_id"] = track_id
            tracked.append(row)

        out.append({**fr, "persons": tracked})
    return out


def extract_wrist_velocity_rows(
    frames: list[dict[str, Any]],
    *,
    infer_width: int,
    infer_height: int,
) -> list[dict[str, Any]]:
    """每帧 × 每人 × 左右手腕速度。"""
    diag = math.hypot(max(1, infer_width), max(1, infer_height))
    rows: list[dict[str, Any]] = []
    prev: dict[tuple[int, str], dict[str, Any]] = {}

    for fr in sorted(frames, key=lambda f: int(f.get("frame_idx") or 0)):
        if not isinstance(fr, dict):
            continue
        frame_idx = int(fr.get("frame_idx") or 0)
        source_frame_idx = int(fr.get("source_frame_idx") or frame_idx)
        ts = float(fr.get("timestamp_sec") or 0.0)

        for person in fr.get("persons") or []:
            if not isinstance(person, dict):
                continue
            person_id = int(person.get("person_id") if person.get("person_id") is not None else -1)
            track_id = int(person.get("person_track_id") or 0)

            for kpt_idx, wrist_name in WRIST_KEYPOINTS:
                pt = read_wrist_point(person, kpt_idx)
                key = (track_id, wrist_name)
                row: dict[str, Any] = {
                    "frame_idx": frame_idx,
                    "source_frame_idx": source_frame_idx,
                    "timestamp_sec": ts,
                    "person_id": person_id,
                    "person_track_id": track_id,
                    "wrist": wrist_name,
                    "x": None,
                    "y": None,
                    "kpt_score": None,
                    "valid": False,
                    "vx": None,
                    "vy": None,
                    "speed": None,
                    "speed_norm": None,
                    "velocity_valid": False,
                }
                if pt is None:
                    rows.append(row)
                    continue

                x, y, score = pt
                row.update({
                    "x": round(x, 2),
                    "y": round(y, 2),
                    "kpt_score": round(score, 4),
                    "valid": True,
                })

                prev_pt = prev.get(key)
                if prev_pt is not None:
                    dt = ts - float(prev_pt["timestamp_sec"])
                    if dt <= 0:
                        dt = (frame_idx - int(prev_pt["frame_idx"])) / 25.0
                    if dt > 0:
                        vx = (x - float(prev_pt["x"])) / dt
                        vy = (y - float(prev_pt["y"])) / dt
                        speed = math.hypot(vx, vy)
                        row.update({
                            "vx": round(vx, 3),
                            "vy": round(vy, 3),
                            "speed": round(speed, 3),
                            "speed_norm": round(speed / diag, 6),
                            "velocity_valid": True,
                        })

                prev[key] = {
                    "frame_idx": frame_idx,
                    "timestamp_sec": ts,
                    "x": x,
                    "y": y,
                }
                rows.append(row)

    return rows


def _finalize_segment(seg: _OpenSegment) -> dict[str, Any]:
    dx = seg.x_last - seg.x_enter
    dy = seg.y_last - seg.y_enter
    duration = max(0.0, seg.ts_last - seg.ts_enter)
    return {
        "segment_id": seg.segment_id,
        "event_type": "collision",
        "person_id": seg.person_id,
        "person_track_id": seg.person_track_id,
        "wrist": seg.wrist,
        "box_token": seg.box_token,
        "frame_enter": seg.frame_enter,
        "frame_exit": seg.last_frame,
        "source_frame_enter": seg.source_frame_enter,
        "source_frame_exit": seg.source_last_frame,
        "ts_enter": round(seg.ts_enter, 6),
        "ts_exit": round(seg.ts_last, 6),
        "x_enter": round(seg.x_enter, 2),
        "y_enter": round(seg.y_enter, 2),
        "x_exit": round(seg.x_last, 2),
        "y_exit": round(seg.y_last, 2),
        "dx": round(dx, 2),
        "dy": round(dy, 2),
        "displacement": round(math.hypot(dx, dy), 2),
        "path_length": round(seg.path_length, 2),
        "duration_sec": round(duration, 6),
        "frame_count": seg.frame_count,
        "enter_kpt_score": round(seg.enter_score, 4),
        "exit_kpt_score": round(seg.exit_score, 4),
        "had_alarm": seg.had_alarm,
    }


def extract_collision_segment_rows(
    frames: list[dict[str, Any]],
    boxes: list[dict[str, Any]],
    *,
    max_gap_frames: int = 1,
) -> list[dict[str, Any]]:
    """
    碰撞段：手腕点进入某 box 到离开的连续占用区间（与告警无关）。
    max_gap_frames：手腕短暂不可见/出框时允许合并的最大间隔帧数。
    """
    gap_allow = max(0, int(max_gap_frames))
    open_map: dict[tuple[int, str, str], _OpenSegment] = {}
    completed: list[dict[str, Any]] = []
    next_seg_id = 1

    sorted_frames = sorted(frames, key=lambda f: int(f.get("frame_idx") or 0))

    def _close_segment(key: tuple[int, str, str]) -> None:
        nonlocal open_map
        seg = open_map.pop(key, None)
        if seg is None:
            return
        if seg.frame_count <= 0:
            return
        completed.append(_finalize_segment(seg))

    for fr in sorted_frames:
        if not isinstance(fr, dict):
            continue
        frame_idx = int(fr.get("frame_idx") or 0)
        source_frame_idx = int(fr.get("source_frame_idx") or frame_idx)
        ts = float(fr.get("timestamp_sec") or 0.0)
        alarms = {str(t) for t in (fr.get("alarm_collisions") or []) if str(t).strip()}

        # (track_id, wrist) -> hit
        hits_by_key: dict[tuple[int, str], dict[str, Any]] = {}
        person_meta: dict[int, int] = {}

        for person in fr.get("persons") or []:
            if not isinstance(person, dict):
                continue
            track_id = int(person.get("person_track_id") or 0)
            person_id = int(person.get("person_id") if person.get("person_id") is not None else -1)
            person_meta[track_id] = person_id
            for hit in person_wrist_hits(person, boxes):
                hits_by_key[(track_id, hit["wrist"])] = hit

        # 更新仍活跃的段
        for key in list(open_map.keys()):
            track_id, wrist, token = key
            hit = hits_by_key.get((track_id, wrist))
            if hit and hit["token"] == token:
                seg = open_map[key]
                if seg.last_frame < frame_idx and seg.x_last is not None:
                    seg.path_length += math.hypot(
                        float(hit["x"]) - seg.x_last,
                        float(hit["y"]) - seg.y_last,
                    )
                seg.last_frame = frame_idx
                seg.source_last_frame = source_frame_idx
                seg.ts_last = ts
                seg.x_last = float(hit["x"])
                seg.y_last = float(hit["y"])
                seg.exit_score = float(hit["score"])
                seg.frame_count += 1
                seg.gap_frames = 0
                if token in alarms:
                    seg.had_alarm = True
                continue

            seg = open_map[key]
            seg.gap_frames += 1
            if seg.gap_frames > gap_allow:
                _close_segment(key)

        # 新开段
        for (track_id, wrist), hit in hits_by_key.items():
            token = str(hit["token"])
            key = (track_id, wrist, token)
            if key in open_map:
                continue
            open_map[key] = _OpenSegment(
                segment_id=next_seg_id,
                person_id=person_meta.get(track_id, -1),
                person_track_id=track_id,
                wrist=wrist,
                box_token=token,
                frame_enter=frame_idx,
                source_frame_enter=source_frame_idx,
                ts_enter=ts,
                x_enter=float(hit["x"]),
                y_enter=float(hit["y"]),
                enter_score=float(hit["score"]),
                last_frame=frame_idx,
                source_last_frame=source_frame_idx,
                ts_last=ts,
                x_last=float(hit["x"]),
                y_last=float(hit["y"]),
                exit_score=float(hit["score"]),
                had_alarm=token in alarms,
            )
            next_seg_id += 1

    for key in list(open_map.keys()):
        _close_segment(key)

    completed.sort(
        key=lambda r: (
            int(r.get("frame_enter") or 0),
            int(r.get("person_track_id") or 0),
            str(r.get("wrist") or ""),
        )
    )
    return completed


def extract_wrist_features_from_frames(
    frames: list[dict[str, Any]],
    boxes: list[dict[str, Any]],
    *,
    infer_width: int,
    infer_height: int,
    video_fps: float = 25.0,
    max_gap_frames: int = 1,
) -> dict[str, Any]:
    """从已加载帧数据提取手腕速度与碰撞段特征。"""
    tracked = assign_person_tracks_to_frames(frames, video_fps=video_fps)
    velocity_rows = extract_wrist_velocity_rows(
        tracked,
        infer_width=infer_width,
        infer_height=infer_height,
    )
    segment_rows = extract_collision_segment_rows(
        tracked,
        boxes,
        max_gap_frames=max_gap_frames,
    )
    return {
        "schema": FEATURE_SCHEMA,
        "velocity_rows": velocity_rows,
        "segment_rows": segment_rows,
        "velocity_count": len(velocity_rows),
        "segment_count": len(segment_rows),
    }
