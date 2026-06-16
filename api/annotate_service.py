"""标注页：按模型层 + 机位解析内置视频与 reflection 标注。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from annotation_store import load_annotation_json
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


@dataclass
class AnnotationListItem:
    annotation_id: str
    json_file: str
    has_file: bool
    box_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "annotation_id": self.annotation_id,
            "json_file": self.json_file,
            "has_file": self.has_file,
            "box_count": self.box_count,
        }


def list_annotations_for_camera(
    paths: AppPaths,
    reflection: Any,
    camera_label: str,
) -> list[AnnotationListItem]:
    ann_ids = reflection.annotations_for_camera(camera_label)
    items: list[AnnotationListItem] = []
    for aid in ann_ids:
        path = annotation_json_path(aid, paths.annotation_dir)
        box_count = 0
        if path.is_file():
            data = load_annotation_json(path.stem, annotation_dir=paths.annotation_dir)
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
                has_file=path.is_file(),
                box_count=box_count,
            )
        )
    return items


def build_annotate_context(
    paths: AppPaths,
    reflection: Any,
    *,
    pose_tier: str,
    camera_label: str,
) -> dict[str, Any]:
    tier = normalize_pose_tier(pose_tier)
    label = str(camera_label or "").strip()
    if not label:
        raise ValueError("请填写机位标识")
    if not reflection.has_camera(label):
        raise ValueError(f"机位 {label!r} 不在 reflection.json 中")

    annotations = list_annotations_for_camera(paths, reflection, label)
    video_hit = resolve_camera_video_bucket(paths, label, pose_tier=tier)
    camera_slug = video_hit[0] if video_hit else camera_storage_slug(label)
    video_file = video_hit[1].name if video_hit else ""

    return {
        "pose_tier": tier,
        "camera_label": label,
        "camera_slug": camera_slug,
        "annotations": [a.to_dict() for a in annotations],
        "has_video": bool(video_hit),
        "video_file": video_file,
        "video_dir": f"localdata/video/{tier}/{camera_slug}",
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
