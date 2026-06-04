#!/usr/bin/env python3
"""简易 Web：上传视频采集骨架 JSON；默认保存配套视频至 video_dir，回放自动加载。"""

from __future__ import annotations

# 必须在 import onnxruntime / rtmlib 之前加载 NVIDIA DLL（Windows cuDNN）
from ort_cuda_setup import prepare_ort_cuda_dll_path

prepare_ort_cuda_dll_path()

import json
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from annotation_store import (
    annotation_path_for_video_stem,
    load_annotation_json,
    normalize_annotation_payload,
    require_annotation_for_collect,
    resolve_video_stem_from_record,
    save_annotation_json,
    validate_annotation_payload,
)
from collect_core import run_collect_job, validate_video_path
from config_loader import (
    build_settings,
    default_pose_json_path,
    default_save_video,
    load_config_file,
    project_root,
    record_video_path,
    resolve_app_paths,
    resolve_config_path,
    sanitize_file_stem,
    variant_to_backend,
)
from model_assets import VIDEO_EXTENSIONS
from export_pose_xlsx import export_pose_to_xlsx_bytes
from pose_store import (
    STORAGE_V2_PARQUET,
    locate_record,
    iter_active_records,
    load_frames_range,
    load_pose_document,
    load_pose_header,
    load_timeline,
    load_events,
    meta_sidecar_path,
    migrate_v1_json_dir,
    record_id_from_path,
    delete_record,
)
from video_frame import first_frame_base64

app = FastAPI(title="visual-dps-datacollect", version="0.2.0")

_VIDEO_MIME = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".m4v": "video/mp4",
}

_jobs_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}


def _get_job(job_id: str) -> dict[str, Any]:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return job


def _update_job(job_id: str, **fields: Any) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(fields)


def _json_archive_dir() -> Path:
    return resolve_app_paths().json_dir / "archive"


def _locate_record(record_id: str, *, include_archive: bool = True):
    paths = resolve_app_paths()
    return locate_record(paths.json_dir, record_id, include_archive=include_archive)


def _record_id_from_pose_path(pose_path: Path) -> str:
    return record_id_from_path(pose_path)


def _meta_path_for_record(record_id: str, locator=None) -> Path:
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


def _resolve_video_stem_for_record(record_id: str, locator=None, meta: dict | None = None) -> str:
    if meta:
        vs = str(meta.get("video_stem") or "").strip()
        if vs:
            return vs
        src = str(meta.get("source_video") or "").strip()
        if src:
            return sanitize_file_stem(Path(src).stem)
    if locator is None:
        locator = _locate_record(record_id)
    if locator:
        return resolve_video_stem_from_record(
            record_id,
            json_dir=resolve_app_paths().json_dir,
            pose_path=locator.path,
            meta=meta,
        )
    return sanitize_file_stem(record_id)


def _annotation_path_for_video_stem(video_stem: str) -> Path:
    paths = resolve_app_paths()
    return annotation_path_for_video_stem(video_stem, annotation_dir=paths.annotation_dir)


