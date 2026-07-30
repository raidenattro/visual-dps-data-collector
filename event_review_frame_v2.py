"""Canonical frame-level event review schema and lossless legacy conversion.

Disk schema v2 has exactly one ``verified_true`` entry per frame.  Model
detections are reference data (``detected_*``); human truth exists only in
``bindings``.  A binding associates one person/track with one or more
confirmed boxes.

Legacy schema 1 used the same schema number for several incompatible shapes.
Consequently this module never guesses that ``box_tokens`` means human truth
unless the caller explicitly selects a legacy source profile.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from event_engine.box_identity import canonicalize_box_token_list

EVENT_REVIEW_SCHEMA_V2 = 2

SOURCE_EXPLICIT_CONFIRMED = "explicit-confirmed"
SOURCE_LEGACY_PER_BOX = "legacy-per-box"
SOURCE_LEGACY_FRAME_EVENT = "legacy-frame-event"
LEGACY_SOURCE_FORMATS = {
    SOURCE_EXPLICIT_CONFIRMED,
    SOURCE_LEGACY_PER_BOX,
    SOURCE_LEGACY_FRAME_EVENT,
}


class LegacyReviewMigrationRequired(ValueError):
    """Raised when schema 1 rows cannot be converted without user intent."""


@dataclass
class FrameV2MigrationStats:
    input_count: int = 0
    output_frame_count: int = 0
    binding_count: int = 0
    deduped_bindings: int = 0
    skipped_entries: int = 0
    unresolved_entries: int = 0
    unresolved_frames: list[int] = field(default_factory=list)
    unresolved_items: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # Compatibility fields retained for callers from the integration branch.
    cleared_entries: int = 0
    cleared_frames: list[int] = field(default_factory=list)

    def needs_migration(self) -> bool:
        return self.input_count > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_count": self.input_count,
            "output_frame_count": self.output_frame_count,
            "binding_count": self.binding_count,
            "deduped_bindings": self.deduped_bindings,
            "skipped_entries": self.skipped_entries,
            "unresolved_entries": self.unresolved_entries,
            "unresolved_frames": sorted(set(self.unresolved_frames)),
            "warnings": list(self.warnings),
            # No row is silently cleared in the lossless converter.
            "cleared_entries": 0,
            "cleared_frames": [],
        }


def _int_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _tokens(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return canonicalize_box_token_list(
        [str(item).strip() for item in value if str(item).strip()]
    )


def extract_entry_confirmed_tokens(entry: dict[str, Any]) -> list[str]:
    confirmed = _tokens(entry.get("confirmed_box_tokens"))
    if confirmed:
        return confirmed
    single = str(entry.get("confirmed_box_token") or "").strip()
    return canonicalize_box_token_list([single]) if single else []


def is_frame_v2_entry(entry: dict[str, Any]) -> bool:
    return isinstance(entry, dict) and isinstance(entry.get("bindings"), list)


def is_frame_v2_review(raw: dict[str, Any]) -> bool:
    if not isinstance(raw, dict):
        return False
    try:
        schema = int(raw.get("schema") or 0)
    except (TypeError, ValueError):
        schema = 0
    if schema < EVENT_REVIEW_SCHEMA_V2:
        return False
    verified = raw.get("verified_true")
    return not verified or (
        isinstance(verified, list)
        and all(
            isinstance(item, dict) and is_frame_v2_entry(item)
            for item in verified
        )
    )


def detect_review_format(raw: dict[str, Any]) -> str:
    """Classify a review without deciding ambiguous schema-1 semantics."""
    if is_frame_v2_review(raw):
        return "frame-v2"
    verified = raw.get("verified_true") if isinstance(raw, dict) else None
    if not isinstance(verified, list) or not verified:
        return "empty-legacy"
    rows = [row for row in verified if isinstance(row, dict)]
    if not rows:
        return "invalid-legacy"
    has_confirmed = [bool(extract_entry_confirmed_tokens(row)) for row in rows]
    if all(has_confirmed):
        return SOURCE_EXPLICIT_CONFIRMED
    if any(has_confirmed):
        return "mixed-legacy"
    token_counts = [len(_tokens(row.get("box_tokens"))) for row in rows]
    if all(count == 1 for count in token_counts):
        return "legacy-per-box-candidate"
    if any(count > 1 for count in token_counts):
        return "legacy-frame-event-candidate"
    return "ambiguous-legacy"


def normalize_binding(binding: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(binding, dict):
        return None
    confirmed = _tokens(binding.get("confirmed_box_tokens"))
    if not confirmed:
        return None
    out: dict[str, Any] = {"confirmed_box_tokens": confirmed}
    person_id = _int_or_none(binding.get("person_id"))
    if person_id is not None:
        out["person_id"] = person_id
    track_id = binding.get("person_track_id")
    if track_id is None:
        track_id = binding.get("track_id")
    if track_id is not None and str(track_id).strip():
        out["person_track_id"] = str(track_id).strip()
    return out


def binding_identity(binding: dict[str, Any]) -> tuple[Any, ...]:
    """Stable upsert identity; anonymous bindings fall back to their boxes."""
    norm = normalize_binding(binding)
    if not norm:
        return ("invalid",)
    if norm.get("person_track_id") is not None:
        return ("track", str(norm["person_track_id"]))
    if norm.get("person_id") is not None:
        return ("person", int(norm["person_id"]))
    return ("anonymous", tuple(norm["confirmed_box_tokens"]))


def _binding_dedupe_key(binding: dict[str, Any]) -> tuple[Any, ...]:
    norm = normalize_binding(binding)
    if not norm:
        return ("invalid",)
    return (
        norm.get("person_id"),
        norm.get("person_track_id"),
        tuple(norm["confirmed_box_tokens"]),
    )


def upsert_binding(
    bindings: Iterable[dict[str, Any]],
    binding: dict[str, Any],
) -> list[dict[str, Any]]:
    """Replace the same person/track binding while preserving other people."""
    incoming = normalize_binding(binding)
    if not incoming:
        return [
            norm
            for item in bindings
            if (norm := normalize_binding(item if isinstance(item, dict) else {}))
        ]
    identity = binding_identity(incoming)
    incoming_track = incoming.get("person_track_id")
    incoming_person = incoming.get("person_id")
    out: list[dict[str, Any]] = []
    replaced = False
    for item in bindings:
        norm = normalize_binding(item if isinstance(item, dict) else {})
        if not norm:
            continue
        same_track = (
            incoming_track is not None
            and norm.get("person_track_id") is not None
            and str(norm["person_track_id"]) == str(incoming_track)
        )
        same_person = (
            incoming_person is not None
            and norm.get("person_id") is not None
            and int(norm["person_id"]) == int(incoming_person)
        )
        if same_track or same_person or binding_identity(norm) == identity:
            if not replaced:
                out.append(incoming)
                replaced = True
            continue
        out.append(norm)
    if not replaced:
        # A legacy anonymous singleton represents the current person.  Replace
        # it when the user now supplies an explicit person identity.
        if identity[0] in {"person", "track"} and len(out) == 1:
            existing_identity = binding_identity(out[0])
            if existing_identity[0] == "anonymous":
                out = [incoming]
                replaced = True
        if not replaced:
            out.append(incoming)
    return normalize_bindings(out)


def normalize_bindings(bindings: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for binding in bindings:
        norm = normalize_binding(binding if isinstance(binding, dict) else {})
        if not norm:
            continue
        key = _binding_dedupe_key(norm)
        if key in seen:
            continue
        seen.add(key)
        out.append(norm)
    return out


def legacy_entry_to_binding(
    entry: dict[str, Any],
    *,
    source_format: str = SOURCE_EXPLICIT_CONFIRMED,
) -> tuple[dict[str, Any] | None, str | None]:
    """Convert one legacy row according to an explicit source profile."""
    if source_format not in LEGACY_SOURCE_FORMATS:
        return None, f"未知 source_format: {source_format}"
    if not isinstance(entry, dict):
        return None, "条目不是对象"

    confirmed = extract_entry_confirmed_tokens(entry)
    detected = _tokens(entry.get("box_tokens"))
    if not confirmed:
        if source_format == SOURCE_LEGACY_PER_BOX:
            if len(detected) != 1:
                return None, "逐货框格式要求 box_tokens 恰好一个"
            confirmed = detected
        elif source_format == SOURCE_LEGACY_FRAME_EVENT:
            if not detected:
                return None, "旧逐帧格式缺少 box_tokens"
            confirmed = detected
        else:
            return None, "缺少 confirmed_box_tokens，不能猜测 box_tokens 是人工标真"

    binding: dict[str, Any] = {"confirmed_box_tokens": confirmed}
    person_id = _int_or_none(entry.get("person_id"))
    if person_id is not None:
        binding["person_id"] = person_id
    track_id = entry.get("person_track_id")
    if track_id is None:
        track_id = entry.get("track_id")
    if track_id is not None and str(track_id).strip():
        binding["person_track_id"] = str(track_id).strip()
    return binding, None


def _timeline_detection(
    row: dict[str, Any] | None,
) -> tuple[list[str], list[str]]:
    if not isinstance(row, dict):
        return [], []
    alarms = _tokens(row.get("alarm_collisions"))
    collisions = _tokens(row.get("collisions"))
    types: list[str] = []
    if alarms:
        types.append("alarm")
    if collisions:
        types.append("collision")
    return types, canonicalize_box_token_list([*alarms, *collisions])


def normalize_frame_v2_entry(
    entry: dict[str, Any],
    *,
    timeline_row: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    frame_idx = _int_or_none(entry.get("frame_idx"))
    if frame_idx is None:
        return None
    source_frame_idx = _int_or_none(entry.get("source_frame_idx"))
    if source_frame_idx is None:
        source_frame_idx = frame_idx
    bindings = normalize_bindings(entry.get("bindings") or [])
    if not bindings:
        return None

    timeline_types, timeline_tokens = _timeline_detection(timeline_row)
    detected_types = [
        str(item).strip()
        for item in (entry.get("detected_event_types") or [])
        if str(item).strip() in {"alarm", "collision"}
    ]
    legacy_type = str(entry.get("event_type") or "").strip()
    if legacy_type in {"alarm", "collision"} and legacy_type not in detected_types:
        detected_types.append(legacy_type)
    if timeline_types:
        detected_types = timeline_types

    detected_tokens = _tokens(entry.get("detected_box_tokens"))
    if not detected_tokens:
        detected_tokens = _tokens(entry.get("box_tokens"))
    if timeline_tokens:
        detected_tokens = timeline_tokens

    return {
        "frame_idx": frame_idx,
        "source_frame_idx": source_frame_idx,
        "detected_event_types": list(dict.fromkeys(detected_types)),
        "detected_box_tokens": detected_tokens,
        "bindings": bindings,
    }


def build_frame_v2_entry(
    frame_idx: int,
    bindings: list[dict[str, Any]],
    *,
    source_frame_idx: int | None = None,
    timeline_row: dict[str, Any] | None = None,
    legacy_event_type: str = "",
    detected_box_tokens: list[str] | None = None,
    detected_event_types: list[str] | None = None,
) -> dict[str, Any] | None:
    raw = {
        "frame_idx": frame_idx,
        "source_frame_idx": source_frame_idx if source_frame_idx is not None else frame_idx,
        "detected_event_types": detected_event_types or [],
        "detected_box_tokens": detected_box_tokens or [],
        "event_type": legacy_event_type,
        "bindings": bindings,
    }
    return normalize_frame_v2_entry(raw, timeline_row=timeline_row)


def migrate_verified_true_to_frame_v2(
    verified_true: list[Any],
    *,
    timeline_by_frame: dict[int, dict[str, Any]] | None = None,
    source_format: str = SOURCE_EXPLICIT_CONFIRMED,
) -> tuple[list[dict[str, Any]], FrameV2MigrationStats]:
    """Convert rows without discarding unresolved legacy data.

    Unresolved rows are returned in ``stats.unresolved_items`` for the caller
    to preserve in ``unresolved_legacy`` or an audit artifact.
    """
    stats = FrameV2MigrationStats(input_count=len(verified_true or []))
    if not verified_true:
        return [], stats
    if source_format not in LEGACY_SOURCE_FORMATS:
        raise ValueError(f"不支持的 source_format: {source_format}")

    timeline = timeline_by_frame or {}
    buckets: dict[int, dict[str, Any]] = {}

    for raw_item in verified_true:
        if not isinstance(raw_item, dict):
            stats.skipped_entries += 1
            stats.unresolved_entries += 1
            stats.warnings.append("非对象条目未转换")
            continue

        frame_idx = _int_or_none(raw_item.get("frame_idx"))
        if frame_idx is None:
            stats.skipped_entries += 1
            stats.unresolved_entries += 1
            stats.unresolved_items.append(dict(raw_item))
            stats.warnings.append("缺少有效 frame_idx 的条目未转换")
            continue
        source_frame_idx = _int_or_none(raw_item.get("source_frame_idx"))
        if source_frame_idx is None:
            source_frame_idx = frame_idx
        bucket = buckets.setdefault(
            frame_idx,
            {
                "source_frame_idx": source_frame_idx,
                "detected_event_types": [],
                "detected_box_tokens": [],
                "bindings": [],
            },
        )
        bucket["source_frame_idx"] = min(bucket["source_frame_idx"], source_frame_idx)

        if is_frame_v2_entry(raw_item):
            normalized = normalize_frame_v2_entry(
                raw_item,
                timeline_row=timeline.get(frame_idx),
            )
            if not normalized:
                stats.unresolved_entries += 1
                stats.unresolved_frames.append(frame_idx)
                stats.unresolved_items.append(dict(raw_item))
                stats.warnings.append(f"帧 {frame_idx}: v2 条目无有效 binding")
                continue
            bucket["detected_event_types"].extend(
                normalized.get("detected_event_types") or []
            )
            bucket["detected_box_tokens"].extend(
                normalized.get("detected_box_tokens") or []
            )
            bucket["bindings"].extend(normalized.get("bindings") or [])
            continue

        event_type = str(raw_item.get("event_type") or "").strip()
        if event_type in {"alarm", "collision"}:
            bucket["detected_event_types"].append(event_type)
        bucket["detected_box_tokens"].extend(_tokens(raw_item.get("box_tokens")))

        binding, warning = legacy_entry_to_binding(
            raw_item,
            source_format=source_format,
        )
        if not binding:
            stats.unresolved_entries += 1
            stats.unresolved_frames.append(frame_idx)
            stats.unresolved_items.append(dict(raw_item))
            stats.warnings.append(f"帧 {frame_idx}: {warning}")
            continue
        bucket["bindings"].append(binding)

    out: list[dict[str, Any]] = []
    for frame_idx in sorted(buckets):
        bucket = buckets[frame_idx]
        raw_bindings = list(bucket["bindings"])
        normalized_bindings = normalize_bindings(raw_bindings)
        stats.deduped_bindings += max(0, len(raw_bindings) - len(normalized_bindings))
        if not normalized_bindings:
            continue
        entry = build_frame_v2_entry(
            frame_idx,
            normalized_bindings,
            source_frame_idx=bucket["source_frame_idx"],
            timeline_row=timeline.get(frame_idx),
            detected_box_tokens=canonicalize_box_token_list(
                bucket["detected_box_tokens"]
            ),
            detected_event_types=list(dict.fromkeys(bucket["detected_event_types"])),
        )
        if entry:
            out.append(entry)
            stats.binding_count += len(entry["bindings"])

    stats.output_frame_count = len(out)
    return out, stats


def verify_frame_v2_verified_true(verified_true: list[Any]) -> list[str]:
    issues: list[str] = []
    seen_frames: set[int] = set()
    for index, raw_item in enumerate(verified_true or []):
        if not isinstance(raw_item, dict):
            issues.append(f"[{index}] 条目不是对象")
            continue
        if not is_frame_v2_entry(raw_item):
            issues.append(f"[{index}] 缺少 bindings")
            continue
        frame_idx = _int_or_none(raw_item.get("frame_idx"))
        if frame_idx is None:
            issues.append(f"[{index}] frame_idx 无效")
            continue
        if frame_idx in seen_frames:
            issues.append(f"帧 {frame_idx}: 出现多条帧记录")
        seen_frames.add(frame_idx)
        normalized = normalize_frame_v2_entry(raw_item)
        if not normalized:
            issues.append(f"帧 {frame_idx}: 无有效 binding")
            continue
        if len(normalized["bindings"]) != len(raw_item.get("bindings") or []):
            issues.append(f"帧 {frame_idx}: 存在无效或重复 binding")
    return issues


def _compat_event_type(entry: dict[str, Any]) -> str:
    types = entry.get("detected_event_types") or []
    if "alarm" in types:
        return "alarm"
    if "collision" in types:
        return "collision"
    return "frame"


def frame_v2_entry_to_playback_row(entry: dict[str, Any]) -> dict[str, Any] | None:
    normalized = normalize_frame_v2_entry(entry)
    if not normalized:
        return None
    bindings = normalized["bindings"]
    confirmed = canonicalize_box_token_list(
        [
            token
            for binding in bindings
            for token in binding.get("confirmed_box_tokens") or []
        ]
    )
    row: dict[str, Any] = {
        "event_type": _compat_event_type(normalized),
        "frame_idx": normalized["frame_idx"],
        "source_frame_idx": normalized["source_frame_idx"],
        "box_tokens": list(normalized["detected_box_tokens"]),
        "confirmed_box_tokens": confirmed,
        "bindings": [dict(binding) for binding in bindings],
    }
    if len(bindings) == 1:
        binding = bindings[0]
        if binding.get("person_id") is not None:
            row["person_id"] = binding["person_id"]
        if binding.get("person_track_id") is not None:
            row["person_track_id"] = binding["person_track_id"]
    return row


def flatten_frame_v2_to_legacy_shape(entry: dict[str, Any]) -> list[dict[str, Any]]:
    normalized = normalize_frame_v2_entry(entry)
    if not normalized:
        return []
    event_type = _compat_event_type(normalized)
    out: list[dict[str, Any]] = []
    for binding in normalized["bindings"]:
        row: dict[str, Any] = {
            "event_type": event_type,
            "frame_idx": normalized["frame_idx"],
            "source_frame_idx": normalized["source_frame_idx"],
            "box_tokens": list(normalized["detected_box_tokens"]),
            "confirmed_box_tokens": list(binding["confirmed_box_tokens"]),
        }
        if binding.get("person_id") is not None:
            row["person_id"] = binding["person_id"]
        if binding.get("person_track_id") is not None:
            row["person_track_id"] = binding["person_track_id"]
        out.append(row)
    return out


def review_ground_truth_rows(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one explicit human-truth row per binding for downstream tools."""
    if not isinstance(raw, dict):
        return []
    if is_frame_v2_review(raw):
        return [
            row
            for entry in raw.get("verified_true") or []
            if isinstance(entry, dict)
            for row in flatten_frame_v2_to_legacy_shape(entry)
        ]
    rows: list[dict[str, Any]] = []
    for entry in raw.get("verified_true") or []:
        if not isinstance(entry, dict):
            continue
        binding, _ = legacy_entry_to_binding(
            entry,
            source_format=SOURCE_EXPLICIT_CONFIRMED,
        )
        if not binding:
            continue
        row = dict(entry)
        row["confirmed_box_tokens"] = list(binding["confirmed_box_tokens"])
        if binding.get("person_id") is not None:
            row["person_id"] = binding["person_id"]
        rows.append(row)
    return rows


def load_verified_items_for_write(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Load canonical frames and reject ambiguous legacy rows before a PATCH."""
    if not isinstance(raw, dict):
        return []
    verified = raw.get("verified_true")
    if not isinstance(verified, list) or not verified:
        return []
    frames, stats = migrate_verified_true_to_frame_v2(
        verified,
        source_format=SOURCE_EXPLICIT_CONFIRMED,
    )
    if stats.unresolved_entries:
        raise LegacyReviewMigrationRequired(
            f"旧复核包含 {stats.unresolved_entries} 条无 confirmed_box_tokens 的歧义记录；"
            "请先用明确的 --source-format 生成迁移候选，当前写入已拒绝以防覆盖"
        )
    return frames
