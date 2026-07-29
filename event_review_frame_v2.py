"""event_review schema v2：逐帧 verified_true + bindings（person ↔ confirmed box）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from event_engine.box_identity import canonicalize_box_token_list
from pose_store import extract_confirmed_box_tokens

EVENT_REVIEW_SCHEMA_V2 = 2


@dataclass
class FrameV2MigrationStats:
    """legacy verified_true → schema v2 统计。"""

    input_count: int = 0
    output_frame_count: int = 0
    binding_count: int = 0
    deduped_bindings: int = 0
    skipped_entries: int = 0
    ambiguous_frames: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def needs_migration(self) -> bool:
        return self.input_count > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_count": self.input_count,
            "output_frame_count": self.output_frame_count,
            "binding_count": self.binding_count,
            "deduped_bindings": self.deduped_bindings,
            "skipped_entries": self.skipped_entries,
            "ambiguous_frames": sorted(set(self.ambiguous_frames)),
            "warnings": list(self.warnings),
        }


def is_frame_v2_entry(entry: dict[str, Any]) -> bool:
    """是否为 schema v2 帧级条目（含 bindings 数组）。"""
    return isinstance(entry, dict) and isinstance(entry.get("bindings"), list)


def is_frame_v2_review(raw: dict[str, Any]) -> bool:
    schema = int(raw.get("schema") or 0)
    if schema >= EVENT_REVIEW_SCHEMA_V2:
        return True
    verified = raw.get("verified_true") or []
    if not verified:
        return False
    return all(is_frame_v2_entry(item) for item in verified if isinstance(item, dict))


def _binding_key(binding: dict[str, Any]) -> tuple[int, tuple[str, ...]]:
    person_raw = binding.get("person_id")
    try:
        person_id = int(person_raw) if person_raw is not None else -1
    except (TypeError, ValueError):
        person_id = -1
    confirmed = canonicalize_box_token_list(binding.get("confirmed_box_tokens") or [])
    return person_id, tuple(confirmed)


def normalize_binding(binding: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(binding, dict):
        return None
    confirmed = canonicalize_box_token_list(binding.get("confirmed_box_tokens") or [])
    if not confirmed:
        return None
    out: dict[str, Any] = {"confirmed_box_tokens": confirmed}
    if binding.get("person_id") is not None:
        try:
            person_id = int(binding.get("person_id"))
        except (TypeError, ValueError):
            person_id = -1
        if person_id >= 0:
            out["person_id"] = person_id
    return out


def legacy_entry_to_binding(entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """将一条 legacy verified_true 转为 binding；不做 box_tokens 误并。"""
    if not isinstance(entry, dict):
        return None, "条目非对象"
    if is_frame_v2_entry(entry):
        bindings = [normalize_binding(item) for item in entry.get("bindings") or []]
        bindings = [b for b in bindings if b]
        if not bindings:
            return None, "v2 帧条目无有效 binding"
        if len(bindings) == 1:
            return bindings[0], None
        return None, "v2 帧条目应整帧迁移，不应逐条拆 binding"

    confirmed = extract_confirmed_box_tokens(entry)
    box_tokens = canonicalize_box_token_list(entry.get("box_tokens") or [])

    if not confirmed:
        # 旧版逐货框：box_tokens 仅含一个货框时，表示「该 box 事件标真」
        if len(box_tokens) == 1:
            confirmed = list(box_tokens)
        else:
            return None, "无 confirmed 且 box_tokens 不唯一，跳过"

    binding: dict[str, Any] = {"confirmed_box_tokens": confirmed}
    if entry.get("person_id") is not None:
        try:
            person_id = int(entry.get("person_id"))
        except (TypeError, ValueError):
            person_id = -1
        if person_id >= 0:
            binding["person_id"] = person_id
    return binding, None


def _frame_event_type_from_timeline(row: dict[str, Any] | None, *, has_manual_only: bool) -> str:
    if not row:
        return "frame" if has_manual_only else "collision"
    alarms = canonicalize_box_token_list(row.get("alarm_collisions") or [])
    collisions = canonicalize_box_token_list(row.get("collisions") or [])
    if alarms:
        return "alarm"
    if collisions:
        return "collision"
    return "frame"


def _frame_box_tokens_from_timeline(row: dict[str, Any] | None, fallback: list[str]) -> list[str]:
    if not row:
        return canonicalize_box_token_list(fallback)
    alarms = canonicalize_box_token_list(row.get("alarm_collisions") or [])
    collisions = canonicalize_box_token_list(row.get("collisions") or [])
    merged = canonicalize_box_token_list([*alarms, *collisions])
    return merged if merged else canonicalize_box_token_list(fallback)


def build_frame_v2_entry(
    frame_idx: int,
    bindings: list[dict[str, Any]],
    *,
    source_frame_idx: int | None = None,
    timeline_row: dict[str, Any] | None = None,
    legacy_event_type: str = "",
) -> dict[str, Any] | None:
    """由同帧 bindings 构建一条 schema v2 帧记录。"""
    normalized_bindings: list[dict[str, Any]] = []
    seen: set[tuple[int, tuple[str, ...]]] = set()
    for binding in bindings:
        norm = normalize_binding(binding)
        if not norm:
            continue
        key = _binding_key(norm)
        if key in seen:
            continue
        seen.add(key)
        normalized_bindings.append(norm)

    if not normalized_bindings:
        return None

    sfi = source_frame_idx if source_frame_idx is not None else frame_idx
    fallback_tokens: list[str] = []
    for binding in normalized_bindings:
        fallback_tokens.extend(binding.get("confirmed_box_tokens") or [])

    timeline_has_detection = False
    if timeline_row:
        alarms = canonicalize_box_token_list(timeline_row.get("alarm_collisions") or [])
        collisions = canonicalize_box_token_list(timeline_row.get("collisions") or [])
        timeline_has_detection = bool(alarms or collisions)

    legacy_type = str(legacy_event_type or "").strip()
    event_type = _frame_event_type_from_timeline(
        timeline_row,
        has_manual_only=not timeline_has_detection,
    )
    if legacy_type == "alarm":
        event_type = "alarm"
    elif legacy_type == "collision" and event_type == "frame":
        event_type = "collision"
    elif legacy_type == "frame":
        event_type = "frame"

    box_tokens = _frame_box_tokens_from_timeline(timeline_row, fallback_tokens)

    return {
        "frame_idx": frame_idx,
        "source_frame_idx": sfi,
        "event_type": event_type,
        "box_tokens": box_tokens,
        "bindings": normalized_bindings,
    }


def migrate_verified_true_to_frame_v2(
    verified_true: list[Any],
    *,
    timeline_by_frame: dict[int, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], FrameV2MigrationStats]:
    """将 legacy 或混合 verified_true 列表迁移为 schema v2 帧级列表。"""
    stats = FrameV2MigrationStats()
    if not verified_true:
        return [], stats

    # 已是 v2：规范化后返回
    if all(is_frame_v2_entry(item) for item in verified_true if isinstance(item, dict)):
        out: list[dict[str, Any]] = []
        for item in verified_true:
            if not isinstance(item, dict):
                stats.skipped_entries += 1
                continue
            try:
                frame_idx = int(item.get("frame_idx") or 0)
            except (TypeError, ValueError):
                stats.skipped_entries += 1
                continue
            row = (timeline_by_frame or {}).get(frame_idx)
            rebuilt = build_frame_v2_entry(
                frame_idx,
                list(item.get("bindings") or []),
                source_frame_idx=int(item.get("source_frame_idx") or frame_idx),
                timeline_row=row,
                legacy_event_type=str(item.get("event_type") or ""),
            )
            if rebuilt:
                out.append(rebuilt)
        stats.input_count = len(verified_true)
        stats.output_frame_count = len(out)
        stats.binding_count = sum(len(e.get("bindings") or []) for e in out)
        out.sort(key=lambda e: int(e.get("frame_idx") or 0))
        return out, stats

    by_frame: dict[int, dict[str, Any]] = {}
    stats.input_count = len(verified_true)

    for item in verified_true:
        if not isinstance(item, dict):
            stats.skipped_entries += 1
            continue
        if is_frame_v2_entry(item):
            try:
                frame_idx = int(item.get("frame_idx") or 0)
            except (TypeError, ValueError):
                stats.skipped_entries += 1
                continue
            bucket = by_frame.setdefault(
                frame_idx,
                {
                    "source_frame_idx": int(item.get("source_frame_idx") or frame_idx),
                    "legacy_types": set(),
                    "bindings": [],
                },
            )
            for binding in item.get("bindings") or []:
                norm = normalize_binding(binding if isinstance(binding, dict) else {})
                if norm:
                    bucket["bindings"].append(norm)
            if item.get("event_type"):
                bucket["legacy_types"].add(str(item.get("event_type")).strip())
            continue

        try:
            frame_idx = int(item.get("frame_idx") or 0)
            source_frame_idx = int(item.get("source_frame_idx") or frame_idx)
        except (TypeError, ValueError):
            stats.skipped_entries += 1
            continue

        binding, warn = legacy_entry_to_binding(item)
        bucket = by_frame.setdefault(
            frame_idx,
            {
                "source_frame_idx": source_frame_idx,
                "legacy_types": set(),
                "bindings": [],
            },
        )
        bucket["source_frame_idx"] = min(
            int(bucket.get("source_frame_idx") or frame_idx),
            source_frame_idx,
        )
        if item.get("event_type"):
            bucket["legacy_types"].add(str(item.get("event_type")).strip())

        if binding is None:
            stats.skipped_entries += 1
            stats.ambiguous_frames.append(frame_idx)
            if warn:
                stats.warnings.append(f"帧 {frame_idx}: {warn}")
            continue
        bucket["bindings"].append(binding)

    out_entries: list[dict[str, Any]] = []
    timeline = timeline_by_frame or {}

    for frame_idx in sorted(by_frame.keys()):
        bucket = by_frame[frame_idx]
        raw_bindings = list(bucket["bindings"])
        before = len(raw_bindings)
        legacy_types = bucket.get("legacy_types") or set()
        legacy_type = "alarm" if "alarm" in legacy_types else (
            "collision" if "collision" in legacy_types else "frame"
        )
        entry = build_frame_v2_entry(
            frame_idx,
            raw_bindings,
            source_frame_idx=int(bucket.get("source_frame_idx") or frame_idx),
            timeline_row=timeline.get(frame_idx),
            legacy_event_type=legacy_type,
        )
        if not entry:
            stats.skipped_entries += before
            stats.ambiguous_frames.append(frame_idx)
            continue
        after = len(entry.get("bindings") or [])
        stats.deduped_bindings += max(0, before - after)
        stats.binding_count += after
        out_entries.append(entry)

    stats.output_frame_count = len(out_entries)
    return out_entries, stats


def verify_frame_v2_verified_true(verified_true: list[Any]) -> list[str]:
    """校验 verified_true 是否已为合法 schema v2。"""
    issues: list[str] = []
    if not verified_true:
        return issues
    for idx, item in enumerate(verified_true):
        if not isinstance(item, dict):
            issues.append(f"[{idx}] 非对象")
            continue
        if not is_frame_v2_entry(item):
            issues.append(f"[{idx}] 缺少 bindings（仍为 legacy 条目）")
            continue
        bindings = item.get("bindings") or []
        if not bindings:
            issues.append(f"帧 {item.get('frame_idx')}: bindings 为空")
            continue
        for bidx, binding in enumerate(bindings):
            if normalize_binding(binding if isinstance(binding, dict) else {}) is None:
                issues.append(
                    f"帧 {item.get('frame_idx')} binding[{bidx}]: 无 confirmed_box_tokens"
                )
    return issues


def flatten_frame_v2_to_legacy_shape(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """v2 帧条目展平为多条 legacy 形（只读兼容/export 用，不含 bindings）。"""
    if not is_frame_v2_entry(entry):
        return [entry] if isinstance(entry, dict) else []
    frame_idx = int(entry.get("frame_idx") or 0)
    source_frame_idx = int(entry.get("source_frame_idx") or frame_idx)
    event_type = str(entry.get("event_type") or "collision")
    box_tokens = list(entry.get("box_tokens") or [])
    out: list[dict[str, Any]] = []
    for binding in entry.get("bindings") or []:
        if not isinstance(binding, dict):
            continue
        confirmed = canonicalize_box_token_list(binding.get("confirmed_box_tokens") or [])
        if not confirmed:
            continue
        row: dict[str, Any] = {
            "event_type": event_type,
            "frame_idx": frame_idx,
            "source_frame_idx": source_frame_idx,
            "box_tokens": box_tokens if box_tokens else confirmed,
            "confirmed_box_tokens": confirmed,
        }
        if binding.get("person_id") is not None:
            row["person_id"] = binding.get("person_id")
        out.append(row)
    return out
