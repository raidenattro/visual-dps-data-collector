"""评估/导出脚本共用的优质样本筛选与段重叠工具。"""

from __future__ import annotations

from typing import Any

from config_loader import parse_record_path_segments, resolve_app_paths
from event_engine.box_identity import token_matches_any
from record_index_store import list_record_summaries, maybe_sync_record_summaries
from record_tag_store import normalize_tag_name, record_ids_with_all_tags

from api.accuracy_service import GroundTruthSegment
from api.record_service import locate_record_by_id

DEFAULT_CAMERAS = (
    "1-1-1",
    "1-2-1",
    "2-2-2",
    "2-3-1",
    "2-4-1",
    "2-5-1",
    "2-6-1",
    "2-7-2",
)
DEFAULT_TAGS = ("单人", "无遮挡")
DEFAULT_TIER = "rtmpose-m"
DEFAULT_REVIEW_STATUS = "completed"


def parse_csv_list(raw: str) -> list[str]:
    return [p.strip() for p in str(raw or "").split(",") if p.strip()]


def parse_tags(raw: str) -> list[str]:
    out: list[str] = []
    for part in parse_csv_list(raw):
        name = normalize_tag_name(part)
        if name not in out:
            out.append(name)
    return out


def collect_record_ids(
    *,
    tier: str,
    cameras: set[str],
    tags: list[str],
    review_status: str | None = DEFAULT_REVIEW_STATUS,
    has_verified: bool | None = True,
    sync_index: bool = True,
) -> list[str]:
    """与回放列表筛选一致：标签 + 机位 + 复核状态 + 有标真（record_index）。"""
    paths = resolve_app_paths()
    if sync_index:
        maybe_sync_record_summaries(paths, tier or None, force=False, offset=0)

    allowed = record_ids_with_all_tags(tags) if tags else None
    review_filter = str(review_status or "").strip().lower() or None

    items = list_record_summaries(
        pose_tier=tier or None,
        allowed_ids=allowed,
        review_status=review_filter,
        has_verified=has_verified,
    )

    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        rid = str(item.get("record_id") or "").strip()
        if not rid or rid in seen:
            continue
        slug = str(item.get("camera_slug") or "").strip()
        if not slug:
            _, slug, _ = parse_record_path_segments(rid)
        if cameras and slug not in cameras:
            continue
        if not locate_record_by_id(rid):
            continue
        seen.add(rid)
        out.append(rid)
    return sorted(out)


def ranges_overlap(a0: int, a1: int, b0: int, b1: int) -> bool:
    return a0 <= b1 and b0 <= a1


def seg_overlaps_gt(seg: dict[str, Any], gt_segments: list[GroundTruthSegment]) -> bool:
    """手腕碰撞段是否与某条人工标真段在时间与范本货框上重叠。"""
    a = int(seg.get("frame_enter") or 0)
    b = int(seg.get("frame_exit") or 0)
    tok = str(seg.get("box_token") or "").strip()
    if not tok or not gt_segments:
        return False
    for gt in gt_segments:
        if not ranges_overlap(a, b, gt.frame_start, gt.frame_end):
            continue
        if token_matches_any(tok, list(gt.gt_tokens)):
            return True
    return False


def seg_overlaps_false_alarm(
    seg: dict[str, Any],
    false_alarm_by_frame: dict[int, list[str]],
) -> bool:
    """手腕碰撞段是否与误报告警在帧范围 + 货位 token 上重叠。"""
    if not false_alarm_by_frame:
        return False
    a = int(seg.get("frame_enter") or 0)
    b = int(seg.get("frame_exit") or 0)
    tok = str(seg.get("box_token") or "").strip()
    if not tok:
        return False
    for fi in range(a, b + 1):
        if token_matches_any(tok, false_alarm_by_frame.get(fi, ())):
            return True
    return False


def build_false_alarm_by_frame(
    timeline: list[dict[str, Any]],
    gt_segments: list[GroundTruthSegment],
) -> dict[int, list[str]]:
    """误报告警帧：timeline alarm_collisions 未被任何标真段（时间+范本货框）覆盖。"""
    by_frame: dict[int, list[str]] = {}
    for row in timeline:
        fi = int(row.get("frame_idx") or 0)
        for raw in row.get("alarm_collisions") or []:
            token = str(raw).strip()
            if not token:
                continue
            covered = any(
                gt.frame_start <= fi <= gt.frame_end and token_matches_any(token, list(gt.gt_tokens))
                for gt in gt_segments
            )
            if not covered:
                by_frame.setdefault(fi, []).append(token)
    return by_frame
