#!/usr/bin/env python3
"""将 data28 对应 28 条记录导出为 ShelfPickSense 目录结构，标注使用 reflection 合并版。

与 export_shelf_picksense_demo 的区别：
- 目录名与 data28/manifest.json 一致（record_001 … record_028）
- annotation.json 始终来自 resolve_annotation_for_accuracy_record（合并标注），
  不使用包内单货架 annotation.json

每条记录目录：
  record_XXX/
    annotation.json   # 合并标注
    skeleton.parquet
    event_review.json

用法（项目根目录）:
  python scripts/data/export_shelf_picksense_data28_merged.py
  python scripts/data/export_shelf_picksense_data28_merged.py \\
    --out D:/work/workspace/git-repo/ShelfPickSense/data/data28-merged
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

from config_loader import resolve_app_paths, resolve_config_path
from pose_store import (
    SKELETON_FILE,
    STORAGE_V2_PARQUET,
    load_event_review_raw,
)
from review_store import event_review_read_paths
from api.accuracy_service import resolve_annotation_for_accuracy_record
from api.record_service import locate_record_by_id
from scripts.data.analyze_wrist_feature_discrimination import DEFAULT_TIER

ANNOTATION_FILE = "annotation.json"
EVENT_REVIEW_FILE = "event_review.json"
DEFAULT_MANIFEST = Path(
    r"D:\work\workspace\git-repo\ShelfPickSense\data\data28\manifest.json"
)
DEFAULT_OUT = Path(r"D:\work\workspace\git-repo\ShelfPickSense\data\data28-merged")


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


def _resolve_merged_annotation_path(locator, *, tier: str) -> Path | None:
    """始终使用 reflection 合并标注（与 export_rule_baseline_frames 一致）。"""
    paths = resolve_app_paths()
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
    entry: dict[str, Any],
    dest_dir: Path,
    *,
    tier: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    record_id = str(entry.get("source_record_id") or "").strip()
    folder = str(entry.get("folder") or "").strip()
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

    skel_src = locator.path / SKELETON_FILE
    if not skel_src.is_file():
        return {"folder": folder, "record_id": record_id, "status": "error", "error": "缺少 skeleton.parquet"}

    ann_src = _resolve_merged_annotation_path(locator, tier=tier)
    if not ann_src:
        return {"folder": folder, "record_id": record_id, "status": "error", "error": "缺少合并标注 JSON"}

    review_src = _resolve_event_review_path(locator)
    review_raw = load_event_review_raw(locator) if not review_src else None
    if not review_src and not (review_raw.get("verified_true") or review_raw.get("status")):
        return {"folder": folder, "record_id": record_id, "status": "error", "error": "缺少 event_review.json"}

    if dry_run:
        return {
            "folder": folder,
            "record_id": record_id,
            "status": "dry_run",
            "dest": str(dest_dir),
            "annotation_source": str(ann_src),
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
        "folder": folder,
        "record_id": record_id,
        "status": "ok",
        "dest": str(dest_dir),
        "clip": entry.get("clip"),
        "camera_slug": entry.get("camera_slug"),
        "skeleton_bytes": (dest_dir / SKELETON_FILE).stat().st_size,
        "annotation_source": str(ann_src),
        "annotation_merged": True,
        "event_review_source": str(review_src) if review_src else "memory",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="导出 data28 记录（合并标注）")
    parser.add_argument("--tier", default=DEFAULT_TIER)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="data28 manifest.json")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出根目录（其下 Train/record_XXX）")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--clean", action="store_true", help="导出前清空 Train/record_*")
    args = parser.parse_args()

    resolve_config_path(None)
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
        row = _export_one_record(entry, dest, tier=args.tier, dry_run=args.dry_run)
        rows.append(row)
        status = row.get("status")
        if status == "ok":
            print(f"{folder}: {entry.get('clip')} ← {Path(row.get('annotation_source', '')).name}")
        elif status == "dry_run":
            print(f"{folder}: {row.get('record_id')} ← {row.get('annotation_source')}")
        else:
            print(f"FAIL {folder}: {row.get('error')}", file=sys.stderr)

    failed = [r for r in rows if r.get("status") == "error"]
    if failed:
        return 1

    if not args.dry_run:
        out_root = Path(args.out)
        out_root.mkdir(parents=True, exist_ok=True)
        manifest = {
            "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "source_project": "visual-dps-datacollect",
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
        (out_root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        readme = """# data28 导出（合并标注）

28 条记录，目录结构与 data28 一致；`annotation.json` 为 reflection 合并标注
（与 visual-dps `export_rule_baseline_frames` / 现场碰撞重算一致）。

```
Train/record_XXX/
  annotation.json   # 合并标注
  skeleton.parquet
  event_review.json
```

ShelfPickSense infer-collision 示例：

```bash
python main.py infer-collision --record-dir data/data28-merged/Train/record_001 --output outputs/out.jsonl --pose-frame-interval 2
```
"""
        (out_root / "README.md").write_text(readme, encoding="utf-8")
        print(f"\n导出完成: {train_root} ({len(rows)} 条)")
        print(f"清单: {out_root / 'manifest.json'}")
    else:
        print(f"\n干跑: 将导出 {len(rows)} 条 → {train_root}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
