#!/usr/bin/env python3
"""按当前 load_events 结果回写 event_review.json 的 event_total。

用法:
  python scripts/data/backfill_event_review_event_total.py --dry-run
  python scripts/data/backfill_event_review_event_total.py --write

说明:
  - 默认 --dry-run，仅统计不写盘
  - 不修改 verified_true 等复核内容
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config_loader import resolve_app_paths, resolve_config_path
from pose_store import iter_active_records, load_event_review_raw, load_events
from review_store import event_review_write_path


def _filter_records(paths, slug_filter: str) -> list:
    items = iter_active_records(paths.json_dir)
    if not slug_filter:
        return items
    out = []
    for loc in items:
        rid = loc.record_id
        bucket = rid.split("/", 1)[0] if "/" in rid else ""
        if bucket == slug_filter or rid.split("/", 1)[0] == slug_filter:
            out.append(loc)
    return out


def process_record(locator, *, dry_run: bool) -> tuple[str, str]:
    raw = load_event_review_raw(locator)
    if not raw:
        return "skipped_no_review", "无 event_review"

    try:
        event_total = len(load_events(locator))
    except Exception as exc:
        return "error", str(exc)

    old_raw = raw.get("event_total")
    try:
        old_total = int(old_raw) if old_raw is not None else None
    except (TypeError, ValueError):
        old_total = None

    if old_total == event_total:
        return "unchanged", f"event_total={event_total}"

    note = f"event_total {old_total} → {event_total}"
    if dry_run:
        return "would_update", note

    updated = dict(raw)
    updated["event_total"] = event_total
    updated["updated_at"] = datetime.now(timezone.utc).isoformat()
    path = event_review_write_path(locator)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
    return "updated", note


def main() -> int:
    parser = argparse.ArgumentParser(description="回写 event_review.event_total（默认 dry-run）")
    parser.add_argument("camera_slug", nargs="?", default="", help="可选机位过滤")
    parser.add_argument("--write", action="store_true", help="实际写入")
    args = parser.parse_args()

    dry_run = not args.write
    resolve_config_path(None)
    paths = resolve_app_paths()
    slug_filter = str(args.camera_slug or "").strip()
    locators = _filter_records(paths, slug_filter)
    if not locators:
        print(f"未找到记录（filter={slug_filter or '全部'}）")
        return 1

    counts: Counter[str] = Counter()
    errors = 0
    mode = "预览（dry-run）" if dry_run else "写入"
    print(f"{mode}：共 {len(locators)} 条记录\n")

    for loc in sorted(locators, key=lambda x: x.record_id):
        kind, note = process_record(loc, dry_run=dry_run)
        counts[kind] += 1
        if kind in ("would_update", "updated", "error"):
            print(f"  {loc.record_id}: [{kind}] {note}")
        if kind == "error":
            errors += 1

    print(
        f"\n汇总："
        f" 将更新 {counts['would_update'] + counts['updated']}"
        f" · 已更新 {counts['updated']}"
        f" · 无需变更 {counts['unchanged']}"
        f" · 无 review {counts['skipped_no_review']}"
        f" · 失败 {counts['error']}"
    )
    if dry_run and counts["would_update"]:
        print("\n确认无误后加 --write 执行写入。")
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
