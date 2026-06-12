"""HTTP 路由（FastAPI APIRouter）。"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Body, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from annotation_store import (
    annotation_path_for_video_stem,
    load_annotation_json,
    resolve_video_stem_from_record,
    validate_annotation_payload,
)
from collect_core import validate_video_path
from config_loader import (
    allocate_camera_storage_slug,
    build_settings,
    camera_storage_slug,
    default_pose_json_path,
    load_config_file,
    pose_model_tier_from_backend,
    resolve_app_paths,
    resolve_config_path,
    sanitize_file_stem,
)
from export_pose_xlsx import export_pose_to_xlsx_bytes
from model_assets import VIDEO_EXTENSIONS
from pose_store import (
    STORAGE_V2_PARQUET,
    normalize_review_entry,
    delete_record,
    enrich_events_with_review,
    events_to_verified_entries,
    event_review_write_lock,
    event_signature,
    iter_active_records,
    load_event_review,
    load_events,
    load_frames_range,
    load_pose_document,
    load_pose_header,
    load_timeline,
    REVIEW_STATUS_NO_COLLISION,
    ensure_no_collision_review_completed,
    event_review_status_label,
    record_has_skeleton_data,
    resolve_event_review_status,
    save_event_review,
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
from annotation_store import annotation_path_for_video_stem
from api.collision_recompute_service import recompute_records_collisions
from api.record_service import (
    annotation_frame_size,
    annotation_path_for_record,
    attach_tags_to_summaries,
    find_records_for_annotation_stem,
    locate_record_by_id,
    meta_path_for_record,
    parse_save_video_flag,
    persist_annotation_for_video,
    record_id_from_pose_path,
    record_meta_for_list,
    record_summary_for_list,
    resolve_annotation_path_for_record,
    video_path_for_record,
    video_path_for_video_stem,
)
from record_index_store import (
    delete_record_index,
    import_event_reviews_to_index,
    list_record_summaries,
    refresh_record_summary,
    sync_record_summaries,
)
from record_tag_store import (
    delete_record_tags,
    get_tags_for_record,
    list_tags_with_counts,
    normalize_tag_name,
    patch_record_tags,
    record_ids_with_all_tags,
)
from api.reflection_service import (
    REFLECTION_OK,
    load_reflection_or_http,
    normalize_corner_label,
    reflection_json_path,
)

router = APIRouter()


def _parse_record_ids_param(raw: str) -> list[str]:
    return [p.strip() for p in str(raw or "").replace(";", ",").split(",") if p.strip()]


def _annotation_save_message(saved_stem: str, requested_stem: str, *, preserved: bool) -> str:
    if preserved and saved_stem != requested_stem:
        return f"已保留原标注，另存为 {saved_stem}.json"
    return "已覆盖保存标注"


def _maybe_recompute_collisions(
    *,
    annotation_path: Path,
    saved_stem: str,
    requested_stem: str,
    recompute_collisions: bool,
    record_ids: list[str],
) -> dict[str, Any] | None:
    if not recompute_collisions:
        return None
    targets = record_ids or find_records_for_annotation_stem(requested_stem)
    if not targets:
        return {"status": "skipped", "reason": "未找到关联骨架记录", "record_ids": []}
    return recompute_records_collisions(
        targets,
        annotation_path,
        locate_record=locate_record_by_id,
        video_stem=saved_stem,
    )

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


def _parse_tags_query(raw: str) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for part in str(raw or "").replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            name = normalize_tag_name(part)
        except ValueError:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


@router.get("/api/tags")
def list_record_tags() -> dict[str, Any]:
    """全部标签及关联记录数。"""
    return {"tags": list_tags_with_counts()}


@router.post("/api/records/import-event-reviews")
def import_event_reviews_api(pose_tier: str = "") -> dict[str, Any]:
    """从已有 event_review.json 导入复核状态到 data.db 索引（不写回 JSON）。"""
    paths = resolve_app_paths()
    tier_filter = str(pose_tier or "").strip().lower() or None
    stats = import_event_reviews_to_index(paths, pose_tier=tier_filter)
    return {"ok": True, **stats}


def _parse_has_verified_query(raw: str) -> bool | None:
    text = str(raw or "").strip().lower()
    if not text or text in {"all", "any"}:
        return None
    if text in {"1", "true", "yes", "y", "has", "verified"}:
        return True
    if text in {"0", "false", "no", "n", "none", "unverified"}:
        return False
    return None


@router.get("/api/records")
def list_records(
    summary: bool = True,
    offset: int = 0,
    limit: int = 0,
    pose_tier: str = "",
    tags: str = "",
    review_status: str = "",
    has_verified: str = "",
) -> list[dict[str, Any]]:
    """列出采集记录。pose_tier 过滤 rtmpose-t/s/m；tags 逗号分隔多标签（需全部匹配）。"""
    paths = resolve_app_paths()
    paths.json_dir.mkdir(parents=True, exist_ok=True)
    tier_filter = str(pose_tier or "").strip().lower() or None
    tag_filter = _parse_tags_query(tags)
    review_filter = str(review_status or "").strip().lower() or None
    verified_filter = _parse_has_verified_query(has_verified)
    off = max(0, int(offset))
    lim = int(limit)

    if summary:
        sync_record_summaries(paths, tier_filter)
        allowed_ids = record_ids_with_all_tags(tag_filter) if tag_filter else None
        items = list_record_summaries(
            pose_tier=tier_filter,
            offset=off,
            limit=lim,
            allowed_ids=allowed_ids,
            review_status=review_filter,
            has_verified=verified_filter,
        )
        attach_tags_to_summaries(items)
        return items

    locators = list(iter_active_records(paths.json_dir, pose_tier=tier_filter))
    if tag_filter:
        allowed = record_ids_with_all_tags(tag_filter)
        locators = [loc for loc in locators if loc.record_id in allowed]
    if off:
        locators = locators[off:]
    if lim > 0:
        locators = locators[:lim]
    items = [record_meta_for_list(loc) for loc in locators]
    attach_tags_to_summaries(items)
    return items


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
        delete_record_tags(rid)
        delete_record_index(rid)
    except OSError as exc:
        raise HTTPException(500, f"删除失败: {exc}") from exc
    return result


@router.get("/api/records/{record_id:path}/tags")
def get_record_tags_api(record_id: str) -> dict[str, Any]:
    rid = str(record_id or "").strip()
    if not rid:
        raise HTTPException(400, "record_id 无效")
    locator = locate_record_by_id(rid, include_archive=False)
    if not locator:
        raise HTTPException(404, "记录不存在")
    return {"record_id": rid, "tags": get_tags_for_record(rid)}


@router.patch("/api/records/{record_id:path}/tags")
def patch_record_tags_api(
    record_id: str,
    body: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    rid = str(record_id or "").strip()
    if not rid:
        raise HTTPException(400, "record_id 无效")
    locator = locate_record_by_id(rid, include_archive=False)
    if not locator:
        raise HTTPException(404, "记录不存在")
    add_raw = body.get("add") if isinstance(body.get("add"), list) else []
    remove_raw = body.get("remove") if isinstance(body.get("remove"), list) else []
    try:
        tags = patch_record_tags(rid, add=add_raw, remove=remove_raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"record_id": rid, "tags": tags}


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
        meta = record_summary_for_list(locator, paths)
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


@router.put("/api/annotations/by-video/{video_stem}")
async def put_annotation_by_video(
    video_stem: str,
    body: dict[str, Any],
    preserve_existing: bool = False,
    recompute_collisions: bool = False,
    record_ids: str = "",
) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体须为 JSON 对象")
    requested_stem = sanitize_file_stem(video_stem)
    fw, fh = annotation_frame_size(body)
    try:
        path, saved_stem = persist_annotation_for_video(
            body,
            requested_stem,
            frame_width=fw,
            frame_height=fh,
            preserve_existing=preserve_existing,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    boxes, _ = validate_annotation_payload(body)
    recompute_result = None
    try:
        recompute_result = _maybe_recompute_collisions(
            annotation_path=path,
            saved_stem=saved_stem,
            requested_stem=requested_stem,
            recompute_collisions=recompute_collisions,
            record_ids=_parse_record_ids_param(record_ids),
        )
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(500, f"碰撞重算失败: {exc}") from exc
    return {
        "status": "ok",
        "video_stem": saved_stem,
        "requested_stem": requested_stem,
        "path": str(path),
        "box_count": len(boxes),
        "preserved_original": saved_stem != requested_stem if preserve_existing else False,
        "message": _annotation_save_message(saved_stem, requested_stem, preserved=preserve_existing),
        "recompute": recompute_result,
    }


@router.post("/api/annotations/by-video/{video_stem}/upload")
async def upload_annotation_by_video(
    video_stem: str,
    file: UploadFile = File(...),
    preserve_existing: bool = False,
    recompute_collisions: bool = False,
    record_ids: str = "",
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
    requested_stem = sanitize_file_stem(video_stem)
    fw, fh = annotation_frame_size(data)
    try:
        path, saved_stem = persist_annotation_for_video(
            data,
            requested_stem,
            source_video=file.filename,
            frame_width=fw,
            frame_height=fh,
            preserve_existing=preserve_existing,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    boxes, _ = validate_annotation_payload(data)
    recompute_result = None
    try:
        recompute_result = _maybe_recompute_collisions(
            annotation_path=path,
            saved_stem=saved_stem,
            requested_stem=requested_stem,
            recompute_collisions=recompute_collisions,
            record_ids=_parse_record_ids_param(record_ids),
        )
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(500, f"碰撞重算失败: {exc}") from exc
    return {
        "status": "ok",
        "video_stem": saved_stem,
        "requested_stem": requested_stem,
        "path": str(path),
        "box_count": len(boxes),
        "preserved_original": saved_stem != requested_stem if preserve_existing else False,
        "message": _annotation_save_message(saved_stem, requested_stem, preserved=preserve_existing),
        "recompute": recompute_result,
    }


@router.post("/api/records/{record_id:path}/recompute-collisions")
async def recompute_record_collisions_api(
    record_id: str,
    body: dict[str, Any] | None = Body(default=None),
) -> dict[str, Any]:
    """复用已有骨架，仅按指定标注重算碰撞/告警。"""
    locator = locate_record_by_id(record_id)
    if not locator:
        raise HTTPException(404, "记录不存在")
    payload = body if isinstance(body, dict) else {}
    ann_stem = str(payload.get("annotation_stem") or "").strip()
    paths = resolve_app_paths()
    if ann_stem:
        ann_path = annotation_path_for_video_stem(ann_stem, annotation_dir=paths.annotation_dir)
    else:
        ann_path = resolve_annotation_path_for_record(record_id, locator=locator)
    if not ann_path or not ann_path.is_file():
        raise HTTPException(404, "未找到可用标注 JSON")
    try:
        result = recompute_records_collisions(
            [record_id],
            ann_path,
            locate_record=locate_record_by_id,
            video_stem=ann_stem or ann_path.stem,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(500, str(exc)) from exc
    if result.get("errors"):
        raise HTTPException(400, result["errors"][0].get("error", "重算失败"))
    return result


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
    event_review = load_event_review(locator)
    try:
        blob = export_pose_to_xlsx_bytes(
            pose_data,
            annotation_path=ann_path,
            alarm_min_consecutive_frames=settings.alarm_min_consecutive_frames,
            alarm_cooldown_frames=settings.alarm_cooldown_frames,
            event_review=event_review,
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
        if not record_has_skeleton_data(locator):
            review = ensure_no_collision_review_completed(locator, event_count=0)
        else:
            review = ensure_no_collision_review_completed(locator, event_count=len(events))
        events = enrich_events_with_review(events, locator)
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc
    alarm_n = sum(1 for e in events if e.get("event_type") == "alarm")
    collision_n = sum(1 for e in events if e.get("event_type") == "collision")
    verified_n = sum(1 for e in events if e.get("verified_true"))
    review_status = resolve_event_review_status(review, event_count=len(events))
    return JSONResponse(
        {
            "record_id": record_id,
            "count": len(events),
            "alarm_count": alarm_n,
            "collision_count": collision_n,
            "verified_true_count": verified_n,
            "event_review_status": review_status,
            "event_review_label": event_review_status_label(review_status),
            "events": events,
            "event_review": review,
        }
    )


@router.get("/api/records/{record_id:path}/event-review")
def get_record_event_review(record_id: str) -> JSONResponse:
    locator = locate_record_by_id(record_id)
    if not locator:
        raise HTTPException(404, "记录不存在")
    return JSONResponse(load_event_review(locator))


@router.patch("/api/records/{record_id:path}/event-review")
def patch_record_event_review(record_id: str, body: dict[str, Any] = Body(...)) -> JSONResponse:
    """更新人工复核：verified_true / toggle / set_all_verified / status=completed。"""
    locator = locate_record_by_id(record_id)
    if not locator:
        raise HTTPException(404, "记录不存在")
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体须为 JSON 对象")

    with event_review_write_lock(record_id):
        return _patch_record_event_review_locked(record_id, locator, body)


def _patch_record_event_review_locked(
    record_id: str,
    locator: Any,
    body: dict[str, Any],
) -> JSONResponse:
    review = load_event_review(locator)
    verified: list[dict[str, Any]] = list(review.get("verified_true") or [])
    by_sig = {
        event_signature(str(v.get("event_type") or ""), int(v.get("frame_idx") or 0), v.get("box_tokens")): v
        for v in verified
        if isinstance(v, dict)
    }

    requested_status = str(body.get("status") or "").strip().lower()
    if requested_status == "completed":
        if "verified_true" in body and isinstance(body.get("verified_true"), list):
            verified = []
            seen: set[str] = set()
            for item in body.get("verified_true") or []:
                norm = normalize_review_entry(item if isinstance(item, dict) else {})
                if not norm:
                    continue
                sig = event_signature(norm["event_type"], norm["frame_idx"], norm["box_tokens"])
                if sig in seen:
                    continue
                seen.add(sig)
                verified.append(norm)
        try:
            event_total = int(body.get("event_total")) if body.get("event_total") is not None else len(load_events(locator))
        except (TypeError, ValueError):
            event_total = len(load_events(locator))
        try:
            path = save_event_review(
                locator,
                verified,
                status="completed",
                event_total=event_total,
            )
        except OSError as exc:
            raise HTTPException(500, f"保存复核失败: {exc}") from exc
        saved = load_event_review(locator)
        events = enrich_events_with_review(load_events(locator), locator)
        review_status = resolve_event_review_status(saved, event_count=len(events))
        refresh_record_summary(record_id)
        return JSONResponse(
            {
                "status": "ok",
                "record_id": record_id,
                "path": str(path),
                "verified_true_count": len(saved.get("verified_true") or []),
                "event_review_status": review_status,
                "event_review_label": event_review_status_label(review_status),
                "event_review": saved,
                "events": events,
            }
        )

    action = str(body.get("action") or "").strip().lower()
    light_response = False
    all_events: list[dict[str, Any]] | None = None

    if action == "set_all_verified":
        light_response = True
        if "mark_all" not in body:
            raise HTTPException(400, "set_all_verified 须包含 mark_all 布尔值")
        if bool(body.get("mark_all")):
            try:
                all_events = load_events(locator)
            except RuntimeError as exc:
                raise HTTPException(500, str(exc)) from exc
            verified = events_to_verified_entries(all_events)
        else:
            verified = []
    elif action == "toggle":
        entry = body.get("event")
        if not isinstance(entry, dict):
            raise HTTPException(400, "toggle 须包含 event 对象")
        norm = normalize_review_entry(entry)
        if not norm:
            raise HTTPException(400, "event 字段无效")
        sig = event_signature(norm["event_type"], norm["frame_idx"], norm["box_tokens"])
        want = body.get("verified_true")
        if want is None:
            want = sig not in by_sig
        if bool(want):
            by_sig[sig] = norm
        else:
            by_sig.pop(sig, None)
        verified = list(by_sig.values())
    elif "verified_true" in body:
        raw_list = body.get("verified_true")
        if not isinstance(raw_list, list):
            raise HTTPException(400, "verified_true 须为数组")
        verified = []
        seen: set[str] = set()
        for item in raw_list:
            norm = normalize_review_entry(item if isinstance(item, dict) else {})
            if not norm:
                continue
            sig = event_signature(norm["event_type"], norm["frame_idx"], norm["box_tokens"])
            if sig in seen:
                continue
            seen.add(sig)
            verified.append(norm)
    else:
        raise HTTPException(400, "请提供 verified_true、action=toggle、action=set_all_verified 或 status=completed")

    event_total_hint: int | None = None
    if body.get("event_total") is not None:
        try:
            event_total_hint = max(0, int(body.get("event_total")))
        except (TypeError, ValueError):
            event_total_hint = None

    if all_events is None:
        if light_response and action == "set_all_verified" and not bool(body.get("mark_all")):
            all_events = []
        else:
            try:
                all_events = load_events(locator)
            except RuntimeError as exc:
                raise HTTPException(500, str(exc)) from exc

    effective_event_count = len(all_events) if all_events else (event_total_hint or 0)

    if effective_event_count == 0 and not verified:
        event_total_empty = 0
        if body.get("event_total") is not None:
            try:
                event_total_empty = max(0, int(body.get("event_total")))
            except (TypeError, ValueError):
                event_total_empty = 0
        # 时间轴无事件时仍保留已有标真，避免并发 PATCH 把 verified_true 清空
        if verified:
            try:
                path = save_event_review(
                    locator,
                    verified,
                    status="in_progress",
                    event_total=event_total_empty or len(verified),
                )
            except OSError as exc:
                raise HTTPException(500, f"保存复核失败: {exc}") from exc
            saved = load_event_review(locator)
            review_status = resolve_event_review_status(saved, event_count=0)
            return JSONResponse(
                {
                    "status": "ok",
                    "record_id": record_id,
                    "path": str(path),
                    "verified_true_count": len(saved.get("verified_true") or []),
                    "event_review_status": review_status,
                    "event_review_label": event_review_status_label(review_status),
                    "event_review": saved,
                    "events": [],
                }
            )
        try:
            path = save_event_review(
                locator,
                [],
                status=REVIEW_STATUS_NO_COLLISION,
                event_total=0,
            )
        except OSError as exc:
            raise HTTPException(500, f"保存复核失败: {exc}") from exc
        saved = load_event_review(locator)
        review_status = resolve_event_review_status(saved, event_count=0)
        refresh_record_summary(record_id)
        return JSONResponse(
            {
                "status": "ok",
                "record_id": record_id,
                "path": str(path),
                "verified_true_count": 0,
                "event_review_status": review_status,
                "event_review_label": event_review_status_label(review_status),
                "event_review": saved,
                "events": [],
            }
        )

    next_status = "in_progress"
    event_total = event_total_hint if event_total_hint is not None else len(all_events)

    try:
        path = save_event_review(
            locator,
            verified,
            status=next_status,
            event_total=event_total,
        )
    except OSError as exc:
        raise HTTPException(500, f"保存复核失败: {exc}") from exc

    saved = load_event_review(locator)
    review_status = resolve_event_review_status(saved, event_count=event_total)
    refresh_record_summary(record_id)
    payload: dict[str, Any] = {
        "status": "ok",
        "record_id": record_id,
        "path": str(path),
        "verified_true_count": len(saved.get("verified_true") or []),
        "event_review_status": review_status,
        "event_review_label": event_review_status_label(review_status),
        "event_review": saved,
    }
    if light_response:
        payload["light"] = True
    else:
        payload["events"] = enrich_events_with_review(all_events, locator)
    return JSONResponse(payload)


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
    skeleton_only: str = Form(""),
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

    infer_w = int(width) if int(width) > 0 else settings.infer_width
    infer_h = int(height) if int(height) > 0 else settings.infer_height
    max_f = int(max_pose_frames) if int(max_pose_frames) > 0 else settings.max_pose_frames
    infer_frame_rate = float(frame_rate) if float(frame_rate) > 0 else settings.frame_rate
    save_video_flag = parse_save_video_flag(
        save_video if str(save_video).strip() else None,
        default=settings.save_video,
    )
    skeleton_only_flag = parse_save_video_flag(
        skeleton_only if str(skeleton_only).strip() else None,
        default=False,
    )

    upload_ann_path: Path | None = None
    camera_match_label: str | None = None
    camera_match_slug: str | None = None
    cam_slug = ""

    if annotation and annotation.filename:
        ann_suffix = Path(annotation.filename).suffix.lower()
        if ann_suffix != ".json":
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise HTTPException(400, "标注文件须为 .json")
        upload_ann_path = tmp_dir / f"annotation{ann_suffix}"
        with open(upload_ann_path, "wb") as out:
            shutil.copyfileobj(annotation.file, out)

    if skeleton_only_flag and upload_ann_path:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(400, "仅计算骨架模式下请勿上传标注 JSON")

    pose_tier = pose_model_tier_from_backend(settings.backend)
    if skeleton_only_flag:
        annotation_path = None
        camera_match_label = cam_norm or None
        if cam_norm:
            cam_slug = allocate_camera_storage_slug(paths, cam_norm, pose_tier=pose_tier)
            camera_match_slug = cam_slug
    else:
        try:
            annotation_path, camera_match_label, resolved_slug = resolve_collect_annotation(
                tmp_dir,
                paths,
                video_stem=video_stem,
                camera_label=camera_label,
                upload_ann_path=upload_ann_path,
            )
            if resolved_slug and camera_match_label:
                camera_match_slug = resolved_slug
        except HTTPException:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

    cam_for_storage = camera_match_label or cam_norm
    if cam_for_storage and not cam_slug:
        cam_slug = allocate_camera_storage_slug(paths, cam_for_storage, pose_tier=pose_tier)
        camera_match_slug = cam_slug
    pose_path = default_pose_json_path(
        paths,
        backend=settings.backend,
        video_stem=video_stem,
        job_id=job_id,
        camera_slug=cam_slug or None,
        pose_tier=pose_tier,
    )
    record_id = record_id_from_pose_path(pose_path)

    if upload_ann_path:
        ann_source = "upload"
    elif skeleton_only_flag:
        ann_source = "skeleton_only"
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
        skeleton_only=skeleton_only_flag,
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
        "collision_computed": annotation_path is not None and not skeleton_only_flag,
        "skeleton_only": skeleton_only_flag,
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
    skeleton_only: str = Form(""),
) -> dict[str, Any]:
    """同一机位文件夹内多视频顺序批处理，结果写入 json_dir/{rtmpose-t}/{camera_slug}/。"""
    cam = normalize_corner_label(camera_label) if normalize_corner_label else str(camera_label or "").strip()
    if not cam:
        raise HTTPException(400, "请填写机位标识")
    paths = resolve_app_paths()

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
    pose_tier = pose_model_tier_from_backend(settings.backend)
    cam_slug = allocate_camera_storage_slug(paths, cam, pose_tier=pose_tier)

    batch_id = uuid.uuid4().hex[:12]
    paths.upload_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = paths.upload_dir / f"batch_{batch_id}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    skeleton_only_flag = parse_save_video_flag(
        skeleton_only if str(skeleton_only).strip() else None,
        default=False,
    )

    annotation_path: Path | None = None
    if not skeleton_only_flag:
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
        annotation_source="skeleton_only" if skeleton_only_flag else ("reflection" if annotation_path else ""),
        skeleton_only=skeleton_only_flag,
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

