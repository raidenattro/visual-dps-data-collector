#!/usr/bin/env python3
"""导出 ShelfPickSense 记录目录（skeleton + annotation + event_review）。

布局（--layout）:
  demo          按标签筛 28 条，record_001 顺序编号，annotation 优先包内
  data28-merged 按 data28 manifest，folder 与 data28 一致，annotation 强制合并标注

用法（项目根目录）:
  python scripts/data/export_shelf_picksense.py --layout demo
  python scripts/data/export_shelf_picksense.py --layout data28-merged --clean
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config_loader import parse_record_path_segments, resolve_app_paths, resolve_config_path
from pose_store import (
    SKELETON_FILE,
    STORAGE_V2_PARQUET,
    load_event_review_raw,
)
from review_store import event_review_read_paths
from api.accuracy_service import resolve_annotation_for_accuracy_record
from api.record_service import locate_record_by_id
from scripts.data.eval_dataset import (
    DEFAULT_CAMERAS,
    DEFAULT_REVIEW_STATUS,
    DEFAULT_TAGS,
    DEFAULT_TIER,
    collect_record_ids,
    parse_csv_list,
    parse_tags,
)

ANNOTATION_FILE = "annotation.json"
EVENT_REVIEW_FILE = "event_review.json"
DEFAULT_DATA28_MANIFEST = Path(
    r"D:\work\workspace\git-repo\ShelfPickSense\data\data28\manifest.json"
)
DEFAULT_OUT_BY_LAYOUT = {
    "demo": Path(r"D:\work\workspace\git-repo\ShelfPickSense\data\demo"),
    "data28-merged": Path(r"D:\work\workspace\git-repo\ShelfPickSense\data\data28-merged"),
}
Layout = Literal["demo", "data28-merged"]


def _load_data28_manifest(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    records = data.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError(f"manifest 无有效 records: {path}")
    out: list[dict[str, Any]] = []
    for row in records:
        if not isinstance(row, dict):
            continue
        folder = str(row.get("folder") or "").strip()
        record_id = str(row.get("source_record_id") or "").strip()
        if folder and record_id:
            out.append(row)
    if not out:
        raise ValueError(f"manifest records 为空: {path}")
    return out


def _resolve_event_review_path(locator) -> Path | None:
    for p in event_review_read_paths(locator):
        if p.is_file():
            return p
    legacy = locator.path / EVENT_REVIEW_FILE
    if legacy.is_file():
        return legacy
    return None


def _resolve_annotation_path(
    locator,
    *,
    tier: str,
    merged: bool,
) -> Path | None:
    paths = resolve_app_paths()
    if not merged:
        pkg_ann = locator.path / ANNOTATION_FILE
        if pkg_ann.is_file():
            return pkg_ann
    ann = resolve_annotation_for_accuracy_record(paths, locator, pose_tier=tier)
    if ann and ann.is_file():
        return ann
    return None


def _copy_record_files(
    locator,
    dest_dir: Path,
    *,
    ann_src: Path,
    review_src: Path | None,
    review_raw: dict[str, Any] | None,
) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(locator.path / SKELETON_FILE, dest_dir / SKELETON_FILE)
    shutil.copy2(ann_src, dest_dir / ANNOTATION_FILE)
    if review_src:
        shutil.copy2(review_src, dest_dir / EVENT_REVIEW_FILE)
    elif review_raw:
        (dest_dir / EVENT_REVIEW_FILE).write_text(
            json.dumps(review_raw, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _export_by_record_id(
    record_id: str,
    dest_dir: Path,
    *,
    tier: str,
    merged: bool,
    dry_run: bool,
    folder: str = "",
    clip_hint: str = "",
    camera_hint: str = "",
) -> dict[str, Any]:
    locator = locate_record_by_id(record_id)
    if not locator:
        return {"folder": folder, "record_id": record_id, "status": "error", "error": "记录不存在"}

    if locator.storage != STORAGE_V2_PARQUET:
        return {
            "folder": folder,
            "record_id": record_id,
            "status": "error",
            "error": f"不支持的存储: {locator.storage}",
        }

    if not (locator.path / SKELETON_FILE).is_file():
        return {"folder": folder, "record_id": record_id, "status": "error", "error": "缺少 skeleton.parquet"}

    ann_src = _resolve_annotation_path(locator, tier=tier, merged=merged)
    if not ann_src:
        err = "缺少合并标注 JSON" if merged else "缺少 annotation.json"
        return {"folder": folder, "record_id": record_id, "status": "error", "error": err}

    review_src = _resolve_event_review_path(locator)
    review_raw = load_event_review_raw(locator) if not review_src else None
    if not review_src and not (review_raw.get("verified_true") or review_raw.get("status")):
        return {"folder": folder, "record_id": record_id, "status": "error", "error": "缺少 event_review.json"}

    _, slug, _ = parse_record_path_segments(record_id)
    clip = clip_hint or locator.path.name
    camera_slug = camera_hint or slug

    if dry_run:
        return {
            "folder": folder,
            "record_id": record_id,
            "status": "dry_run",
            "dest": str(dest_dir),
            "clip": clip,
            "camera_slug": camera_slug,
            "annotation_source": str(ann_src),
            "annotation_merged": merged,
        }

    _copy_record_files(locator, dest_dir, ann_src=ann_src, review_src=review_src, review_raw=review_raw)
    return {
        "folder": folder,
        "record_id": record_id,
        "status": "ok",
        "dest": str(dest_dir),
        "clip": clip,
        "camera_slug": camera_slug,
        "skeleton_bytes": (dest_dir / SKELETON_FILE).stat().st_size,
        "annotation_source": str(ann_src),
        "annotation_merged": merged,
        "event_review_source": str(review_src) if review_src else "memory",
    }


def _export_demo(args: argparse.Namespace) -> int:
    tags = parse_tags(args.tags)
    cameras = parse_csv_list(args.cameras)
    record_ids = collect_record_ids(
        tier=args.tier,
        cameras=set(cameras),
        tags=tags,
        review_status=DEFAULT_REVIEW_STATUS,
        has_verified=True,
    )
    if not record_ids:
        print("未找到匹配记录", file=sys.stderr)
        return 1

    out_root = Path(args.out)
    if args.clean and not args.dry_run and out_root.is_dir():
        for child in out_root.glob("record_*"):
            if child.is_dir():
                shutil.rmtree(child)

    rows: list[dict[str, Any]] = []
    for i, rid in enumerate(record_ids, 1):
        folder = f"record_{i:03d}"
        dest = out_root / folder
        row = _export_by_record_id(
            rid, dest, tier=args.tier, merged=False, dry_run=args.dry_run, folder=folder
        )
        rows.append(row)
        _print_row(row, folder)

    if _any_failed(rows):
        return 1
    if args.dry_run:
        print(f"\n干跑: 将导出 {len(rows)} 条 → {out_root}")
        return 0

    out_root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "source_project": "visual-dps-datacollect",
        "layout": "demo",
        "tier": args.tier,
        "tags": tags,
        "cameras": cameras,
        "review_status": DEFAULT_REVIEW_STATUS,
        "has_verified": True,
        "record_count": len(rows),
        "records": [
            {
                "folder": f"record_{i:03d}",
                "source_record_id": r["record_id"],
                "clip": r.get("clip"),
                "camera_slug": r.get("camera_slug"),
            }
            for i, r in enumerate(rows, 1)
        ],
    }
    (out_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n导出完成: {out_root} ({len(rows)} 条)")
    return 0


def _export_data28_merged(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest)
    entries = _load_data28_manifest(manifest_path)
    train_root = Path(args.out) / "Train"

    if args.clean and not args.dry_run and train_root.is_dir():
        for child in train_root.glob("record_*"):
            if child.is_dir():
                shutil.rmtree(child)

    rows: list[dict[str, Any]] = []
    for entry in entries:
        folder = str(entry.get("folder") or "").strip()
        dest = train_root / folder
        row = _export_by_record_id(
            str(entry.get("source_record_id") or "").strip(),
            dest,
            tier=args.tier,
            merged=True,
            dry_run=args.dry_run,
            folder=folder,
            clip_hint=str(entry.get("clip") or ""),
            camera_hint=str(entry.get("camera_slug") or ""),
        )
        rows.append(row)
        _print_row(row, folder, entry.get("clip"))

    if _any_failed(rows):
        return 1
    if args.dry_run:
        print(f"\n干跑: 将导出 {len(rows)} 条 → {train_root}")
        return 0

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "source_project": "visual-dps-datacollect",
        "layout": "data28-merged",
        "annotation_mode": "reflection_merged",
        "source_data28_manifest": str(manifest_path.resolve()),
        "tier": args.tier,
        "record_count": len(rows),
        "records": [
            {
                "folder": r.get("folder"),
                "source_record_id": r.get("record_id"),
                "clip": r.get("clip"),
                "camera_slug": r.get("camera_slug"),
                "annotation_source": r.get("annotation_source"),
                "annotation_merged": True,
            }
            for r in rows
        ],
    }
    (out_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n导出完成: {train_root} ({len(rows)} 条)")
    return 0


def _any_failed(rows: list[dict[str, Any]]) -> bool:
    return any(r.get("status") == "error" for r in rows)


def _print_row(row: dict[str, Any], folder: str, clip: str | None = None) -> None:
    status = row.get("status")
    if status == "ok":
        clip_name = clip or row.get("clip")
        ann = Path(str(row.get("annotation_source") or "")).name
        print(f"{folder}: {clip_name} ← {ann}")
    elif status == "dry_run":
        print(f"{folder}: {row.get('record_id')} ← {row.get('annotation_source')}")
    else:
        print(f"FAIL {folder}: {row.get('error')}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="导出 ShelfPickSense 记录目录")
    parser.add_argument(
        "--layout",
        choices=("demo", "data28-merged"),
        default="data28-merged",
        help="demo=顺序编号+包内标注；data28-merged=manifest 映射+合并标注",
    )
    parser.add_argument("--tier", default=DEFAULT_TIER)
    parser.add_argument("--tags", default=",".join(DEFAULT_TAGS), help="layout=demo 时生效")
    parser.add_argument("--cameras", default=",".join(DEFAULT_CAMERAS), help="layout=demo 时生效")
    parser.add_argument("--manifest", default=str(DEFAULT_DATA28_MANIFEST), help="layout=data28-merged")
    parser.add_argument("--out", default="", help="输出根目录（默认随 layout）")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--clean", action="store_true", help="导出前清空 record_*")
    args = parser.parse_args()

    if not args.out:
        args.out = str(DEFAULT_OUT_BY_LAYOUT[args.layout])

    resolve_config_path(None)
    if args.layout == "demo":
        return _export_demo(args)
    return _export_data28_merged(args)


if __name__ == "__main__":
    raise SystemExit(main())
