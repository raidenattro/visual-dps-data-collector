"""标注页：按模型层 + 机位解析内置视频与 reflection 标注。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from annotation_store import (
    ANNOTATION_SOURCE_MASTER,
    annotation_dir_display_rel,
    annotation_dir_for_source,
    annotation_path_for_video_stem,
    load_annotation_json,
    normalize_annotation_source,
)
from config_loader import (
    POSE_MODEL_TIERS,
    AppPaths,
    camera_storage_slug,
)
from corner_label.reflection import annotation_json_path
from model_assets import VIDEO_EXTENSIONS


def normalize_pose_tier(raw: str) -> str:
    """rtmpose_t / rtmpose-t / t → rtmpose-t。"""
    s = str(raw or "").strip().lower().replace("_", "-")
    if s in POSE_MODEL_TIERS:
        return s
    if s in ("t", "s", "m"):
        return f"rtmpose-{s}"
    raise ValueError(f"无效 pose_tier: {raw!r}，可选 rtmpose-t / rtmpose-s / rtmpose-m")


def _list_video_files(bucket: Path) -> list[Path]:
    if not bucket.is_dir():
        return []
    files = [
        p
        for p in bucket.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS and not p.name.startswith(".")
    ]
    return sorted(files, key=lambda p: p.name.lower())


def _camera_slug_candidates(camera_label: str) -> list[str]:
    base = camera_storage_slug(camera_label)
    return [base] if base else []


def resolve_camera_video_bucket(
    paths: AppPaths,
    camera_label: str,
    *,
    pose_tier: str,
) -> tuple[str, Path] | None:
    """在 video_dir/{pose_tier}/ 下解析机位目录并返回首个视频路径。"""
    tier = normalize_pose_tier(pose_tier)
    tier_root = paths.video_dir / tier
    if not tier_root.is_dir():
        return None

    base_slug = camera_storage_slug(camera_label)
    slug_order: list[str] = []
    seen: set[str] = set()

    def add_slug(name: str) -> None:
        if name and name not in seen:
            seen.add(name)
            slug_order.append(name)

    add_slug(base_slug)
    if base_slug:
        prefix = f"{base_slug}-("
        for p in sorted(tier_root.iterdir(), key=lambda x: x.name.lower()):
            if not p.is_dir():
                continue
            if p.name == base_slug or p.name.startswith(prefix):
                add_slug(p.name)

    for slug in slug_order:
        bucket = tier_root / slug
        videos = _list_video_files(bucket)
        if videos:
            return slug, videos[0]
    return None


def video_pose_tier_for_annotate(annotation_source: str) -> str:
    """标注来源为模型层时同层取视频；母本时默认 rtmpose-t。"""
    norm = normalize_annotation_source(annotation_source)
    if norm != ANNOTATION_SOURCE_MASTER:
        return norm
    return "rtmpose-t"


@dataclass
class AnnotationListItem:
    annotation_id: str
    json_file: str
    has_file: bool
    has_master_file: bool
    has_tier_file: bool
    resolved_from: str
    box_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "annotation_id": self.annotation_id,
            "json_file": self.json_file,
            "has_file": self.has_file,
            "has_master_file": self.has_master_file,
            "has_tier_file": self.has_tier_file,
            "resolved_from": self.resolved_from,
            "box_count": self.box_count,
        }


def list_annotations_for_camera(
    paths: AppPaths,
    reflection: Any,
    camera_label: str,
    *,
    annotation_source: str,
) -> list[AnnotationListItem]:
    norm = normalize_annotation_source(annotation_source)
    tier_dir = annotation_dir_for_source(paths, norm) if norm != ANNOTATION_SOURCE_MASTER else None
    ann_ids = reflection.annotations_for_camera(camera_label)
    items: list[AnnotationListItem] = []
    for aid in ann_ids:
        master_path = annotation_json_path(aid, paths.annotation_dir)
        has_master = master_path.is_file()
        has_tier = False
        if tier_dir is not None:
            has_tier = annotation_path_for_video_stem(aid, annotation_dir=tier_dir).is_file()

        if norm == ANNOTATION_SOURCE_MASTER:
            load_dir = paths.annotation_dir
            has_file = has_master
            resolved_from = "master" if has_master else "none"
        elif has_tier:
            load_dir = tier_dir
            has_file = True
            resolved_from = "tier"
        elif has_master:
            load_dir = paths.annotation_dir
            has_file = True
            resolved_from = "master"
        else:
            load_dir = tier_dir or paths.annotation_dir
            has_file = False
            resolved_from = "none"

        box_count = 0
        if has_file:
            data = load_annotation_json(aid, annotation_dir=load_dir)
            if data:
                boxes = data.get("boxes")
                if isinstance(boxes, list):
                    box_count = len(boxes)
                else:
                    for shelf in data.get("shelves") or []:
                        if isinstance(shelf, dict) and isinstance(shelf.get("boxes"), list):
                            box_count += len(shelf["boxes"])
        items.append(
            AnnotationListItem(
                annotation_id=aid,
                json_file=f"{aid}.json",
                has_file=has_file,
                has_master_file=has_master,
                has_tier_file=has_tier,
                resolved_from=resolved_from,
                box_count=box_count,
            )
        )
    return items


def build_annotate_context(
    paths: AppPaths,
    reflection: Any,
    *,
    annotation_source: str,
    camera_label: str,
) -> dict[str, Any]:
    norm = normalize_annotation_source(annotation_source)
    video_tier = video_pose_tier_for_annotate(norm)
    label = str(camera_label or "").strip()
    if not label:
        raise ValueError("请填写机位标识")
    if not reflection.has_camera(label):
        raise ValueError(f"机位 {label!r} 不在 reflection.json 中")

    annotations = list_annotations_for_camera(
        paths, reflection, label, annotation_source=norm
    )
    video_hit = resolve_camera_video_bucket(paths, label, pose_tier=video_tier)
    camera_slug = video_hit[0] if video_hit else camera_storage_slug(label)
    video_file = video_hit[1].name if video_hit else ""

    return {
        "annotation_source": norm,
        "video_pose_tier": video_tier,
        "annotation_readonly": norm == ANNOTATION_SOURCE_MASTER,
        "annotation_save_dir": (
            annotation_dir_display_rel(paths, norm) if norm != ANNOTATION_SOURCE_MASTER else None
        ),
        "camera_label": label,
        "camera_slug": camera_slug,
        "annotations": [a.to_dict() for a in annotations],
        "has_video": bool(video_hit),
        "video_file": video_file,
        "video_dir": f"localdata/video/{video_tier}/{camera_slug}",
    }


def first_frame_video_for_camera(
    paths: AppPaths,
    camera_label: str,
    *,
    pose_tier: str,
) -> Path:
    hit = resolve_camera_video_bucket(paths, camera_label, pose_tier=pose_tier)
    if not hit:
        tier = normalize_pose_tier(pose_tier)
        slug = camera_storage_slug(camera_label)
        raise FileNotFoundError(
            f"未在 localdata/video/{tier}/{slug} 找到配套视频，请先完成该机位采集"
        )
    return hit[1]
