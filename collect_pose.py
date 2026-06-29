#!/usr/bin/env python3
"""命令行：视频骨架采集 → JSON（写入 paths.json_dir）。"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import uuid
from pathlib import Path

from collect_core import run_collect_job, validate_video_path
from collect_core import parse_variant as _parse_variant
from api.collect_service import (
    attach_reflection_annotation_to_record,
    build_collect_config_snapshot,
    resolve_annotation_for_collect_cli,
)
from api.record_service import persist_record_video, record_id_from_pose_path
from api.reflection_service import normalize_corner_label
from config_loader import (
    UNGROUPED_CAMERA_LABEL,
    UNGROUPED_CAMERA_SLUG,
    allocate_camera_storage_slug,
    build_settings,
    default_pose_json_path,
    load_config_file,
    pose_model_tier_from_backend,
    resolve_app_paths,
    resolve_config_path,
    sanitize_file_stem,
)
from pose_store import STORAGE_V2_PARQUET, collect_result_has_skeleton, meta_sidecar_path


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="RTMPose 单视频骨架采集 → localdata（默认写入未分组机位目录）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            '  python collect_pose.py -v "/path/to/video.mp4" --backend rtmpose_m --det-variant m \\\n'
            '    --camera-label "1-1组-1" --save-video\n'
            "  python collect_pose.py -v clip.mp4 --backend rtmpose_t --det-variant m --save-video\n"
            "  python collect_pose.py -v clip.mp4 --skeleton-only --variant t\n"
        ),
    )
    p.add_argument("--config", "-c", default=None, help="config.json 路径")

    io = p.add_argument_group("输入输出")
    io.add_argument("--video", "-v", default=None, help="源视频路径")
    io.add_argument("--input", "-i", default=None, help="同 --video")
    io.add_argument(
        "--output",
        "-o",
        default=None,
        help="记录包输出路径；默认 localdata/json/{rtmpose-t}/{机位|_ungrouped}/{视频}_{backend}/",
    )
    io.add_argument(
        "--camera-label",
        "--camera",
        dest="camera_label",
        default="",
        help="机位标识（如 1-2组-1）；从 reflection.json 解析标注；未指定则写入 _ungrouped 目录",
    )
    io.add_argument(
        "--annotation",
        "-a",
        default=None,
        help="标注 JSON；与 --camera-label 二选一",
    )
    io.add_argument("--save-video", action="store_true", default=None, help="保存配套视频")
    io.add_argument("--no-save-video", action="store_true", help="不保存配套视频")

    model = p.add_argument_group("模型（覆盖 config.json models.*）")
    model.add_argument(
        "--backend",
        default=None,
        help="姿态模型：rtmpose_t / rtmpose_s / rtmpose_m（同 Web models.backend）",
    )
    model.add_argument(
        "--variant",
        choices=["t", "s", "m", "ms"],
        default=None,
        help="姿态档 t|s|m（与 --backend 二选一，指定后自动推导 backend）",
    )
    model.add_argument(
        "--det-variant",
        "--det-backend",
        dest="det_variant",
        default=None,
        help="检测档 nano|s|m|l（同 Web models.det_variant；t 为 nano 旧别名；推荐 m）",
    )
    model.add_argument(
        "--models-dir",
        "--models-onnx-dir",
        default=None,
        dest="models_dir",
        help="ONNX 根目录（默认 localdata/models/onnx）",
    )
    model.add_argument("--device", default=None, choices=("cpu", "cuda"), help="推理设备")
    model.add_argument("--ort-backend", default=None, help="ONNX Runtime 后端名")

    infer = p.add_argument_group("推理（覆盖 config.json inference.*）")
    infer.add_argument("--width", type=int, default=None, help="推理宽，0=按高自动")
    infer.add_argument("--height", type=int, default=None, help="推理高，0=不缩放")
    infer.add_argument(
        "--pose-frame-interval",
        "--frame-interval",
        dest="pose_frame_interval",
        type=int,
        default=None,
        help="抽帧间隔 N（每 N 帧推理一次）",
    )
    infer.add_argument(
        "--max-pose-frames",
        "--max-frames",
        dest="max_pose_frames",
        type=int,
        default=None,
        help="最多采集帧数，0=不限",
    )
    infer.add_argument(
        "--frame-rate",
        type=float,
        default=None,
        help="采集节拍（帧/秒），0=全速",
    )
    infer.add_argument(
        "--alarm-min-consecutive-frames",
        type=int,
        default=None,
        help="碰撞报警：连续命中帧数（默认 config）",
    )
    infer.add_argument(
        "--alarm-cooldown-frames",
        type=int,
        default=None,
        help="碰撞报警：同货框冷却帧数（默认 config）",
    )
    infer.add_argument(
        "--skeleton-only",
        action="store_true",
        help="仅计算骨架，不算碰撞（无需标注）",
    )
    return p


def _make_cli_progress_printer(interval_sec: float = 10.0):
    """每 interval_sec 秒在终端刷新一次「处理帧数/总帧数」。"""
    last_print_at = time.perf_counter()

    def on_progress(current: int, frame_total: int) -> None:
        nonlocal last_print_at
        now = time.perf_counter()
        finished = frame_total > 0 and current >= frame_total
        if not finished and now - last_print_at < interval_sec:
            return
        last_print_at = now
        total_label = str(frame_total) if frame_total > 0 else "?"
        print(f"  ⏳ 处理帧数 {current}/{total_label}", end="\r", flush=True)

    return on_progress


def _resolve_storage_camera(
    paths,
    *,
    camera_label_arg: str,
    cam_label: str | None,
    cam_slug: str | None,
    pose_tier: str,
    skeleton_only: bool,
) -> tuple[str | None, str]:
    """确定 JSON/视频存储用的机位 label 与 slug。"""
    label_arg = str(camera_label_arg or "").strip()
    if label_arg and skeleton_only:
        norm = normalize_corner_label(label_arg) if normalize_corner_label else label_arg
        if norm:
            slug = allocate_camera_storage_slug(paths, norm, pose_tier=pose_tier)
            return norm, slug
    if cam_label and cam_slug:
        return cam_label, cam_slug
    if cam_label and not cam_slug:
        slug = allocate_camera_storage_slug(paths, cam_label, pose_tier=pose_tier)
        return cam_label, slug
    return UNGROUPED_CAMERA_LABEL, UNGROUPED_CAMERA_SLUG


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    save_video_cli = None
    if args.no_save_video:
        save_video_cli = False
    elif args.save_video:
        save_video_cli = True

    cli_overrides: dict = {
        "backend": args.backend,
        "variant": args.variant,
        "video": args.video or args.input,
        "output": args.output,
        "models_dir": args.models_dir,
        "device": args.device,
        "ort_backend": args.ort_backend,
        "width": args.width,
        "height": args.height,
        "frame_interval": args.pose_frame_interval,
        "max_frames": args.max_pose_frames,
        "frame_rate": args.frame_rate,
        "save_video": save_video_cli,
        "det_variant": args.det_variant,
    }
    if args.alarm_min_consecutive_frames is not None:
        cli_overrides["alarm_min_consecutive_frames"] = args.alarm_min_consecutive_frames
    if args.alarm_cooldown_frames is not None:
        cli_overrides["alarm_cooldown_frames"] = args.alarm_cooldown_frames

    settings = build_settings(
        config_path=resolve_config_path(args.config),
        cli=cli_overrides,
    )

    if not settings.video:
        print("❌ 请设置 source.video 或 --video", file=sys.stderr)
        return 2

    camera_label_arg = str(args.camera_label or "").strip()
    skeleton_only = bool(args.skeleton_only)
    if camera_label_arg and args.annotation:
        print("❌ --camera-label 与 --annotation 不能同时使用", file=sys.stderr)
        return 2
    if skeleton_only and args.annotation:
        print("❌ --skeleton-only 与 --annotation 不能同时使用", file=sys.stderr)
        return 2

    try:
        video_path = validate_video_path(settings.video)
    except (FileNotFoundError, ValueError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2

    paths = resolve_app_paths(load_config_file(resolve_config_path(args.config)))
    video_stem = sanitize_file_stem(video_path.stem)
    upload_ann: Path | None = None
    if args.annotation:
        upload_ann = Path(args.annotation).resolve()
        if not upload_ann.is_file():
            print(f"❌ 标注文件不存在: {upload_ann}", file=sys.stderr)
            return 2

    work_dir = paths.upload_dir / f"cli_{uuid.uuid4().hex[:8]}"
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        inference_ann: Path | None = None
        cam_label: str | None = None
        cam_slug: str | None = None
        ann_source = "skeleton_only"
        resolve_result = None

        if skeleton_only:
            if camera_label_arg:
                norm = normalize_corner_label(camera_label_arg) if normalize_corner_label else camera_label_arg
                if norm:
                    cam_label = norm
        else:
            try:
                inference_ann, cam_label, cam_slug, ann_source, resolve_result = resolve_annotation_for_collect_cli(
                    paths,
                    video_stem=video_stem,
                    camera_label=camera_label_arg,
                    upload_ann_path=upload_ann,
                    work_dir=work_dir,
                )
            except FileNotFoundError as exc:
                print(f"❌ {exc}", file=sys.stderr)
                return 2
            except ValueError as exc:
                print(f"❌ {exc}", file=sys.stderr)
                return 2

        pose_tier = pose_model_tier_from_backend(settings.backend)
        cam_label, cam_slug = _resolve_storage_camera(
            paths,
            camera_label_arg=camera_label_arg,
            cam_label=cam_label,
            cam_slug=cam_slug,
            pose_tier=pose_tier,
            skeleton_only=skeleton_only,
        )

        if args.output:
            output_path = Path(settings.output)
        else:
            output_path = default_pose_json_path(
                paths,
                backend=settings.backend,
                video_stem=video_stem,
                camera_slug=cam_slug,
                pose_tier=pose_tier,
            )

        print(f"📄 配置: {settings.config_path}")
        print(f"🎬 视频: {video_path}")
        print(f"📦 姿态: {settings.backend} ({settings.variant}) · 检测: {settings.det_backend} ({settings.det_variant})")
        print(f"📍 存储机位: {cam_label} → {cam_slug}")
        print(f"⏱️ 采集节拍 frame_rate={settings.frame_rate}（0=全速）· 间隔={settings.pose_frame_interval}")
        print(f"🎬 保存配套视频: {'是' if settings.save_video else '否'}")
        if skeleton_only:
            print("🦴 模式: 仅骨架（不算碰撞）")
        else:
            print(f"📐 标注: {inference_ann}（来源: {ann_source}）")
        print(f"💾 输出: {output_path}")

        collect_config = build_collect_config_snapshot(
            backend=settings.backend,
            variant=settings.variant,
            det_variant=settings.det_variant,
            det_backend=settings.det_backend,
            width=settings.infer_width,
            height=settings.infer_height,
            pose_frame_interval=settings.pose_frame_interval,
            frame_rate=settings.frame_rate,
            max_pose_frames=settings.max_pose_frames,
            save_video=settings.save_video,
            alarm_min_consecutive_frames=settings.alarm_min_consecutive_frames,
            alarm_cooldown_frames=settings.alarm_cooldown_frames,
            camera_label=cam_label or "",
            camera_slug=cam_slug or "",
            annotation_source=ann_source,
            skeleton_only=skeleton_only,
        )

        print("⏳ 开始推理…")
        t0 = time.perf_counter()
        try:
            data = run_collect_job(
                video_path=video_path,
                output_path=output_path,
                models_dir=settings.models_dir,
                variant=_parse_variant(settings.variant),
                det_variant=settings.det_variant,
                device=settings.device,
                ort_backend=settings.ort_backend,
                width=settings.infer_width,
                height=settings.infer_height,
                frame_interval=settings.pose_frame_interval,
                frame_rate=settings.frame_rate,
                max_frames=settings.max_pose_frames,
                annotation_path=str(inference_ann) if inference_ann else None,
                alarm_min_consecutive_frames=settings.alarm_min_consecutive_frames,
                alarm_cooldown_frames=settings.alarm_cooldown_frames,
                on_progress=_make_cli_progress_printer(10.0),
            )
        finally:
            print()

        pose_path = Path(output_path)
        record_id = record_id_from_pose_path(pose_path)
        has_skeleton = collect_result_has_skeleton(data)
        collision_computed = bool(
            not skeleton_only and data.get("collision", {}).get("enabled")
        )

        source_annotation: Path | None = None
        if ann_source == "reflection" and resolve_result is not None:
            source_annotation = attach_reflection_annotation_to_record(
                paths,
                resolve_result,
                pose_path=pose_path,
            )
        elif inference_ann and inference_ann.is_file():
            rid = pose_path.name if pose_path.is_dir() else pose_path.stem
            ann_dest = (
                pose_path.parent / f"{rid}_annotation.json"
                if pose_path.is_dir()
                else pose_path.with_name(f"{pose_path.stem}_annotation.json")
            )
            shutil.copy2(inference_ann, ann_dest)
            source_annotation = ann_dest

        saved_video_path: Path | None = None
        if settings.save_video:
            try:
                saved_video_path = persist_record_video(
                    video_path,
                    pose_path,
                    camera_slug=cam_slug or None,
                )
            except (OSError, ValueError) as exc:
                print(f"❌ 保存配套视频失败: {exc}", file=sys.stderr)
                return 1

        sidecar = meta_sidecar_path(paths.json_dir, record_id)
        meta = {
            "record_id": record_id,
            "storage": data.get("storage") or STORAGE_V2_PARQUET,
            "pose_file": f"{record_id}/manifest.json",
            "video_stem": video_stem,
            "camera_label": cam_label,
            "camera_slug": cam_slug,
            "pose_model_tier": pose_tier,
            "source_video": video_path.name,
            "backend": settings.backend,
            "variant": settings.variant,
            "det_variant": settings.det_variant,
            "det_backend": settings.det_backend,
            "frame_count": data.get("frame_count", 0),
            "has_skeleton": has_skeleton,
            "collision_computed": collision_computed,
            "skeleton_only": skeleton_only,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "save_video": bool(settings.save_video),
            "has_annotation": bool(source_annotation),
            "collect_config": {
                **collect_config,
                "source_video": video_path.name,
                "video_stem": video_stem,
            },
        }
        if source_annotation and source_annotation.is_file():
            meta["annotation_file"] = source_annotation.name
            if resolve_result and resolve_result.annotation_ids:
                meta["annotation_ids"] = list(resolve_result.annotation_ids)
        if saved_video_path and saved_video_path.is_file():
            meta["video_file"] = saved_video_path.name
            meta["video_url"] = f"/api/records/{record_id}/video"
            meta["has_video"] = True
        else:
            meta["has_video"] = False

        sidecar.parent.mkdir(parents=True, exist_ok=True)
        with open(sidecar, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        if saved_video_path and saved_video_path.is_file():
            print(f"🎬 视频: {saved_video_path}")
        print(f"✅ {output_path} （{data.get('frame_count', 0)} 帧, {time.perf_counter() - t0:.1f}s）")
        return 0
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
