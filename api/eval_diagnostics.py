"""评估诊断：完整漏报/误报列表与回放 overlay 数据。"""

from __future__ import annotations

from typing import Any


def _format_tokens(tokens: list[str]) -> str:
    items = [str(t).strip() for t in tokens if str(t).strip()]
    return ", ".join(items) if items else "—"


def build_playback_overlay(
    segment_details: list[dict[str, Any]],
    alarms: list[tuple[int, str]],
    *,
    collisions: list[tuple[int, str]] | None = None,
    source_label: str = "",
    collision_count: int = 0,
    verified_count: int = 0,
    missed_segments: int | None = None,
    false_alarms: int | None = None,
) -> dict[str, Any]:
    """生成回放页可用的 overlay（segments + 评估碰撞/告警 + 统计）。"""
    segments: list[dict[str, Any]] = []
    for seg in segment_details:
        tokens = list(seg.get("gt_tokens") or [])
        segments.append({
            "frame_start": int(seg.get("frame_start") or 0),
            "frame_end": int(seg.get("frame_end") or 0),
            "tokens": tokens,
            "detected": bool(seg.get("detected")),
        })
    alarm_rows: list[list[Any]] = []
    for frame, token in alarms:
        fi = int(frame or 0)
        text = str(token or "").strip()
        if fi > 0:
            alarm_rows.append([fi, text])
    collision_rows: list[list[Any]] = []
    for frame, token in collisions or []:
        fi = int(frame or 0)
        text = str(token or "").strip()
        if fi > 0 and text:
            collision_rows.append([fi, text])
    missed = (
        int(missed_segments)
        if missed_segments is not None
        else sum(1 for seg in segments if not seg["detected"])
    )
    false_n = int(false_alarms) if false_alarms is not None else 0
    return {
        "segments": segments,
        "alarms": alarm_rows,
        "collisions": collision_rows,
        "source_label": str(source_label or "").strip(),
        "counts": {
            "alarms": len(alarm_rows),
            "collisions": max(
                0,
                int(collision_count or 0) if collision_count else len(collision_rows),
            ),
            "verified": max(0, int(verified_count or 0)),
            "missed_segments": missed,
            "false_alarms": false_n,
        },
    }


def build_clip_diagnostics(
    segments: list[Any],
    alarms: list[tuple[int, str]],
    metrics: dict[str, Any],
    *,
    collisions: list[tuple[int, str]] | None = None,
    source_label: str = "",
    collision_count: int = 0,
    verified_count: int = 0,
) -> dict[str, Any]:
    """完整 FN/FP 诊断（无截断）。"""
    from api.accuracy_service import _alarm_covered_by_segment

    missed_segments: list[dict[str, Any]] = []
    detected_segments: list[dict[str, Any]] = []

    for seg_dict in metrics.get("segment_details") or []:
        if not isinstance(seg_dict, dict):
            continue
        gt_tokens = list(seg_dict.get("gt_tokens") or [])
        frame_start = int(seg_dict.get("frame_start") or 0)
        frame_end = int(seg_dict.get("frame_end") or 0)
        detected = bool(seg_dict.get("detected"))
        base = {
            "frame_start": frame_start,
            "frame_end": frame_end,
            "gt_tokens": gt_tokens,
            "seek_frame": frame_start,
        }
        token_label = _format_tokens(gt_tokens)
        if detected:
            detected_segments.append({
                **base,
                "kind": "tp",
                "label": f"检出 · {token_label} · 帧 {frame_start}–{frame_end}",
            })
        else:
            missed_segments.append({
                **base,
                "kind": "fn",
                "label": f"漏报 · {token_label} · 帧 {frame_start}–{frame_end}",
            })

    false_alarms: list[dict[str, Any]] = []
    for frame, token in alarms:
        fi = int(frame or 0)
        token_text = str(token or "").strip()
        if token_text:
            covered = any(_alarm_covered_by_segment(fi, token_text, seg) for seg in segments)
        else:
            covered = False
        if not covered:
            false_alarms.append({
                "kind": "fp",
                "frame_idx": fi,
                "box_token": token_text or "(无货框)",
                "seek_frame": fi,
                "label": f"误报 · {token_text or '无货框'} · 帧 {fi}",
            })

    overlay = build_playback_overlay(
        metrics.get("segment_details") or [],
        alarms,
        collisions=collisions,
        source_label=source_label,
        collision_count=collision_count,
        verified_count=verified_count,
        missed_segments=len(missed_segments),
        false_alarms=len(false_alarms),
    )
    return {
        "missed_segments": missed_segments,
        "false_alarms": false_alarms,
        "detected_segments": detected_segments,
        "playback_overlay": overlay,
        "counts": {
            "missed_segments": len(missed_segments),
            "false_alarms": len(false_alarms),
            "detected_segments": len(detected_segments),
        },
    }


def clip_summary_from_full(clip: dict[str, Any]) -> dict[str, Any]:
    """列表展示用摘要（去掉大体量 diagnostics 明细）。"""
    out = dict(clip)
    diag = out.pop("diagnostics", None)
    if isinstance(diag, dict):
        counts = diag.get("counts") or {}
        out["diagnostics_counts"] = {
            "missed_segments": counts.get("missed_segments", len(diag.get("missed_segments") or [])),
            "false_alarms": counts.get("false_alarms", len(diag.get("false_alarms") or [])),
        }
    return out
