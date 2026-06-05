"""货位标注存储：每个视频（video_stem）仅保留一份 JSON，新上传/保存覆盖旧文件。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from config_loader import sanitize_file_stem
from event_engine.annotation_boxes import flatten_annotation_boxes, load_annotation_config
from pose_store import meta_sidecar_path


def annotation_path_for_video_stem(video_stem: str, *, annotation_dir: Path) -> Path:
    annotation_dir.mkdir(parents=True, exist_ok=True)
    safe = sanitize_file_stem(video_stem)
    return annotation_dir / f"{safe}.json"


def save_annotation_json(
    video_stem: str,
    data: dict[str, Any],
    *,
    annotation_dir: Path,
) -> Path:
    """写入标注 JSON（覆盖已有）。"""
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
