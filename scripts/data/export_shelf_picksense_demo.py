#!/usr/bin/env python3
"""将优质 28 条记录导出为 ShelfPickSense data/demo 格式。

每条记录目录：
  record_XXX/
    annotation.json
    skeleton.parquet
    event_review.json

用法（项目根目录）:
  python scripts/data/export_shelf_picksense_demo.py
  python scripts/data/export_shelf_picksense_demo.py \\
    --out D:/work/workspace/git-repo/ShelfPickSense/data/demo
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
from scripts.data.analyze_wrist_feature_discrimination import (
    DEFAULT_CAMERAS,
    DEFAULT_REVIEW_STATUS,
    DEFAULT_TAGS,
    DEFAULT_TIER,
    _collect_record_ids,
    _parse_csv_list,
    _parse_tags,
)

ANNOTATION_FILE = "annotation.json"
EVENT_REVIEW_FILE = "event_review.json"
DEFAULT_OUT = Path(r"D:\work\workspace\git-repo\ShelfPickSense\data\demo")


def _resolve_annotation_path(locator, *, tier: str) -> Path | None:
    paths = resolve_app_paths()
    pkg_ann = locator.path / ANNOTATION_FILE
    if pkg_ann.is_file():
        return pkg_ann
    ann = resolve_annotation_for_accuracy_record(paths, locator, pose_tier=tier)
    if ann and ann.is_file():
        return ann
    return None


def _resolve_event_review_path(locator) -> Path | None:
    for p in event_review_read_paths(locator):
        if p.is_file():
            return p
    legacy = locator.path / EVENT_REVIEW_FILE
    if legacy.is_file():
        return legacy
    return None


def _export_one_record(
    record_id: str,
    dest_dir: Path,
    *,
    tier: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    locator = locate_record_by_id(record_id)
    if not locator:
        return {"record_id": record_id, "status": "error", "error": "记录不存在"}

    if locator.storage != STORAGE_V2_PARQUET:
        return {"record_id": record_id, "status": "error", "error": f"不支持的存储: {locator.storage}"}

    skel_src = locator.path / SKELETON_FILE
    if not skel_src.is_file():
        return {"record_id": record_id, "status": "error", "error": "缺少 skeleton.parquet"}

    ann_src = _resolve_annotation_path(locator, tier=tier)
    if not ann_src:
        return {"record_id": record_id, "status": "error", "error": "缺少 annotation.json"}

    review_src = _resolve_event_review_path(locator)
    review_raw = load_event_review_raw(locator) if not review_src else None
    if not review_src and not (review_raw.get("verified_true") or review_raw.get("status")):
        return {"record_id": record_id, "status": "error", "error": "缺少 event_review.json"}

    _, slug, _ = parse_record_path_segments(record_id)
    clip = locator.path.name

    if dry_run:
        return {
            "record_id": record_id,
            "status": "dry_run",
            "dest": str(dest_dir),
            "clip": clip,
            "camera_slug": slug,
        }

    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(skel_src, dest_dir / SKELETON_FILE)
    shutil.copy2(ann_src, dest_dir / ANNOTATION_FILE)
    if review_src:
        shutil.copy2(review_src, dest_dir / EVENT_REVIEW_FILE)
    else:
        (dest_dir / EVENT_REVIEW_FILE).write_text(
            json.dumps(review_raw, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return {
        "record_id": record_id,
        "status": "ok",
        "dest": str(dest_dir),
        "clip": clip,
        "camera_slug": slug,
        "skeleton_bytes": (dest_dir / SKELETON_FILE).stat().st_size,
        "annotation_source": str(ann_src),
        "event_review_source": str(review_src) if review_src else "memory",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="导出 ShelfPickSense demo 数据")
    parser.add_argument("--tier", default=DEFAULT_TIER)
    parser.add_argument("--tags", default=",".join(DEFAULT_TAGS))
    parser.add_argument("--cameras", default=",".join(DEFAULT_CAMERAS))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--clean", action="store_true", help="导出前清空目标目录下 record_*")
    args = parser.parse_args()

    resolve_config_path(None)
    tags = _parse_tags(args.tags)
    cameras = _parse_csv_list(args.cameras)
    record_ids = _collect_record_ids(
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
        dest = out_root / f"record_{i:03d}"
        row = _export_one_record(rid, dest, tier=args.tier, dry_run=args.dry_run)
        rows.append(row)
        status = row.get("status")
        if status == "ok":
            print(f"record_{i:03d}: {row.get('clip')} ({row.get('camera_slug')})")
        elif status == "dry_run":
            print(f"record_{i:03d}: {rid}")
        else:
            print(f"FAIL {rid}: {row.get('error')}", file=sys.stderr)

    failed = [r for r in rows if r.get("status") == "error"]
    if failed:
        return 1

    if not args.dry_run:
        out_root.mkdir(parents=True, exist_ok=True)
        manifest = {
            "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "source_project": "visual-dps-datacollect",
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
        (out_root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        readme = """# demo 数据（来自 visual-dps-datacollect）

28 条优质样本：rtmpose-m · 单人+无遮挡 · 已复核 · 有标真。

```
record_XXX/
  annotation.json
  skeleton.parquet
  event_review.json
```

索引见 `manifest.json`（含源 `record_id` / clip / 机位 slug）。

ShelfPickSense 用法：

```bash
uv run python -m analysis.cli train --data-dir data/demo
```
"""
        (out_root / "README.md").write_text(readme, encoding="utf-8")
        print(f"\n导出完成: {out_root} ({len(rows)} 条)")
        print(f"清单: {out_root / 'manifest.json'}")
    else:
        print(f"\n干跑: 将导出 {len(rows)} 条 → {out_root}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
