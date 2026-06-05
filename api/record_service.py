"""记录定位、元数据与配套视频路径。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from annotation_store import (
    annotation_path_for_video_stem,
    load_annotation_json,
    normalize_annotation_payload,
    resolve_video_stem_from_record,
    save_annotation_json,
    validate_annotation_payload,
)
from event_engine.annotation_boxes import load_annotation_config
from config_loader import (
    record_id_for_pose_path,
    record_video_path,
    resolve_app_paths,
    sanitize_file_stem,
    variant_to_backend,
)
from model_assets import VIDEO_EXTENSIONS
from pose_store import (
    STORAGE_V2_PARQUET,
    load_pose_header,
    locate_record as find_record,
    meta_sidecar_path,
)

from api.naming import display_name_from_pose_file
from api.reflection_service import REFLECTION_OK, load_reflection_or_http, normalize_corner_label

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
) -> Path:
    paths = resolve_app_paths()
    normalized = normalize_annotation_payload(
        payload,
        video_stem=video_stem,
        source_video=source_video,
        frame_width=frame_width,
        frame_height=frame_height,
    )
    _, err = validate_annotation_payload(normalized)
    if err:
        raise ValueError(err)
    return save_annotation_json(video_stem, normalized, annotation_dir=paths.annotation_dir)


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

    camera_slug = ""
    if meta:
        camera_slug = str(meta.get("camera_slug") or "").strip()
    if not camera_slug and "/" in str(record_id):
        camera_slug = str(record_id).split("/", 1)[0]
    video_base = paths.video_dir / camera_slug if camera_slug else paths.video_dir
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


def persist_record_video(
    src: Path,
    pose_path: Path,
    *,
    camera_slug: str | None = None,
) -> Path:
    paths = resolve_app_paths()
    suffix = src.suffix.lower() if src.suffix else ".mp4"
    dest = record_video_path(paths, pose_path, suffix, camera_slug=camera_slug)
    if dest.is_file():
        dest.unlink()
    shutil.move(str(src), str(dest))
    return dest




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
    if "/" in record_id and not meta.get("camera_slug"):
        meta["camera_slug"] = record_id.split("/", 1)[0]
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
    return meta


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

    if locator.path.is_dir():
        pkg_ann = locator.path / "annotation.json"
        if pkg_ann.is_file():
            return pkg_ann

    if meta:
        ann_name = str(meta.get("annotation_file") or "").strip()
        if ann_name:
            by_name = paths.annotation_dir / ann_name
            if by_name.is_file():
                return by_name
        cam = str(meta.get("camera_label") or "").strip()
        if cam and REFLECTION_OK and normalize_corner_label:
            try:
                from corner_label.resolve import resolve_annotation_for_camera as resolve_cam

                reflection = load_reflection_or_http()
                resolved = resolve_cam(
                    normalize_corner_label(cam),
                    reflection=reflection,
                    annotations_dir=paths.annotation_dir,
                )
                if resolved.annotation_path.is_file():
                    return resolved.annotation_path
            except (HTTPException, ValueError, FileNotFoundError):
                pass

    video_stem = resolve_video_stem_from_record(
        record_id,
        json_dir=paths.json_dir,
        pose_path=locator.path,
        meta=meta,
    )
    ann_path = annotation_path_for_video_stem(video_stem, annotation_dir=paths.annotation_dir)
    if ann_path.is_file():
        return ann_path

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

