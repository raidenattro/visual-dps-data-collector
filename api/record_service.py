"""记录定位、元数据与配套视频路径。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from annotation_store import (
    ANNOTATION_SOURCE_MASTER,
    annotation_dir_display_rel,
    annotation_dir_for_source,
    annotation_path_for_video_stem,
    load_annotation_json,
    normalize_annotation_payload,
    normalize_annotation_source,
    resolve_annotation_save_stem,
    resolve_video_stem_from_record,
    save_annotation_json,
    validate_annotation_payload,
)
from event_engine.annotation_boxes import load_annotation_config
from config_loader import (
    AppPaths,
    parse_record_path_segments,
    pose_model_tier_from_backend,
    pose_model_tier_from_variant,
    record_id_for_pose_path,
    record_video_path,
    resolve_app_paths,
    sanitize_file_stem,
    variant_to_backend,
    video_bucket_dir,
)
from model_assets import VIDEO_EXTENSIONS
from pose_store import (
    STORAGE_V2_PARQUET,
    ensure_no_collision_review_completed,
    event_review_status_label,
    is_persisted_review_terminal,
    iter_active_records,
    load_event_review,
    load_pose_header,
    locate_record as find_record,
    meta_sidecar_path,
    persisted_event_review_status,
    record_has_skeleton_data,
    resolve_event_review_status,
)

from api.naming import display_name_from_pose_file
from api.reflection_service import REFLECTION_OK, load_reflection_or_http, normalize_corner_label
from video_transcode import (
    default_playback_transcode_height,
    ensure_preview_transcode_async,
    read_video_height,
    resolve_playback_serve_path,
    transcode_preview_video,
)
from record_tag_store import get_tags_map

def json_archive_dir() -> Path:
    return resolve_app_paths().json_dir / "archive"


def locate_record_by_id(record_id: str, *, include_archive: bool = True):
    paths = resolve_app_paths()
    return find_record(paths.json_dir, record_id, include_archive=include_archive)


def record_id_from_pose_path(pose_path: Path) -> str:
    paths = resolve_app_paths()
    return record_id_for_pose_path(paths.json_dir, pose_path)


def meta_path_for_record(record_id: str, locator=None) -> Path:
    paths = resolve_app_paths()
    sidecar = meta_sidecar_path(paths.json_dir, record_id)
    if sidecar.is_file():
        return sidecar
    if locator is not None:
        if locator.storage == STORAGE_V2_PARQUET:
            legacy = locator.path / "meta.json"
            if legacy.is_file():
                return legacy
        elif locator.path.is_file():
            legacy = locator.path.with_suffix(".meta.json")
            if legacy.is_file():
                return legacy
    return sidecar


def resolve_video_stem_for_record(record_id: str, locator=None, meta: dict | None = None) -> str:
    if meta:
        vs = str(meta.get("video_stem") or "").strip()
        if vs:
            return vs
        src = str(meta.get("source_video") or "").strip()
        if src:
            return sanitize_file_stem(Path(src).stem)
    if locator is None:
        locator = locate_record_by_id(record_id)
    if locator:
        return resolve_video_stem_from_record(
            record_id,
            json_dir=resolve_app_paths().json_dir,
            pose_path=locator.path,
            meta=meta,
        )
    return sanitize_file_stem(record_id)


def stem_annotation_path(video_stem: str) -> Path:
    paths = resolve_app_paths()
    return annotation_path_for_video_stem(video_stem, annotation_dir=paths.annotation_dir)


def persist_annotation_for_video(
    payload: dict[str, Any],
    video_stem: str,
    *,
    source_video: str = "",
    frame_width: int = 0,
    frame_height: int = 0,
    preserve_existing: bool = False,
    annotation_source: str = "master",
) -> tuple[Path, str]:
    from annotation_store import annotation_dir_for_source, normalize_annotation_source

    paths = resolve_app_paths()
    norm = normalize_annotation_source(annotation_source)
    if norm == "master":
        raise ValueError("母本目录只读，请指定模型层 annotation_source（rtmpose-t/s/m）")
    ann_dir = annotation_dir_for_source(paths, norm)
    save_stem = resolve_annotation_save_stem(
        ann_dir,
        video_stem,
        preserve_existing=preserve_existing,
    )
    normalized = normalize_annotation_payload(
        payload,
        video_stem=save_stem,
        source_video=source_video,
        frame_width=frame_width,
        frame_height=frame_height,
    )
    _, err = validate_annotation_payload(normalized)
    if err:
        raise ValueError(err)
    path = save_annotation_json(save_stem, normalized, annotation_dir=ann_dir)
    return path, save_stem


def find_records_for_annotation_stem(annotation_stem: str) -> list[str]:
    """查找曾关联 annotations/{stem}.json 的骨架记录。"""
    paths = resolve_app_paths()
    stem = sanitize_file_stem(annotation_stem)
    target = annotation_path_for_video_stem(stem, annotation_dir=paths.annotation_dir)
    target_key = str(target.resolve()) if target.is_file() else str(target)
    found: list[str] = []
    for locator in iter_active_records(paths.json_dir):
        record_id = locator.record_id
        ann_path = resolve_annotation_path_for_record(record_id, locator=locator)
        if ann_path and ann_path.is_file():
            if str(ann_path.resolve()) == target_key:
                found.append(record_id)
                continue
        meta_side = meta_path_for_record(record_id, locator)
        meta: dict[str, Any] | None = None
        if meta_side.is_file():
            try:
                meta = json.loads(meta_side.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                meta = None
        vs = resolve_video_stem_for_record(record_id, locator=locator, meta=meta)
        if vs == stem:
            found.append(record_id)
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for rid in found:
        if rid not in seen:
            seen.add(rid)
            out.append(rid)
    return out


def annotation_frame_size(payload: dict[str, Any]) -> tuple[int, int]:
    ann = payload.get("annotation_size")
    if isinstance(ann, dict):
        try:
            w = int(ann.get("width") or 0)
            h = int(ann.get("height") or 0)
            return w, h
        except (TypeError, ValueError):
            pass
    return 0, 0


def _video_base_for_record(
    paths: AppPaths,
    record_id: str,
    meta: dict[str, Any],
) -> Path:
    """解析记录配套视频所在目录（含 rtmpose-t/机位 分层）。"""
    pose_tier = str(meta.get("pose_model_tier") or "").strip()
    camera_slug = str(meta.get("camera_slug") or "").strip()
    if not pose_tier or not camera_slug:
        parsed_tier, parsed_slug, _ = parse_record_path_segments(record_id)
        pose_tier = pose_tier or (parsed_tier or "")
        camera_slug = camera_slug or (parsed_slug or "")
    if pose_tier and camera_slug:
        return video_bucket_dir(paths, camera_slug, pose_tier=pose_tier)
    if camera_slug:
        return paths.video_dir / camera_slug
    return paths.video_dir


def parse_save_video_flag(raw: Any, *, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return default


def video_path_for_record(record_id: str) -> Path | None:
    locator = locate_record_by_id(record_id)
    if not locator:
        return None
    paths = resolve_app_paths()
    sidecar = meta_path_for_record(record_id, locator)
    meta: dict[str, Any] | None = None
    if sidecar.is_file():
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = None

    candidates: list[Path] = []
    seen: set[str] = set()

    def add(p: Path) -> None:
        key = str(p)
        if key in seen:
            return
        seen.add(key)
        candidates.append(p)

    camera_slug = str(meta.get("camera_slug") or "").strip() if meta else ""
    if not camera_slug:
        _, parsed_slug, _ = parse_record_path_segments(record_id)
        camera_slug = parsed_slug or ""
    video_base = _video_base_for_record(paths, record_id, meta or {})
    pkg_stem = locator.path.name if locator.path.is_dir() else Path(str(record_id)).name

    if meta:
        vf = str(meta.get("video_file") or "").strip()
        if vf:
            add(video_base / vf)
            add(paths.video_dir / vf)
        src = str(meta.get("source_video") or "").strip()
        if src:
            add(video_base / Path(src).name)
            add(paths.video_dir / Path(src).name)
        for stem_key in (
            str(meta.get("video_stem") or "").strip(),
            str(meta.get("display_name") or "").strip(),
        ):
            if not stem_key:
                continue
            safe = sanitize_file_stem(stem_key)
            for ext in VIDEO_EXTENSIONS:
                add(video_base / f"{safe}{ext}")
                add(paths.video_dir / f"{safe}{ext}")

    for ext in VIDEO_EXTENSIONS:
        add(video_base / f"{pkg_stem}{ext}")
        add(paths.video_dir / f"{pkg_stem}{ext}")

    if meta is None or not str(meta.get("video_stem") or "").strip():
        resolved_stem = resolve_video_stem_for_record(record_id, locator=locator, meta=meta)
        if resolved_stem and resolved_stem != pkg_stem:
            for ext in VIDEO_EXTENSIONS:
                add(paths.video_dir / f"{resolved_stem}{ext}")

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def video_path_for_video_stem(video_stem: str) -> Path | None:
    """按 video_stem / 标注内 source_video 在 video_dir 查找配套视频。"""
    paths = resolve_app_paths()
    stem = sanitize_file_stem(video_stem)
    if not stem:
        return None
    candidates: list[Path] = []
    seen: set[str] = set()

    def add(p: Path) -> None:
        key = str(p)
        if key in seen:
            return
        seen.add(key)
        candidates.append(p)

    ann = load_annotation_json(stem, annotation_dir=paths.annotation_dir)
    if ann:
        src = str((ann.get("source_info") or {}).get("source_video") or "").strip()
        if src:
            _add(paths.video_dir / Path(src).name)
    for ext in VIDEO_EXTENSIONS:
        _add(paths.video_dir / f"{stem}{ext}")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def playback_video_path_for_record(record_id: str) -> Path | None:
    """回放用视频路径：仅返回有效预览，否则原片。"""
    path = video_path_for_record(record_id)
    if not path or not path.is_file():
        return None
    return resolve_playback_serve_path(path)


def playback_video_prepare_status(record_id: str) -> dict[str, Any]:
    """启动或查询预览转码进度。"""
    path = video_path_for_record(record_id)
    if not path or not path.is_file():
        return {
            "status": "missing",
            "progress": 0,
            "needs_transcode": False,
            "source_height": 0,
            "preview_height": 0,
            "message": "配套视频不存在",
            "error": "",
        }
    return ensure_preview_transcode_async(path)


def persist_record_video(
    src: Path,
    pose_path: Path,
    *,
    camera_slug: str | None = None,
) -> Path:
    """保存配套视频到 localdata/video；源分辨率过高时按配置转码为预览高度。"""
    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(f"源视频不存在: {src}")
    paths = resolve_app_paths()
    suffix = src.suffix.lower() if src.suffix else ".mp4"
    dest = record_video_path(paths, pose_path, suffix, camera_slug=camera_slug)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.resolve() == src.resolve():
        raise ValueError(f"源与目标相同，拒绝操作: {src}")
    if dest.is_file():
        dest.unlink()

    th = default_playback_transcode_height()
    src_h = read_video_height(src)
    if th > 0 and src_h > th and transcode_preview_video(src, dest, th):
        if not src.is_file():
            raise RuntimeError(f"源视频在转码后丢失（不应发生）: {src}")
        return dest

    shutil.copy2(src, dest)
    if not src.is_file():
        raise RuntimeError(f"源视频在复制后丢失（不应发生）: {src}")
    return dest




def _read_sidecar_meta(record_id: str, locator) -> dict[str, Any]:
    sidecar = meta_path_for_record(record_id, locator)
    if not sidecar.is_file():
        return {}
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _video_stem_for_summary(record_id: str, locator, meta: dict[str, Any]) -> str:
    for key in ("video_stem", "display_name"):
        v = str(meta.get(key) or "").strip()
        if v:
            return sanitize_file_stem(v)
    src = str(meta.get("source_video") or "").strip()
    if src:
        return sanitize_file_stem(Path(src).stem)
    if locator.path.is_dir():
        return sanitize_file_stem(locator.path.name)
    return sanitize_file_stem(Path(str(record_id)).name)


def _has_video_fast(record_id: str, locator, meta: dict[str, Any], paths: AppPaths) -> bool:
    video_base = _video_base_for_record(paths, record_id, meta)
    pkg_stem = locator.path.name if locator.path.is_dir() else Path(str(record_id)).name

    vf = str(meta.get("video_file") or "").strip()
    if vf:
        if (video_base / vf).is_file() or (paths.video_dir / vf).is_file():
            return True
    src = str(meta.get("source_video") or "").strip()
    if src:
        name = Path(src).name
        if (video_base / name).is_file() or (paths.video_dir / name).is_file():
            return True
    for stem_key in (str(meta.get("video_stem") or "").strip(), pkg_stem):
        if not stem_key:
            continue
        safe = sanitize_file_stem(stem_key)
        for ext in VIDEO_EXTENSIONS:
            if (video_base / f"{safe}{ext}").is_file() or (paths.video_dir / f"{safe}{ext}").is_file():
                return True
    return False


def _has_annotation_fast(locator, video_stem: str, paths: AppPaths) -> bool:
    if locator.path.is_dir() and (locator.path / "annotation.json").is_file():
        return True
    ann_path = paths.annotation_dir / f"{sanitize_file_stem(video_stem)}.json"
    return ann_path.is_file()


def _parse_event_total(review: dict[str, Any]) -> int | None:
    raw = review.get("event_total")
    if raw is None:
        return None
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return None


def _collision_computed_from_meta(meta: dict[str, Any], collision_enabled: bool) -> bool:
    if "collision_computed" in meta:
        return bool(meta.get("collision_computed"))
    return collision_enabled


def sync_event_review_for_list(locator) -> tuple[dict[str, Any], str]:
    """列表/元数据：无碰撞记录写入 event_review.json，状态与已复核一样常驻磁盘。"""
    review = load_event_review(locator)
    meta = _read_sidecar_meta(locator.record_id, locator)
    if not record_has_skeleton_data(locator, meta):
        try:
            review = ensure_no_collision_review_completed(locator, event_count=0)
        except (RuntimeError, OSError, ValueError):
            pass
        status = resolve_event_review_status(review, event_count=0)
        return review, status

    header: dict[str, Any] = {}
    collision = meta.get("collision")
    if collision is None:
        try:
            header = load_pose_header(locator)
            collision = header.get("collision")
        except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError):
            collision = None
    collision_enabled = isinstance(collision, dict) and bool(collision.get("enabled"))
    if not _collision_computed_from_meta(meta, collision_enabled):
        cached_total = _parse_event_total(review)
        status = resolve_event_review_status(review, event_count=cached_total)
        return review, status

    cached_total = _parse_event_total(review)
    if is_persisted_review_terminal(review):
        status = persisted_event_review_status(review)
        return review, status
    if cached_total is not None and cached_total > 0:
        status = resolve_event_review_status(review, event_count=cached_total)
        return review, status
    try:
        review = ensure_no_collision_review_completed(
            locator,
            event_count=0 if cached_total == 0 else None,
        )
    except (RuntimeError, OSError, ValueError):
        pass
    cached_total = _parse_event_total(review)
    status = resolve_event_review_status(review, event_count=cached_total)
    return review, status


def record_summary_for_list(locator, paths: AppPaths | None = None) -> dict[str, Any]:
    """回放列表用轻量元数据（避免 reflection/标注全文/重复定位）。"""
    paths = paths or resolve_app_paths()
    record_id = locator.record_id
    meta = _read_sidecar_meta(record_id, locator)

    header: dict[str, Any] = {}
    if meta.get("frame_count") is None or meta.get("collision") is None:
        try:
            header = load_pose_header(locator)
        except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError):
            header = {}

    collision = meta.get("collision") if meta.get("collision") is not None else header.get("collision")
    collision_enabled = isinstance(collision, dict) and bool(collision.get("enabled"))
    collision_computed = _collision_computed_from_meta(meta, collision_enabled)
    frame_count = meta.get("frame_count") if meta.get("frame_count") is not None else header.get("frame_count")

    camera_slug = str(meta.get("camera_slug") or "").strip()
    pose_model_tier = str(meta.get("pose_model_tier") or "").strip()
    if not camera_slug or not pose_model_tier:
        parsed_tier, parsed_slug, _ = parse_record_path_segments(record_id)
        camera_slug = camera_slug or (parsed_slug or "")
        pose_model_tier = pose_model_tier or (parsed_tier or "")
    if not pose_model_tier:
        variant = str(meta.get("variant") or header.get("variant") or "")
        backend_for_tier = str(meta.get("backend") or variant_to_backend(variant))
        pose_model_tier = pose_model_tier_from_backend(backend_for_tier)

    video_stem = _video_stem_for_summary(record_id, locator, meta)
    backend = str(meta.get("backend") or variant_to_backend(str(meta.get("variant") or header.get("variant") or "")))
    display_name = str(meta.get("display_name") or "").strip() or display_name_from_pose_file(
        record_id if locator.storage == STORAGE_V2_PARQUET else locator.path.name,
        backend,
    )

    review, review_status = sync_event_review_for_list(locator)

    if locator.storage == STORAGE_V2_PARQUET:
        pose_file = f"{record_id}/manifest.json"
        pose_label = record_id
    else:
        pose_file = locator.path.name
        pose_label = locator.path.name

    has_video = _has_video_fast(record_id, locator, meta, paths)
    return {
        "record_id": record_id,
        "storage": locator.storage,
        "display_name": display_name,
        "camera_slug": camera_slug,
        "pose_model_tier": pose_model_tier,
        "camera_label": str(meta.get("camera_label") or "").strip(),
        "video_stem": video_stem,
        "frame_count": frame_count,
        "has_video": has_video,
        "has_stored_annotation": _has_annotation_fast(locator, video_stem, paths),
        "collision_enabled": collision_enabled,
        "collision_computed": collision_computed,
        "event_review_status": review_status,
        "event_review_label": event_review_status_label(review_status),
        "event_review_verified_count": len(review.get("verified_true") or [])
        if isinstance(review.get("verified_true"), list)
        else 0,
        "event_review_total": _parse_event_total(review),
        "pose_file": pose_file,
        "pose_label": pose_label,
        "summary": True,
        "tags": [],
    }


def attach_tags_to_summaries(items: list[dict[str, Any]]) -> None:
    """批量合并 SQLite 标签，避免列表接口 N+1 查询。"""
    if not items:
        return
    record_ids = [str(item.get("record_id") or "").strip() for item in items]
    record_ids = [rid for rid in record_ids if rid]
    if not record_ids:
        return
    tag_map = get_tags_map(record_ids)
    for item in items:
        rid = str(item.get("record_id") or "").strip()
        item["tags"] = tag_map.get(rid, [])


def record_meta_for_list(locator) -> dict[str, Any]:
    paths = resolve_app_paths()
    record_id = locator.record_id
    meta: dict[str, Any] = {
        "record_id": record_id,
        "storage": locator.storage,
        "pose_url": f"/api/records/{record_id}/manifest.json",
        "manifest_url": f"/api/records/{record_id}/manifest.json",
        "frames_url": f"/api/records/{record_id}/frames",
    }
    if locator.storage == STORAGE_V2_PARQUET:
        meta["pose_file"] = f"{record_id}/manifest.json"
        meta["pose_label"] = record_id
    else:
        meta["pose_file"] = locator.path.name
        meta["pose_label"] = locator.path.name

    sidecar = meta_path_for_record(record_id, locator)
    if sidecar.is_file():
        try:
            meta.update(json.loads(sidecar.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass

    try:
        header = load_pose_header(locator)
        for key in (
            "schema",
            "frame_count",
            "backend",
            "variant",
            "det_backend",
            "det_variant",
            "model",
            "det_model",
            "fps",
            "collision",
            "annotation",
            "infer_width",
            "infer_height",
        ):
            if key not in meta and key in header:
                meta[key] = header[key]
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        pass

    if "backend" not in meta and "variant" in meta:
        meta["backend"] = variant_to_backend(str(meta["variant"]))
    if not meta.get("display_name"):
        meta["display_name"] = display_name_from_pose_file(
            meta.get("pose_label") or record_id, str(meta.get("backend") or "")
        )
    if not meta.get("camera_slug") or not meta.get("pose_model_tier"):
        parsed_tier, parsed_slug, _ = parse_record_path_segments(record_id)
        if parsed_slug and not meta.get("camera_slug"):
            meta["camera_slug"] = parsed_slug
        if parsed_tier and not meta.get("pose_model_tier"):
            meta["pose_model_tier"] = parsed_tier
        elif not meta.get("pose_model_tier"):
            variant = str(meta.get("variant") or "")
            if variant:
                meta["pose_model_tier"] = pose_model_tier_from_variant(variant)
    video_stem = resolve_video_stem_from_record(
        record_id,
        json_dir=paths.json_dir,
        pose_path=locator.path,
        meta=meta,
    )
    meta["video_stem"] = video_stem
    stored_ann = load_annotation_json(video_stem, annotation_dir=paths.annotation_dir)
    ann_on_disk = resolve_annotation_path_for_record(record_id, locator=locator, meta=meta)
    meta["has_stored_annotation"] = stored_ann is not None or (
        ann_on_disk is not None and ann_on_disk.is_file()
    )
    if meta["has_stored_annotation"] and not meta.get("has_annotation"):
        meta["has_annotation"] = True
    vpath = video_path_for_record(record_id)
    meta["has_video"] = bool(vpath and vpath.is_file())
    if meta["has_video"]:
        meta["video_file"] = vpath.name
        meta["video_url"] = f"/api/records/{record_id}/video"
    else:
        meta.pop("video_url", None)

    review, review_status = sync_event_review_for_list(locator)
    meta["event_review_status"] = review_status
    meta["event_review_label"] = event_review_status_label(review_status)
    meta["event_review_verified_count"] = len(review.get("verified_true") or [])
    event_total = _parse_event_total(review)
    if event_total is not None:
        meta["event_review_total"] = event_total
    return meta


def _annotation_file_candidates(ann_dir: Path, ann_name: str) -> list[Path]:
    """meta.annotation_file 可能为 71.json / 71 / 路径形式。"""
    name = str(ann_name or "").strip()
    if not name:
        return []
    ordered: list[Path] = [ann_dir / name]
    if not name.lower().endswith(".json"):
        ordered.append(ann_dir / f"{name}.json")
    stem = Path(name).stem
    if stem:
        ordered.append(ann_dir / f"{stem}.json")
    seen: set[str] = set()
    out: list[Path] = []
    for path in ordered:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def _meta_camera_label(meta: dict[str, Any] | None) -> str:
    """meta 机位名；无 camera_label 时回退 camera_slug。"""
    if not meta:
        return ""
    cam = str(meta.get("camera_label") or "").strip()
    if cam:
        return cam
    return str(meta.get("camera_slug") or "").strip()


def _reflection_fallback_ann_dir(paths: AppPaths, ann_dir: Path) -> Path | None:
    try:
        if ann_dir.resolve() == paths.annotation_dir.resolve():
            return None
    except (OSError, ValueError):
        if str(ann_dir) == str(paths.annotation_dir):
            return None
    return paths.annotation_dir


def _resolve_reflection_merged_annotation(
    meta: dict[str, Any] | None,
    *,
    paths: AppPaths,
    ann_dir: Path,
) -> Path | None:
    """多编号机位：合并 reflection 下全部标注 JSON。"""
    if not meta:
        return None
    meta_with_cam = dict(meta)
    if not str(meta_with_cam.get("camera_label") or "").strip():
        slug = str(meta_with_cam.get("camera_slug") or "").strip()
        if slug:
            meta_with_cam["camera_label"] = slug
    return resolve_reflection_annotation_path(
        meta_with_cam,
        ann_dir=ann_dir,
        fallback_ann_dir=_reflection_fallback_ann_dir(paths, ann_dir),
    )


def _try_reflection_annotation_files_in_dir(
    meta: dict[str, Any] | None,
    *,
    ann_dir: Path,
    fallback_ann_dir: Path | None = None,
) -> Path | None:
    """按 reflection 机位解析标注；多编号时合并全部货框。"""
    return resolve_reflection_annotation_path(
        meta,
        ann_dir=ann_dir,
        fallback_ann_dir=fallback_ann_dir,
    )


def resolve_reflection_annotation_path(
    meta: dict[str, Any] | None,
    *,
    ann_dir: Path,
    fallback_ann_dir: Path | None = None,
) -> Path | None:
    """机位 reflection → 单文件或合并后的标注 JSON（用于碰撞重算 / 回放）。"""
    if not meta:
        return None
    cam = _meta_camera_label(meta)
    if not cam or not REFLECTION_OK or not normalize_corner_label:
        return None
    try:
        from corner_label.resolve import (
            collect_annotation_paths_for_camera,
            materialize_annotation_paths,
        )

        reflection = load_reflection_or_http()
        label = normalize_corner_label(cam)
        fallbacks = (fallback_ann_dir,) if fallback_ann_dir else ()
        src_paths = collect_annotation_paths_for_camera(
            label, reflection, ann_dir, *fallbacks
        )
        return materialize_annotation_paths(src_paths, label)
    except (HTTPException, ValueError, FileNotFoundError, OSError):
        return None


def _resolve_annotation_in_dir(
    record_id: str,
    *,
    paths: AppPaths,
    ann_dir: Path,
    locator,
    meta: dict[str, Any] | None,
    allow_reflection: bool,
) -> Path | None:
    """在指定 annotations 目录下解析标注；多编号机位优先合并 reflection 全部编号。"""
    if allow_reflection:
        reflection_hit = _resolve_reflection_merged_annotation(
            meta,
            paths=paths,
            ann_dir=ann_dir,
        )
        if reflection_hit and reflection_hit.is_file():
            return reflection_hit

    if meta:
        ann_name = str(meta.get("annotation_file") or "").strip()
        if ann_name:
            for candidate in _annotation_file_candidates(ann_dir, ann_name):
                if candidate.is_file():
                    return candidate

    video_stem = resolve_video_stem_from_record(
        record_id,
        json_dir=paths.json_dir,
        pose_path=locator.path,
        meta=meta,
    )
    ann_path = annotation_path_for_video_stem(video_stem, annotation_dir=ann_dir)
    if ann_path.is_file():
        return ann_path
    return None


def resolve_annotation_path_for_source(
    record_id: str,
    *,
    source: str,
    paths: AppPaths | None = None,
    locator=None,
    meta: dict[str, Any] | None = None,
) -> tuple[Path | None, str]:
    """
    按标注来源（master / rtmpose-*）解析标注路径。
    返回 (path, resolved_from)；resolved_from 为 tier | master | none。
    """
    paths = paths or resolve_app_paths()
    norm = normalize_annotation_source(source)
    ann_dir = annotation_dir_for_source(paths, norm)

    if locator is None:
        locator = locate_record_by_id(record_id)
    if not locator:
        return None, "none"

    sidecar = meta_path_for_record(record_id, locator)
    if meta is None and sidecar.is_file():
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = None

    path = _resolve_annotation_in_dir(
        record_id,
        paths=paths,
        ann_dir=ann_dir,
        locator=locator,
        meta=meta,
        allow_reflection=True,
    )
    if path:
        try:
            in_tier = path.resolve().is_relative_to(ann_dir.resolve())
        except AttributeError:
            in_tier = str(path.resolve()).startswith(str(ann_dir.resolve()))
        tag = "tier" if norm != ANNOTATION_SOURCE_MASTER and in_tier else "master"
        return path, tag

    if norm != ANNOTATION_SOURCE_MASTER:
        master_path = _resolve_annotation_in_dir(
            record_id,
            paths=paths,
            ann_dir=paths.annotation_dir,
            locator=locator,
            meta=meta,
            allow_reflection=True,
        )
        if master_path:
            return master_path, "master"

    return None, "none"


def resolve_annotation_path_for_record(
    record_id: str,
    locator=None,
    meta: dict[str, Any] | None = None,
) -> Path | None:
    """解析记录关联的标注 JSON（机位 reflection / 包内副本 / annotations/{video_stem}）。"""
    if locator is None:
        locator = locate_record_by_id(record_id)
    if not locator:
        return None
    paths = resolve_app_paths()
    sidecar = meta_path_for_record(record_id, locator)
    if meta is None and sidecar.is_file():
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = None

    parsed_tier, _, _ = parse_record_path_segments(record_id)
    tier_dir = (
        annotation_dir_for_source(paths, parsed_tier)
        if parsed_tier
        else paths.annotation_dir
    )
    reflection_merged = _resolve_reflection_merged_annotation(
        meta if isinstance(meta, dict) else None,
        paths=paths,
        ann_dir=tier_dir,
    )
    if reflection_merged and reflection_merged.is_file():
        return reflection_merged

    if locator.path.is_dir():
        pkg_ann = locator.path / "annotation.json"
        if pkg_ann.is_file():
            return pkg_ann

    path = _resolve_annotation_in_dir(
        record_id,
        paths=paths,
        ann_dir=paths.annotation_dir,
        locator=locator,
        meta=meta,
        allow_reflection=True,
    )
    if path:
        return path

    if sidecar.is_file():
        legacy = sidecar.parent / f"{locator.path.name}_annotation.json"
        if legacy.is_file():
            return legacy
    legacy_flat = paths.json_dir / f"{str(record_id).replace('/', '_')}_annotation.json"
    if legacy_flat.is_file():
        return legacy_flat
    return None


def annotation_path_for_record(record_id: str, locator=None) -> Path | None:
    return resolve_annotation_path_for_record(record_id, locator=locator)


def persist_playback_annotation(
    annotation_path: Path,
    *,
    video_stem: str,
    pose_path: Path,
    source_video: str = "",
) -> Path | None:
    """采集完成后写入回放可查找的标注（annotations/{video_stem}.json + 包内 annotation.json）。"""
    if not annotation_path.is_file() or not video_stem:
        return None
    paths = resolve_app_paths()
    try:
        raw = load_annotation_config(annotation_path)
        normalized = normalize_annotation_payload(
            raw,
            video_stem=video_stem,
            source_video=source_video or str(raw.get("source_info", {}).get("source_video") or ""),
        )
        dest = save_annotation_json(video_stem, normalized, annotation_dir=paths.annotation_dir)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if pose_path.is_dir():
        try:
            shutil.copy2(dest, pose_path / "annotation.json")
        except OSError:
            pass
    return dest

