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
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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


def _json_path_for_record(record_id: str) -> Path | None:
    paths = resolve_app_paths()
    rid = str(record_id or "").strip()
    if not rid:
        return None
    direct = paths.json_dir / f"{rid}.json"
    if direct.is_file() and not direct.name.endswith(".meta.json"):
        return direct
    for p in paths.json_dir.glob("*.json"):
        if p.name.endswith(".meta.json"):
            continue
        if p.stem == rid or p.stem.startswith(rid + "_"):
            return p
    return None


def _record_id_from_pose_path(pose_path: Path) -> str:
    return pose_path.stem


def _annotation_path_for_pose(pose_path: Path) -> Path:
    return pose_path.with_name(f"{pose_path.stem}_annotation.json")


def _save_annotation_copy(src_path: Path, pose_path: Path) -> Path | None:
    if not src_path.is_file():
        return None
    dest = _annotation_path_for_pose(pose_path)
    shutil.copy2(src_path, dest)
    return dest


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
    pose_path = _json_path_for_record(record_id)
    if not pose_path:
        return None
    paths = resolve_app_paths()
    sidecar = pose_path.with_suffix(".meta.json")
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
    for ext in VIDEO_EXTENSIONS:
        candidate = paths.video_dir / f"{pose_path.stem}{ext}"
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
) -> None:
    settings = build_settings(config_path=resolve_config_path(None), cli={})

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
            alarm_min_consecutive_frames=settings.alarm_min_consecutive_frames,
            alarm_cooldown_frames=settings.alarm_cooldown_frames,
        )
        record_id = _record_id_from_pose_path(pose_path)
        saved_annotation: Path | None = None
        if annotation_path and annotation_path.is_file():
            saved_annotation = _save_annotation_copy(annotation_path, pose_path)
        saved_video_path: Path | None = None
        if save_video and video_path.is_file():
            try:
                saved_video_path = _persist_record_video(video_path, pose_path)
            except OSError as exc:
                raise RuntimeError(f"保存配套视频失败: {exc}") from exc

        meta = {
            "record_id": record_id,
            "job_id": job_id,
            "display_name": video_stem or _display_name_from_pose_file(pose_path.name, backend),
            "pose_file": pose_path.name,
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

        sidecar = pose_path.with_suffix(".meta.json")
        with open(sidecar, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        _update_job(
            job_id,
            status="done",
            progress=100,
            message="完成",
            frame_count=data.get("frame_count", 0),
            pose_url=f"/api/records/{record_id}/pose.json",
            record_id=record_id,
            pose_file=pose_path.name,
            display_name=meta["display_name"],
            has_video=meta.get("has_video", False),
            has_annotation=meta.get("has_annotation", False),
            collision_enabled=meta.get("collision_enabled", False),
            video_url=meta.get("video_url"),
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


@app.get("/api/records")
def list_records() -> list[dict[str, Any]]:
    paths = resolve_app_paths()
    paths.json_dir.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, Any]] = []
    for pose_file in sorted(paths.json_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        if pose_file.name.endswith(".meta.json"):
            continue
        record_id = pose_file.stem
        meta: dict[str, Any] = {
            "record_id": record_id,
            "pose_file": pose_file.name,
            "pose_url": f"/api/records/{record_id}/pose.json",
        }
        sidecar = pose_file.with_suffix(".meta.json")
        if sidecar.is_file():
            try:
                meta.update(json.loads(sidecar.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                pass
        if "backend" not in meta and "variant" in meta:
            meta["backend"] = variant_to_backend(str(meta["variant"]))
        if not meta.get("display_name"):
            meta["display_name"] = _display_name_from_pose_file(
                pose_file.name, str(meta.get("backend") or "")
            )
        meta["pose_label"] = pose_file.name
        vpath = _video_path_for_record(record_id)
        if vpath and vpath.is_file():
            meta["has_video"] = True
            meta["video_file"] = vpath.name
            meta["video_url"] = f"/api/records/{record_id}/video"
        else:
            meta.setdefault("has_video", False)
        items.append(meta)
    return items[:50]


@app.get("/api/records/{record_id}/video")
def get_record_video(record_id: str) -> FileResponse:
    path = _video_path_for_record(record_id)
    if not path or not path.is_file():
        raise HTTPException(404, "配套视频不存在")
    media = _VIDEO_MIME.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media)


@app.get("/api/records/{record_id}/annotation.json")
def get_record_annotation(record_id: str) -> FileResponse:
    pose_path = _json_path_for_record(record_id)
    if not pose_path:
        raise HTTPException(404, "记录不存在")
    ann_path = _annotation_path_for_pose(pose_path)
    if not ann_path.is_file():
        raise HTTPException(404, "标注 JSON 不存在")
    return FileResponse(ann_path, media_type="application/json")


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
    from event_engine.annotation_boxes import flatten_annotation_boxes

    boxes = flatten_annotation_boxes(data if isinstance(data, dict) else {})
    return {
        "status": "ok",
        "box_count": len(boxes),
        "has_shelves": isinstance(data, dict) and isinstance(data.get("shelves"), list),
        "annotation_size": data.get("annotation_size") if isinstance(data, dict) else None,
    }


@app.get("/api/records/{record_id}/pose.json")
def get_record_pose(record_id: str) -> FileResponse:
    path = _json_path_for_record(record_id)
    if not path or not path.is_file():
        raise HTTPException(404, "pose JSON 不存在")
    return FileResponse(path, media_type="application/json")


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    job = dict(_get_job(job_id))
    if job.get("status") == "done":
        rid = job.get("record_id") or job_id
        job.setdefault("pose_url", f"/api/records/{rid}/pose.json")
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
    if annotation and annotation.filename:
        ann_suffix = Path(annotation.filename).suffix.lower()
        if ann_suffix != ".json":
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise HTTPException(400, "标注文件须为 .json")
        paths.annotation_dir.mkdir(parents=True, exist_ok=True)
        annotation_path = tmp_dir / f"annotation{ann_suffix}"
        with open(annotation_path, "wb") as out:
            shutil.copyfileobj(annotation.file, out)
        try:
            from event_engine.annotation_boxes import flatten_annotation_boxes, load_annotation_config

            ann_data = load_annotation_config(annotation_path)
            if not flatten_annotation_boxes(ann_data):
                shutil.rmtree(tmp_dir, ignore_errors=True)
                raise HTTPException(400, "标注 JSON 无有效 boxes/shelves")
        except FileNotFoundError as exc:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise HTTPException(400, str(exc)) from exc
        except ValueError as exc:
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
    )

    return {
        "job_id": job_id,
        "record_id": record_id,
        "status": "pending",
        "pose_file": pose_path.name,
        "save_video": save_video_flag,
        "has_annotation": annotation_path is not None,
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
    settings = build_settings(config_path=resolve_config_path(None), cli={})
    print(f"🌐 Web UI: http://{host}:{port}")
    print(f"📁 JSON 目录: {paths.json_dir}")
    print(f"🎬 视频目录: {paths.video_dir}（默认保存: {default_save_video()})")
    print(f"📦 ONNX 目录: {paths.models_onnx_dir}")
    print(f"   ├─ detection: {paths.models_detection_dir}")
    print(f"   └─ pose: {paths.models_pose_dir}")
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
