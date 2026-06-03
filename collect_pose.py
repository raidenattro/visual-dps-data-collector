#!/usr/bin/env python3
"""命令行：视频骨架采集 → JSON（写入 paths.json_dir）。"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from collect_core import run_collect_job, validate_video_path
from collect_core import parse_variant as _parse_variant
from config_loader import (
    build_settings,
    load_config_file,
    record_video_path,
    resolve_app_paths,
    resolve_config_path,
)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="RTMPose 视频骨架采集 → JSON")
    p.add_argument("--config", "-c", default=None)
    p.add_argument("--backend", default=None, help="如 rtmpose_t（同 visual-dps models.backend）")
    p.add_argument("--variant", choices=["t", "s", "m", "ms"], default=None)
    p.add_argument(
        "--det-variant",
        "--det-backend",
        dest="det_variant",
        default=None,
        help="检测模型 t|s|m|l（rtmdet_t/m；s/l 无 ONNX 时回退）",
    )
    p.add_argument("--video", "-v", default=None)
    p.add_argument("--input", "-i", default=None)
    p.add_argument("--output", "-o", default=None, help="默认写入 paths.json_dir")
    p.add_argument(
        "--models-dir",
        "--models-onnx-dir",
        default=None,
        dest="models_dir",
        help="ONNX 根目录（默认 config paths.models_onnx_dir，即 localdata/models/onnx）",
    )
    p.add_argument("--device", default=None, choices=("cpu", "cuda"))
    p.add_argument("--ort-backend", default=None)
    p.add_argument("--width", type=int, default=None)
    p.add_argument("--height", type=int, default=None)
    p.add_argument(
        "--pose-frame-interval",
        "--frame-interval",
        dest="pose_frame_interval",
        type=int,
        default=None,
    )
    p.add_argument("--max-pose-frames", "--max-frames", dest="max_pose_frames", type=int, default=None)
    p.add_argument(
        "--frame-rate",
        type=float,
        default=None,
        help="采集推理节拍（帧/秒），0 不限制；默认 config inference.frame_rate",
    )
    p.add_argument(
        "--save-video",
        action="store_true",
        default=None,
        help="保存配套视频至 paths.video_dir（默认读 config storage.save_video）",
    )
    p.add_argument(
        "--annotation",
        "-a",
        default=None,
        help="visual-dps 格式标注 JSON（启用碰撞检测）",
    )
    p.add_argument(
        "--no-save-video",
        action="store_true",
        help="不保存配套视频",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    save_video_cli = None
    if args.no_save_video:
        save_video_cli = False
    elif args.save_video:
        save_video_cli = True

    settings = build_settings(
        config_path=resolve_config_path(args.config),
        cli={
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
        },
    )

    if not settings.video:
        print("❌ 请设置 source.video 或 --video", file=sys.stderr)
        return 2

    try:
        video_path = validate_video_path(settings.video)
    except (FileNotFoundError, ValueError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2

    print(f"📄 配置: {settings.config_path}")
    print(f"🎬 视频: {video_path}")
    print(f"📦 姿态: {settings.backend} · 检测: {settings.det_backend}")
    print(f"⏱️ 采集节拍 frame_rate={settings.frame_rate}（0=全速）")
    print(f"🎬 保存配套视频: {'是' if settings.save_video else '否'}")
    annotation_path = args.annotation
    if annotation_path:
        print(f"📐 标注 JSON: {annotation_path}")
    t0 = time.perf_counter()
    data = run_collect_job(
        video_path=video_path,
        output_path=settings.output,
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
        annotation_path=annotation_path,
        alarm_min_consecutive_frames=settings.alarm_min_consecutive_frames,
        alarm_cooldown_frames=settings.alarm_cooldown_frames,
    )
    pose_path = Path(settings.output)
    if annotation_path:
        ann_src = Path(annotation_path)
        if ann_src.is_file():
            shutil.copy2(ann_src, pose_path.with_name(f"{pose_path.stem}_annotation.json"))
    if settings.save_video:
        paths = resolve_app_paths(load_config_file(resolve_config_path(args.config)))
        dest = record_video_path(paths, pose_path, video_path.suffix)
        shutil.copy2(video_path, dest)
        sidecar = pose_path.with_suffix(".meta.json")
        record_id = pose_path.stem
        meta = {
            "record_id": record_id,
            "pose_file": pose_path.name,
            "video_file": dest.name,
            "video_url": f"/api/records/{record_id}/video",
            "has_video": True,
            "save_video": True,
            "source_video": video_path.name,
            "backend": settings.backend,
            "variant": settings.variant,
            "frame_count": data.get("frame_count", 0),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        with open(sidecar, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        print(f"🎬 视频: {dest}")
    print(f"💾 {settings.output} （{data.get('frame_count', 0)} 帧, {time.perf_counter() - t0:.1f}s）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
