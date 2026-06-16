#!/usr/bin/env python3
"""将记录包内 event_review.json 迁移到 localdata/review（按 source_video + 片段时间窗）。

用法:
  python scripts/data/migrate_event_review_to_review_dir.py --dry-run
  python scripts/data/migrate_event_review_to_review_dir.py
  python scripts/data/migrate_event_review_to_review_dir.py --tier rtmpose-t
  python scripts/data/migrate_event_review_to_review_dir.py --remove-legacy

说明:
  - 读取各记录包内旧 event_review.json（或 review_dir 已有文件）
  - 按 review_key 合并后写入 localdata/review/{机位}/{source}__{start}__{end}/
  - 默认保留包内旧文件；--remove-legacy 在成功写入后删除
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config_loader import resolve_app_paths
from pose_store import iter_active_records
from record_index_store import refresh_record_summary
from review_store import (
    legacy_package_event_review_path,
    merge_event_reviews,
    resolve_review_context,
)


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _has_review_data(review: dict) -> bool:
    if not review:
        return False
    if review.get("verified_true"):
        return True
    st = str(review.get("status") or "").strip().lower()
    if st in ("completed", "in_progress", "no_collision"):
        return True
    return bool(str(review.get("updated_at") or "").strip())


def migrate_one(
    locator,
    paths,
    *,
    dry_run: bool,
    remove_legacy: bool,
) -> str:
    legacy = legacy_package_event_review_path(locator)
    review_key, canonical, _ = resolve_review_context(locator, paths)
    existing_canonical = _load_json(canonical) if canonical.is_file() else {}
    legacy_raw = _load_json(legacy) if legacy.is_file() else {}

    if not _has_review_data(existing_canonical) and not _has_review_data(legacy_raw):
        return "skipped_no_review"

    if _has_review_data(existing_canonical) and _has_review_data(legacy_raw):
        merged = merge_event_reviews(
            legacy_raw,
            existing_canonical,
            review_key=review_key,
            record_id=locator.record_id,
        )
        action = "merged"
    elif _has_review_data(legacy_raw):
        merged = dict(legacy_raw)
        merged["review_key"] = review_key
        merged["record_id"] = locator.record_id
        action = "migrated"
    else:
        return "skipped_already"

    if dry_run:
        return f"dry_run_{action}"

    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")

    if remove_legacy and legacy.is_file() and legacy.resolve() != canonical.resolve():
        legacy.unlink()
        return f"{action}_removed_legacy"

    refresh_record_summary(locator.record_id, paths)
    return action


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="迁移 event_review 到 localdata/review")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写入")
    parser.add_argument("--tier", default="", help="仅处理指定 pose tier，如 rtmpose-t")
    parser.add_argument(
        "--remove-legacy",
        action="store_true",
        help="成功写入 review_dir 后删除记录包内旧 event_review.json",
    )
    args = parser.parse_args(argv)

    paths = resolve_app_paths()
    paths.review_dir.mkdir(parents=True, exist_ok=True)
    tier_filter = str(args.tier or "").strip() or None

    stats: Counter[str] = Counter()
    locators = iter_active_records(paths.json_dir, pose_tier=tier_filter)

    for locator in locators:
        try:
            result = migrate_one(
                locator,
                paths,
                dry_run=args.dry_run,
                remove_legacy=args.remove_legacy,
            )
            stats[result] += 1
        except Exception as exc:
            stats["error"] += 1
            print(f"错误 {locator.record_id}: {exc}", file=sys.stderr)

    print(
        f"扫描 {len(locators)} 条记录；"
        f"迁移 {stats.get('migrated', 0) + stats.get('dry_run_migrated', 0)}；"
        f"合并 {stats.get('merged', 0) + stats.get('dry_run_merged', 0)}；"
        f"已在 review_dir {stats.get('skipped_already', 0)}；"
        f"无复核 {stats.get('skipped_no_review', 0)}；"
        f"错误 {stats.get('error', 0)}"
    )
    if args.dry_run:
        print("（dry-run 模式，未写入磁盘）")
    return 1 if stats.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
