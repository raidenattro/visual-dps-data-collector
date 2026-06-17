"""货位标注存储：母本在 localdata/json/annotations；各模型层在 json/{tier}/annotations。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from config_loader import AppPaths, is_pose_model_tier, sanitize_file_stem
from event_engine.annotation_boxes import flatten_annotation_boxes, load_annotation_config
from pose_store import meta_sidecar_path

# 母本标注目录（paths.annotation_dir），标注页保存时不写入此处
ANNOTATION_SOURCE_MASTER = "master"


def normalize_annotation_source(raw: str) -> str:
    """master / 母本 / rtmpose-t / t → 规范来源键。"""
    s = str(raw or "").strip().lower().replace("_", "-")
    if s in ("", "master", "母本", "canonical", "base"):
        return ANNOTATION_SOURCE_MASTER
    if is_pose_model_tier(s):
        return s
    if s in ("t", "s", "m"):
        return f"rtmpose-{s}"
    raise ValueError(
        f"无效 annotation_source: {raw!r}，可选 master / rtmpose-t / rtmpose-s / rtmpose-m"
    )


def annotation_dir_for_source(paths: AppPaths, source: str) -> Path:
    """母本 → paths.annotation_dir；模型层 → json_dir/{tier}/annotations。"""
    norm = normalize_annotation_source(source)
    if norm == ANNOTATION_SOURCE_MASTER:
        return paths.annotation_dir
    d = paths.json_dir / norm / "annotations"
    d.mkdir(parents=True, exist_ok=True)
    return d


def annotation_dir_display_rel(paths: AppPaths, source: str) -> str:
    """用于 API / 前端展示的路径片段。"""
    norm = normalize_annotation_source(source)
    if norm == ANNOTATION_SOURCE_MASTER:
        try:
            return paths.annotation_dir.relative_to(paths.json_dir.parent).as_posix()
        except ValueError:
            return str(paths.annotation_dir)
    return f"{paths.json_dir.name}/{norm}/annotations"


def materialize_tier_annotation_from_master(
    video_stem: str,
    *,
    paths: AppPaths,
    source: str,
) -> Path | None:
    """模型目录无标注时，从母本复制到 json/{tier}/annotations/{stem}.json。"""
    norm = normalize_annotation_source(source)
    if norm == ANNOTATION_SOURCE_MASTER:
        p = annotation_path_for_video_stem(video_stem, annotation_dir=paths.annotation_dir)
        return p if p.is_file() else None
    tier_dir = annotation_dir_for_source(paths, norm)
    tier_path = annotation_path_for_video_stem(video_stem, annotation_dir=tier_dir)
    if tier_path.is_file():
        return tier_path
    master_path = annotation_path_for_video_stem(video_stem, annotation_dir=paths.annotation_dir)
    if not master_path.is_file():
        return None
    shutil.copy2(master_path, tier_path)
    return tier_path


def load_annotation_for_source(
    video_stem: str,
    *,
    paths: AppPaths,
    source: str,
    materialize: bool = False,
) -> tuple[dict[str, Any] | None, Path, str]:
    """
    按来源加载标注。
    返回 (data, annotation_dir, resolved_from)；resolved_from: master | tier | none。
    模型层无文件时回退母本内容；materialize=True 时复制到模型目录。
    """
    norm = normalize_annotation_source(source)
    stem = sanitize_file_stem(video_stem)
    if norm == ANNOTATION_SOURCE_MASTER:
        data = load_annotation_json(stem, annotation_dir=paths.annotation_dir)
        return data, paths.annotation_dir, "master" if data else "none"

    tier_dir = annotation_dir_for_source(paths, norm)
    tier_path = annotation_path_for_video_stem(stem, annotation_dir=tier_dir)
    if tier_path.is_file():
        data = load_annotation_json(stem, annotation_dir=tier_dir)
        return data, tier_dir, "tier" if data else "none"

    master_path = annotation_path_for_video_stem(stem, annotation_dir=paths.annotation_dir)
    if not master_path.is_file():
        return None, tier_dir, "none"

    if materialize:
        shutil.copy2(master_path, tier_path)
        data = load_annotation_json(stem, annotation_dir=tier_dir)
        return data, tier_dir, "tier" if data else "none"

    data = load_annotation_json(stem, annotation_dir=paths.annotation_dir)
    return data, tier_dir, "master"


def annotation_path_for_video_stem(video_stem: str, *, annotation_dir: Path) -> Path:
    annotation_dir.mkdir(parents=True, exist_ok=True)
    safe = sanitize_file_stem(video_stem)
    return annotation_dir / f"{safe}.json"


def annotation_file_exists(annotation_dir: Path, stem: str) -> bool:
    return annotation_path_for_video_stem(stem, annotation_dir=annotation_dir).is_file()


def allocate_annotation_stem(annotation_dir: Path, base_stem: str) -> str:
    """标注文件名分配：无 71.json 用 71；已有则 71-(2)、71-(3)…"""
    base = sanitize_file_stem(base_stem)
    if not annotation_file_exists(annotation_dir, base):
        return base
    for n in range(2, 10_000):
        candidate = f"{base}-({n})"
        if not annotation_file_exists(annotation_dir, candidate):
            return candidate
    raise ValueError(f"标注 {base_stem} 可用文件名过多，请清理 annotations 目录后重试")


def resolve_annotation_save_stem(
    annotation_dir: Path,
    requested_stem: str,
    *,
    preserve_existing: bool = False,
) -> str:
    """决定本次写入的标注 stem（覆盖或另存）。"""
    base = sanitize_file_stem(requested_stem)
    if preserve_existing:
        return allocate_annotation_stem(annotation_dir, base)
    return base


def save_annotation_json(
    video_stem: str,
    data: dict[str, Any],
    *,
    annotation_dir: Path,
) -> Path:
    """写入标注 JSON（路径由 video_stem 决定，可能为 71-(2) 等）。"""
    path = annotation_path_for_video_stem(video_stem, annotation_dir=annotation_dir)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def load_annotation_json(video_stem: str, *, annotation_dir: Path) -> dict[str, Any] | None:
    path = annotation_path_for_video_stem(video_stem, annotation_dir=annotation_dir)
    if not path.is_file():
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else None


def validate_annotation_payload(data: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    boxes = flatten_annotation_boxes(data)
    if not boxes:
        return [], "标注 JSON 无有效 boxes/shelves"
    for box in boxes:
        poly = box.get("video_polygon") or box.get("video_polygon_norm")
        if not isinstance(poly, list) or len(poly) < 3:
            return [], f"货位 {box.get('box_id')} 缺少有效多边形"
    return boxes, None


def _legacy_to_shelves(data: dict[str, Any], *, shelf_code: str) -> list[dict[str, Any]]:
    """legacy 顶层 boxes/shelf_corners 转为 visual-dps shelves[]。"""
    if isinstance(data.get("shelves"), list) and data["shelves"]:
        return data["shelves"]
    boxes = data.get("boxes")
    if not isinstance(boxes, list):
        boxes = flatten_annotation_boxes(data)
    return [
        {
            "shelf_code": shelf_code,
            "shelf_name": "",
            "shelf_corners": data.get("shelf_corners") if isinstance(data.get("shelf_corners"), list) else [],
            "grid_shape": data.get("grid_shape") if isinstance(data.get("grid_shape"), list) else [],
            "boxes": boxes if isinstance(boxes, list) else [],
        }
    ]


def normalize_annotation_payload(
    data: dict[str, Any],
    *,
    video_stem: str,
    source_video: str = "",
    frame_width: int = 0,
    frame_height: int = 0,
) -> dict[str, Any]:
    """补全 visual-dps 兼容字段（shelves[] + annotation_size）。"""
    out = dict(data)
    w = int(frame_width) or int(out.get("annotation_size", {}).get("width") or 0)
    h = int(frame_height) or int(out.get("annotation_size", {}).get("height") or 0)
    if w > 0 and h > 0:
        out["annotation_size"] = {"width": w, "height": h}

    stem = sanitize_file_stem(video_stem)
    src = out.get("source_info") if isinstance(out.get("source_info"), dict) else {}
    shelf_code = str(src.get("shelf_code") or stem or "SHELF_1").strip() or "SHELF_1"
    out["source_info"] = {
        **src,
        "capture_source": "video",
        "video_stem": stem,
        "source_video": source_video or src.get("source_video") or f"{stem}.mp4",
        "shelf_code": shelf_code,
    }
    out["shelves"] = _legacy_to_shelves(out, shelf_code=shelf_code)
    return out


def resolve_video_stem_from_record(
    record_id: str,
    *,
    json_dir: Path,
    pose_path: Path | None = None,
    meta: dict[str, Any] | None = None,
) -> str:
    """从记录 meta / pose 文件名推断视频主名。"""
    if meta:
        for key in ("video_stem", "display_name"):
            v = str(meta.get(key) or "").strip()
            if v:
                return sanitize_file_stem(v)
        src = str(meta.get("source_video") or "").strip()
        if src:
            return sanitize_file_stem(Path(src).stem)

    if pose_path is None:
        direct = json_dir / f"{record_id}.json"
        if direct.is_file() and not direct.name.endswith(".meta.json"):
            pose_path = direct
        else:
            for p in json_dir.glob("*.json"):
                if p.name.endswith(".meta.json"):
                    continue
                if p.stem == record_id or p.stem.startswith(record_id + "_"):
                    pose_path = p
                    break

    if pose_path and pose_path.is_dir():
        sidecar = meta_sidecar_path(json_dir, record_id) if record_id else None
        if sidecar and sidecar.is_file():
            try:
                m = json.loads(sidecar.read_text(encoding="utf-8"))
                if isinstance(m, dict):
                    return resolve_video_stem_from_record(record_id, json_dir=json_dir, meta=m)
            except json.JSONDecodeError:
                pass
        stem = pose_path.name
        for tag in ("_rtmpose_t", "_rtmpose_s", "_rtmpose_m"):
            if stem.endswith(tag):
                return sanitize_file_stem(stem[: -len(tag)] or stem)
        return sanitize_file_stem(stem)

    if pose_path and pose_path.is_file():
        sidecar = pose_path.with_suffix(".meta.json")
        if sidecar.is_file():
            try:
                m = json.loads(sidecar.read_text(encoding="utf-8"))
                if isinstance(m, dict):
                    return resolve_video_stem_from_record(record_id, json_dir=json_dir, meta=m)
            except json.JSONDecodeError:
                pass
        stem = pose_path.stem
        for tag in ("_rtmpose_t", "_rtmpose_s", "_rtmpose_m"):
            if stem.endswith(tag):
                return sanitize_file_stem(stem[: -len(tag)] or stem)
        return sanitize_file_stem(stem)

    if record_id and "/" in str(record_id):
        return sanitize_file_stem(Path(str(record_id)).name)

    return sanitize_file_stem(record_id)


def load_annotation_for_collect(
    video_stem: str,
    *,
    annotation_dir: Path,
    upload_path: Path | None = None,
) -> Path | None:
    """采集前解析标注：上传优先并覆盖；否则用已存标注。"""
    stem = sanitize_file_stem(video_stem)
    dest = annotation_path_for_video_stem(stem, annotation_dir=annotation_dir)
    if upload_path and upload_path.is_file():
        data = load_annotation_config(upload_path)
        boxes, err = validate_annotation_payload(data)
        if err:
            raise ValueError(err)
        fw, fh = _annotation_frame_size(data)
        normalized = normalize_annotation_payload(
            data,
            video_stem=stem,
            source_video=upload_path.name,
            frame_width=fw,
            frame_height=fh,
        )
        save_annotation_json(stem, normalized, annotation_dir=annotation_dir)
        return dest
    if dest.is_file():
        return dest
    return None


def require_annotation_for_collect(
    video_stem: str,
    *,
    annotation_dir: Path,
    upload_path: Path | None = None,
) -> Path:
    """采集前必须能解析到有效货框标注，否则无法计算并落盘碰撞事件。"""
    path = load_annotation_for_collect(
        video_stem,
        annotation_dir=annotation_dir,
        upload_path=upload_path,
    )
    if path is None:
        raise FileNotFoundError(
            "采集前须提供货框标注：上传标注 JSON，或在「标注」页按视频主名保存后再采集"
        )
    data = load_annotation_config(path)
    _, err = validate_annotation_payload(data)
    if err:
        raise ValueError(err)
    return path
