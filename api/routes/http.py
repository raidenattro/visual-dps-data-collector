"""HTTP 路由（FastAPI APIRouter）。"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from annotation_store import (
    annotation_path_for_video_stem,
    load_annotation_json,
    resolve_video_stem_from_record,
    validate_annotation_payload,
)
from collect_core import validate_video_path
from config_loader import (
    build_settings,
    camera_storage_slug,
    default_pose_json_path,
    load_config_file,
    resolve_app_paths,
    resolve_config_path,
    sanitize_file_stem,
)
from export_pose_xlsx import export_pose_to_xlsx_bytes
from model_assets import VIDEO_EXTENSIONS
from pose_store import (
    STORAGE_V2_PARQUET,
    delete_record,
    iter_active_records,
    load_events,
    load_frames_range,
    load_pose_document,
    load_pose_header,
    load_timeline,
)
from video_frame import first_frame_base64

from api.collect_service import (
    build_collect_config_snapshot,
    resolve_collect_annotation,
    run_batch_job,
    run_job,
)
from api.constants import VIDEO_MIME
from api.job_store import get_job, set_job
from api.record_service import (
    annotation_frame_size,
    annotation_path_for_record,
    locate_record_by_id,
    meta_path_for_record,
    parse_save_video_flag,
    persist_annotation_for_video,
    record_id_from_pose_path,
    record_meta_for_list,
    resolve_annotation_path_for_record,
    video_path_for_record,
    video_path_for_video_stem,
)
from api.reflection_service import (
    REFLECTION_OK,
    load_reflection_or_http,
    normalize_corner_label,
    reflection_json_path,
)

router = APIRouter()

@router.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/api/config/inference")
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


@router.get("/api/reflection/cameras")
def list_reflection_cameras() -> dict[str, Any]:
    """reflection.json 中全部机位标识，供采集页下拉。"""
    reflection = load_reflection_or_http()
    return {
        "reflection_path": str(reflection_json_path()),
        "cameras": reflection.cameras,
    }


@router.get("/api/reflection/lookup")
def lookup_reflection_camera(camera: str = "") -> dict[str, Any]:
    """校验机位标识并返回将装配的标注文件列表（不读视频）。"""
    label = normalize_corner_label(camera) if normalize_corner_label else str(camera or "").strip()
    if not label:
        raise HTTPException(400, "请填写机位标识")
    reflection = load_reflection_or_http()
    paths = resolve_app_paths()
    try:
        from corner_label.reflection import resolve_annotation_paths_for_camera

        src_paths = resolve_annotation_paths_for_camera(
            label, reflection, paths.annotation_dir
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(400, str(exc)) from exc

    json_files = [p.name for p in src_paths]
    rel_dir = "localdata/json/annotations"
    return {
        "ok": True,
        "camera_label": label,
        "annotation_ids": reflection.annotations_for_camera(label),
        "json_files": json_files,
        "json_files_display": [f"{rel_dir}/{name}" for name in json_files],
        "merged": len(json_files) > 1,
        "message": f"机位 {label} → {', '.join(json_files)}"
        + ("（采集时将合并）" if len(json_files) > 1 else ""),
    }


@router.get("/api/records")
def list_records() -> list[dict[str, Any]]:
    paths = resolve_app_paths()
    paths.json_dir.mkdir(parents=True, exist_ok=True)
    items = [record_meta_for_list(loc) for loc in iter_active_records(paths.json_dir)]
    return items[:500]


@router.post("/api/annotate/extract-frame")
async def extract_frame_from_upload(file: UploadFile = File(...)) -> dict[str, Any]:
    """浏览器无法解码时，由服务端 OpenCV 从上传视频提取首帧。"""
    if not file.filename:
        raise HTTPException(400, "未选择视频文件")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in VIDEO_EXTENSIONS:
        raise HTTPException(400, f"仅支持视频: {', '.join(sorted(VIDEO_EXTENSIONS))}")
    paths = resolve_app_paths()
    paths.upload_dir.mkdir(parents=True, exist_ok=True)
    tmp = paths.upload_dir / f"annotate_frame_{uuid.uuid4().hex}{suffix}"
    try:
        content = await file.read()
        if not content:
            raise HTTPException(400, "视频文件为空")
        tmp.write_bytes(content)
        validate_video_path(tmp)
        return first_frame_base64(tmp)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        tmp.unlink(missing_ok=True)


@router.delete("/api/records/{record_id:path}")
def delete_record_api(record_id: str) -> dict[str, Any]:
    """删除历史记录：Parquet 包或 v1 JSON、sidecar meta、配套视频（保留 annotations/ 下标注）。"""
    rid = str(record_id or "").strip()
    if not rid:
        raise HTTPException(400, "record_id 无效")
    locator = locate_record_by_id(rid, include_archive=False)
    if not locator:
        raise HTTPException(404, "记录不存在")
    paths = resolve_app_paths()
    video_path = video_path_for_record(rid)
    try:
        result = delete_record(
            paths.json_dir,
            locator,
            video_path=video_path,
        )
    except OSError as exc:
        raise HTTPException(500, f"删除失败: {exc}") from exc
    return result


@router.get("/api/records/{record_id:path}/video")
def get_record_video(record_id: str) -> FileResponse:
    path = video_path_for_record(record_id)
    if not path or not path.is_file():
        raise HTTPException(404, "配套视频不存在")
    media = VIDEO_MIME.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media)


@router.get("/api/records/{record_id:path}/annotation.json")
def get_record_annotation(record_id: str) -> FileResponse:
    locator = locate_record_by_id(record_id)
    if not locator:
        raise HTTPException(404, "记录不存在")
    paths = resolve_app_paths()
    sidecar = meta_path_for_record(record_id, locator)
    meta: dict[str, Any] | None = None
    if sidecar.is_file():
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = None
    ann_path = resolve_annotation_path_for_record(record_id, locator=locator, meta=meta)
    if not ann_path or not ann_path.is_file():
        raise HTTPException(404, "标注 JSON 不存在")
    return FileResponse(ann_path, media_type="application/json")


@router.get("/api/records/{record_id:path}/annotation/frame")
def get_record_annotation_frame(record_id: str) -> dict[str, Any]:
    path = video_path_for_record(record_id)
    if not path or not path.is_file():
        raise HTTPException(404, "配套视频不存在，无法提取首帧")
    try:
        return first_frame_base64(path)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/api/annotations/by-video/{video_stem}")
def get_annotation_by_video(video_stem: str) -> dict[str, Any]:
    paths = resolve_app_paths()
    stem = sanitize_file_stem(video_stem)
    data = load_annotation_json(stem, annotation_dir=paths.annotation_dir)
    if not data:
        raise HTTPException(404, "该视频尚无标注")
    return data


@router.get("/api/annotations/by-video/{video_stem}/frame")
def get_annotation_frame_by_video(video_stem: str) -> dict[str, Any]:
    """按 video_stem 从 video_dir 提取首帧（无 pose 记录时标注页使用）。"""
    path = video_path_for_video_stem(video_stem)
    if not path or not path.is_file():
        raise HTTPException(
            404,
            "未找到配套视频，请将视频放入 localdata/video/ 或上传本地视频",
        )
    try:
        return first_frame_base64(path)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/api/annotate/options")
def list_annotate_options() -> list[dict[str, Any]]:
    """标注页下拉：全部 pose 记录 + 仅存在于 annotations 目录的条目。"""
    paths = resolve_app_paths()
    paths.annotation_dir.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, Any]] = []
    seen_stems: set[str] = set()

    for locator in iter_active_records(paths.json_dir):
        meta = record_meta_for_list(locator)
        record_id = locator.record_id
        video_stem = meta.get("video_stem") or record_id
        seen_stems.add(video_stem)
        has_disk_video = bool(video_path_for_record(record_id)) or bool(
            video_path_for_video_stem(video_stem)
        )
        items.append({
            "video_stem": video_stem,
            "display_name": meta.get("display_name") or video_stem,
            "record_id": record_id,
            "pose_file": meta.get("pose_file") or record_id,
            "source_video": meta.get("source_video") or "",
            "has_video": bool(meta.get("has_video")) or has_disk_video,
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


@router.put("/api/annotations/by-video/{video_stem}")
async def put_annotation_by_video(video_stem: str, body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体须为 JSON 对象")
    stem = sanitize_file_stem(video_stem)
    fw, fh = annotation_frame_size(body)
    try:
        path = persist_annotation_for_video(
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


@router.post("/api/annotations/by-video/{video_stem}/upload")
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
    fw, fh = annotation_frame_size(data)
    try:
        path = persist_annotation_for_video(
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


@router.post("/api/annotation/validate")
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



@router.get("/api/records/{record_id:path}/export.xlsx")
def export_record_xlsx(record_id: str) -> Response:
    """导出 COCO-17 骨架至 xlsx；碰撞/告警写入单独事件表。"""
    locator = locate_record_by_id(record_id)
    if not locator:
        raise HTTPException(404, "记录不存在")
    try:
        pose_data = load_pose_document(locator, include_frames=True)
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc

    settings = build_settings(config_path=resolve_config_path(None), cli={})
    ann_path = annotation_path_for_record(record_id, locator=locator)
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


@router.get("/api/records/{record_id:path}/events")
def get_record_events(record_id: str) -> JSONResponse:
    """碰撞/告警事件列表，供回放进度条跳转。"""
    locator = locate_record_by_id(record_id)
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


@router.get("/api/records/{record_id:path}/timeline")
def get_record_timeline(record_id: str) -> JSONResponse:
    """轻量时间轴（frame_idx / timestamp），供回放索引。"""
    locator = locate_record_by_id(record_id)
    if not locator:
        raise HTTPException(404, "记录不存在")
    try:
        timeline = load_timeline(locator)
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc
    return JSONResponse({"record_id": record_id, "count": len(timeline), "timeline": timeline})


@router.get("/api/records/{record_id:path}/manifest.json")
def get_record_manifest(record_id: str) -> JSONResponse:
    locator = locate_record_by_id(record_id)
    if not locator:
        raise HTTPException(404, "记录不存在")
    try:
        header = load_pose_header(locator)
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    header.setdefault("record_id", record_id)
    header.setdefault("frames_url", f"/api/records/{record_id}/frames")
    return JSONResponse(header)


@router.get("/api/records/{record_id:path}/frames")
def get_record_frames(
    record_id: str,
    from_frame: int = 1,
    to_frame: int | None = None,
) -> JSONResponse:
    """按帧段返回骨架与事件（schema v2 分页加载）。"""
    locator = locate_record_by_id(record_id)
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


@router.get("/api/records/{record_id:path}/pose.json")
def get_record_pose(record_id: str) -> JSONResponse:
    """兼容旧客户端：v1 返回 FileResponse；v2 返回 manifest（不含 frames）。"""
    locator = locate_record_by_id(record_id)
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


@router.get("/api/records/{record_id:path}")
def get_record_meta(record_id: str) -> dict[str, Any]:
    """单条记录元数据（须在 /manifest.json 等子路径路由之后注册，避免 path 贪婪匹配）。"""
    rid = str(record_id or "").strip()
    if not rid or rid.endswith(".json") or rid.endswith(".xlsx"):
        raise HTTPException(404, "记录不存在")
    locator = locate_record_by_id(rid)
    if not locator:
        raise HTTPException(404, "记录不存在")
    return record_meta_for_list(locator)


@router.get("/api/jobs/{job_id}")
def get_job_status(job_id: str) -> dict[str, Any]:
    job = dict(get_job(job_id))
    if job.get("status") == "done":
        rid = job.get("record_id") or job_id
        job.setdefault("pose_url", f"/api/records/{rid}/manifest.json")
        job.setdefault("manifest_url", f"/api/records/{rid}/manifest.json")
        job.setdefault("frames_url", f"/api/records/{rid}/frames")
    return job


@router.post("/api/collect")
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
    camera_label: str = Form(""),
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
    cam_norm = normalize_corner_label(camera_label) if normalize_corner_label else str(camera_label or "").strip()
    cam_slug = camera_storage_slug(cam_norm) if cam_norm else ""
    pose_path = default_pose_json_path(
        paths,
        backend=settings.backend,
        video_stem=video_stem,
        job_id=job_id,
        camera_slug=cam_slug or None,
    )
    record_id = record_id_from_pose_path(pose_path)

    infer_w = int(width) if int(width) > 0 else settings.infer_width
    infer_h = int(height) if int(height) > 0 else settings.infer_height
    max_f = int(max_pose_frames) if int(max_pose_frames) > 0 else settings.max_pose_frames
    infer_frame_rate = float(frame_rate) if float(frame_rate) > 0 else settings.frame_rate
    save_video_flag = parse_save_video_flag(
        save_video if str(save_video).strip() else None,
        default=settings.save_video,
    )

    upload_ann_path: Path | None = None
    camera_match_label: str | None = None
    camera_match_slug: str | None = cam_slug or None

    if annotation and annotation.filename:
        ann_suffix = Path(annotation.filename).suffix.lower()
        if ann_suffix != ".json":
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise HTTPException(400, "标注文件须为 .json")
        upload_ann_path = tmp_dir / f"annotation{ann_suffix}"
        with open(upload_ann_path, "wb") as out:
            shutil.copyfileobj(annotation.file, out)

    try:
        annotation_path, camera_match_label, resolved_slug = resolve_collect_annotation(
            tmp_dir,
            paths,
            video_stem=video_stem,
            camera_label=camera_label,
            upload_ann_path=upload_ann_path,
        )
        if resolved_slug:
            camera_match_slug = resolved_slug
            if camera_match_label:
                cam_slug = resolved_slug
                pose_path = default_pose_json_path(
                    paths,
                    backend=settings.backend,
                    video_stem=video_stem,
                    job_id=job_id,
                    camera_slug=cam_slug,
                )
                record_id = record_id_from_pose_path(pose_path)
    except HTTPException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    if upload_ann_path:
        ann_source = "upload"
    elif annotation_path is not None and upload_ann_path is None:
        ann_source = "reflection" if (camera_match_label or cam_norm) else "stored"
    else:
        ann_source = ""
    collect_config = build_collect_config_snapshot(
        backend=settings.backend,
        variant=settings.variant,
        det_variant=settings.det_variant,
        det_backend=settings.det_backend,
        width=infer_w,
        height=infer_h,
        pose_frame_interval=max(1, int(pose_frame_interval) or settings.pose_frame_interval),
        frame_rate=infer_frame_rate,
        max_pose_frames=max_f,
        save_video=save_video_flag,
        alarm_min_consecutive_frames=settings.alarm_min_consecutive_frames,
        alarm_cooldown_frames=settings.alarm_cooldown_frames,
        camera_label=camera_match_label or cam_norm,
        camera_slug=camera_match_slug or cam_slug,
        annotation_source=ann_source,
    )

    set_job(job_id, {
        "job_id": job_id,
        "record_id": record_id,
        "status": "pending",
        "progress": 0,
        "message": "排队中",
        "backend": settings.backend,
        "pose_file": pose_path.name,
    })

    background_tasks.add_task(
        run_job,
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
        camera_label=camera_match_label or cam_norm,
        camera_slug=camera_match_slug or cam_slug,
        collect_config=collect_config,
    )

    resp: dict[str, Any] = {
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
    if camera_match_label or cam_norm:
        resp["camera_label"] = camera_match_label or cam_norm
    if camera_match_slug or cam_slug:
        resp["camera_slug"] = camera_match_slug or cam_slug
    return resp


@router.post("/api/collect/batch")
async def collect_batch(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    camera_label: str = Form(...),
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
    """同一机位文件夹内多视频顺序批处理，结果写入 json_dir/{camera_slug}/。"""
    cam = normalize_corner_label(camera_label) if normalize_corner_label else str(camera_label or "").strip()
    if not cam:
        raise HTTPException(400, "请填写机位标识")
    cam_slug = camera_storage_slug(cam)

    video_files: list[UploadFile] = []
    for f in files:
        if not f.filename:
            continue
        suffix = Path(f.filename).suffix.lower()
        if suffix in VIDEO_EXTENSIONS:
            video_files.append(f)
    if not video_files:
        raise HTTPException(400, "文件夹内无有效视频文件")

    video_files.sort(key=lambda u: str(u.filename or "").replace("\\", "/").lower())

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

    batch_id = uuid.uuid4().hex[:12]
    paths = resolve_app_paths()
    paths.upload_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = paths.upload_dir / f"batch_{batch_id}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    try:
        annotation_path, _, _ = resolve_collect_annotation(
            tmp_dir,
            paths,
            video_stem=sanitize_file_stem(Path(video_files[0].filename or "video").stem),
            camera_label=cam,
            upload_ann_path=None,
        )
    except HTTPException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    batch_items: list[tuple[Path, str, str]] = []
    for i, uf in enumerate(video_files):
        rel = str(uf.filename or f"video{i}.mp4").replace("\\", "/")
        name = Path(rel).name
        suffix = Path(name).suffix.lower()
        dest = tmp_dir / f"{i:04d}_{sanitize_file_stem(Path(name).stem)}{suffix}"
        with open(dest, "wb") as out:
            shutil.copyfileobj(uf.file, out)
        try:
            validate_video_path(dest)
        except (FileNotFoundError, ValueError) as exc:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise HTTPException(400, f"{name}: {exc}") from exc
        batch_items.append((dest, sanitize_file_stem(Path(name).stem), name))

    infer_w = int(width) if int(width) > 0 else settings.infer_width
    infer_h = int(height) if int(height) > 0 else settings.infer_height
    max_f = int(max_pose_frames) if int(max_pose_frames) > 0 else settings.max_pose_frames
    infer_frame_rate = float(frame_rate) if float(frame_rate) > 0 else settings.frame_rate
    save_video_flag = parse_save_video_flag(
        save_video if str(save_video).strip() else None,
        default=settings.save_video,
    )

    collect_config = build_collect_config_snapshot(
        backend=settings.backend,
        variant=settings.variant,
        det_variant=settings.det_variant,
        det_backend=settings.det_backend,
        width=infer_w,
        height=infer_h,
        pose_frame_interval=max(1, int(pose_frame_interval) or settings.pose_frame_interval),
        frame_rate=infer_frame_rate,
        max_pose_frames=max_f,
        save_video=save_video_flag,
        alarm_min_consecutive_frames=settings.alarm_min_consecutive_frames,
        alarm_cooldown_frames=settings.alarm_cooldown_frames,
        camera_label=cam,
        camera_slug=cam_slug,
        batch_id=batch_id,
        annotation_source="reflection" if annotation_path else "",
    )

    set_job(batch_id, {
            "job_id": batch_id,
            "type": "batch",
            "status": "pending",
            "progress": 0,
            "message": f"批处理排队中（{len(batch_items)} 个视频）",
            "camera_label": cam,
            "camera_slug": cam_slug,
            "total_videos": len(batch_items),
            "current_index": 0,
            "started_at": time.perf_counter(),
            "video_durations": [],
        })

    background_tasks.add_task(
        run_batch_job,
        batch_id,
        batch_items,
        annotation_path=annotation_path,
        camera_label=cam,
        camera_slug=cam_slug,
        collect_config=collect_config,
        backend=settings.backend,
        variant=settings.variant,
        det_variant=settings.det_variant,
        det_backend=settings.det_backend,
        width=infer_w,
        height=infer_h,
        pose_frame_interval=max(1, int(pose_frame_interval) or settings.pose_frame_interval),
        frame_rate=infer_frame_rate,
        max_pose_frames=max_f,
        save_video=save_video_flag,
        alarm_min_consecutive_frames=settings.alarm_min_consecutive_frames,
        alarm_cooldown_frames=settings.alarm_cooldown_frames,
    )

    return {
        "job_id": batch_id,
        "type": "batch",
        "status": "pending",
        "camera_label": cam,
        "camera_slug": cam_slug,
        "video_count": len(batch_items),
        "storage_dir": f"localdata/json/{cam_slug}",
    }


@router.post("/api/playback/video")
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


@router.get("/api/playback/video/{playback_id}")
def serve_playback_video(playback_id: str) -> FileResponse:
    paths = resolve_app_paths()
    dest_dir = paths.playback_temp_dir / playback_id
    if not dest_dir.is_dir():
        raise HTTPException(404, "临时视频不存在或已删除")
    video = next((f for f in dest_dir.iterdir() if f.suffix.lower() in VIDEO_EXTENSIONS), None)
    if not video:
        raise HTTPException(404, "临时视频不存在")
    media = VIDEO_MIME.get(video.suffix.lower(), "application/octet-stream")
    return FileResponse(video, media_type=media)


@router.delete("/api/playback/video/{playback_id}")
def delete_playback_video(playback_id: str) -> dict[str, str]:
    paths = resolve_app_paths()
    dest_dir = paths.playback_temp_dir / playback_id
    if dest_dir.is_dir():
        shutil.rmtree(dest_dir, ignore_errors=True)
    return {"status": "deleted", "playback_id": playback_id}


# 兼容旧 API 路径
@router.get("/api/sessions")
def list_sessions_alias() -> list[dict[str, Any]]:
    return list_records()


@router.get("/api/sessions/{record_id}/pose.json")
def get_session_pose_alias(record_id: str) -> FileResponse:
    return get_record_pose(record_id)

