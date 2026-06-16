#!/usr/bin/env python3
"""合并另一台机器（或导出目录）上的 RTMPose 采集数据到本机 localdata。

支持：
  - rtmpose-t / rtmpose-s / rtmpose-m 模型层下的采集记录（Parquet 包 + meta）
  - 配套视频 localdata/video/{tier}/
  - 全局标注 localdata/json/annotations/（含 reflection 编号源文件）
  - 记录包内 annotation.json（回放用副本）
  - 人工事件复核 event_review.json（可合并 verified_true）

用法:
  # 预览（不写入）；结束时输出「本地文件 / 导入文件」冲突对照表
  python scripts/data/merge_pose_tier_data.py --source /path/to/other-machine/export --dry-run

  # 正式合并：冲突记录跳过（保留本地包/视频），导入侧人工复核 event_review 仍并集合并
  python scripts/data/merge_pose_tier_data.py --source /path/to/export --tier rtmpose-t

  # 源机位目录与目标冲突时，用源覆盖
  python scripts/data/merge_pose_tier_data.py --source /path/to/export --on-conflict overwrite

  # 仅合并采集记录，不合并 annotations/
  python scripts/data/merge_pose_tier_data.py --source /path/to/export --no-merge-annotations

源目录可以是：
  - 另一台机器的项目根（含 localdata/json、localdata/video）
  - 仅含 localdata 的导出目录
  - 直接指向 localdata/json/rtmpose-t 的 tier 目录
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from annotation_store import allocate_annotation_stem, annotation_file_exists
from config_loader import is_pose_model_tier, resolve_app_paths
from pose_store import (
    EVENT_REVIEW_FILE,
    MANIFEST_FILE,
    REVIEW_STATUS_COMPLETED,
    REVIEW_STATUS_IN_PROGRESS,
    REVIEW_STATUS_NO_COLLISION,
    REVIEW_STATUS_TERMINAL,
    event_signature,
    normalize_review_entry,
)

SKIP_JSON_TOP = frozenset({"annotations", "archive"})


@dataclass
class MergeStats:
    records_copy: int = 0
    records_skip: int = 0
    records_overwrite: int = 0
    records_review_merge: int = 0
    meta_copy: int = 0
    video_copy: int = 0
    video_skip: int = 0
    annotations_copy: int = 0
    annotations_skip: int = 0
    annotations_allocate: int = 0
    batch_manifest_copy: int = 0
    camera_dirs: int = 0
    conflicts: list[str] = field(default_factory=list)
    # (本地路径, 导入路径)
    conflict_pairs: list[tuple[str, str]] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "records_copy": self.records_copy,
            "records_skip": self.records_skip,
            "records_overwrite": self.records_overwrite,
            "records_review_merge": self.records_review_merge,
            "meta_copy": self.meta_copy,
            "video_copy": self.video_copy,
            "video_skip": self.video_skip,
            "annotations_copy": self.annotations_copy,
            "annotations_skip": self.annotations_skip,
            "annotations_allocate": self.annotations_allocate,
            "batch_manifest_copy": self.batch_manifest_copy,
            "camera_dirs": self.camera_dirs,
            "conflicts": list(self.conflicts),
            "conflict_pairs": [{"local": a, "import": b} for a, b in self.conflict_pairs],
        }


def _log(stats: MergeStats, msg: str, *, dry_run: bool) -> None:
    prefix = "[dry-run] " if dry_run else ""
    line = f"{prefix}{msg}"
    print(line)
    stats.actions.append(line)


def _add_conflict(
    stats: MergeStats,
    local: Path,
    imported: Path,
    *,
    record_id: str | None = None,
) -> None:
    local_s = str(local.resolve())
    import_s = str(imported.resolve())
    pair = (local_s, import_s)
    if pair in stats.conflict_pairs:
        return
    stats.conflict_pairs.append(pair)
    if record_id:
        stats.conflicts.append(record_id)


def print_conflict_table(pairs: list[tuple[str, str]]) -> None:
    if not pairs:
        return
    col_local, col_import = "本地文件", "导入文件"
    w1 = max(len(col_local), *(len(p[0]) for p in pairs))
    w2 = max(len(col_import), *(len(p[1]) for p in pairs))
    gap = "     "
    print(f"\n冲突文件（共 {len(pairs)} 条）")
    print(f"{col_local:<{w1}}{gap}{col_import}")
    for local, imported in pairs:
        print(f"{local:<{w1}}{gap}{imported}")


def _file_newer(src: Path, dest: Path) -> bool:
    if not dest.is_file():
        return True
    try:
        return src.stat().st_mtime > dest.stat().st_mtime
    except OSError:
        return False


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(path: Path, data: dict[str, Any], *, dry_run: bool) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve_source_roots(source: Path, tier: str) -> tuple[Path, Path, Path]:
    """解析源目录 → (json_tier_dir, video_tier_dir, annotations_dir)。"""
    source = source.resolve()
    tier = str(tier or "rtmpose-t").strip().lower()

    def _pick_json_tier(base_json: Path) -> Path | None:
        direct = base_json / tier
        if direct.is_dir():
            return direct
        if is_pose_model_tier(base_json.name) and base_json.name == tier:
            return base_json
        # 旧版扁平：base_json 下直接是机位目录
        if base_json.is_dir() and any(
            p.is_dir() and (p / "manifest.json").is_file() for p in base_json.iterdir()
        ):
            return None
        if base_json.is_dir():
            has_camera = any(
                p.is_dir() and not p.name.startswith(".") and p.name not in SKIP_JSON_TOP
                for p in base_json.iterdir()
            )
            if has_camera and not is_pose_model_tier(base_json.name):
                return None  # 扁平机位根
        return None

    candidates: list[Path] = [source]
    if (source / "localdata").is_dir():
        candidates.append(source / "localdata")
    if (source / "json").is_dir():
        candidates.append(source / "json")

    src_json_tier: Path | None = None
    src_video_tier: Path | None = None
    src_ann: Path | None = None
    flat_json_root: Path | None = None

    for base in candidates:
        json_root = base / "json" if (base / "json").is_dir() else base
        if not json_root.is_dir():
            continue
        ann = json_root / "annotations"
        if ann.is_dir():
            src_ann = ann
        picked = _pick_json_tier(json_root)
        if picked is not None:
            src_json_tier = picked
        elif json_root.is_dir() and not is_pose_model_tier(json_root.name):
            # 扁平：json_root/1-2-1/...
            flat_json_root = json_root
        video_root = base / "video" if (base / "video").is_dir() else None
        if video_root and video_root.is_dir():
            vt = video_root / tier
            if vt.is_dir():
                src_video_tier = vt
            elif not is_pose_model_tier(video_root.name):
                flat_v = video_root
                if flat_v.is_dir():
                    src_video_tier = flat_v  # 扁平 video 根，后续按机位对齐

    if src_json_tier is None and flat_json_root is None:
        raise FileNotFoundError(
            f"无法在 {source} 找到 json 数据（期望 localdata/json/{tier} 或扁平机位目录）"
        )

    if src_ann is None:
        for base in candidates:
            ann = base / "json" / "annotations"
            if ann.is_dir():
                src_ann = ann
                break
        if src_ann is None:
            src_ann = resolve_app_paths().annotation_dir  # 允许无源标注

    return (
        src_json_tier or flat_json_root,  # type: ignore[return-value]
        src_video_tier or Path("/nonexistent"),
        src_ann,
    )


def _is_record_package(path: Path) -> bool:
    return path.is_dir() and (path / MANIFEST_FILE).is_file()


def _iter_source_records(json_root: Path, tier: str) -> list[tuple[str, Path]]:
    """返回 [(rel_posix, package_path), ...]，rel 形如 tier/camera/record 或 camera/record。"""
    out: list[tuple[str, Path]] = []
    json_root = json_root.resolve()
    use_tier_prefix = is_pose_model_tier(json_root.name)

    def _scan_camera(cam_dir: Path, prefix: str) -> None:
        for child in sorted(cam_dir.iterdir(), key=lambda p: p.name):
            if child.name.startswith("."):
                continue
            if child.name.endswith(".meta.json") or child.name.startswith("_batch_"):
                continue
            if _is_record_package(child):
                rel = f"{prefix}/{child.name}" if prefix else child.name
                out.append((rel, child))

    if use_tier_prefix:
        for cam_dir in sorted(json_root.iterdir(), key=lambda p: p.name):
            if not cam_dir.is_dir() or cam_dir.name.startswith("."):
                continue
            _scan_camera(cam_dir, f"{json_root.name}/{cam_dir.name}")
    else:
        for cam_dir in sorted(json_root.iterdir(), key=lambda p: p.name):
            if cam_dir.name in SKIP_JSON_TOP or cam_dir.name.startswith("."):
                continue
            if not cam_dir.is_dir():
                continue
            _scan_camera(cam_dir, f"{tier}/{cam_dir.name}")

    return out


def _dest_record_rel(rel: str, tier: str, *, src_is_flat: bool) -> str:
    """统一目标相对路径：tier/camera/record_name。"""
    parts = [p for p in rel.replace("\\", "/").split("/") if p]
    if len(parts) >= 3 and is_pose_model_tier(parts[0]):
        return "/".join(parts)
    if len(parts) >= 2:
        if src_is_flat:
            return f"{tier}/{parts[0]}/{parts[1]}"
        return "/".join(parts)
    return f"{tier}/{rel}"


def _merge_event_review(
    src_review: dict[str, Any],
    dest_review: dict[str, Any],
    *,
    record_id: str,
) -> dict[str, Any]:
    """合并两份 event_review（verified_true 并集，状态取更完整的一方）。"""
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in list(dest_review.get("verified_true") or []) + list(src_review.get("verified_true") or []):
        norm = normalize_review_entry(raw if isinstance(raw, dict) else {})
        if not norm:
            continue
        sig = event_signature(norm["event_type"], norm["frame_idx"], norm["box_tokens"])
        if sig in seen:
            continue
        seen.add(sig)
        merged.append(norm)
    merged.sort(
        key=lambda e: (
            int(e.get("frame_idx") or 0),
            str(e.get("event_type") or ""),
            ",".join(e.get("box_tokens") or []),
        )
    )

    def _rank(status: str) -> int:
        s = str(status or "").strip().lower()
        if s == REVIEW_STATUS_COMPLETED:
            return 4
        if s == REVIEW_STATUS_NO_COLLISION:
            return 3
        if s == REVIEW_STATUS_IN_PROGRESS:
            return 2
        return 1

    dest_st = str(dest_review.get("status") or "").strip().lower()
    src_st = str(src_review.get("status") or "").strip().lower()
    status = dest_st
    if _rank(src_st) > _rank(dest_st):
        status = src_st
    elif _rank(src_st) == _rank(dest_st) and len(merged) > len(dest_review.get("verified_true") or []):
        status = src_st or dest_st

    if merged and status not in REVIEW_STATUS_TERMINAL:
        status = REVIEW_STATUS_IN_PROGRESS

    event_total = dest_review.get("event_total")
    src_total = src_review.get("event_total")
    if src_total is not None:
        try:
            st = int(src_total)
            dt = int(event_total) if event_total is not None else 0
            event_total = max(st, dt)
        except (TypeError, ValueError):
            pass

    payload: dict[str, Any] = {
        "schema": dest_review.get("schema") or src_review.get("schema") or 1,
        "record_id": record_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "verified_true": merged,
    }
    if event_total is not None:
        payload["event_total"] = event_total
    if status:
        payload["status"] = status
    if status == REVIEW_STATUS_COMPLETED:
        payload["completed_at"] = (
            dest_review.get("completed_at")
            or src_review.get("completed_at")
            or datetime.now(timezone.utc).isoformat()
        )
    return payload


def _review_has_import_data(review: dict[str, Any] | None) -> bool:
    """导入侧 event_review 是否含有效人工复核。"""
    if not review:
        return False
    verified = review.get("verified_true") or []
    if isinstance(verified, list) and verified:
        return True
    st = str(review.get("status") or "").strip().lower()
    if st in (REVIEW_STATUS_COMPLETED, REVIEW_STATUS_IN_PROGRESS, REVIEW_STATUS_NO_COLLISION):
        return True
    return bool(str(review.get("updated_at") or "").strip() or str(review.get("completed_at") or "").strip())


def _review_merge_changed(dest_rev: dict[str, Any], merged: dict[str, Any]) -> bool:
    return (
        merged.get("verified_true") != dest_rev.get("verified_true")
        or merged.get("status") != dest_rev.get("status")
        or merged.get("event_total") != dest_rev.get("event_total")
    )


def _maybe_merge_review_on_record_skip(
    *,
    paths,
    dest_pkg: Path,
    src_pkg: Path,
    dest_rel: str,
    review_mode: str,
    stats: MergeStats,
    dry_run: bool,
) -> None:
    """记录包冲突且保留本地时，将导入侧人工复核并入 review_dir。"""
    if review_mode == "skip":
        return
    from pose_store import locate_record, load_event_review_raw

    src_rev = _load_json(src_pkg / EVENT_REVIEW_FILE)
    if not _review_has_import_data(src_rev):
        return
    locator = locate_record(paths.json_dir, dest_rel)
    if not locator:
        return
    dest_rev = load_event_review_raw(locator) or {}
    from review_store import event_review_write_path, merge_event_reviews, resolve_review_context

    review_key, _, _ = resolve_review_context(locator, paths)
    if review_mode == "overwrite":
        merged = dict(src_rev)
        merged["review_key"] = review_key
        merged["record_id"] = dest_rel
    else:
        merged = merge_event_reviews(src_rev, dest_rev, review_key=review_key, record_id=dest_rel)
    if not _review_merge_changed(dest_rev, merged):
        return
    stats.records_review_merge += 1
    n_dest = len(dest_rev.get("verified_true") or [])
    n_src = len(src_rev.get("verified_true") or [])
    n_out = len(merged.get("verified_true") or [])
    if dest_rev:
        msg = (
            f"合并复核（保留本地记录）: {dest_rel}（verified {n_dest} + {n_src} → {n_out}，"
            f"status={merged.get('status') or '-'}）"
        )
    else:
        msg = (
            f"写入导入复核（保留本地记录）: {dest_rel}（verified {n_src}，"
            f"status={merged.get('status') or '-'}）"
        )
    _log(stats, msg, dry_run=dry_run)
    _write_json(event_review_write_path(locator, paths), merged, dry_run=dry_run)


def _import_src_package_review(
    *,
    paths,
    dest_rel: str,
    src_pkg: Path,
    review_mode: str,
    stats: MergeStats,
    dry_run: bool,
) -> None:
    """将源记录包内复核导入 review_dir（新增/覆盖记录后调用）。"""
    from pose_store import locate_record, load_event_review_raw
    from review_store import event_review_write_path, merge_event_reviews, resolve_review_context

    src_rev = _load_json(src_pkg / EVENT_REVIEW_FILE)
    if not _review_has_import_data(src_rev):
        return
    locator = locate_record(paths.json_dir, dest_rel)
    if not locator:
        return
    dest_rev = load_event_review_raw(locator) or {}
    review_key, _, _ = resolve_review_context(locator, paths)
    if review_mode == "overwrite" or not _review_has_import_data(dest_rev):
        merged = dict(src_rev)
        merged["review_key"] = review_key
        merged["record_id"] = dest_rel
    else:
        merged = merge_event_reviews(src_rev, dest_rev, review_key=review_key, record_id=dest_rel)
    if dest_rev and not _review_merge_changed(dest_rev, merged):
        return
    stats.records_review_merge += 1
    _log(stats, f"导入复核到 review_dir: {dest_rel}", dry_run=dry_run)
    _write_json(event_review_write_path(locator, paths), merged, dry_run=dry_run)


def _copy_file(src: Path, dest: Path, *, dry_run: bool) -> None:
    if dry_run:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)


def _copy_tree(src: Path, dest: Path, *, dry_run: bool) -> None:
    if dry_run:
        return
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)


def _should_take_src(on_conflict: str, src: Path, dest: Path) -> bool:
    if on_conflict == "overwrite":
        return True
    if on_conflict == "newer":
        try:
            return src.stat().st_mtime > dest.stat().st_mtime
        except OSError:
            return False
    return False


def merge_annotations(
    src_ann: Path,
    dest_ann: Path,
    *,
    policy: str,
    stats: MergeStats,
    dry_run: bool,
) -> None:
    if not src_ann.is_dir():
        return
    dest_ann.mkdir(parents=True, exist_ok=True)
    for src_file in sorted(src_ann.glob("*.json")):
        name = src_file.name
        dest_file = dest_ann / name
        if dest_file.is_file():
            if policy == "skip":
                stats.annotations_skip += 1
                _add_conflict(stats, dest_file, src_file)
                _log(stats, f"跳过标注（已存在）: annotations/{name}", dry_run=dry_run)
                continue
            if policy == "newer" and not _file_newer(src_file, dest_file):
                stats.annotations_skip += 1
                _add_conflict(stats, dest_file, src_file)
                _log(stats, f"跳过标注（目标较新）: annotations/{name}", dry_run=dry_run)
                continue
            if policy == "allocate":
                stem = src_file.stem
                new_stem = allocate_annotation_stem(dest_ann, stem)
                dest_file = dest_ann / f"{new_stem}.json"
                stats.annotations_allocate += 1
                _log(
                    stats,
                    f"标注另存: annotations/{name} → annotations/{dest_file.name}",
                    dry_run=dry_run,
                )
                _copy_file(src_file, dest_file, dry_run=dry_run)
                continue
            if policy == "overwrite":
                _log(stats, f"覆盖标注: annotations/{name}", dry_run=dry_run)
                _copy_file(src_file, dest_file, dry_run=dry_run)
                stats.annotations_copy += 1
                continue
            stats.annotations_skip += 1
            continue
        _log(stats, f"新增标注: annotations/{name}", dry_run=dry_run)
        _copy_file(src_file, dest_file, dry_run=dry_run)
        stats.annotations_copy += 1


def merge_records(
    *,
    paths,
    src_json_root: Path,
    dest_json_tier: Path,
    dest_video_tier: Path,
    src_video_root: Path,
    tier: str,
    on_conflict: str,
    review_mode: str,
    include_videos: bool,
    stats: MergeStats,
    dry_run: bool,
) -> None:
    src_is_flat = not is_pose_model_tier(src_json_root.name)
    records = _iter_source_records(src_json_root, tier)
    seen_cameras: set[str] = set()

    for rel, src_pkg in records:
        dest_rel = _dest_record_rel(rel, tier, src_is_flat=src_is_flat)
        parts = dest_rel.split("/")
        if len(parts) < 3:
            continue
        _tier, camera_slug, record_name = parts[0], parts[1], parts[2]
        seen_cameras.add(camera_slug)

        dest_pkg = dest_json_tier / camera_slug / record_name
        dest_meta = dest_json_tier / camera_slug / f"{record_name}.meta.json"
        src_meta = src_pkg.parent / f"{record_name}.meta.json"

        pkg_exists = dest_pkg.is_dir() and (dest_pkg / MANIFEST_FILE).is_file()

        if pkg_exists:
            if on_conflict == "skip":
                stats.records_skip += 1
                _add_conflict(stats, dest_pkg, src_pkg, record_id=dest_rel)
                _log(stats, f"跳过记录（已存在，保留本地）: {dest_rel}", dry_run=dry_run)
                _maybe_merge_review_on_record_skip(
                    paths=paths,
                    dest_pkg=dest_pkg,
                    src_pkg=src_pkg,
                    dest_rel=dest_rel,
                    review_mode=review_mode,
                    stats=stats,
                    dry_run=dry_run,
                )
                continue
            if not _should_take_src(on_conflict, src_pkg / MANIFEST_FILE, dest_pkg / MANIFEST_FILE):
                stats.records_skip += 1
                _add_conflict(stats, dest_pkg, src_pkg, record_id=dest_rel)
                _log(stats, f"跳过记录（冲突策略，保留本地）: {dest_rel}", dry_run=dry_run)
                _maybe_merge_review_on_record_skip(
                    paths=paths,
                    dest_pkg=dest_pkg,
                    src_pkg=src_pkg,
                    dest_rel=dest_rel,
                    review_mode=review_mode,
                    stats=stats,
                    dry_run=dry_run,
                )
                continue
            stats.records_overwrite += 1
            _log(stats, f"覆盖记录: {dest_rel}", dry_run=dry_run)
            _copy_tree(src_pkg, dest_pkg, dry_run=dry_run)
        else:
            stats.records_copy += 1
            _log(stats, f"新增记录: {dest_rel}", dry_run=dry_run)
            _copy_tree(src_pkg, dest_pkg, dry_run=dry_run)

        # 同步 sidecar meta
        if src_meta.is_file():
            if not dest_meta.is_file() or _should_take_src(on_conflict, src_meta, dest_meta) or not pkg_exists:
                meta_data = _load_json(src_meta)
                if meta_data:
                    meta_data["record_id"] = dest_rel
                    meta_data["pose_file"] = f"{dest_rel}/manifest.json"
                    meta_data["pose_model_tier"] = _tier
                    meta_data["camera_slug"] = camera_slug
                    if meta_data.get("video_url"):
                        meta_data["video_url"] = f"/api/records/{dest_rel}/video"
                    _write_json(dest_meta, meta_data, dry_run=dry_run)
                    stats.meta_copy += 1
                    _log(stats, f"同步 meta: {dest_rel}.meta.json", dry_run=dry_run)

        # 复核写入 review_dir，不再复制到记录包
        if (src_pkg / EVENT_REVIEW_FILE).is_file():
            _import_src_package_review(
                paths=paths,
                dest_rel=dest_rel,
                src_pkg=src_pkg,
                review_mode=review_mode,
                stats=stats,
                dry_run=dry_run,
            )

    # 配套视频：按机位目录合并（避免每条记录重复扫描）
    if include_videos and src_video_root.is_dir():
        for camera_slug in sorted(seen_cameras):
            src_cam_video = src_video_root / camera_slug
            if not src_cam_video.is_dir():
                continue
            dest_cam_video = dest_video_tier / camera_slug
            for src_vid in sorted(src_cam_video.iterdir(), key=lambda p: p.name):
                if not src_vid.is_file() or src_vid.name.startswith("."):
                    continue
                dest_vid = dest_cam_video / src_vid.name
                if dest_vid.is_file():
                    if on_conflict == "skip":
                        stats.video_skip += 1
                        _add_conflict(stats, dest_vid, src_vid)
                        continue
                    if on_conflict == "newer" and not _file_newer(src_vid, dest_vid):
                        stats.video_skip += 1
                        _add_conflict(stats, dest_vid, src_vid)
                        continue
                _log(stats, f"视频: {tier}/{camera_slug}/{src_vid.name}", dry_run=dry_run)
                _copy_file(src_vid, dest_vid, dry_run=dry_run)
                stats.video_copy += 1

    # 批处理清单 _batch_*.json
    cam_dirs = [src_json_root] if src_is_flat else [src_json_root]
    if not src_is_flat:
        cam_dirs = [p for p in src_json_root.iterdir() if p.is_dir()]
    else:
        cam_dirs = [
            p
            for p in src_json_root.iterdir()
            if p.is_dir() and p.name not in SKIP_JSON_TOP and not p.name.startswith(".")
        ]

    for cam_dir in cam_dirs:
        cam_name = cam_dir.name
        dest_cam = dest_json_tier / cam_name
        for batch_file in cam_dir.glob("_batch_*.json"):
            dest_bf = dest_cam / batch_file.name
            if dest_bf.is_file() and on_conflict == "skip":
                _add_conflict(stats, dest_bf, batch_file)
                continue
            _log(stats, f"批处理清单: {tier}/{cam_name}/{batch_file.name}", dry_run=dry_run)
            _copy_file(batch_file, dest_bf, dry_run=dry_run)
            stats.batch_manifest_copy += 1

    stats.camera_dirs = len(seen_cameras)


def run_merge(
    *,
    source: Path,
    tier: str,
    dry_run: bool,
    on_conflict: str,
    review_mode: str,
    annotation_policy: str,
    merge_annotations_flag: bool,
    include_videos: bool,
    report_path: Path | None,
) -> MergeStats:
    if not is_pose_model_tier(tier):
        raise ValueError(f"无效 tier: {tier}")

    paths = resolve_app_paths()
    src_json_root, src_video_root, src_ann = resolve_source_roots(source, tier)
    dest_json_tier = paths.json_dir / tier
    dest_video_tier = paths.video_dir / tier
    dest_ann = paths.annotation_dir

    if not dry_run:
        dest_json_tier.mkdir(parents=True, exist_ok=True)
        dest_video_tier.mkdir(parents=True, exist_ok=True)

    stats = MergeStats()
    src_flat = not is_pose_model_tier(src_json_root.name)
    _log(
        stats,
        f"源 json: {src_json_root}（{'扁平' if src_flat else '分层'}）→ 目标: {dest_json_tier}",
        dry_run=dry_run,
    )
    if src_video_root.is_dir():
        _log(stats, f"源 video: {src_video_root} → 目标: {dest_video_tier}", dry_run=dry_run)
    if merge_annotations_flag and src_ann.is_dir():
        _log(stats, f"源 annotations: {src_ann} → 目标: {dest_ann}", dry_run=dry_run)

    merge_records(
        paths=paths,
        src_json_root=src_json_root,
        dest_json_tier=dest_json_tier,
        dest_video_tier=dest_video_tier,
        src_video_root=src_video_root if src_video_root.is_dir() else Path(),
        tier=tier,
        on_conflict=on_conflict,
        review_mode=review_mode,
        include_videos=include_videos,
        stats=stats,
        dry_run=dry_run,
    )

    if merge_annotations_flag:
        merge_annotations(
            src_ann,
            dest_ann,
            policy=annotation_policy,
            stats=stats,
            dry_run=dry_run,
        )

    if report_path:
        payload = {
            "dry_run": dry_run,
            "tier": tier,
            "source": str(source.resolve()),
            "stats": stats.to_dict(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        if dry_run:
            _log(stats, f"报告将写入: {report_path}", dry_run=True)
        else:
            report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"报告已写入: {report_path}")

    return stats


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="合并另一台机器的 RTMPose 采集数据与人工复核到本机")
    p.add_argument(
        "--source",
        type=Path,
        required=True,
        help="源机器项目根、localdata 导出目录、或 localdata/json/rtmpose-t",
    )
    p.add_argument("--tier", default="rtmpose-t", help="模型层 rtmpose-t / rtmpose-s / rtmpose-m")
    p.add_argument("--dry-run", action="store_true", help="仅预览合并计划，不写入")
    p.add_argument(
        "--on-conflict",
        choices=("skip", "overwrite", "newer"),
        default="skip",
        help="记录/视频/标注冲突策略（默认 skip=保留本机）",
    )
    p.add_argument(
        "--review-mode",
        choices=("merge", "skip", "overwrite"),
        default="merge",
        help="记录冲突保留本地时 event_review：merge=并集合并导入复核（默认）；overwrite=用导入覆盖",
    )
    p.add_argument(
        "--annotation-policy",
        choices=("skip", "newer", "overwrite", "allocate"),
        default="newer",
        help="annotations/ 冲突策略（默认 newer=源较新则覆盖）",
    )
    p.add_argument("--no-merge-annotations", action="store_true", help="不合并 localdata/json/annotations/")
    p.add_argument("--no-videos", action="store_true", help="不合并 localdata/video/")
    p.add_argument("--report", type=Path, default=None, help="将统计写入 JSON 报告")
    args = p.parse_args(argv)

    try:
        stats = run_merge(
            source=args.source,
            tier=str(args.tier).strip().lower(),
            dry_run=args.dry_run,
            on_conflict=args.on_conflict,
            review_mode=args.review_mode,
            annotation_policy=args.annotation_policy,
            merge_annotations_flag=not args.no_merge_annotations,
            include_videos=not args.no_videos,
            report_path=args.report,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2

    mode = "[dry-run] " if args.dry_run else ""
    print(
        f"\n{mode}汇总：新增记录 {stats.records_copy}，覆盖 {stats.records_overwrite}，"
        f"跳过 {stats.records_skip}，复核合并 {stats.records_review_merge}，"
        f"视频 {stats.video_copy}，标注新增 {stats.annotations_copy} / 跳过 {stats.annotations_skip} / "
        f"另存 {stats.annotations_allocate}，机位 {stats.camera_dirs}"
    )
    if stats.conflict_pairs:
        if args.dry_run:
            print_conflict_table(stats.conflict_pairs)
        elif args.on_conflict == "skip":
            print(f"冲突 {len(stats.conflict_pairs)} 条（已跳过，可用 --on-conflict newer|overwrite 处理）")
            print("使用 --dry-run 可查看「本地文件 / 导入文件」对照表")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
