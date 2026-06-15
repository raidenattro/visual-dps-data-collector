#!/usr/bin/env python3
"""将「已复核」但标真条目缺货框确认的记录降为「复核中」。

仅修改 event_review.json 的 status / completed_at，不改动 verified_true 等人工复核数据。

用法:
  python scripts/data/demote_incomplete_box_review.py --dry-run
  python scripts/data/demote_incomplete_box_review.py
  python scripts/data/demote_incomplete_box_review.py 2-7-2
  python scripts/data/demote_incomplete_box_review.py --dry-run 2-7-2

说明:
  - 仅处理落盘 status=completed 且存在缺 confirmed_box_tokens 的标真条目
  - 已有货框确认、复核中、无碰撞、未复核等记录一律跳过
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

from config_loader import resolve_app_paths, resolve_config_path
from pose_store import (
    REVIEW_STATUS_COMPLETED,
    REVIEW_STATUS_IN_PROGRESS,
    count_verified_missing_box_annotation,
    iter_active_records,
    load_event_review_raw,
    meta_sidecar_path,
    patch_event_review_persisted_status,
    persisted_event_review_status,
    review_missing_box_annotation,
)
from record_index_store import refresh_record_summary


def _camera_slug_for_record(record_id: str, paths) -> str:
    if "/" in record_id:
        return record_id.split("/", 1)[0]
    sidecar = meta_sidecar_path(paths.json_dir, record_id)
    if sidecar.is_file():
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
            slug = str(meta.get("camera_slug") or "").strip()
            if slug:
                return slug
        except (OSError, json.JSONDecodeError):
            pass
    return ""


def _filter_records(paths, slug_filter: str) -> list:
    items = iter_active_records(paths.json_dir)
    if not slug_filter:
        return items
    out = []
    for loc in items:
        rid = loc.record_id
        bucket = rid.split("/", 1)[0] if "/" in rid else ""
        if bucket == slug_filter or _camera_slug_for_record(rid, paths) == slug_filter:
            out.append(loc)
    return out


def process_record(locator, *, dry_run: bool) -> tuple[str, str]:
    """返回 (结果类别, 说明)。"""
    review = load_event_review_raw(locator)
    if not review:
        return "no_review", "无 event_review"

    persisted = persisted_event_review_status(review)
    if persisted != REVIEW_STATUS_COMPLETED:
        return "skip", f"非已复核 (status={persisted or '空'})"

    if not review_missing_box_annotation(review):
        return "skip_boxes_ok", "已复核且标真条目均已确认货框"

    missing_n = count_verified_missing_box_annotation(review)
    verified_n = len(review.get("verified_true") or [])
    note = f"{missing_n}/{verified_n} 条标真缺货框确认 → 复核中"

    if dry_run:
        return "would_demote", note

    try:
        patch_event_review_persisted_status(locator, REVIEW_STATUS_IN_PROGRESS)
        refresh_record_summary(locator.record_id)
    except (OSError, ValueError, FileNotFoundError) as exc:
        return "error", str(exc)

    return "demoted", note


def main() -> int:
    parser = argparse.ArgumentParser(
        description="将已复核但缺货框确认的 SP 记录降为复核中（仅改 status）"
    )
    parser.add_argument(
        "camera_slug",
        nargs="?",
        default="",
        help="可选：仅处理该机位目录（如 2-7-2）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只统计，不写入 event_review.json",
    )
    args = parser.parse_args()
    slug_filter = str(args.camera_slug or "").strip()

    resolve_config_path(None)
    paths = resolve_app_paths()
    locators = _filter_records(paths, slug_filter)
    if not locators:
        print(f"未找到记录（filter={slug_filter or '全部'}）")
        return 1

    counts: Counter[str] = Counter()
    errors = 0

    mode = "预览" if args.dry_run else "执行"
    print(f"{mode}：共 {len(locators)} 条记录（filter={slug_filter or '全部'}）\n")

    for loc in sorted(locators, key=lambda x: x.record_id):
        kind, note = process_record(loc, dry_run=args.dry_run)
        counts[kind] += 1
        if kind == "error":
            errors += 1
        if kind in ("would_demote", "demoted", "error") or args.dry_run:
            print(f"  {loc.record_id}: [{kind}] {note}")

    print(
        f"\n汇总："
        f" 降为复核中 {counts['demoted'] + counts['would_demote']}"
        f" · 已复核且货框齐全 {counts['skip_boxes_ok']}"
        f" · 其他跳过 {counts['skip'] + counts['no_review']}"
        f" · 失败 {counts['error']}"
    )
    if args.dry_run and counts["would_demote"]:
        print("\n去掉 --dry-run 后将实际写入。")

    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
