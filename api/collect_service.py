"""视频采集后台任务（单条与批处理）。"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from annotation_store import (
    load_annotation_json,
    require_annotation_for_collect,
    validate_annotation_payload,
)
from collect_core import run_collect_job
from config_loader import (
    build_settings,
    camera_storage_slug,
    default_pose_json_path,
    json_bucket_dir,
    resolve_app_paths,
    resolve_config_path,
    sanitize_file_stem,
)
from pose_store import STORAGE_V2_PARQUET, meta_sidecar_path

from api.job_store import batch_timing_from_progress, update_job
from api.job_store import _jobs, _jobs_lock
from api.record_service import (
    persist_playback_annotation,
    persist_record_video,
    record_id_from_pose_path,
)
from api.reflection_service import (
    REFLECTION_OK,
    load_reflection_or_http,
    normalize_corner_label,
    resolve_annotation_for_camera,
)
from event_engine.annotation_boxes import load_annotation_config

def build_collect_config_snapshot(
    *,
    backend: str,
    variant: str,
    det_variant: str,
    det_backend: str,
    width: int,
    height: int,
    pose_frame_interval: int,
    frame_rate: float,
    max_pose_frames: int | None,
    save_video: bool,
    alarm_min_consecutive_frames: int,
    alarm_cooldown_frames: int,
    camera_label: str = "",
    camera_slug: str = "",
    batch_id: str = "",
    annotation_source: str = "",
) -> dict[str, Any]:
    """采集任务配置快照，写入 meta 与批处理清单，便于与结果数据关联。"""
    return {
        "backend": backend,
        "variant": variant,
        "det_variant": det_variant,
        "det_backend": det_backend,
        "infer_width": int(width),
        "infer_height": int(height),
        "pose_frame_interval": int(pose_frame_interval),
        "frame_rate": float(frame_rate),
        "max_pose_frames": int(max_pose_frames) if max_pose_frames is not None else 0,
        "save_video": bool(save_video),
        "alarm_min_consecutive_frames": int(alarm_min_consecutive_frames),
        "alarm_cooldown_frames": int(alarm_cooldown_frames),
        "camera_label": camera_label or None,
        "camera_slug": camera_slug or None,
        "batch_id": batch_id or None,
        "annotation_source": annotation_source or None,
    }


from api.naming import display_name_from_pose_file


def run_job(
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
    camera_label: str = "",
    camera_slug: str = "",
    collect_config: dict[str, Any] | None = None,
    progress_job_id: str | None = None,
    batch_status_message: str = "",
) -> None:
    settings = build_settings(config_path=resolve_config_path(None), cli={})
    alarm_min = max(1, int(alarm_min_consecutive_frames))
    alarm_cd = max(1, int(alarm_cooldown_frames))
    progress_target = progress_job_id or job_id

    def on_progress(current: int, total: int) -> None:
        inner = (current / total) if total > 0 else 0.0
        if progress_job_id and progress_job_id != job_id:
            with _jobs_lock:
                batch_job = dict(_jobs.get(progress_target, {}))
            batch_t0 = float(batch_job.get("started_at") or time.perf_counter())
            video_t0 = float(batch_job.get("current_video_started_at") or batch_t0)
            vi = int(batch_job.get("batch_video_index", 0))
            vt = int(batch_job.get("total_videos") or 1)
            durations = list(batch_job.get("video_durations") or [])
            pct, elapsed, eta = batch_timing_from_progress(
                batch_started_at=batch_t0,
                video_started_at=video_t0,
                video_index=vi,
                video_total=vt,
                inner=inner,
                completed_video_secs=durations,
            )
            msg = batch_status_message or f"处理中 {current}/{total or '?'} 帧"
            fields: dict[str, Any] = {
                "progress": pct,
                "message": msg,
                "current_frame": current,
                "frame_total": total,
                "elapsed_sec": elapsed,
            }
            if eta is not None:
                fields["eta_sec"] = eta
            update_job(progress_target, **fields)
        else:
            pct = round(inner * 100, 1) if total > 0 else 0.0
            update_job(
                progress_target,
                progress=pct,
                message=f"处理中 {current}/{total or '?'} 帧",
                current_frame=current,
                frame_total=total,
            )

    try:
        update_job(job_id, status="running", message="模型推理中…")
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
        record_id = record_id_from_pose_path(pose_path)
        saved_annotation: Path | None = None
        if annotation_path and annotation_path.is_file():
            playback_ann = persist_playback_annotation(
                annotation_path,
                video_stem=video_stem,
                pose_path=pose_path,
                source_video=source_video_name,
            )
            saved_annotation = playback_ann or annotation_path
        saved_video_path: Path | None = None
        if save_video and video_path.is_file():
            try:
                saved_video_path = persist_record_video(
                    video_path,
                    pose_path,
                    camera_slug=camera_slug or None,
                )
            except OSError as exc:
                raise RuntimeError(f"保存配套视频失败: {exc}") from exc

        meta = {
            "record_id": record_id,
            "job_id": job_id,
            "display_name": video_stem or display_name_from_pose_file(record_id, backend),
            "video_stem": video_stem,
            "camera_label": camera_label or None,
            "camera_slug": camera_slug or None,
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
        if collect_config:
            meta["collect_config"] = collect_config
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
        if collect_config and pose_path.is_dir():
            manifest_path = pose_path / "manifest.json"
            if manifest_path.is_file():
                try:
                    with open(manifest_path, encoding="utf-8") as f:
                        manifest_doc = json.load(f)
                    manifest_doc["collect_config"] = collect_config
                    with open(manifest_path, "w", encoding="utf-8") as f:
                        json.dump(manifest_doc, f, ensure_ascii=False, indent=2)
                except (OSError, json.JSONDecodeError):
                    pass
        update_job(
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
        update_job(job_id, status="error", message=str(exc))
    finally:
        if video_path.is_file():
            try:
                video_path.unlink()
            except OSError:
                pass
        parent = video_path.parent
        if parent.name.startswith("tmp_") and parent.is_dir():
            shutil.rmtree(parent, ignore_errors=True)


def resolve_collect_annotation(
    tmp_dir: Path,
    paths,
    *,
    video_stem: str,
    camera_label: str,
    upload_ann_path: Path | None,
) -> tuple[Path | None, str | None, str | None]:
    """返回 (annotation_path, camera_label, camera_slug)。"""
    annotation_path: Path | None = None
    cam_label: str | None = None
    cam_slug: str | None = None

    if upload_ann_path and upload_ann_path.is_file():
        annotation_path = upload_ann_path
        _, err = validate_annotation_payload(load_annotation_config(annotation_path))
        if err:
            raise HTTPException(400, err)
        return annotation_path, cam_label, cam_slug

    cam = normalize_corner_label(camera_label) if normalize_corner_label else str(camera_label or "").strip()
    if cam and REFLECTION_OK and resolve_annotation_for_camera:
        from corner_label.resolve import resolve_annotation_for_camera as resolve_cam

        reflection = load_reflection_or_http()
        resolved = resolve_cam(
            cam,
            reflection=reflection,
            annotations_dir=paths.annotation_dir,
        )
        _, err = validate_annotation_payload(load_annotation_config(resolved.annotation_path))
        if err:
            raise HTTPException(400, err)
        if len(resolved.source_annotation_paths) > 1:
            dest = tmp_dir / "annotation_merged.json"
            shutil.copy2(resolved.annotation_path, dest)
            if resolved.annotation_path.name.startswith("tmp"):
                resolved.annotation_path.unlink(missing_ok=True)
            annotation_path = dest
        else:
            annotation_path = resolved.source_annotation_paths[0]
        cam_label = resolved.camera_label
        cam_slug = camera_storage_slug(cam_label)
        return annotation_path, cam_label, cam_slug

    try:
        annotation_path = require_annotation_for_collect(
            video_stem,
            annotation_dir=paths.annotation_dir,
            upload_path=None,
        )
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return annotation_path, cam_label, cam_slug


def run_batch_job(
    batch_id: str,
    items: list[tuple[Path, str, str]],
    *,
    annotation_path: Path | None,
    camera_label: str,
    camera_slug: str,
    collect_config: dict[str, Any],
    backend: str,
    variant: str,
    det_variant: str,
    det_backend: str,
    width: int,
    height: int,
    pose_frame_interval: int,
    frame_rate: float,
    max_pose_frames: int | None,
    save_video: bool,
    alarm_min_consecutive_frames: int,
    alarm_cooldown_frames: int,
) -> None:
    total = len(items)
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    paths = resolve_app_paths()
    batch_t0 = time.perf_counter()
    video_durations: list[float] = []

    update_job(
        batch_id,
        started_at=batch_t0,
        video_durations=[],
        total_videos=total,
    )

    for i, (video_path, video_stem, source_name) in enumerate(items):
        sub_id = f"{batch_id}_{i}"
        status_msg = f"批处理 {i + 1}/{total}: {source_name}"
        video_t0 = time.perf_counter()
        inner = 0.0
        pct, elapsed_batch, eta_sec = batch_timing_from_progress(
            batch_started_at=batch_t0,
            video_started_at=video_t0,
            video_index=i,
            video_total=total,
            inner=inner,
            completed_video_secs=video_durations,
        )
        update_job(
            batch_id,
            status="running",
            progress=pct,
            message=status_msg,
            current_index=i + 1,
            batch_video_index=i,
            total_videos=total,
            elapsed_sec=elapsed_batch,
            eta_sec=eta_sec,
            current_video=source_name,
            current_video_started_at=video_t0,
        )
        pose_path = default_pose_json_path(
            paths,
            backend=backend,
            video_stem=video_stem,
            job_id=sub_id,
            camera_slug=camera_slug or None,
        )
        per_config = {
            **collect_config,
            "source_video": source_name,
            "video_stem": video_stem,
        }
        t_video = time.perf_counter()
        try:
            run_job(
                sub_id,
                video_path,
                pose_path,
                backend=backend,
                variant=variant,
                det_variant=det_variant,
                det_backend=det_backend,
                video_stem=video_stem,
                source_video_name=source_name,
                width=width,
                height=height,
                pose_frame_interval=pose_frame_interval,
                frame_rate=frame_rate,
                max_pose_frames=max_pose_frames,
                save_video=save_video,
                annotation_path=annotation_path,
                alarm_min_consecutive_frames=alarm_min_consecutive_frames,
                alarm_cooldown_frames=alarm_cooldown_frames,
                camera_label=camera_label,
                camera_slug=camera_slug,
                collect_config=per_config,
                progress_job_id=batch_id,
                batch_status_message=status_msg,
            )
            video_durations.append(time.perf_counter() - t_video)
            update_job(batch_id, video_durations=list(video_durations))
            with _jobs_lock:
                sub = dict(_jobs.get(sub_id, {}))
            if sub.get("status") == "done":
                results.append(
                    {
                        "index": i,
                        "source_video": source_name,
                        "record_id": sub.get("record_id"),
                        "frame_count": sub.get("frame_count"),
                        "display_name": sub.get("display_name"),
                        "elapsed_sec": sub.get("elapsed_sec"),
                    }
                )
            else:
                errors.append(
                    {
                        "index": i,
                        "source_video": source_name,
                        "error": sub.get("message") or "采集失败",
                    }
                )
        except Exception as exc:
            video_durations.append(time.perf_counter() - t_video)
            errors.append({"index": i, "source_video": source_name, "error": str(exc)})

        done_pct = round(min(100.0, (i + 1) / total * 100.0), 1) if total else 100.0
        update_job(
            batch_id,
            progress=done_pct,
            elapsed_sec=round(time.perf_counter() - batch_t0, 1),
        )

    status = "error" if not results and errors else "done"
    total_elapsed = round(time.perf_counter() - batch_t0, 1)
    batch_manifest = {
        "batch_id": batch_id,
        "camera_label": camera_label,
        "camera_slug": camera_slug,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total_videos": total,
        "success_count": len(results),
        "error_count": len(errors),
        "elapsed_sec": total_elapsed,
        "collect_config": collect_config,
        "results": results,
        "errors": errors,
    }
    try:
        bucket = json_bucket_dir(paths, camera_slug or None)
        manifest_path = bucket / f"_batch_{batch_id}.json"
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(batch_manifest, f, ensure_ascii=False, indent=2)
        batch_manifest["manifest_path"] = str(manifest_path)
    except OSError:
        pass

    update_job(
        batch_id,
        status=status,
        progress=100,
        message=f"批处理完成：成功 {len(results)}，失败 {len(errors)} · 总耗时 {total_elapsed}s",
        results=results,
        errors=errors,
        camera_label=camera_label,
        camera_slug=camera_slug,
        success_count=len(results),
        error_count=len(errors),
        elapsed_sec=total_elapsed,
        eta_sec=0,
        batch_manifest=batch_manifest,
    )

