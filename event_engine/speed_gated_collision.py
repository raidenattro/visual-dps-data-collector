"""前置速度门控碰撞：高速人体跳过手腕进框检测。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import cv2

from event_engine.box_identity import box_collision_token
from event_engine.collision import CollisionProcessor
from event_engine.skeleton_features import AggregateVelocitySnapshot, IncrementalAggregateVelocityTracker


@dataclass
class SpeedGateConfig:
    feature: str = "lower_mean_speed"
    max_threshold: float = 60.0
    fail_open: bool = True
    # 取货时手腕/上肢停留较慢：下肢高速时若上肢也慢则豁免门控（仍做手腕进框检测）
    wrist_exempt_max_threshold: float | None = None
    upper_exempt_max_threshold: float | None = None


def _speed_value(snapshot: AggregateVelocitySnapshot, feature: str) -> float | None:
    if feature == "lower_mean_speed":
        return snapshot.lower_mean_speed
    if feature == "knee_ankle_mean_speed":
        return snapshot.knee_ankle_mean_speed
    if feature == "ankle_mean_speed":
        return snapshot.ankle_mean_speed
    if feature == "ankle_max_speed":
        return snapshot.ankle_max_speed
    if feature == "torso_speed":
        return snapshot.torso_speed
    if feature == "body_mean_speed":
        return snapshot.body_mean_speed
    if feature == "upper_mean_speed":
        return snapshot.upper_mean_speed
    if feature == "wrist_max_speed":
        return snapshot.wrist_max_speed
    return snapshot.lower_mean_speed


def _picking_limb_exempt(snapshot: AggregateVelocitySnapshot, *, config: SpeedGateConfig) -> bool:
    """上肢/手腕低速 → 更像取货停留，豁免下肢高速门控。"""
    wrist_thr = config.wrist_exempt_max_threshold
    if wrist_thr is not None:
        wrist = snapshot.wrist_max_speed
        if wrist is not None and float(wrist) <= float(wrist_thr):
            return True
    upper_thr = config.upper_exempt_max_threshold
    if upper_thr is not None:
        upper = snapshot.upper_mean_speed
        if upper is not None and float(upper) <= float(upper_thr):
            return True
    return False


def speed_gate_blocks(
    snapshot: AggregateVelocitySnapshot,
    *,
    config: SpeedGateConfig,
) -> bool:
    """True 表示应跳过该人手腕碰撞检测。"""
    val = _speed_value(snapshot, config.feature)
    if val is None:
        return not config.fail_open
    try:
        lower_fast = float(val) > float(config.max_threshold)
    except (TypeError, ValueError):
        return not config.fail_open
    if not lower_fast:
        return False
    # 下肢高速：若手腕/肩肘腕平均速度低（取货停留），不 block
    if _picking_limb_exempt(snapshot, config=config):
        return False
    return True


class LocalBaselineCollisionProcessor(CollisionProcessor):
    """本仓库 baseline 重算：无速度门控，cooldown=0 可生效。"""

    def __init__(
        self,
        boxes: list,
        *,
        alarm_min_consecutive_frames: int = 3,
        alarm_cooldown_frames: int = 0,
        video_fps: float = 25.0,
    ):
        super().__init__(
            boxes,
            alarm_min_consecutive_frames=alarm_min_consecutive_frames,
            alarm_cooldown_frames=alarm_cooldown_frames,
            video_fps=video_fps,
        )
        self.alarm_cooldown_frames = max(0, int(alarm_cooldown_frames))


class SpeedGatedCollisionProcessor(CollisionProcessor):
    """在手腕 pointPolygonTest 之前按帧级速度门控。"""

    def __init__(
        self,
        boxes: list,
        *,
        alarm_min_consecutive_frames: int = 3,
        alarm_cooldown_frames: int = 0,
        video_fps: float = 25.0,
        speed_gate: SpeedGateConfig | None = None,
        infer_width: int = 640,
        infer_height: int = 480,
    ):
        super().__init__(
            boxes,
            alarm_min_consecutive_frames=alarm_min_consecutive_frames,
            alarm_cooldown_frames=alarm_cooldown_frames,
            video_fps=video_fps,
        )
        # baseline prod-test 允许 cooldown=0；父类 max(1,…) 会抬升为 1，此处还原
        self.alarm_cooldown_frames = max(0, int(alarm_cooldown_frames))
        self.speed_gate = speed_gate or SpeedGateConfig()
        self._velocity_tracker = IncrementalAggregateVelocityTracker(
            infer_width=infer_width,
            infer_height=infer_height,
            video_fps=video_fps,
        )

    def process(self, pose_frame: dict) -> dict:
        frame_idx = int(pose_frame.get("frame_idx") or pose_frame.get("source_frame_idx") or 0)
        now_ts = frame_idx / self.video_fps if self.video_fps > 0 else 0.0
        persons = pose_frame.get("persons") or pose_frame.get("skeletons") or []

        active_collisions: list[str] = []
        skeletons_data = []
        used_track_ids: set[int] = set()

        for person in persons:
            if not isinstance(person, dict):
                continue
            keypoints = person.get("keypoints") or []
            if len(keypoints) < 11:
                skeletons_data.append(person)
                continue

            def _pt(i: int):
                kp = keypoints[i]
                return float(kp[0]), float(kp[1]), float(kp[2]) if len(kp) > 2 else 0.0

            lx, ly, ls = _pt(5)
            rx, ry, rs = _pt(6)
            if ls > 0.2 and rs > 0.2:
                anchor_x = (lx + rx) / 2.0
                anchor_y = (ly + ry) / 2.0
            else:
                xs = [float(k[0]) for k in keypoints if len(k) >= 2]
                ys = [float(k[1]) for k in keypoints if len(k) >= 2]
                anchor_x = sum(xs) / len(xs) if xs else 0.0
                anchor_y = sum(ys) / len(ys) if ys else 0.0

            person_track_id = self.person_assigner.assign(
                anchor_x, anchor_y, now_ts=now_ts, occupied_track_ids=used_track_ids
            )
            skel = dict(person)
            skel["person_track_id"] = person_track_id
            skeletons_data.append(skel)

            ts_sec = float(pose_frame.get("timestamp_sec") or 0.0)
            snapshot = self._velocity_tracker.update_for_person(
                person_track_id,
                person,
                frame_idx=frame_idx,
                timestamp_sec=ts_sec,
            )
            if speed_gate_blocks(snapshot, config=self.speed_gate):
                continue

            for kpt_idx in (9, 10):
                if len(keypoints) <= kpt_idx:
                    continue
                kp = keypoints[kpt_idx]
                if len(kp) < 3 or float(kp[2]) <= 0.3:
                    continue
                wx, wy = float(kp[0]), float(kp[1])
                for box in self.boxes:
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

        for token in list(self._box_consecutive_hits.keys()):
            if token not in current_tokens:
                self._box_consecutive_hits[token] = 0

        alarm_collisions: list[str] = []
        for token in current_tokens:
            self._box_consecutive_hits[token] = self._box_consecutive_hits.get(token, 0) + 1
            last_alarm = self._box_last_alarm_frame.get(token, -10**9)
            if (
                self._box_consecutive_hits[token] >= self.alarm_min_consecutive_frames
                and frame_idx - last_alarm >= self.alarm_cooldown_frames
            ):
                alarm_collisions.append(token)
                self._box_last_alarm_frame[token] = frame_idx

        return {
            "collisions": active_collisions,
            "alarm_collisions": alarm_collisions,
            "skeletons": skeletons_data,
            "frame_idx": frame_idx,
        }


def build_timeline_frame_index(frames: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """source_frame_idx / frame_idx → 帧字典。"""
    out: dict[int, dict[str, Any]] = {}
    for fr in frames:
        if not isinstance(fr, dict):
            continue
        idx = int(fr.get("source_frame_idx") or fr.get("frame_idx") or 0)
        if idx > 0:
            out[idx] = fr
    return out


def _recompute_upload_frames_with_processor(
    timeline_frames: list[dict[str, Any]],
    export_frame_indices: set[int] | list[int],
    processor: CollisionProcessor,
    *,
    record_id: str,
) -> list[dict[str, Any]]:
    """只读 timeline，用给定 processor 重算并输出 upload clip 行。"""
    wanted = sorted({int(x) for x in export_frame_indices if int(x) > 0})
    if not wanted:
        return []

    by_idx = build_timeline_frame_index(timeline_frames)
    upload_rows: list[dict[str, Any]] = []
    for frame_idx in wanted:
        src = by_idx.get(frame_idx)
        if not src:
            continue
        event = processor.process({
            "frame_idx": frame_idx,
            "timestamp_sec": src.get("timestamp_sec"),
            "persons": src.get("persons") or [],
        })
        collisions = list(event.get("collisions") or [])
        alarm_collisions = list(event.get("alarm_collisions") or [])
        upload_rows.append({
            "record_id": record_id,
            "frame_idx": frame_idx,
            "is_picking": bool(alarm_collisions),
            "picking_prob": None,
            "predicted_box_tokens": [],
            "rule_collisions": collisions,
            "rule_alarm_collisions": alarm_collisions,
        })
    return upload_rows


def recompute_baseline_upload_frames(
    timeline_frames: list[dict[str, Any]],
    export_frame_indices: set[int] | list[int],
    boxes: list[dict[str, Any]],
    *,
    record_id: str,
    infer_width: int,
    infer_height: int,
    video_fps: float = 25.0,
    alarm_min_consecutive_frames: int = 3,
    alarm_cooldown_frames: int = 0,
) -> list[dict[str, Any]]:
    """本仓库 baseline：无速度门控，只读 timeline 内存重算。"""
    processor = LocalBaselineCollisionProcessor(
        boxes,
        alarm_min_consecutive_frames=alarm_min_consecutive_frames,
        alarm_cooldown_frames=alarm_cooldown_frames,
        video_fps=video_fps,
    )
    return _recompute_upload_frames_with_processor(
        timeline_frames,
        export_frame_indices,
        processor,
        record_id=record_id,
    )


def recompute_prefilter_upload_frames(
    timeline_frames: list[dict[str, Any]],
    export_frame_indices: set[int] | list[int],
    boxes: list[dict[str, Any]],
    *,
    record_id: str,
    speed_gate: SpeedGateConfig,
    infer_width: int,
    infer_height: int,
    video_fps: float = 25.0,
    alarm_min_consecutive_frames: int = 3,
    alarm_cooldown_frames: int = 0,
) -> list[dict[str, Any]]:
    """只读 timeline，内存重算前置过滤碰撞，输出 upload clip 行。"""
    processor = SpeedGatedCollisionProcessor(
        boxes,
        alarm_min_consecutive_frames=alarm_min_consecutive_frames,
        alarm_cooldown_frames=alarm_cooldown_frames,
        video_fps=video_fps,
        speed_gate=speed_gate,
        infer_width=infer_width,
        infer_height=infer_height,
    )
    return _recompute_upload_frames_with_processor(
        timeline_frames,
        export_frame_indices,
        processor,
        record_id=record_id,
    )
