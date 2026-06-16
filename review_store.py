"""人工复核存储：与 pose 模型 tier 解耦，按 source_video + 片段时间窗定位。"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config_loader import AppPaths, parse_record_path_segments, resolve_app_paths, sanitize_file_stem

EVENT_REVIEW_FILE = "event_review.json"
SEGMENT_FULL = "full"
SEGMENT_ZERO = "0"

_START_RE = re.compile(r"_start_([\d:\-_.]+)", re.IGNORECASE)
_END_RE = re.compile(r"_end_([\d:\-_.]+)", re.IGNORECASE)


def normalize_segment_token(raw: Any) -> str:
    """将时间窗 token 规范为安全目录名片段（HH-MM-SS 或秒数 / full）。"""
    s = str(raw or "").strip().lower()
    if not s:
        return ""
    if s in ("full", "end", "eof", "*"):
        return SEGMENT_FULL
    s = s.replace(":", "-").replace("_", "-").replace(".", "-")
    s = re.sub(r"-+", "-", s).strip("-")
    return s or ""


def _segment_from_seconds(val: Any) -> str:
    try:
        sec = float(val)
    except (TypeError, ValueError):
        return ""
    if sec < 0:
        return ""
    if sec == 0:
        return SEGMENT_ZERO
    if sec == float("inf"):
        return SEGMENT_FULL
    if abs(sec - round(sec)) < 1e-6:
        return str(int(round(sec)))
    return f"{sec:.3f}".rstrip("0").rstrip(".")


def _read_dict_field(meta: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in meta and meta.get(key) not in (None, ""):
            return meta.get(key)
    collect = meta.get("collect_config")
    if isinstance(collect, dict):
        for key in keys:
            if key in collect and collect.get(key) not in (None, ""):
                return collect.get(key)
    return None


def extract_segment_window(
    *,
    meta: dict[str, Any] | None = None,
    record_id: str = "",
    record_name: str = "",
    manifest: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """从 meta / 记录名 / manifest 解析片段时间窗，返回 (start, end) token。"""
    meta = meta if isinstance(meta, dict) else {}
    manifest = manifest if isinstance(manifest, dict) else {}

    start = normalize_segment_token(
        _read_dict_field(meta, "clip_start", "clip_start_hms", "segment_start", "segment_start_hms")
        or _read_dict_field(manifest, "clip_start", "clip_start_hms", "segment_start", "segment_start_hms")
    )
    end = normalize_segment_token(
        _read_dict_field(meta, "clip_end", "clip_end_hms", "segment_end", "segment_end_hms")
        or _read_dict_field(manifest, "clip_end", "clip_end_hms", "segment_end", "segment_end_hms")
    )

    if not start:
        sec = _read_dict_field(meta, "segment_start_sec", "clip_start_sec") or _read_dict_field(
            manifest, "segment_start_sec", "clip_start_sec"
        )
        start = _segment_from_seconds(sec) if sec not in (None, "") else ""
    if not end:
        sec = _read_dict_field(meta, "segment_end_sec", "clip_end_sec") or _read_dict_field(
            manifest, "segment_end_sec", "clip_end_sec"
        )
        end = _segment_from_seconds(sec) if sec not in (None, "") else ""

    name = str(record_name or "").strip()
    if not name:
        _, _, rec = parse_record_path_segments(record_id)
        name = str(rec or "").strip()

    if not start:
        m = _START_RE.search(name)
        if m:
            start = normalize_segment_token(m.group(1))
    if not end:
        m = _END_RE.search(name)
        if m:
            end = normalize_segment_token(m.group(1))

    if not start:
        start = SEGMENT_ZERO
    if not end:
        end = SEGMENT_FULL
    return start, end


def source_video_stem(meta: dict[str, Any] | None, record_id: str = "") -> str:
    meta = meta if isinstance(meta, dict) else {}
    src = str(meta.get("source_video") or "").strip()
    if src:
        return sanitize_file_stem(Path(src).stem)
    vs = str(meta.get("video_stem") or "").strip()
    if vs:
        return sanitize_file_stem(vs)
    _, _, rec = parse_record_path_segments(record_id)
    if rec:
        stem = rec
        for tag in ("_rtmpose_t", "_rtmpose_s", "_rtmpose_m"):
            if stem.endswith(tag):
                stem = stem[: -len(tag)]
                break
        return sanitize_file_stem(stem)
    return "unknown"


def camera_slug_for_meta(meta: dict[str, Any] | None, record_id: str = "") -> str:
    meta = meta if isinstance(meta, dict) else {}
    slug = str(meta.get("camera_slug") or "").strip()
    if slug:
        return slug
    _, parsed_slug, _ = parse_record_path_segments(record_id)
    return str(parsed_slug or "unknown").strip() or "unknown"


def build_review_key(
    *,
    camera_slug: str,
    source_video: str,
    segment_start: str,
    segment_end: str,
) -> str:
    """逻辑复核键：{camera_slug}/{source_stem}__{start}__{end}。"""
    cam = sanitize_file_stem(camera_slug) or "unknown"
    stem = sanitize_file_stem(Path(str(source_video or "").strip()).stem) or "unknown"
    start = normalize_segment_token(segment_start) or SEGMENT_ZERO
    end = normalize_segment_token(segment_end) or SEGMENT_FULL
    clip_key = f"{stem}__{start}__{end}"
    return f"{cam}/{clip_key}"


def review_key_for_record(
    *,
    meta: dict[str, Any] | None,
    record_id: str = "",
    manifest: dict[str, Any] | None = None,
) -> str:
    meta = meta if isinstance(meta, dict) else {}
    start, end = extract_segment_window(meta=meta, record_id=record_id, manifest=manifest)
    src = str(meta.get("source_video") or "").strip()
    if not src:
        stem = source_video_stem(meta, record_id)
        src = f"{stem}.mp4"
    return build_review_key(
        camera_slug=camera_slug_for_meta(meta, record_id),
        source_video=src,
        segment_start=start,
        segment_end=end,
    )


def split_review_key(review_key: str) -> tuple[str, str]:
    key = str(review_key or "").strip().replace("\\", "/")
    if "/" in key:
        camera, clip = key.split("/", 1)
        return camera.strip() or "unknown", clip.strip() or "unknown"
    return "unknown", key or "unknown"


def canonical_event_review_path(paths: AppPaths, review_key: str) -> Path:
    camera, clip_key = split_review_key(review_key)
    return paths.review_dir / camera / clip_key / EVENT_REVIEW_FILE


def legacy_package_event_review_path(locator) -> Path:
    """记录包内旧版 event_review.json 路径。"""
    from pose_store import STORAGE_V1_JSON, STORAGE_V2_PARQUET

    if locator.storage == STORAGE_V2_PARQUET:
        return locator.path / EVENT_REVIEW_FILE
    return locator.path.with_suffix(".event_review.json")


def load_meta_for_locator(locator, paths: AppPaths | None = None) -> dict[str, Any]:
    from pose_store import meta_sidecar_path

    paths = paths or resolve_app_paths()
    meta: dict[str, Any] = {}
    sidecar = meta_sidecar_path(paths.json_dir, locator.record_id)
    if sidecar.is_file():
        try:
            raw = json.loads(sidecar.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                meta.update(raw)
        except (OSError, json.JSONDecodeError):
            pass
    if locator.path.is_dir():
        manifest_path = locator.path / "manifest.json"
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(manifest, dict):
                    meta.setdefault("collect_config", manifest.get("collect_config"))
            except (OSError, json.JSONDecodeError):
                pass
    meta.setdefault("record_id", locator.record_id)
    return meta


def resolve_review_context(locator, paths: AppPaths | None = None) -> tuple[str, Path, Path | None]:
    """返回 (review_key, canonical_path, legacy_path|None)。"""
    paths = paths or resolve_app_paths()
    meta = load_meta_for_locator(locator, paths)
    manifest = None
    if locator.path.is_dir():
        mp = locator.path / "manifest.json"
        if mp.is_file():
            try:
                manifest = json.loads(mp.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                manifest = None
    review_key = review_key_for_record(meta=meta, record_id=locator.record_id, manifest=manifest)
    canonical = canonical_event_review_path(paths, review_key)
    legacy = legacy_package_event_review_path(locator)
    if legacy.resolve() == canonical.resolve():
        legacy = None
    return review_key, canonical, legacy


def event_review_read_paths(locator, paths: AppPaths | None = None) -> list[Path]:
    """读取优先级：review_dir → 记录包内旧路径。"""
    paths = paths or resolve_app_paths()
    _, canonical, legacy = resolve_review_context(locator, paths)
    out: list[Path] = []
    if canonical.is_file():
        out.append(canonical)
    if legacy and legacy.is_file():
        try:
            if legacy.resolve() not in {p.resolve() for p in out}:
                out.append(legacy)
        except OSError:
            out.append(legacy)
    return out


def event_review_write_path(locator, paths: AppPaths | None = None) -> Path:
    """写入目标：仅 review_dir。"""
    paths = paths or resolve_app_paths()
    _, canonical, _ = resolve_review_context(locator, paths)
    return canonical


def merge_event_reviews(
    src_review: dict[str, Any],
    dest_review: dict[str, Any],
    *,
    review_key: str,
    record_id: str = "",
) -> dict[str, Any]:
    """合并两份 event_review（verified_true 并集，状态取更完整的一方）。"""
    from pose_store import (
        REVIEW_STATUS_COMPLETED,
        REVIEW_STATUS_IN_PROGRESS,
        REVIEW_STATUS_NO_COLLISION,
        REVIEW_STATUS_TERMINAL,
        event_signature,
        normalize_review_entry,
    )

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in list(dest_review.get("verified_true") or []) + list(src_review.get("verified_true") or []):
        norm = normalize_review_entry(raw if isinstance(raw, dict) else {})
        if not norm:
            continue
        sig = event_signature(norm["event_type"], norm["frame_idx"], norm["box_tokens"])
        if sig in seen:
            continue
        seen.add(sig)
        merged.append(norm)
    merged.sort(
        key=lambda e: (
            int(e.get("frame_idx") or 0),
            str(e.get("event_type") or ""),
            ",".join(e.get("box_tokens") or []),
        )
    )

    def _rank(status: str) -> int:
        s = str(status or "").strip().lower()
        if s == REVIEW_STATUS_COMPLETED:
            return 4
        if s == REVIEW_STATUS_NO_COLLISION:
            return 3
        if s == REVIEW_STATUS_IN_PROGRESS:
            return 2
        return 1

    dest_st = str(dest_review.get("status") or "").strip().lower()
    src_st = str(src_review.get("status") or "").strip().lower()
    status = dest_st
    if _rank(src_st) > _rank(dest_st):
        status = src_st
    elif _rank(src_st) == _rank(dest_st) and len(merged) > len(dest_review.get("verified_true") or []):
        status = src_st or dest_st

    if merged and status not in REVIEW_STATUS_TERMINAL:
        status = REVIEW_STATUS_IN_PROGRESS

    event_total = dest_review.get("event_total")
    src_total = src_review.get("event_total")
    if src_total is not None:
        try:
            st = int(src_total)
            dt = int(event_total) if event_total is not None else 0
            event_total = max(st, dt)
        except (TypeError, ValueError):
            pass

    meta = src_review if len(src_review.get("verified_true") or []) >= len(dest_review.get("verified_true") or []) else dest_review
    start, end = extract_segment_window(meta=meta)
    payload: dict[str, Any] = {
        "schema": dest_review.get("schema") or src_review.get("schema") or 1,
        "review_key": review_key,
        "record_id": str(record_id or dest_review.get("record_id") or src_review.get("record_id") or ""),
        "source_video": str(meta.get("source_video") or dest_review.get("source_video") or src_review.get("source_video") or ""),
        "segment_start": normalize_segment_token(dest_review.get("segment_start") or src_review.get("segment_start") or start),
        "segment_end": normalize_segment_token(dest_review.get("segment_end") or src_review.get("segment_end") or end),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "verified_true": merged,
    }
    if event_total is not None:
        payload["event_total"] = event_total
    if status:
        payload["status"] = status
    if status == REVIEW_STATUS_COMPLETED:
        payload["completed_at"] = (
            dest_review.get("completed_at")
            or src_review.get("completed_at")
            or datetime.now(timezone.utc).isoformat()
        )
    return payload
