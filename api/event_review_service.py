"""Frame-level event review PATCH service.

The write index is ``frame_idx`` and bindings are upserted by person/track
identity.  Detection signatures are deliberately not used as persistence
keys because detections can change after a recompute and are not human truth.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from event_engine.box_identity import canonicalize_box_token_list
from event_review_frame_v2 import (
    LegacyReviewMigrationRequired,
    binding_identity,
    build_frame_v2_entry,
    load_verified_items_for_write,
    normalize_binding,
    normalize_bindings,
    normalize_frame_v2_entry,
    upsert_binding,
)
from pose_store import (
    REVIEW_STATUS_NO_COLLISION,
    enrich_events_with_review,
    event_review_status_label,
    events_to_verified_entries,
    extract_confirmed_box_tokens,
    load_event_review,
    load_events,
    resolve_event_review_status,
    save_event_review,
    _first_existing_event_review_raw,
)


def _frame_idx(event: dict[str, Any]) -> int:
    try:
        value = int(event.get("frame_idx"))
    except (TypeError, ValueError):
        raise HTTPException(400, "event.frame_idx 无效")
    if value < 0:
        raise HTTPException(400, "event.frame_idx 无效")
    return value


def _source_frame_idx(event: dict[str, Any], frame_idx: int) -> int:
    try:
        value = int(event.get("source_frame_idx"))
    except (TypeError, ValueError):
        value = frame_idx
    return value if value >= 0 else frame_idx


def _detected_types(event: dict[str, Any]) -> list[str]:
    raw = event.get("detected_event_types")
    values = raw if isinstance(raw, list) else []
    out = [
        str(value).strip()
        for value in values
        if str(value).strip() in {"alarm", "collision"}
    ]
    legacy = str(event.get("event_type") or "").strip()
    if legacy in {"alarm", "collision"} and legacy not in out:
        out.append(legacy)
    return out


def _detected_tokens(event: dict[str, Any]) -> list[str]:
    raw = event.get("detected_box_tokens")
    if not isinstance(raw, list):
        raw = event.get("box_tokens")
    if not isinstance(raw, list):
        return []
    return canonicalize_box_token_list(
        [str(value).strip() for value in raw if str(value).strip()]
    )


def _binding_from_flat_event(
    event: dict[str, Any],
    *,
    confirmed_override: list[str] | None = None,
) -> dict[str, Any] | None:
    confirmed = (
        canonicalize_box_token_list(confirmed_override)
        if confirmed_override is not None
        else extract_confirmed_box_tokens(event)
    )
    if not confirmed:
        return None
    binding: dict[str, Any] = {"confirmed_box_tokens": confirmed}
    if event.get("person_id") is not None and event.get("person_id") != "":
        try:
            person_id = int(event.get("person_id"))
        except (TypeError, ValueError):
            person_id = -1
        if person_id >= 0:
            binding["person_id"] = person_id
    track_id = event.get("person_track_id")
    if track_id is None:
        track_id = event.get("track_id")
    if track_id is not None and str(track_id).strip():
        binding["person_track_id"] = str(track_id).strip()
    return normalize_binding(binding)


def _incoming_bindings(event: dict[str, Any]) -> list[dict[str, Any]]:
    raw = event.get("bindings")
    if isinstance(raw, list):
        bindings = normalize_bindings(
            item for item in raw if isinstance(item, dict)
        )
        if bindings:
            return bindings
    flat = _binding_from_flat_event(event)
    return [flat] if flat else []


def _binding_selector_identity(event: dict[str, Any]) -> tuple[Any, ...]:
    """Return a binding selector without inventing a confirmed box token."""
    track_id = event.get("person_track_id")
    if track_id is None:
        track_id = event.get("track_id")
    if track_id is not None and str(track_id).strip():
        return ("track", str(track_id).strip())
    if event.get("person_id") is not None and event.get("person_id") != "":
        try:
            person_id = int(event.get("person_id"))
        except (TypeError, ValueError):
            person_id = -1
        if person_id >= 0:
            return ("person", person_id)
    return ("anonymous",)


def _merge_event_into_frame(
    event: dict[str, Any],
    existing: dict[str, Any] | None,
) -> dict[str, Any]:
    frame_idx = _frame_idx(event)
    current = normalize_frame_v2_entry(existing or {}) if existing else None
    bindings = list((current or {}).get("bindings") or [])
    incoming = _incoming_bindings(event)
    if not incoming:
        raise HTTPException(400, "标真须选择 confirmed_box_tokens")
    for binding in incoming:
        bindings = upsert_binding(bindings, binding)
    entry = build_frame_v2_entry(
        frame_idx,
        bindings,
        source_frame_idx=_source_frame_idx(event, frame_idx),
        detected_event_types=(
            _detected_types(event)
            or list((current or {}).get("detected_event_types") or [])
        ),
        detected_box_tokens=(
            _detected_tokens(event)
            or list((current or {}).get("detected_box_tokens") or [])
        ),
    )
    if not entry:
        raise HTTPException(400, "该帧没有有效 binding")
    return entry


def _replace_confirmed_binding(
    event: dict[str, Any],
    existing: dict[str, Any] | None,
    confirmed: list[str],
) -> dict[str, Any] | None:
    if not existing:
        raise HTTPException(400, "该帧尚未标真，请先标真")
    current = normalize_frame_v2_entry(existing)
    if not current:
        raise HTTPException(409, "该帧复核格式无效，请先迁移")

    identity = _binding_selector_identity(event)
    bindings = list(current["bindings"])

    if confirmed:
        incoming = _binding_from_flat_event(event, confirmed_override=confirmed)
        if not incoming:
            raise HTTPException(400, "confirmed_box_tokens 无效")
        bindings = upsert_binding(bindings, incoming)
    else:
        if identity[0] in {"person", "track"}:
            bindings = [
                binding
                for binding in bindings
                if binding_identity(binding) != identity
            ]
        elif len(bindings) == 1:
            bindings = []
        else:
            raise HTTPException(400, "本帧有多个 binding，请先选择 person_id 再清空")

    if not bindings:
        return None
    return build_frame_v2_entry(
        current["frame_idx"],
        bindings,
        source_frame_idx=current["source_frame_idx"],
        detected_event_types=current["detected_event_types"],
        detected_box_tokens=current["detected_box_tokens"],
    )


def _binding_count(review: dict[str, Any]) -> int:
    return sum(
        len(item.get("bindings") or [])
        for item in review.get("verified_true") or []
        if isinstance(item, dict)
    )


def _range_events(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate an inclusive, gap-free frame range before any file is written."""
    try:
        start = int(body.get("range_start"))
        end = int(body.get("range_end"))
    except (TypeError, ValueError):
        raise HTTPException(400, "区间标真须提供有效 range_start / range_end")
    if start < 0 or end < start:
        raise HTTPException(400, "区间标真帧范围无效")
    expected_count = end - start + 1
    if expected_count > 100_000:
        raise HTTPException(400, "单次区间标真不能超过 100000 帧")

    raw_events = body.get("events")
    if not isinstance(raw_events, list):
        raise HTTPException(400, "区间标真须提供 events 数组")

    by_frame: dict[int, dict[str, Any]] = {}
    duplicates: list[int] = []
    for raw_event in raw_events:
        if not isinstance(raw_event, dict):
            raise HTTPException(400, "区间标真 events 中存在无效条目")
        frame_idx = _frame_idx(raw_event)
        if frame_idx in by_frame:
            duplicates.append(frame_idx)
        by_frame[frame_idx] = raw_event

    missing = [frame for frame in range(start, end + 1) if frame not in by_frame]
    extras = sorted(frame for frame in by_frame if frame < start or frame > end)
    if duplicates or missing or extras or len(raw_events) != expected_count:
        details: list[str] = []
        if missing:
            details.append(f"缺少帧 {missing[:10]}")
        if duplicates:
            details.append(f"重复帧 {sorted(set(duplicates))[:10]}")
        if extras:
            details.append(f"越界帧 {extras[:10]}")
        if not details:
            details.append(
                f"应有 {expected_count} 帧，实际收到 {len(raw_events)} 条"
            )
        raise HTTPException(400, "区间标真必须逐帧连续：" + "；".join(details))
    return [by_frame[frame] for frame in range(start, end + 1)]


