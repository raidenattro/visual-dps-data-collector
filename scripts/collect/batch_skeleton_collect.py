#!/usr/bin/env python3
"""递归扫描文件夹内全部视频，批采集骨架；可选碰撞检测，效果对齐 Web 文件夹批处理。

用法:
  # 仅骨架（不算碰撞）
  python scripts/collect/batch_skeleton_collect.py D:/videos/1-1-1 --camera-label 1-1-1
  python scripts/collect/batch_skeleton_collect.py D:/videos --group-by-subfolder --dry-run

  # 骨架 + 碰撞（需 reflection.json 与 annotations/{编号}.json）
  python scripts/collect/batch_skeleton_collect.py D:/videos --group-by-subfolder --with-collision
  python scripts/collect/batch_skeleton_collect.py D:/videos/1-2组-1 --camera-label 1-2组-1 --with-collision --save-video

  python scripts/collect/batch_skeleton_collect.py D:/videos/1-1-1 --camera-label 1-1-1 --skip-existing

说明:
  - 递归包含所有子文件夹中的视频
  - 结果写入 localdata/json/{rtmpose-t|s|m}/{机位slug}/{视频主名}_{backend}/
  - --group-by-subfolder：用 root 下第一级子目录名作机位（如 1-2组-1、1-2组-2）
  - 输入文件夹不能同名时：同机位第二批用 1-2组-1(2)、1-2组-1(3)…（机位仍为 1-2组-1）
  - 输出 slug：1-2组-1 → 1-2-1；1-2组-1(2) → 1-2-1-(2)；无后缀且已占用则自动递增
  - --with-collision：按机位从 reflection.json 解析标注并计算 collisions / alarm_collisions
  - 碰撞模式下复用 annotations/{编号}.json，不为每个视频新建 clip_*.json
  - 保存配套视频时仅复制到 localdata/video，绝不移动或删除源目录中的 MP4
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
import uuid
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collect_core import run_collect_job, validate_video_path
from collect_core import parse_variant as _parse_variant
from config_loader import (
    allocate_camera_storage_slug,
    build_settings,
    camera_storage_slug,
    camera_storage_slug_for_folder,
    default_pose_json_path,
    json_bucket_dir,
    load_config_file,
    parse_camera_folder_name,
    pose_model_tier_from_backend,
    resolve_app_paths,
    resolve_config_path,
    sanitize_file_stem,
)
from model_assets import VIDEO_EXTENSIONS
from pose_store import STORAGE_V2_PARQUET, collect_result_has_skeleton, meta_sidecar_path

from api.collect_service import build_collect_config_snapshot
from api.naming import display_name_from_pose_file
from api.record_service import persist_record_video, record_id_from_pose_path
from api.reflection_service import reflection_json_path

try:
    from corner_label.reflection import load_reflection, normalize_corner_label
    from corner_label.resolve import ResolveResult, resolve_annotation_for_camera

    REFLECTION_OK = True
except ImportError:
    REFLECTION_OK = False


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="递归文件夹仅骨架批采集（不算碰撞，对齐 Web 批处理）",
    )
    p.add_argument(
        "root",
        type=Path,
        help="待扫描的视频根目录（含子文件夹）",
    )
    p.add_argument(
        "--camera-label",
        default="",
        help="机位标识；全部视频写入 json_dir/{机位slug}/（与 Web 批处理一致）",
    )
    p.add_argument(
        "--group-by-subfolder",
        action="store_true",
        help="按 root 下第一级子目录分组机位（与 --camera-label 二选一；根目录直下的视频需 --camera-label 作 fallback）",
    )
    p.add_argument("--config", "-c", default=None)
    p.add_argument("--backend", default=None)
    p.add_argument("--variant", choices=["t", "s", "m", "ms"], default=None)
    p.add_argument(
        "--det-variant",
        "--det-backend",
        dest="det_variant",
        default=None,
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
    p.add_argument(
        "--max-pose-frames",
        "--max-frames",
        dest="max_pose_frames",
        type=int,
        default=None,
    )
    p.add_argument("--frame-rate", type=float, default=None)
    p.add_argument(
        "--save-video",
        action="store_true",
        default=None,
        help="保存配套视频至 localdata/video/{机位slug}/",
    )
    p.add_argument("--no-save-video", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="仅列出待处理视频，不推理")
    p.add_argument(
        "--skip-existing",
        action="store_true",
        help="若目标记录目录已存在 manifest.json 则跳过",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="最多处理 N 个视频（0=不限，便于试跑）",
    )
    p.add_argument(
        "--with-collision",
        action="store_true",
        help="按机位从 reflection.json 加载标注并计算碰撞（默认仅骨架）",
    )
    return p


def iter_videos_recursive(root: Path) -> list[tuple[Path, str]]:
    """返回 (绝对路径, 相对 root 的 posix 路径) 列表，按路径排序。"""
    root = root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"目录不存在: {root}")
    out: list[tuple[Path, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        rel = path.relative_to(root).as_posix()
        out.append((path, rel))
    return out


def folder_key_for_item(
    rel_posix: str,
    *,
    group_by_subfolder: bool,
    fallback_camera: str,
) -> str:
    """返回分组键：--group-by-subfolder 时为第一级子目录名，否则为 --camera-label。"""
    parts = [p for p in rel_posix.split("/") if p]
    if group_by_subfolder and len(parts) >= 2:
        return parts[0]
    return fallback_camera


def base_record_dir(
    paths,
    *,
    backend: str,
    video_stem: str,
    camera_slug: str,
    pose_tier: str,
) -> Path:
    base = json_bucket_dir(paths, camera_slug or None, pose_tier=pose_tier)
    prefix = sanitize_file_stem(video_stem)
    safe_backend = re.sub(r"[^\w.-]", "_", backend)
    return base / f"{prefix}_{safe_backend}"


def record_already_exists(record_dir: Path) -> bool:
    manifest = record_dir / "manifest.json"
    return record_dir.is_dir() and manifest.is_file()


def load_reflection_for_cli():
    if not REFLECTION_OK:
        raise RuntimeError("reflection 模块未就绪")
    reflection_path = reflection_json_path()
    if not reflection_path.is_file():
        raise FileNotFoundError(
            f"缺少 reflection.json: {reflection_path}（可复制 examples/reflection.example.json）"
        )
    return load_reflection(reflection_path)


def resolve_annotation_for_camera_label(paths, camera_label: str) -> ResolveResult:
    """按机位标识从 reflection 解析标注（推理路径 + 源 annotations 文件）。"""
    label = normalize_corner_label(camera_label) if normalize_corner_label else str(camera_label or "").strip()
    if not label:
        raise ValueError("机位标识不能为空")
    reflection = load_reflection_for_cli()
    return resolve_annotation_for_camera(
        label,
        reflection=reflection,
        annotations_dir=paths.annotation_dir,
    )


def _persistent_source_annotation(paths, resolved: ResolveResult) -> Path | None:
    """取 annotations 目录下的持久源标注（非临时合并文件）。"""
    ann_dir = paths.annotation_dir.resolve()
    for path in resolved.source_annotation_paths:
        try:
            if path.resolve().parent == ann_dir and path.is_file():
                return path
        except OSError:
            continue
    for path in resolved.source_annotation_paths:
        if path.is_file():
            return path
    return None


def attach_source_annotation_to_record(
    paths,
    resolved: ResolveResult,
    *,
    pose_path: Path,
) -> Path | None:
    """复用 reflection 源标注：仅写入记录包 annotation.json，不新建 annotations/clip_*.json。"""
    src = _persistent_source_annotation(paths, resolved)
    if not src:
        return None
    if pose_path.is_dir():
        try:
            shutil.copy2(src, pose_path / "annotation.json")
        except OSError:
            pass
    return src


def collect_one_video(
    *,
    video_path: Path,
    rel_name: str,
    camera_label: str,
    camera_slug: str,
    settings,
    paths,
    collect_config: dict,
    save_video: bool,
    skip_existing: bool,
    batch_id: str,
    index: int,
    total: int,
    camera_annotation: ResolveResult | None = None,
    with_collision: bool = False,
) -> tuple[str, str]:
    """处理单个视频，返回 (status, message)。status: ok | skip | error"""
    video_stem = sanitize_file_stem(video_path.stem)
    source_name = Path(rel_name).name

    pose_tier = pose_model_tier_from_backend(settings.backend)
    pose_path = default_pose_json_path(
        paths,
        backend=settings.backend,
        video_stem=video_stem,
        job_id=f"{batch_id}_{index}",
        camera_slug=camera_slug or None,
        pose_tier=pose_tier,
    )
    if skip_existing:
        expected = base_record_dir(
            paths,
            backend=settings.backend,
            video_stem=video_stem,
            camera_slug=camera_slug,
            pose_tier=pose_tier,
        )
        if record_already_exists(expected):
            return "skip", f"已存在 {expected.relative_to(paths.json_dir)}"

    job_id = f"{batch_id}_{index}"
    print(f"\n[{index + 1}/{total}] {rel_name} → 机位 {camera_label} ({camera_slug})")
    t0 = time.perf_counter()

    def on_progress(current: int, frame_total: int) -> None:
        if frame_total > 0 and current % max(1, frame_total // 10) == 0:
            pct = round(current / frame_total * 100)
            print(f"  帧 {current}/{frame_total} ({pct}%)", end="\r", flush=True)

    inference_ann = camera_annotation.annotation_path if camera_annotation else None

    try:
        validate_video_path(video_path)
        data = run_collect_job(
            video_path=video_path,
            output_path=pose_path,
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
            on_progress=on_progress,
            annotation_path=str(inference_ann) if inference_ann else None,
            alarm_min_consecutive_frames=settings.alarm_min_consecutive_frames,
            alarm_cooldown_frames=settings.alarm_cooldown_frames,
        )
    except Exception as exc:
        return "error", str(exc)

    record_id = record_id_from_pose_path(pose_path)
    has_skeleton = collect_result_has_skeleton(data)
    collision_computed = bool(
        with_collision
        and inference_ann
        and Path(inference_ann).is_file()
        and data.get("collision", {}).get("enabled")
    )
    source_annotation: Path | None = None
    if with_collision and camera_annotation:
        source_annotation = attach_source_annotation_to_record(
            paths,
            camera_annotation,
            pose_path=pose_path,
        )
    saved_video_path = None
    if save_video and video_path.is_file():
        try:
            saved_video_path = persist_record_video(
                video_path,
                pose_path,
                camera_slug=camera_slug or None,
            )
        except OSError as exc:
            return "error", f"保存配套视频失败: {exc}"

    per_config = {
        **collect_config,
        "source_video": source_name,
        "video_stem": video_stem,
        "relative_path": rel_name,
    }
    meta = {
        "record_id": record_id,
        "job_id": job_id,
        "display_name": video_stem or display_name_from_pose_file(record_id, settings.backend),
        "video_stem": video_stem,
        "camera_label": camera_label or None,
        "camera_slug": camera_slug or None,
        "pose_model_tier": pose_tier,
        "storage": data.get("storage") or STORAGE_V2_PARQUET,
        "pose_file": f"{record_id}/manifest.json",
        "source_video": source_name,
        "backend": settings.backend,
        "variant": settings.variant,
        "det_backend": settings.det_backend,
        "det_variant": settings.det_variant,
        "det_model": data.get("det_model"),
        "frame_count": data.get("frame_count", 0),
        "has_skeleton": has_skeleton,
        "collision_computed": collision_computed,
        "elapsed_sec": data.get("elapsed_sec"),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "save_video": bool(save_video),
        "has_annotation": bool(source_annotation or data.get("annotation")),
        "collision_enabled": collision_computed,
        "collect_config": per_config,
    }
    if source_annotation and source_annotation.is_file():
        meta["annotation_file"] = source_annotation.name
        if camera_annotation and camera_annotation.annotation_ids:
            meta["annotation_ids"] = list(camera_annotation.annotation_ids)
    if saved_video_path and saved_video_path.is_file():
        meta["video_file"] = saved_video_path.name
        meta["video_url"] = f"/api/records/{record_id}/video"
        meta["has_video"] = True
    else:
        meta["has_video"] = False

    sidecar = meta_sidecar_path(paths.json_dir, record_id)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    with open(sidecar, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    manifest_path = pose_path / "manifest.json" if pose_path.is_dir() else pose_path
    if manifest_path.is_file():
        try:
            with open(manifest_path, encoding="utf-8") as f:
                manifest_doc = json.load(f)
            manifest_doc["collect_config"] = per_config
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest_doc, f, ensure_ascii=False, indent=2)
        except (OSError, json.JSONDecodeError):
            pass

    elapsed = time.perf_counter() - t0
    frames = data.get("frame_count", 0)
    return "ok", f"{record_id} · {frames} 帧 · {elapsed:.1f}s"


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    root = args.root.resolve()

    save_video_cli = None
    if args.no_save_video:
        save_video_cli = False
    elif args.save_video:
        save_video_cli = True

    if not args.group_by_subfolder and not str(args.camera_label or "").strip():
        print("❌ 请指定 --camera-label，或使用 --group-by-subfolder 按子目录分组", file=sys.stderr)
        return 2

    if args.with_collision and not REFLECTION_OK:
        print("❌ --with-collision 需要 corner_label / reflection 模块", file=sys.stderr)
        return 2

    settings = build_settings(
        config_path=resolve_config_path(args.config),
        cli={
            "backend": args.backend,
            "variant": args.variant,
            "det_variant": args.det_variant,
            "device": args.device,
            "ort_backend": args.ort_backend,
            "width": args.width,
            "height": args.height,
            "frame_interval": args.pose_frame_interval,
            "frame_rate": args.frame_rate,
            "max_frames": args.max_pose_frames,
            "save_video": save_video_cli,
        },
    )
    cfg_path = resolve_config_path(args.config)
    paths = resolve_app_paths(load_config_file(cfg_path), base=cfg_path.parent)

    try:
        videos = iter_videos_recursive(root)
    except FileNotFoundError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2

    if not videos:
        print(f"❌ 目录内无视频: {root}", file=sys.stderr)
        return 2

    fallback_camera = str(args.camera_label or "").strip()
    grouped: dict[str, list[tuple[Path, str]]] = defaultdict(list)
    for video_path, rel in videos:
        folder_key = folder_key_for_item(
            rel,
            group_by_subfolder=args.group_by_subfolder,
            fallback_camera=fallback_camera,
        )
        if not folder_key:
            print(f"❌ 无法确定机位: {rel}（请设置 --camera-label）", file=sys.stderr)
            return 2
        grouped[folder_key].append((video_path, rel))

    pose_tier = pose_model_tier_from_backend(settings.backend)
    flat: list[tuple[Path, str, str, str]] = []
    label_by_folder: dict[str, str] = {}
    slug_by_folder: dict[str, str] = {}
    for folder_key in sorted(grouped.keys()):
        if args.group_by_subfolder:
            cam_label, cam_slug = camera_storage_slug_for_folder(
                paths, folder_key, pose_tier=pose_tier
            )
        else:
            cam_label, dup_n = parse_camera_folder_name(folder_key)
            cam_label = cam_label or folder_key
            if dup_n is not None:
                cam_slug = f"{camera_storage_slug(cam_label)}-({dup_n})"
            else:
                cam_slug = allocate_camera_storage_slug(
                    paths, cam_label, pose_tier=pose_tier
                )
        label_by_folder[folder_key] = cam_label
        slug_by_folder[folder_key] = cam_slug
        for video_path, rel in grouped[folder_key]:
            flat.append((video_path, rel, cam_label, cam_slug))

    if args.limit and args.limit > 0:
        flat = flat[: args.limit]

    total = len(flat)
    print(f"📁 根目录: {root}")
    print(f"🎬 视频数: {total}（扩展名: {', '.join(sorted(VIDEO_EXTENSIONS))}）")
    print(f"📦 姿态: {settings.backend} · 检测: {settings.det_backend} · 数据层: {pose_tier}/")
    print(f"🏷️ 标注目录: {paths.annotation_dir}")
    print(f"⏱️ 采集节拍 frame_rate={settings.frame_rate}（0=全速）")
    if settings.save_video:
        print("💾 保存配套视频: 是（复制到 localdata/video，不移动源文件）")
    else:
        print("💾 保存配套视频: 否")
    print(f"🦴 模式: {'骨架 + 碰撞' if args.with_collision else '仅骨架（不算碰撞）'}")
    if args.group_by_subfolder:
        print(f"📂 机位分组: 按第一级子目录（{len(slug_by_folder)} 个文件夹）")
        for folder_key in sorted(slug_by_folder):
            cam = label_by_folder[folder_key]
            slug = slug_by_folder[folder_key]
            suffix = f"（机位 {cam}）" if folder_key != cam else ""
            print(f"   {folder_key} → {slug}{suffix}")
    else:
        fk = fallback_camera
        print(f"📂 机位: {label_by_folder.get(fk, fk)} → {slug_by_folder.get(fk, '')}")

    if args.dry_run:
        print("\n[dry-run] 待处理列表:")
        for i, (_, rel, cam, slug) in enumerate(flat):
            print(f"  {i + 1:4d}. [{cam} / {slug}] {rel}")
        return 0

    annotation_by_camera: dict[str, ResolveResult] = {}
    if args.with_collision:
        for cam_label in sorted(set(label_by_folder.values())):
            try:
                annotation_by_camera[cam_label] = resolve_annotation_for_camera_label(paths, cam_label)
            except (FileNotFoundError, ValueError, RuntimeError) as exc:
                print(f"❌ 机位 {cam_label} 标注解析失败: {exc}", file=sys.stderr)
                return 2
        print(f"📐 碰撞标注: 已解析 {len(annotation_by_camera)} 个机位")

    batch_id = uuid.uuid4().hex[:12]
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
        camera_label=fallback_camera or None,
        camera_slug=slug_by_folder.get(fallback_camera) if fallback_camera else None,
        batch_id=batch_id,
        annotation_source="reflection" if args.with_collision else "skeleton_only",
        skeleton_only=not args.with_collision,
    )

    ok = skip = err = 0
    batch_t0 = time.perf_counter()
    results: list[dict] = []
    errors: list[dict] = []

    for i, (video_path, rel, cam_label, cam_slug) in enumerate(flat):
        per_config = {
            **collect_config,
            "camera_label": cam_label,
            "camera_slug": cam_slug,
        }
        status, msg = collect_one_video(
            video_path=video_path,
            rel_name=rel,
            camera_label=cam_label,
            camera_slug=cam_slug,
            settings=settings,
            paths=paths,
            collect_config=per_config,
            save_video=settings.save_video,
            skip_existing=args.skip_existing,
            batch_id=batch_id,
            index=i,
            total=total,
            camera_annotation=annotation_by_camera.get(cam_label) if args.with_collision else None,
            with_collision=args.with_collision,
        )
        if status == "ok":
            ok += 1
            print(f"  ✅ {msg}")
            results.append({"relative_path": rel, "record_id": msg.split(" · ", 1)[0]})
        elif status == "skip":
            skip += 1
            print(f"  ⏭️ {msg}")
        else:
            err += 1
            print(f"  ❌ {msg}")
            errors.append({"relative_path": rel, "error": msg})

    total_elapsed = round(time.perf_counter() - batch_t0, 1)
    print(
        f"\n完成：成功 {ok}，跳过 {skip}，失败 {err}，共 {total} · 总耗时 {total_elapsed}s"
    )
    print(f"数据目录: {paths.json_dir}")
    if settings.save_video:
        print(f"视频目录: {paths.video_dir}")

    try:
        for folder_key, cam_slug in slug_by_folder.items():
            cam_label = label_by_folder[folder_key]
            bucket = json_bucket_dir(paths, cam_slug, pose_tier=pose_tier)
            manifest_path = bucket / f"_batch_{batch_id}.json"
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "batch_id": batch_id,
                        "camera_label": cam_label,
                        "camera_slug": cam_slug,
                        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "root": str(root),
                        "skeleton_only": not args.with_collision,
                        "with_collision": args.with_collision,
                        "success_count": ok,
                        "error_count": err,
                        "skip_count": skip,
                        "elapsed_sec": total_elapsed,
                        "collect_config": collect_config,
                        "results": [r for r in results],
                        "errors": errors,
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
    except OSError:
        pass

    return 0 if err == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