def _persist_annotation_for_video(
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


def _annotation_frame_size(payload: dict[str, Any]) -> tuple[int, int]:
    ann = payload.get("annotation_size")
    if isinstance(ann, dict):
        try:
            w = int(ann.get("width") or 0)
            h = int(ann.get("height") or 0)
            return w, h
        except (TypeError, ValueError):
            pass
    return 0, 0


def _parse_save_video_flag(raw: Any, *, default: bool) -> bool:
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


def _video_path_for_record(record_id: str) -> Path | None:
    locator = _locate_record(record_id)
    if not locator:
        return None
    paths = resolve_app_paths()
    sidecar = _meta_path_for_record(record_id, locator)
    if sidecar.is_file():
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
            vf = str(meta.get("video_file") or "").strip()
            if vf:
                candidate = paths.video_dir / vf
                if candidate.is_file():
                    return candidate
        except json.JSONDecodeError:
            pass
    stem = locator.record_id
    for ext in VIDEO_EXTENSIONS:
        candidate = paths.video_dir / f"{stem}{ext}"
        if candidate.is_file():
            return candidate
    return None


def _persist_record_video(src: Path, pose_path: Path) -> Path:
    paths = resolve_app_paths()
    suffix = src.suffix.lower() if src.suffix else ".mp4"
    dest = record_video_path(paths, pose_path, suffix)
    if dest.is_file():
        dest.unlink()
    shutil.move(str(src), str(dest))
    return dest


def _display_name_from_pose_file(pose_file: str, backend: str = "") -> str:
    """从 multi-samples_rtmpose_t.json 还原展示名 multi-samples。"""
    stem = Path(pose_file).stem
    if backend:
        suffix = f"_{backend}"
        if stem.endswith(suffix):
            return stem[: -len(suffix)] or stem
    for tag in ("_rtmpose_t", "_rtmpose_s", "_rtmpose_m"):
        if stem.endswith(tag):
            return stem[: -len(tag)] or stem
    return stem


def _run_job(
    job_id: str,
    video_path: Path,
    pose_path: Path,
    *,
    backend: str,
    variant: str,
    det_variant: str,
    det_backend: str,
    video_stem: str,
    source_video_name: str,
    width: int,
    height: int,
    pose_frame_interval: int,
    frame_rate: float,
    max_pose_frames: int | None,
    save_video: bool,
    annotation_path: Path | None = None,
    alarm_min_consecutive_frames: int = 3,
    alarm_cooldown_frames: int = 6,
) -> None:
    settings = build_settings(config_path=resolve_config_path(None), cli={})
    alarm_min = max(1, int(alarm_min_consecutive_frames))
    alarm_cd = max(1, int(alarm_cooldown_frames))

    def on_progress(current: int, total: int) -> None:
        pct = int(current / total * 100) if total > 0 else 0
        _update_job(job_id, progress=pct, message=f"处理中 {current}/{total or '?'} 帧")

    try:
        _update_job(job_id, status="running", message="模型推理中…")
        data = run_collect_job(
            video_path=video_path,
            output_path=pose_path,
            models_dir=settings.models_dir,
            variant=variant,
            det_variant=det_variant,
            device=settings.device,
            ort_backend=settings.ort_backend,
            width=width,
            height=height,
            frame_interval=pose_frame_interval,
            frame_rate=frame_rate,
            max_frames=max_pose_frames,
            on_progress=on_progress,
            annotation_path=str(annotation_path) if annotation_path else None,
            alarm_min_consecutive_frames=alarm_min,
            alarm_cooldown_frames=alarm_cd,
        )
        record_id = _record_id_from_pose_path(pose_path)
        if annotation_path and annotation_path.is_file():
            saved_annotation = annotation_path
        else:
            saved_annotation = None
        saved_video_path: Path | None = None
        if save_video and video_path.is_file():
            try:
                saved_video_path = _persist_record_video(video_path, pose_path)
            except OSError as exc:
                raise RuntimeError(f"保存配套视频失败: {exc}") from exc

        meta = {
            "record_id": record_id,
            "job_id": job_id,
            "display_name": video_stem or _display_name_from_pose_file(record_id, backend),
            "video_stem": video_stem,
            "storage": data.get("storage") or STORAGE_V2_PARQUET,
            "pose_file": f"{record_id}/manifest.json",
            "source_video": source_video_name,
            "backend": backend,
            "variant": variant,
            "det_backend": det_backend,
            "det_variant": det_variant,
            "det_model": data.get("det_model"),
            "frame_count": data.get("frame_count", 0),
            "elapsed_sec": data.get("elapsed_sec"),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "save_video": bool(save_video),
        }
        if saved_annotation and saved_annotation.is_file():
            meta["annotation_file"] = saved_annotation.name
            meta["has_annotation"] = True
            meta["annotation_url"] = f"/api/annotations/by-video/{video_stem}"
            meta["collision_enabled"] = bool(data.get("collision", {}).get("enabled"))
        elif data.get("annotation"):
            meta["has_annotation"] = True
            meta["collision_enabled"] = True
        else:
            meta["has_annotation"] = False
            meta["collision_enabled"] = False
        if saved_video_path and saved_video_path.is_file():
            meta["video_file"] = saved_video_path.name
            meta["video_url"] = f"/api/records/{record_id}/video"
            meta["has_video"] = True
        else:
            meta["has_video"] = False

        paths = resolve_app_paths()
        sidecar = meta_sidecar_path(paths.json_dir, record_id)
        with open(sidecar, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        _update_job(
            job_id,
            status="done",
            progress=100,
            message="完成",
            frame_count=data.get("frame_count", 0),
            pose_url=f"/api/records/{record_id}/manifest.json",
            manifest_url=f"/api/records/{record_id}/manifest.json",
            frames_url=f"/api/records/{record_id}/frames",
            record_id=record_id,
            pose_file=f"{record_id}/manifest.json",
            display_name=meta["display_name"],
            has_video=meta.get("has_video", False),
            has_annotation=meta.get("has_annotation", False),
            collision_enabled=meta.get("collision_enabled", False),
            video_url=meta.get("video_url"),
            storage=meta.get("storage") or STORAGE_V2_PARQUET,
        )
    except Exception as exc:
        _update_job(job_id, status="error", message=str(exc))
    finally:
        if video_path.is_file():
            try:
                video_path.unlink()
            except OSError:
                pass
        parent = video_path.parent
        if parent.name.startswith("tmp_") and parent.is_dir():
            shutil.rmtree(parent, ignore_errors=True)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config/inference")
def get_inference_config() -> dict[str, Any]:
    """采集页默认推理/碰撞参数（来自 config.json inference 节点）。"""
    cfg = load_config_file(resolve_config_path(None))
    inference = cfg.get("inference") if isinstance(cfg.get("inference"), dict) else {}
    return {
        "frame_rate": float(inference.get("frame_rate") or 0),
        "height": int(inference.get("height") or 480),
        "pose_frame_interval": int(inference.get("pose_frame_interval") or 1),
        "max_pose_frames": int(inference.get("max_pose_frames") or 0),
        "alarm_min_consecutive_frames": max(
            1, int(inference.get("alarm_min_consecutive_frames") or 3)
        ),
        "alarm_cooldown_frames": max(1, int(inference.get("alarm_cooldown_frames") or 6)),
    }


def _record_meta_for_list(locator) -> dict[str, Any]:
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

    sidecar = _meta_path_for_record(record_id, locator)
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
        meta["display_name"] = _display_name_from_pose_file(
            meta.get("pose_label") or record_id, str(meta.get("backend") or "")
        )
    video_stem = resolve_video_stem_from_record(
        record_id,
        json_dir=paths.json_dir,
        pose_path=locator.path,
        meta=meta,
    )
    meta["video_stem"] = video_stem
    stored_ann = load_annotation_json(video_stem, annotation_dir=paths.annotation_dir)
    meta["has_stored_annotation"] = stored_ann is not None
    if stored_ann and not meta.get("has_annotation"):
        meta["has_annotation"] = True
    vpath = _video_path_for_record(record_id)
    meta["has_video"] = bool(vpath and vpath.is_file())
    if meta["has_video"]:
        meta["video_file"] = vpath.name
        meta["video_url"] = f"/api/records/{record_id}/video"
    else:
        meta.pop("video_url", None)
    return meta


@app.get("/api/records")
def list_records() -> list[dict[str, Any]]:
    paths = resolve_app_paths()
    paths.json_dir.mkdir(parents=True, exist_ok=True)
    items = [_record_meta_for_list(loc) for loc in iter_active_records(paths.json_dir)]
    return items[:50]


@app.delete("/api/records/{record_id}")
def delete_record_api(record_id: str) -> dict[str, Any]:
    """删除历史记录：Parquet 包或 v1 JSON、sidecar meta、配套视频（保留 annotations/ 下标注）。"""
    rid = str(record_id or "").strip()
    if not rid:
        raise HTTPException(400, "record_id 无效")
    locator = _locate_record(rid, include_archive=False)
    if not locator:
        raise HTTPException(404, "记录不存在")
    paths = resolve_app_paths()
    video_path = _video_path_for_record(rid)
    try:
        result = delete_record(
            paths.json_dir,
            locator,
            video_path=video_path,
        )
    except OSError as exc:
        raise HTTPException(500, f"删除失败: {exc}") from exc
    return result


@app.get("/api/records/{record_id}/video")
def get_record_video(record_id: str) -> FileResponse:
    path = _video_path_for_record(record_id)
    if not path or not path.is_file():
        raise HTTPException(404, "配套视频不存在")
    media = _VIDEO_MIME.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media)


@app.get("/api/records/{record_id}/annotation.json")
def get_record_annotation(record_id: str) -> FileResponse:
    locator = _locate_record(record_id)
    if not locator:
        raise HTTPException(404, "记录不存在")
    paths = resolve_app_paths()
    sidecar = _meta_path_for_record(record_id, locator)
    meta: dict[str, Any] | None = None
    if sidecar.is_file():
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = None
    video_stem = resolve_video_stem_from_record(
        record_id,
        json_dir=paths.json_dir,
        pose_path=locator.path,
        meta=meta,
    )
    ann_path = annotation_path_for_video_stem(video_stem, annotation_dir=paths.annotation_dir)
    if not ann_path.is_file():
        legacy = paths.json_dir / f"{record_id}_annotation.json"
        if legacy.is_file():
            ann_path = legacy
        else:
            raise HTTPException(404, "标注 JSON 不存在")
    return FileResponse(ann_path, media_type="application/json")


@app.get("/api/records/{record_id}/annotation/frame")
def get_record_annotation_frame(record_id: str) -> dict[str, Any]:
    path = _video_path_for_record(record_id)
    if not path or not path.is_file():
        raise HTTPException(404, "配套视频不存在，无法提取首帧")
    try:
        return first_frame_base64(path)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/annotations/by-video/{video_stem}")
def get_annotation_by_video(video_stem: str) -> dict[str, Any]:
    paths = resolve_app_paths()
    stem = sanitize_file_stem(video_stem)
    data = load_annotation_json(stem, annotation_dir=paths.annotation_dir)
    if not data:
        raise HTTPException(404, "该视频尚无标注")
    return data


@app.get("/api/annotate/options")
def list_annotate_options() -> list[dict[str, Any]]:
    """标注页下拉：全部 pose 记录 + 仅存在于 annotations 目录的条目。"""
    paths = resolve_app_paths()
    paths.annotation_dir.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, Any]] = []
    seen_stems: set[str] = set()

    for locator in iter_active_records(paths.json_dir):
        meta = _record_meta_for_list(locator)
        record_id = locator.record_id
        video_stem = meta.get("video_stem") or record_id
        seen_stems.add(video_stem)
        items.append({
            "video_stem": video_stem,
            "display_name": meta.get("display_name") or video_stem,
            "record_id": record_id,
            "pose_file": meta.get("pose_file") or record_id,
            "source_video": meta.get("source_video") or "",
            "has_video": bool(meta.get("has_video")),
            "has_stored_annotation": bool(meta.get("has_stored_annotation")),
        })

    for ann_path in sorted(paths.annotation_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        stem = ann_path.stem
        if stem in seen_stems:
            continue
        items.append({
            "video_stem": stem,
            "display_name": stem,
            "record_id": "",
            "pose_file": "",
            "source_video": "",
            "has_video": False,
            "has_stored_annotation": True,
        })

    return items[:50]


@app.put("/api/annotations/by-video/{video_stem}")
async def put_annotation_by_video(video_stem: str, body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体须为 JSON 对象")
    stem = sanitize_file_stem(video_stem)
    fw, fh = _annotation_frame_size(body)
    try:
        path = _persist_annotation_for_video(
            body, stem, frame_width=fw, frame_height=fh
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    boxes, _ = validate_annotation_payload(body)
    return {
        "status": "ok",
        "video_stem": stem,
        "path": str(path),
        "box_count": len(boxes),
    }


@app.post("/api/annotations/by-video/{video_stem}/upload")
async def upload_annotation_by_video(
    video_stem: str,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    if not file.filename:
        raise HTTPException(400, "未选择文件")
    if Path(file.filename).suffix.lower() != ".json":
        raise HTTPException(400, "标注文件须为 .json")
    try:
        raw = await file.read()
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, f"无效 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(400, "标注 JSON 须为对象")
    stem = sanitize_file_stem(video_stem)
    fw, fh = _annotation_frame_size(data)
    try:
        path = _persist_annotation_for_video(
            data,
            stem,
            source_video=file.filename,
            frame_width=fw,
            frame_height=fh,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    boxes, _ = validate_annotation_payload(data)
    return {
        "status": "ok",
        "video_stem": stem,
        "path": str(path),
        "box_count": len(boxes),
        "message": "已覆盖保存（每个视频仅保留最新一份标注）",
    }


@app.post("/api/annotation/validate")
async def validate_annotation(file: UploadFile = File(...)) -> dict[str, Any]:
    """校验上传的标注 JSON 是否符合 visual-dps 格式。"""
    if not file.filename:
        raise HTTPException(400, "未选择文件")
    try:
        raw = await file.read()
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, f"无效 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(400, "标注 JSON 须为对象")
    boxes, err = validate_annotation_payload(data)
    if err:
        raise HTTPException(400, err)
    return {
        "status": "ok",
        "box_count": len(boxes),
        "has_shelves": isinstance(data.get("shelves"), list),
        "annotation_size": data.get("annotation_size"),
    }


def _annotation_path_for_record(record_id: str, locator=None) -> Path | None:
    """解析记录关联的标注 JSON 路径（annotations 目录或 legacy 旁路文件）。"""
    if locator is None:
        locator = _locate_record(record_id)
    if not locator:
        return None
    paths = resolve_app_paths()
    sidecar = _meta_path_for_record(record_id, locator)
    meta: dict[str, Any] | None = None
    if sidecar.is_file():
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = None
    video_stem = resolve_video_stem_from_record(
        record_id,
        json_dir=paths.json_dir,
        pose_path=locator.path,
        meta=meta,
    )
    ann_path = annotation_path_for_video_stem(video_stem, annotation_dir=paths.annotation_dir)
    if ann_path.is_file():
        return ann_path
    legacy = paths.json_dir / f"{record_id}_annotation.json"
    if legacy.is_file():
        return legacy
    return None


@app.get("/api/records/{record_id}/export.xlsx")
def export_record_xlsx(record_id: str) -> Response:
    """导出 COCO-17 骨架至 xlsx；碰撞/告警写入单独事件表。"""
    locator = _locate_record(record_id)
    if not locator:
        raise HTTPException(404, "记录不存在")
    try:
        pose_data = load_pose_document(locator, include_frames=True)
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc

    settings = build_settings(config_path=resolve_config_path(None), cli={})
    ann_path = _annotation_path_for_record(record_id, locator=locator)
    try:
        blob = export_pose_to_xlsx_bytes(
            pose_data,
            annotation_path=ann_path,
            alarm_min_consecutive_frames=settings.alarm_min_consecutive_frames,
            alarm_cooldown_frames=settings.alarm_cooldown_frames,
        )
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f"导出失败: {exc}") from exc

    filename = f"{record_id}_skeleton.xlsx"
    return Response(
        content=blob,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/records/{record_id}/events")
def get_record_events(record_id: str) -> JSONResponse:
    """碰撞/告警事件列表，供回放进度条跳转。"""
    locator = _locate_record(record_id)
    if not locator:
        raise HTTPException(404, "记录不存在")
    try:
        events = load_events(locator)
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc
    alarm_n = sum(1 for e in events if e.get("event_type") == "alarm")
    collision_n = sum(1 for e in events if e.get("event_type") == "collision")
    return JSONResponse(
        {
            "record_id": record_id,
            "count": len(events),
            "alarm_count": alarm_n,
            "collision_count": collision_n,
            "events": events,
        }
    )


@app.get("/api/records/{record_id}/timeline")
def get_record_timeline(record_id: str) -> JSONResponse:
    """轻量时间轴（frame_idx / timestamp），供回放索引。"""
    locator = _locate_record(record_id)
    if not locator:
        raise HTTPException(404, "记录不存在")
    try:
        timeline = load_timeline(locator)
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc
    return JSONResponse({"record_id": record_id, "count": len(timeline), "timeline": timeline})


@app.get("/api/records/{record_id}/manifest.json")
def get_record_manifest(record_id: str) -> JSONResponse:
    locator = _locate_record(record_id)
    if not locator:
        raise HTTPException(404, "记录不存在")
    try:
        header = load_pose_header(locator)
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    header.setdefault("record_id", record_id)
    header.setdefault("frames_url", f"/api/records/{record_id}/frames")
    return JSONResponse(header)


@app.get("/api/records/{record_id}/frames")
def get_record_frames(
    record_id: str,
    from_frame: int = 1,
    to_frame: int | None = None,
) -> JSONResponse:
    """按帧段返回骨架与事件（schema v2 分页加载）。"""
    locator = _locate_record(record_id)
    if not locator:
        raise HTTPException(404, "记录不存在")
    lo = max(1, int(from_frame))
    hi = int(to_frame) if to_frame is not None else lo + 119
    if hi < lo:
        hi = lo
    try:
        frames = load_frames_range(locator, from_frame_idx=lo, to_frame_idx=hi)
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc
    return JSONResponse(
        {
            "record_id": record_id,
            "from_frame": lo,
            "to_frame": hi,
            "count": len(frames),
            "frames": frames,
        }
    )


@app.get("/api/records/{record_id}/pose.json")
def get_record_pose(record_id: str) -> JSONResponse:
    """兼容旧客户端：v1 返回 FileResponse；v2 返回 manifest（不含 frames）。"""
    locator = _locate_record(record_id)
    if not locator:
        raise HTTPException(404, "记录不存在")
    if locator.storage != STORAGE_V2_PARQUET and locator.path.is_file():
        return FileResponse(locator.path, media_type="application/json")
    try:
        header = load_pose_header(locator)
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    header.setdefault("record_id", record_id)
    header.setdefault("frames_url", f"/api/records/{record_id}/frames")
    return JSONResponse(header)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    job = dict(_get_job(job_id))
    if job.get("status") == "done":
        rid = job.get("record_id") or job_id
        job.setdefault("pose_url", f"/api/records/{rid}/manifest.json")
        job.setdefault("manifest_url", f"/api/records/{rid}/manifest.json")
        job.setdefault("frames_url", f"/api/records/{rid}/frames")
    return job


@app.post("/api/collect")
async def collect_video(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    annotation: UploadFile | None = File(None),
    backend: str = Form(""),
    variant: str = Form(""),
    det_variant: str = Form(""),
    width: int = Form(0),
    height: int = Form(0),
    pose_frame_interval: int = Form(1),
    frame_rate: float = Form(0),
    max_pose_frames: int = Form(0),
    save_video: str = Form(""),
    alarm_min_consecutive_frames: int = Form(0),
    alarm_cooldown_frames: int = Form(0),
) -> dict[str, Any]:
    if not file.filename:
        raise HTTPException(400, "未选择文件")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in VIDEO_EXTENSIONS:
        raise HTTPException(400, f"仅支持视频: {', '.join(sorted(VIDEO_EXTENSIONS))}")

    settings = build_settings(
        config_path=resolve_config_path(None),
        cli={
            "backend": backend or None,
            "variant": variant or None,
            "det_variant": det_variant or None,
            "width": width if width else None,
            "height": height if height else None,
            "frame_interval": pose_frame_interval,
            "frame_rate": frame_rate if frame_rate > 0 else None,
            "max_frames": max_pose_frames,
            "save_video": save_video if str(save_video).strip() else None,
            "alarm_min_consecutive_frames": alarm_min_consecutive_frames
            if int(alarm_min_consecutive_frames) > 0
            else None,
            "alarm_cooldown_frames": alarm_cooldown_frames
            if int(alarm_cooldown_frames) > 0
            else None,
        },
    )

    job_id = uuid.uuid4().hex[:12]
    paths = resolve_app_paths()
    paths.upload_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = paths.upload_dir / f"tmp_{job_id}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    video_path = tmp_dir / f"upload{suffix}"

    with open(video_path, "wb") as out:
        shutil.copyfileobj(file.file, out)

    try:
        validate_video_path(video_path)
    except (FileNotFoundError, ValueError) as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(400, str(exc)) from exc

    video_stem = sanitize_file_stem(Path(file.filename).stem)
    pose_path = default_pose_json_path(
        paths,
        backend=settings.backend,
        video_stem=video_stem,
        job_id=job_id,
    )
    record_id = _record_id_from_pose_path(pose_path)

    infer_w = int(width) if int(width) > 0 else settings.infer_width
    infer_h = int(height) if int(height) > 0 else settings.infer_height
    max_f = int(max_pose_frames) if int(max_pose_frames) > 0 else settings.max_pose_frames
    infer_frame_rate = float(frame_rate) if float(frame_rate) > 0 else settings.frame_rate
    save_video_flag = _parse_save_video_flag(
        save_video if str(save_video).strip() else None,
        default=settings.save_video,
    )

    annotation_path: Path | None = None
    upload_ann_path: Path | None = None
    if annotation and annotation.filename:
        ann_suffix = Path(annotation.filename).suffix.lower()
        if ann_suffix != ".json":
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise HTTPException(400, "标注文件须为 .json")
        upload_ann_path = tmp_dir / f"annotation{ann_suffix}"
        with open(upload_ann_path, "wb") as out:
            shutil.copyfileobj(annotation.file, out)

    try:
        annotation_path = require_annotation_for_collect(
            video_stem,
            annotation_dir=paths.annotation_dir,
            upload_path=upload_ann_path,
        )
    except ValueError as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(400, str(exc)) from exc

    with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "record_id": record_id,
            "status": "pending",
            "progress": 0,
            "message": "排队中",
            "backend": settings.backend,
            "pose_file": pose_path.name,
        }

    background_tasks.add_task(
        _run_job,
        job_id,
        video_path,
        pose_path,
        backend=settings.backend,
        variant=settings.variant,
        det_variant=settings.det_variant,
        det_backend=settings.det_backend,
        video_stem=video_stem,
        source_video_name=str(file.filename or ""),
        width=infer_w,
        height=infer_h,
        pose_frame_interval=max(1, int(pose_frame_interval) or settings.pose_frame_interval),
        frame_rate=infer_frame_rate,
        max_pose_frames=max_f,
        save_video=save_video_flag,
        annotation_path=annotation_path,
        alarm_min_consecutive_frames=settings.alarm_min_consecutive_frames,
        alarm_cooldown_frames=settings.alarm_cooldown_frames,
    )

    return {
        "job_id": job_id,
        "record_id": record_id,
        "status": "pending",
        "pose_file": pose_path.name,
        "save_video": save_video_flag,
        "has_annotation": annotation_path is not None,
        "video_stem": video_stem,
        "annotation_auto": annotation_path is not None and upload_ann_path is None,
        "alarm_min_consecutive_frames": settings.alarm_min_consecutive_frames,
        "alarm_cooldown_frames": settings.alarm_cooldown_frames,
    }


@app.post("/api/playback/video")
async def upload_playback_video(file: UploadFile = File(...)) -> dict[str, str]:
    """回放用临时视频，结束后请调用 DELETE 删除。"""
    if not file.filename:
        raise HTTPException(400, "未选择文件")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in VIDEO_EXTENSIONS:
        raise HTTPException(400, f"仅支持视频: {', '.join(sorted(VIDEO_EXTENSIONS))}")

    paths = resolve_app_paths()
    paths.playback_temp_dir.mkdir(parents=True, exist_ok=True)
    playback_id = uuid.uuid4().hex[:12]
    dest_dir = paths.playback_temp_dir / playback_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    video_path = dest_dir / f"video{suffix}"
    with open(video_path, "wb") as out:
        shutil.copyfileobj(file.file, out)

    return {
        "playback_id": playback_id,
        "video_url": f"/api/playback/video/{playback_id}",
    }


@app.get("/api/playback/video/{playback_id}")
def serve_playback_video(playback_id: str) -> FileResponse:
    paths = resolve_app_paths()
    dest_dir = paths.playback_temp_dir / playback_id
    if not dest_dir.is_dir():
        raise HTTPException(404, "临时视频不存在或已删除")
    video = next((f for f in dest_dir.iterdir() if f.suffix.lower() in VIDEO_EXTENSIONS), None)
    if not video:
        raise HTTPException(404, "临时视频不存在")
    media = _VIDEO_MIME.get(video.suffix.lower(), "application/octet-stream")
    return FileResponse(video, media_type=media)


@app.delete("/api/playback/video/{playback_id}")
def delete_playback_video(playback_id: str) -> dict[str, str]:
    paths = resolve_app_paths()
    dest_dir = paths.playback_temp_dir / playback_id
    if dest_dir.is_dir():
        shutil.rmtree(dest_dir, ignore_errors=True)
    return {"status": "deleted", "playback_id": playback_id}


# 兼容旧 API 路径
@app.get("/api/sessions")
def list_sessions_alias() -> list[dict[str, Any]]:
    return list_records()


@app.get("/api/sessions/{record_id}/pose.json")
def get_session_pose_alias(record_id: str) -> FileResponse:
    return get_record_pose(record_id)


WEB_DIR = project_root() / "web"
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


def main() -> None:
    import uvicorn

    cfg = load_config_file(resolve_config_path(None))
    server = cfg.get("server") if isinstance(cfg.get("server"), dict) else {}
    host = str(server.get("host") or "127.0.0.1")
    port = int(server.get("port") or 8765)
    paths = resolve_app_paths(cfg)
    for p in (paths.json_dir, paths.video_dir, paths.upload_dir, paths.playback_temp_dir, paths.annotation_dir):
        p.mkdir(parents=True, exist_ok=True)
    migrated = migrate_v1_json_dir(paths.json_dir)
    if migrated:
        print(f"📦 已迁移 {len(migrated)} 条 v1 JSON → Parquet 包")
    settings = build_settings(config_path=resolve_config_path(None), cli={})
    print(f"🌐 Web UI: http://{host}:{port}")
    print(f"📁 JSON 目录: {paths.json_dir}")
    print(f"🎬 视频目录: {paths.video_dir}（默认保存: {default_save_video()})")
    print(f"📦 ONNX 目录: {paths.models_onnx_dir}")
    print(f"   ├─ detection: {paths.models_detection_dir}")
    print(f"   └─ pose: {paths.models_pose_dir}")
    print(f"🏷 标注目录: {paths.annotation_dir}（每视频一份，新保存覆盖旧文件）")
    print(f"🧠 推理设备: {settings.device}（models.use_gpu / INFERENCE_USE_GPU）")
    if settings.device == "cuda":
        try:
            from rtmpose_infer import assert_cuda_ort_available, ort_available_providers

            assert_cuda_ort_available()
            print(f"✅ ORT GPU 就绪: {ort_available_providers()}")
        except RuntimeError as exc:
            print(f"❌ {exc}")
    uvicorn.run("server:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
