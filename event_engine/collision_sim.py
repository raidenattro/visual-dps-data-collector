"""碰撞/告警内存重算：pose_frame_interval 抽帧与参数化探针。"""

from __future__ import annotations

from typing import Any

from event_engine.box_identity import canonical_box_token
from event_engine.collision import CollisionProcessor
from event_engine.wrist_hits import DEFAULT_EXTENSION_RATIO, ProbeMode


def stored_pose_frame_interval(manifest: dict[str, Any]) -> int:
    """读取记录采集时的 pose_frame_interval（默认 1）。"""
    collect_cfg = manifest.get("collect_config")
    if isinstance(collect_cfg, dict):
        raw = collect_cfg.get("pose_frame_interval")
        if raw is not None:
            try:
                interval = int(raw)
                if interval > 0:
                    return interval
            except (TypeError, ValueError):
                pass
    for key in ("frame_interval", "pose_frame_interval"):
        raw = manifest.get(key)
        if raw is not None:
            try:
                interval = int(raw)
                if interval > 0:
                    return interval
            except (TypeError, ValueError):
                pass
    return 1


def filter_pose_inference_frames(
    frames: list[dict[str, Any]],
    target_interval: int,
    *,
    stored_interval: int | None = None,
) -> list[dict[str, Any]]:
    """模拟现场 pose_frame_interval：仅保留应对齐推理的源帧。

    与 collect_core 一致：源帧 read_idx 从 1 起，保留 (read_idx - 1) % interval == 0。
    若 skeleton 采集间隔已与 target 相同，则不再二次抽帧。
    """
    target = max(1, int(target_interval))
    stored = max(1, int(stored_interval or 1))
    valid = [fr for fr in frames if isinstance(fr, dict)]
    if target == stored:
        return valid
    if target <= 1:
        return valid

    out: list[dict[str, Any]] = []
    for fr in valid:
        sfi = int(fr.get("source_frame_idx") or fr.get("frame_idx") or 0)
        if sfi <= 0:
            continue
        if (sfi - 1) % target == 0:
            out.append(fr)
    return out


def simulate_alarms_from_frames(
    frames: list[dict[str, Any]],
    boxes: list[dict[str, Any]],
    *,
    alarm_min_consecutive_frames: int = 3,
    alarm_cooldown_frames: int = 0,
    video_fps: float = 15.0,
    probe_mode: ProbeMode = "wrist",
    extension_ratio: float = DEFAULT_EXTENSION_RATIO,
    fallback_to_wrist: bool = True,
) -> list[tuple[int, str]]:
    """内存重算告警列表 [(source_frame_idx, box_token), ...]。"""
    events = simulate_frame_events_from_frames(
        frames,
        boxes,
        alarm_min_consecutive_frames=alarm_min_consecutive_frames,
        alarm_cooldown_frames=alarm_cooldown_frames,
        video_fps=video_fps,
        probe_mode=probe_mode,
        extension_ratio=extension_ratio,
        fallback_to_wrist=fallback_to_wrist,
    )
    out: list[tuple[int, str]] = []
    for row in events:
        fi = int(row.get("frame_idx") or 0)
        for raw in row.get("alarm_collisions") or []:
            token = canonical_box_token(str(raw).strip())
            if token:
                out.append((fi, token))
    return out


def simulate_frame_events_from_frames(
    frames: list[dict[str, Any]],
    boxes: list[dict[str, Any]],
    *,
    alarm_min_consecutive_frames: int = 3,
    alarm_cooldown_frames: int = 0,
    video_fps: float = 15.0,
    probe_mode: ProbeMode = "wrist",
    extension_ratio: float = DEFAULT_EXTENSION_RATIO,
    fallback_to_wrist: bool = True,
) -> list[dict[str, Any]]:
    """逐帧重算碰撞/告警，返回 [{frame_idx, collisions, alarm_collisions}, ...]。"""
    processor = CollisionProcessor(
        boxes,
        alarm_min_consecutive_frames=max(1, int(alarm_min_consecutive_frames)),
        alarm_cooldown_frames=max(0, int(alarm_cooldown_frames)),
        video_fps=video_fps,
        probe_mode=probe_mode,
        extension_ratio=extension_ratio,
        fallback_to_wrist=fallback_to_wrist,
    )

    out: list[dict[str, Any]] = []
    for fr in frames:
        if not isinstance(fr, dict):
            continue
        idx = int(fr.get("source_frame_idx") or fr.get("frame_idx") or 0)
        event = processor.process({"frame_idx": idx, "persons": fr.get("persons") or []})
        collisions = [
            canonical_box_token(str(t).strip())
            for t in (event.get("collisions") or [])
            if canonical_box_token(str(t).strip())
        ]
        alarms = [
            canonical_box_token(str(t).strip())
            for t in (event.get("alarm_collisions") or [])
            if canonical_box_token(str(t).strip())
        ]
        out.append({
            "frame_idx": idx,
            "collisions": collisions,
            "alarm_collisions": alarms,
        })
    return out
