"""旧版 schema-1 单货框拆分工具。

此模块只保留给历史迁移脚本使用，不属于运行时 schema-v2 写入路径。混合
schema-1 文件不要直接调用本模块；应使用
``scripts/data/migrate_event_review_to_frame_v2.py`` 并明确指定源格式。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from event_engine.box_identity import canonical_box_token
from pose_store import (
    RecordLocator,
    event_signature,
    extract_confirmed_box_tokens,
    normalize_review_entry,
)


@dataclass
class SplitVerifiedTrueStats:
    """split_verified_true_list 统计。"""

    input_count: int = 0
    output_count: int = 0
    split_entries: int = 0
    unchanged_entries: int = 0
    person_inferred: int = 0
    person_ambiguous: int = 0
    invalid_entries: int = 0
    duplicate_signatures_dropped: int = 0
    ambiguous_frames: list[int] = field(default_factory=list)

    def needs_migration(self) -> bool:
        return self.split_entries > 0 or self.duplicate_signatures_dropped > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_count": self.input_count,
            "output_count": self.output_count,
            "split_entries": self.split_entries,
            "unchanged_entries": self.unchanged_entries,
            "person_inferred": self.person_inferred,
            "person_ambiguous": self.person_ambiguous,
            "invalid_entries": self.invalid_entries,
            "duplicate_signatures_dropped": self.duplicate_signatures_dropped,
            "ambiguous_frames": list(self.ambiguous_frames),
        }


def boxes_to_split_from_entry(norm: dict[str, Any]) -> list[str]:
    """返回旧版拆分目标；该函数会读取检测 box_tokens，不能推断人工真值。"""
    confirmed = extract_confirmed_box_tokens(norm)
    if len(confirmed) > 1:
        return confirmed
    tokens = list(norm.get("box_tokens") or [])
    return tokens


def is_multi_box_review_entry(entry: dict[str, Any]) -> bool:
    norm = normalize_review_entry(entry if isinstance(entry, dict) else {})
    if not norm:
        return False
    return len(boxes_to_split_from_entry(norm)) > 1


def split_review_entry_by_box(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """将一条 verified_true 拆为单 box 条目；已是单 box 则原样返回。"""
    norm = normalize_review_entry(entry if isinstance(entry, dict) else {})
    if not norm:
        return []

    boxes = boxes_to_split_from_entry(norm)
    if len(boxes) <= 1:
        return [norm]

    person_id = norm.get("person_id")
    out: list[dict[str, Any]] = []
    for token in boxes:
        row: dict[str, Any] = {
            "event_type": norm["event_type"],
            "frame_idx": norm["frame_idx"],
            "source_frame_idx": norm.get("source_frame_idx", norm["frame_idx"]),
            "box_tokens": [token],
            "confirmed_box_tokens": [token],
        }
        if person_id is not None:
            row["person_id"] = person_id
        out.append(row)
    return out


def infer_person_id_for_box_at_frame(
    locator: RecordLocator,
    *,
    frame_idx: int,
    box_token: str,
) -> int | None:
    """用该帧骨架 + 标注货框推断 person_id；无法唯一确定时返回 None。"""
    from event_engine.wrist_hits import person_wrist_hits
    from pose_store import load_frames_range, load_manifest

    from api.wrist_features_service import _infer_size_from_frames, _load_boxes_for_wrist_features

    target = canonical_box_token(str(box_token or "").strip())
    if not target:
        return None

    manifest = load_manifest(locator)
    frames = load_frames_range(locator, from_frame_idx=int(frame_idx), to_frame_idx=int(frame_idx))
    if not frames:
        return None
    frame = frames[0]
    persons = [p for p in (frame.get("persons") or []) if isinstance(p, dict)]
    if not persons:
        return None

    infer_w, infer_h = _infer_size_from_frames(frames, manifest)
    boxes, _, _ = _load_boxes_for_wrist_features(
        locator,
        manifest,
        infer_w=infer_w,
        infer_h=infer_h,
    )
    if not boxes:
        return None

    matched: list[int] = []
    for idx, person in enumerate(persons):
        try:
            pid = int(person.get("person_id") if person.get("person_id") is not None else idx)
        except (TypeError, ValueError):
            pid = idx
        for hit in person_wrist_hits(person, boxes):
            if canonical_box_token(str(hit.get("token") or "")) == target:
                matched.append(pid)
                break

    unique = sorted(set(matched))
    if len(unique) == 1:
        return unique[0]
    return None


def _apply_person_inference(
    rows: list[dict[str, Any]],
    *,
    locator: RecordLocator | None,
    stats: SplitVerifiedTrueStats,
    original_had_person: bool,
) -> None:
    if locator is None:
        return
    for row in rows:
        token = (row.get("box_tokens") or [None])[0]
        frame_idx = int(row.get("frame_idx") or 0)
        inferred = infer_person_id_for_box_at_frame(
            locator,
            frame_idx=frame_idx,
            box_token=str(token or ""),
        )
        if inferred is not None:
            row["person_id"] = inferred
            stats.person_inferred += 1
        elif original_had_person:
            row.pop("person_id", None)
            stats.person_ambiguous += 1
            if frame_idx not in stats.ambiguous_frames:
                stats.ambiguous_frames.append(frame_idx)


def split_verified_true_list(
    entries: list[Any],
    *,
    infer_person: bool = False,
    locator: RecordLocator | None = None,
) -> tuple[list[dict[str, Any]], SplitVerifiedTrueStats]:
    """拆分 verified_true 并按 event_signature 去重。"""
    stats = SplitVerifiedTrueStats()
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()

    for raw in entries or []:
        stats.input_count += 1
        if not isinstance(raw, dict):
            stats.invalid_entries += 1
            continue

        norm_before = normalize_review_entry(raw)
        if not norm_before:
            stats.invalid_entries += 1
            continue

        boxes = boxes_to_split_from_entry(norm_before)
        if len(boxes) > 1:
            stats.split_entries += 1
            rows = split_review_entry_by_box(raw)
            original_had_person = norm_before.get("person_id") is not None
            if infer_person:
                _apply_person_inference(
                    rows,
                    locator=locator,
                    stats=stats,
                    original_had_person=original_had_person,
                )
        else:
            stats.unchanged_entries += 1
            rows = [norm_before]

        for row in rows:
            sig = event_signature(
                str(row.get("event_type") or ""),
                int(row.get("frame_idx") or 0),
                row.get("box_tokens"),
            )
            if sig in seen:
                stats.duplicate_signatures_dropped += 1
                continue
            seen.add(sig)
            merged.append(row)

    merged.sort(
        key=lambda e: (
            int(e.get("frame_idx") or 0),
            str(e.get("event_type") or ""),
            ",".join(e.get("box_tokens") or []),
        )
    )
    stats.output_count = len(merged)
    return merged, stats


def verify_single_box_verified_true(entries: list[Any]) -> list[str]:
    """校验 verified_true 是否均为单 box；返回问题描述列表。"""
    issues: list[str] = []
    seen: set[str] = set()

    for idx, raw in enumerate(entries or []):
        if not isinstance(raw, dict):
            issues.append(f"条目 #{idx} 非对象")
            continue
        norm = normalize_review_entry(raw)
        if not norm:
            issues.append(f"条目 #{idx} 无法规范化")
            continue

        box_tokens = list(norm.get("box_tokens") or [])
        if len(box_tokens) != 1:
            issues.append(
                f"条目 #{idx} box_tokens 数量={len(box_tokens)} "
                f"({norm.get('event_type')}:{norm.get('frame_idx')})"
            )

        confirmed = extract_confirmed_box_tokens(norm)
        if confirmed and len(confirmed) != 1:
            issues.append(
                f"条目 #{idx} confirmed_box_tokens 数量={len(confirmed)} "
                f"({norm.get('event_type')}:{norm.get('frame_idx')})"
            )
        if confirmed and box_tokens and confirmed[0] != box_tokens[0]:
            issues.append(
                f"条目 #{idx} confirmed 与 box_tokens 不一致 "
                f"({norm.get('event_type')}:{norm.get('frame_idx')})"
            )

        sig = event_signature(
            str(norm.get("event_type") or ""),
            int(norm.get("frame_idx") or 0),
            norm.get("box_tokens"),
        )
        if sig in seen:
            issues.append(f"重复签名: {sig}")
        seen.add(sig)

    return issues