def _saved_response(
    record_id: str,
    locator: Any,
    path: Any,
    *,
    event_count: int,
    light: bool,
    events: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    from record_index_store import refresh_record_summary

    saved = load_event_review(locator)
    review_status = resolve_event_review_status(saved, event_count=event_count)
    refresh_record_summary(record_id)
    payload: dict[str, Any] = {
        "status": "ok",
        "record_id": record_id,
        "path": str(path),
        "verified_true_count": len(saved.get("verified_true") or []),
        "binding_count": _binding_count(saved),
        "event_review_status": review_status,
        "event_review_label": event_review_status_label(review_status),
        "event_review": saved,
    }
    if light:
        payload["light"] = True
    else:
        payload["events"] = enrich_events_with_review(
            events if events is not None else load_events(locator),
            locator,
        )
    if extra:
        payload.update(extra)
    return JSONResponse(payload)


def patch_event_review_locked(
    record_id: str,
    locator: Any,
    body: dict[str, Any],
) -> JSONResponse:
    raw, _ = _first_existing_event_review_raw(locator)
    try:
        frames = load_verified_items_for_write(raw or {})
    except LegacyReviewMigrationRequired as exc:
        raise HTTPException(409, str(exc)) from exc
    by_frame = {
        int(frame["frame_idx"]): frame
        for frame in frames
        if isinstance(frame, dict) and frame.get("frame_idx") is not None
    }

    action = str(body.get("action") or "").strip().lower()
    requested_status = str(body.get("status") or "").strip().lower()
    light = action in {
        "toggle",
        "set_confirmed_box",
        "set_all_verified",
        "set_range_verified",
    }
    loaded_events: list[dict[str, Any]] | None = None
    response_extra: dict[str, Any] | None = None

    if requested_status == "completed":
        if isinstance(body.get("verified_true"), list):
            candidate = {
                "schema": 2,
                "verified_true": body.get("verified_true") or [],
            }
            try:
                frames = load_verified_items_for_write(candidate)
            except LegacyReviewMigrationRequired as exc:
                raise HTTPException(409, str(exc)) from exc
        event_total = _event_total(body, locator)
        try:
            path = save_event_review(
                locator,
                frames,
                status="completed",
                event_total=event_total,
            )
        except LegacyReviewMigrationRequired as exc:
            raise HTTPException(409, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(500, f"保存复核失败: {exc}") from exc
        return _saved_response(
            record_id,
            locator,
            path,
            event_count=event_total,
            light=False,
        )

    if action == "toggle":
        event = body.get("event")
        if not isinstance(event, dict):
            raise HTTPException(400, "toggle 须包含 event 对象")
        frame_idx = _frame_idx(event)
        want = body.get("verified_true")
        if want is None:
            want = frame_idx not in by_frame
        if bool(want):
            by_frame[frame_idx] = _merge_event_into_frame(
                event,
                by_frame.get(frame_idx),
            )
        else:
            by_frame.pop(frame_idx, None)
        frames = list(by_frame.values())
    elif action == "set_confirmed_box":
        event = body.get("event")
        if not isinstance(event, dict):
            raise HTTPException(400, "set_confirmed_box 须包含 event 对象")
        raw_confirmed = body.get("confirmed_box_tokens")
        if raw_confirmed is None:
            token = str(body.get("confirmed_box_token") or "").strip()
            raw_confirmed = [token] if token else []
        if not isinstance(raw_confirmed, list):
            raise HTTPException(400, "confirmed_box_tokens 须为数组")
        confirmed = canonicalize_box_token_list(
            [str(value).strip() for value in raw_confirmed if str(value).strip()]
        )
        frame_idx = _frame_idx(event)
        replaced = _replace_confirmed_binding(
            event,
            by_frame.get(frame_idx),
            confirmed,
        )
        if replaced:
            by_frame[frame_idx] = replaced
        else:
            by_frame.pop(frame_idx, None)
        frames = list(by_frame.values())
    elif action == "set_all_verified":
        if "mark_all" not in body:
            raise HTTPException(400, "set_all_verified 须包含 mark_all")
        if bool(body.get("mark_all")):
            loaded_events = load_events(locator)
            frames = events_to_verified_entries(loaded_events)
        else:
            frames = []
    elif action == "set_range_verified":
        range_events = _range_events(body)
        for event in range_events:
            frame_idx = _frame_idx(event)
            by_frame[frame_idx] = _merge_event_into_frame(
                event,
                by_frame.get(frame_idx),
            )
        frames = list(by_frame.values())
        response_extra = {
            "range_start": int(body["range_start"]),
            "range_end": int(body["range_end"]),
            "range_applied_count": len(range_events),
        }
    elif "verified_true" in body:
        if not isinstance(body.get("verified_true"), list):
            raise HTTPException(400, "verified_true 须为数组")
        candidate = {"schema": 2, "verified_true": body.get("verified_true") or []}
        try:
            frames = load_verified_items_for_write(candidate)
        except LegacyReviewMigrationRequired as exc:
            raise HTTPException(409, str(exc)) from exc
    else:
        raise HTTPException(
            400,
            "请提供 verified_true、action=toggle、action=set_confirmed_box、"
            "action=set_all_verified、action=set_range_verified 或 status=completed",
        )

    event_total = _event_total(body, locator, loaded_events)
    next_status = None if action == "set_confirmed_box" else "in_progress"
    if event_total == 0 and not frames:
        next_status = REVIEW_STATUS_NO_COLLISION

    try:
        path = save_event_review(
            locator,
            frames,
            status=next_status,
            event_total=event_total,
        )
    except LegacyReviewMigrationRequired as exc:
        raise HTTPException(409, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, f"保存复核失败: {exc}") from exc

    return _saved_response(
        record_id,
        locator,
        path,
        event_count=event_total,
        light=light,
        events=loaded_events,
        extra=response_extra,
    )


def _event_total(
    body: dict[str, Any],
    locator: Any,
    loaded_events: list[dict[str, Any]] | None = None,
) -> int:
    if body.get("event_total") is not None:
        try:
            return max(0, int(body.get("event_total")))
        except (TypeError, ValueError):
            pass
    if loaded_events is not None:
        return len(loaded_events)
    return len(load_events(locator))
