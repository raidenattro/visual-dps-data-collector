"""肩/肘/腕关节开合角与角速度（对齐 export 抽帧序列）。"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any

from event_engine.skeleton_features import (
    MAX_VELOCITY_GAP_FRAMES,
    MEDIAN_FILTER_WINDOW,
    filter_frames_to_indices,
    _KptState,
    _read_kpt,
)
from event_engine.wrist_features import assign_person_tracks_to_frames

# 开合角：关节处内角（度），180° 表示完全伸直
SHOULDER_ANGLE_DEFS = (
    ("left_shoulder_angle", 11, 5, 7),   # 髋-肩-肘
    ("right_shoulder_angle", 12, 6, 8),
)
ELBOW_ANGLE_DEFS = (
    ("left_elbow_angle", 5, 7, 9),       # 肩-肘-腕
    ("right_elbow_angle", 6, 8, 10),
)
WRIST_ANGLE_DEFS = (
    ("left_wrist_angle", 7, 9, 5),       # 肘-腕-肩（前臂相对上臂偏折）
    ("right_wrist_angle", 8, 10, 6),
)
# 膝角：髋-膝-踝内角，站立接近伸直（~150–180°），蹲姿明显变小
KNEE_ANGLE_DEFS = (
    ("left_knee_angle", 11, 13, 15),
    ("right_knee_angle", 12, 14, 16),
)

ALL_ANGLE_DEFS = SHOULDER_ANGLE_DEFS + ELBOW_ANGLE_DEFS + WRIST_ANGLE_DEFS

# 侧身/腰身参考：双髋中心
HIP_LEFT = 11
HIP_RIGHT = 12
SHOULDER_LEFT = 5
SHOULDER_RIGHT = 6
ANKLE_LEFT = 15
ANKLE_RIGHT = 16

# 上半身(肩→髋)与下半身(髋→踝)整体夹角，绕髋；行走时膝弯但上下躯干夹角相对稳定
TORSO_LEG_ANGLE_DEFS = (
    ("left_torso_leg_angle", SHOULDER_LEFT, HIP_LEFT, ANKLE_LEFT),
    ("right_torso_leg_angle", SHOULDER_RIGHT, HIP_RIGHT, ANKLE_RIGHT),
)


def _hip_center(person: dict[str, Any]) -> tuple[float, float] | None:
    """腰身锚点：左右髋中心。"""
    lh = _read_xy(person, HIP_LEFT)
    rh = _read_xy(person, HIP_RIGHT)
    if lh is None and rh is None:
        return None
    if lh is None:
        return rh
    if rh is None:
        return lh
    return (lh[0] + rh[0]) / 2.0, (lh[1] + rh[1]) / 2.0


def _shoulder_center(person: dict[str, Any]) -> tuple[float, float] | None:
    """双肩中心。"""
    ls = _read_xy(person, SHOULDER_LEFT)
    rs = _read_xy(person, SHOULDER_RIGHT)
    if ls is None and rs is None:
        return None
    if ls is None:
        return rs
    if rs is None:
        return ls
    return (ls[0] + rs[0]) / 2.0, (ls[1] + rs[1]) / 2.0


def _ankle_center(person: dict[str, Any]) -> tuple[float, float] | None:
    """双踝中心。"""
    la = _read_xy(person, ANKLE_LEFT)
    ra = _read_xy(person, ANKLE_RIGHT)
    if la is None and ra is None:
        return None
    if la is None:
        return ra
    if ra is None:
        return la
    return (la[0] + ra[0]) / 2.0, (la[1] + ra[1]) / 2.0


def _leg_pose_geometry_for_person(person: dict[str, Any]) -> dict[str, float | None]:
    """下肢姿态几何：上下半身夹角 + 膝角 + 腿长比。"""
    out: dict[str, float | None] = {}
    knee_angles: list[float] = []
    torso_leg_angles: list[float] = []
    leg_span_ratios: list[float] = []
    thigh_calf_ratios: list[float] = []

    for name, sh_idx, hip_idx, ankle_idx in TORSO_LEG_ANGLE_DEFS:
        sh = _read_xy(person, sh_idx)
        hip = _read_xy(person, hip_idx)
        ankle = _read_xy(person, ankle_idx)
        if sh is None or hip is None or ankle is None:
            out[name] = None
            continue
        ang = _angle_at_joint(sh, hip, ankle)
        out[name] = round(ang, 2) if ang is not None else None
        if ang is not None:
            torso_leg_angles.append(float(ang))

    sh_c = _shoulder_center(person)
    hip_c = _hip_center(person)
    anc_c = _ankle_center(person)
    if sh_c is not None and hip_c is not None and anc_c is not None:
        ang = _angle_at_joint(sh_c, hip_c, anc_c)
        out["center_torso_leg_angle"] = round(ang, 2) if ang is not None else None
        if ang is not None:
            torso_leg_angles.append(float(ang))

    if torso_leg_angles:
        out["torso_leg_angle_mean"] = round(sum(torso_leg_angles) / len(torso_leg_angles), 2)
        out["torso_leg_angle_min"] = round(min(torso_leg_angles), 2)
        out["torso_leg_angle_max"] = round(max(torso_leg_angles), 2)

    for name, hip_idx, knee_idx, ankle_idx in KNEE_ANGLE_DEFS:
        hip = _read_xy(person, hip_idx)
        knee = _read_xy(person, knee_idx)
        ankle = _read_xy(person, ankle_idx)
        if hip is None or knee is None or ankle is None:
            out[name] = None
            continue
        ang = _angle_at_joint(hip, knee, ankle)
        out[name] = round(ang, 2) if ang is not None else None
        if ang is not None:
            knee_angles.append(float(ang))

        calf_vert = abs(ankle[1] - knee[1])
        if calf_vert > 1.0:
            thigh_vert = abs(knee[1] - hip[1])
            thigh_calf_ratios.append(thigh_vert / calf_vert)

    sh_c = _shoulder_center(person)
    hip_c = _hip_center(person)
    if sh_c is not None and hip_c is not None:
        torso_vert = abs(hip_c[1] - sh_c[1])
        if torso_vert > 1.0:
            for hip_idx, _knee_idx, ankle_idx in ((11, 13, 15), (12, 14, 16)):
                hip = _read_xy(person, hip_idx)
                ankle = _read_xy(person, ankle_idx)
                if hip is None or ankle is None:
                    continue
                leg_vert = abs(ankle[1] - hip[1])
                leg_span_ratios.append(leg_vert / torso_vert)

    if knee_angles:
        out["knee_angle_mean"] = round(sum(knee_angles) / len(knee_angles), 2)
        out["knee_angle_min"] = round(min(knee_angles), 2)
        out["knee_angle_max"] = round(max(knee_angles), 2)
    if leg_span_ratios:
        out["leg_span_ratio"] = round(sum(leg_span_ratios) / len(leg_span_ratios), 4)
    if thigh_calf_ratios:
        out["hip_knee_ankle_vertical_ratio"] = round(
            sum(thigh_calf_ratios) / len(thigh_calf_ratios), 4
        )
    return out


def _angle_from_downward(dx: float, dy: float) -> float | None:
    """向量相对图像向下 (0,1) 的夹角（度）。

    0°≈手臂下垂，90°≈水平前伸，越大表示越「举起/抬起」偏离自然下垂。
    """
    norm = math.hypot(dx, dy)
    if norm < 1e-6:
        return None
    cos_val = max(-1.0, min(1.0, dy / norm))
    return math.degrees(math.acos(cos_val))


def _orientation_angles_for_person(person: dict[str, Any]) -> dict[str, float | None]:
    """手部朝向与肩肘相对腰身夹角（2D 图像平面）。"""
    out: dict[str, float | None] = {}
    hip_c = _hip_center(person)
    if hip_c is None:
        return out

    side_defs = (
        ("left", 5, 7, 9),
        ("right", 6, 8, 10),
    )
    arm_torso: list[float] = []
    elbow_waist: list[float] = []
    wrist_elev: list[float] = []
    forearm_dir: list[float] = []

    for side, sh_idx, el_idx, wr_idx in side_defs:
        sh = _read_xy(person, sh_idx)
        el = _read_xy(person, el_idx)
        wr = _read_xy(person, wr_idx)

        # 肩-肘相对躯干：∠(髋心, 肩, 肘)
        if sh is not None and el is not None:
            ang = _angle_at_joint(hip_c, sh, el)
            key = f"{side}_arm_torso_angle"
            out[key] = round(ang, 2) if ang is not None else None
            if ang is not None:
                arm_torso.append(ang)

        # 肘相对腰身：∠(肩, 肘, 髋心)
        if sh is not None and el is not None:
            ang = _angle_at_joint(sh, el, hip_c)
            key = f"{side}_elbow_waist_angle"
            out[key] = round(ang, 2) if ang is not None else None
            if ang is not None:
                elbow_waist.append(ang)

        # 手部朝向：肩→腕相对下垂方向抬升角
        if sh is not None and wr is not None:
            dx, dy = wr[0] - sh[0], wr[1] - sh[1]
            ang = _angle_from_downward(dx, dy)
            key = f"{side}_wrist_elevation_angle"
            out[key] = round(ang, 2) if ang is not None else None
            if ang is not None:
                wrist_elev.append(ang)

        # 前臂指向：肘→腕相对下垂方向
        if el is not None and wr is not None:
            dx, dy = wr[0] - el[0], wr[1] - el[1]
            ang = _angle_from_downward(dx, dy)
            key = f"{side}_forearm_direction_angle"
            out[key] = round(ang, 2) if ang is not None else None
            if ang is not None:
                forearm_dir.append(ang)

    if arm_torso:
        out["arm_torso_angle_mean"] = round(sum(arm_torso) / len(arm_torso), 2)
        out["arm_torso_angle_max"] = round(max(arm_torso), 2)
    if elbow_waist:
        out["elbow_waist_angle_mean"] = round(sum(elbow_waist) / len(elbow_waist), 2)
        out["elbow_waist_angle_max"] = round(max(elbow_waist), 2)
    if wrist_elev:
        out["wrist_elevation_angle_mean"] = round(sum(wrist_elev) / len(wrist_elev), 2)
        out["wrist_elevation_angle_max"] = round(max(wrist_elev), 2)
    if forearm_dir:
        out["forearm_direction_angle_mean"] = round(sum(forearm_dir) / len(forearm_dir), 2)
        out["forearm_direction_angle_max"] = round(max(forearm_dir), 2)

    return out


def _angle_at_joint(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
) -> float | None:
    """点 b 处内角（度）。"""
    bax, bay = a[0] - b[0], a[1] - b[1]
    bcx, bcy = c[0] - b[0], c[1] - b[1]
    norm_ba = math.hypot(bax, bay)
    norm_bc = math.hypot(bcx, bcy)
    if norm_ba < 1e-6 or norm_bc < 1e-6:
        return None
    cos_val = (bax * bcx + bay * bcy) / (norm_ba * norm_bc)
    cos_val = max(-1.0, min(1.0, cos_val))
    return math.degrees(math.acos(cos_val))


def _read_xy(person: dict[str, Any], kpt_idx: int) -> tuple[float, float] | None:
    pt = _read_kpt(person, kpt_idx)
    if pt is None:
        return None
    return pt[0], pt[1]


def _joint_angles_for_person(person: dict[str, Any]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for name, ia, ib, ic in ALL_ANGLE_DEFS:
        a = _read_xy(person, ia)
        b = _read_xy(person, ib)
        c = _read_xy(person, ic)
        if a is None or b is None or c is None:
            out[name] = None
            continue
        ang = _angle_at_joint(a, b, c)
        out[name] = round(ang, 2) if ang is not None else None
    return out


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return float(statistics.median(values))


@dataclass
class _AngleBuffer:
    values: list[tuple[float, int, float]] = field(default_factory=list)
    prev_filtered: float | None = None
    prev_meta: _KptState | None = None


def _filtered_angle(buf: _AngleBuffer) -> float | None:
    recent = buf.values[-MEDIAN_FILTER_WINDOW:]
    if not recent:
        return None
    return _median([v[0] for v in recent])


def _angular_speed(
    angle: float,
    prev_angle: float,
    *,
    frame_idx: int,
    ts: float,
    prev: _KptState,
) -> float | None:
    dt = ts - prev.timestamp_sec
    if dt <= 0:
        dt = (frame_idx - prev.frame_idx) / 25.0
    if dt <= 0:
        return None
    # 最短弧差分，避免 179°→1° 跳变
    delta = angle - prev_angle
    while delta > 180.0:
        delta -= 360.0
    while delta < -180.0:
        delta += 360.0
    return abs(delta) / dt


def _mean_of(vals: list[float | None]) -> float | None:
    valid = [float(v) for v in vals if v is not None and math.isfinite(float(v))]
    if not valid:
        return None
    return sum(valid) / len(valid)


def _max_of(vals: list[float | None]) -> float | None:
    valid = [float(v) for v in vals if v is not None and math.isfinite(float(v))]
    if not valid:
        return None
    return max(valid)


def extract_joint_angle_rows(
    frames: list[dict[str, Any]],
    *,
    video_fps: float = 25.0,
) -> list[dict[str, Any]]:
    """每帧 × 每人：肩/肘/腕开合角与角速度聚合。"""
    fps = max(1.0, float(video_fps))
    buffers: dict[tuple[int, str], _AngleBuffer] = {}
    rows: list[dict[str, Any]] = []

    for fr in sorted(frames, key=lambda f: int(f.get("frame_idx") or 0)):
        if not isinstance(fr, dict):
            continue
        frame_idx = int(fr.get("frame_idx") or 0)
        ts = float(fr.get("timestamp_sec") or 0.0)
        if ts <= 0 and frame_idx > 0:
            ts = frame_idx / fps

        for person in fr.get("persons") or []:
            if not isinstance(person, dict):
                continue
            track_id = int(person.get("person_track_id") or 0)
            angles = _joint_angles_for_person(person)
            orient = _orientation_angles_for_person(person)
            leg_pose = _leg_pose_geometry_for_person(person)
            row: dict[str, Any] = {
                "frame_idx": frame_idx,
                "person_track_id": track_id,
            }
            row.update(orient)
            row.update(leg_pose)
            for name, _ia, _ib, _ic in ALL_ANGLE_DEFS:
                row[name] = angles.get(name)
                row[f"{name}_vel"] = None

            shoulder_vels: list[float | None] = []
            elbow_vels: list[float | None] = []
            wrist_vels: list[float | None] = []

            for name, _ia, _ib, _ic in ALL_ANGLE_DEFS:
                ang = angles.get(name)
                key = (track_id, name)
                buf = buffers.setdefault(key, _AngleBuffer())
                if ang is None:
                    continue

                if buf.prev_meta is not None:
                    gap = frame_idx - buf.prev_meta.frame_idx
                    if gap > MAX_VELOCITY_GAP_FRAMES:
                        buf.prev_filtered = None
                        buf.prev_meta = None

                buf.values.append((float(ang), frame_idx, ts))
                filtered = _filtered_angle(buf)
                vel_val: float | None = None
                if filtered is not None and buf.prev_filtered is not None and buf.prev_meta is not None:
                    vel_val = _angular_speed(
                        filtered,
                        buf.prev_filtered,
                        frame_idx=frame_idx,
                        ts=ts,
                        prev=buf.prev_meta,
                    )
                    if vel_val is not None:
                        row[f"{name}_vel"] = round(vel_val, 3)
                        if "shoulder" in name:
                            shoulder_vels.append(vel_val)
                        elif "elbow" in name:
                            elbow_vels.append(vel_val)
                        elif "wrist" in name:
                            wrist_vels.append(vel_val)

                if filtered is not None:
                    buf.prev_filtered = filtered
                    buf.prev_meta = _KptState(
                        frame_idx=frame_idx,
                        timestamp_sec=ts,
                        x=filtered,
                        y=0.0,
                    )

            shoulder_angles = [angles.get(n) for n, *_ in SHOULDER_ANGLE_DEFS]
            elbow_angles = [angles.get(n) for n, *_ in ELBOW_ANGLE_DEFS]
            wrist_angles = [angles.get(n) for n, *_ in WRIST_ANGLE_DEFS]

            shoulder_mean = _mean_of(shoulder_angles)
            elbow_mean = _mean_of(elbow_angles)
            wrist_mean = _mean_of(wrist_angles)
            elbow_min = min((float(v) for v in elbow_angles if v is not None), default=None)
            elbow_max = max((float(v) for v in elbow_angles if v is not None), default=None)

            row["shoulder_angle_mean"] = round(shoulder_mean, 2) if shoulder_mean is not None else None
            row["elbow_angle_mean"] = round(elbow_mean, 2) if elbow_mean is not None else None
            row["wrist_angle_mean"] = round(wrist_mean, 2) if wrist_mean is not None else None
            row["elbow_angle_min"] = round(elbow_min, 2) if elbow_min is not None else None
            row["elbow_angle_max"] = round(elbow_max, 2) if elbow_max is not None else None
            row["arm_extension_mean"] = row["elbow_angle_mean"]
            row["shoulder_angle_vel_max"] = (
                round(_max_of(shoulder_vels), 3) if _max_of(shoulder_vels) is not None else None
            )
            row["elbow_angle_vel_max"] = (
                round(_max_of(elbow_vels), 3) if _max_of(elbow_vels) is not None else None
            )
            row["wrist_angle_vel_max"] = (
                round(_max_of(wrist_vels), 3) if _max_of(wrist_vels) is not None else None
            )
            joint_vels = [row["shoulder_angle_vel_max"], row["elbow_angle_vel_max"], row["wrist_angle_vel_max"]]
            row["joint_open_vel_max"] = round(_max_of(joint_vels), 3) if _max_of(joint_vels) is not None else None
            rows.append(row)

    return rows


def extract_subsampled_joint_angle_from_frames(
    frames: list[dict[str, Any]],
    export_frame_indices: set[int] | list[int],
    *,
    video_fps: float = 25.0,
) -> list[dict[str, Any]]:
    """仅在 export 抽帧上计算关节角（pose_frame_interval 对齐）。"""
    subsampled = filter_frames_to_indices(frames, export_frame_indices)
    if not subsampled:
        return []
    tracked = assign_person_tracks_to_frames(subsampled, video_fps=video_fps)
    return extract_joint_angle_rows(tracked, video_fps=video_fps)
