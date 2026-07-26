#!/usr/bin/env python3
"""将 event_review.json 中多货框 verified_true 拆为单货框条目。

用法:
  python scripts/data/migrate_event_review_split_by_box.py --dry-run
  python scripts/data/migrate_event_review_split_by_box.py
  python scripts/data/migrate_event_review_split_by_box.py --verify-only
  python scripts/data/migrate_event_review_split_by_box.py --infer-person --dry-run
  python scripts/data/migrate_event_review_split_by_box.py 1-1-1-_2

说明:
  - 默认 --dry-run，仅统计不写盘；去掉 --dry-run 才会写入
  - 写入前在同目录备份 event_review.json.bak.{timestamp}
  - --verify-only 仅校验 verified_true 是否已为单 box
  - --infer-person 对多 box 拆条尝试 wrist_hits 推断 person_id
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
from event_review_split import (
    SplitVerifiedTrueStats,
    split_verified_true_list,
    verify_single_box_verified_true,
)
from pose_store import (
    REVIEW_STATUS_COMPLETED,
    REVIEW_STATUS_IN_PROGRESS,
    iter_active_records,
    meta_sidecar_path,
)
from review_store import EVENT_REVIEW_FILE, event_review_read_paths, event_review_write_path


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


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _discover_review_targets(paths, slug_filter: str) -> list[tuple[Path, object | None]]:
    """返回 (review 文件路径, 可选 locator)。"""
    seen: set[str] = set()
    out: list[tuple[Path, object | None]] = []

    for loc in _filter_records(paths, slug_filter):
        for path in event_review_read_paths(loc, paths):
            try:
                key = str(path.resolve())
            except OSError:
                key = str(path)
            if key in seen:
                continue
            seen.add(key)
            out.append((path, loc))

    review_root = paths.review_dir
    if review_root.is_dir():
        for path in sorted(review_root.rglob(EVENT_REVIEW_FILE)):
            try:
                key = str(path.resolve())
            except OSError:
                key = str(path)
            if key in seen:
                continue
            seen.add(key)
            out.append((path, None))

    out.sort(key=lambda item: str(item[0]))
    return out


def _maybe_demote_status(raw: dict, stats: SplitVerifiedTrueStats) -> dict:
    if stats.person_ambiguous <= 0:
        return raw
    if str(raw.get("status") or "").strip().lower() != REVIEW_STATUS_COMPLETED:
        return raw
    updated = dict(raw)
    updated["status"] = REVIEW_STATUS_IN_PROGRESS
    updated.pop("completed_at", None)
    return updated


def process_review_file(
    path: Path,
    locator,
    *,
    dry_run: bool,
    infer_person: bool,
    verify_only: bool,
) -> tuple[str, str, SplitVerifiedTrueStats | None]:
    raw = _load_json(path)
    verified = raw.get("verified_true")
    if not isinstance(verified, list) or not verified:
        return "skipped_empty", "无 verified_true", None

    if verify_only:
        issues = verify_single_box_verified_true(verified)
        if issues:
            preview = "; ".join(issues[:3])
            if len(issues) > 3:
                preview += f" …共 {len(issues)} 项"
            return "verify_fail", preview, None
        return "verify_ok", f"{len(verified)} 条均为单 box", None

    new_verified, stats = split_verified_true_list(
        verified,
        infer_person=infer_person,
        locator=locator,
    )
    if not stats.needs_migration() and stats.invalid_entries == 0:
        return "unchanged", f"{stats.input_count} 条无需拆分", stats

    note_parts = [
        f"{stats.input_count}→{stats.output_count} 条",
        f"拆 {stats.split_entries}",
    ]
    if stats.person_inferred:
        note_parts.append(f"推断 person {stats.person_inferred}")
    if stats.person_ambiguous:
        note_parts.append(f"person 待复核 {stats.person_ambiguous}")
    if stats.invalid_entries:
        note_parts.append(f"无效 {stats.invalid_entries}")
    note = " · ".join(note_parts)

    if dry_run:
        return "would_migrate", note, stats

    updated = dict(raw)
    updated["verified_true"] = new_verified
    updated["updated_at"] = datetime.now(timezone.utc).isoformat()
    updated = _maybe_demote_status(updated, stats)

    backup = path.with_name(f"{path.name}.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    backup.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    write_path = path
    if locator is not None:
        write_path = event_review_write_path(locator, paths=resolve_app_paths())
    write_path.parent.mkdir(parents=True, exist_ok=True)
    write_path.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
    return "migrated", f"{note} · 备份 {backup.name}", stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="将 event_review verified_true 多货框条目拆为单货框（默认 dry-run）"
    )
    parser.add_argument(
        "camera_slug",
        nargs="?",
        default="",
        help="可选：仅处理该机位目录（如 1-1-1-_2）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="只统计，不写入（默认开启）",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="实际写入 review 文件（关闭 dry-run）",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="仅校验 verified_true 是否已为单 box",
    )
    parser.add_argument(
        "--infer-person",
        action="store_true",
        help="多 box 拆条时用 wrist_hits 推断 person_id",
    )
    args = parser.parse_args()

    dry_run = not args.write
    if args.verify_only:
        dry_run = True

    resolve_config_path(None)
    paths = resolve_app_paths()
    slug_filter = str(args.camera_slug or "").strip()
    targets = _discover_review_targets(paths, slug_filter)
    if not targets:
        print(f"未找到 review 文件（filter={slug_filter or '全部'}）")
        return 1

    counts: Counter[str] = Counter()
    errors = 0
    total_split_entries = 0

    if args.verify_only:
        mode = "校验"
    elif dry_run:
        mode = "预览（dry-run）"
    else:
        mode = "写入"

    print(f"{mode}：共 {len(targets)} 个 review 文件（filter={slug_filter or '全部'}）\n")

    for path, locator in targets:
        label = str(path.relative_to(paths.review_dir)) if path.is_relative_to(paths.review_dir) else path.name
        if locator is not None:
            label = f"{locator.record_id} · {label}"
        try:
            kind, note, stats = process_review_file(
                path,
                locator,
                dry_run=dry_run,
                infer_person=bool(args.infer_person),
                verify_only=bool(args.verify_only),
            )
            counts[kind] += 1
            if stats is not None:
                total_split_entries += stats.split_entries
            if kind not in ("unchanged", "skipped_empty", "verify_ok"):
                print(f"  {label}: [{kind}] {note}")
            elif args.verify_only and kind == "verify_fail":
                print(f"  {label}: [{kind}] {note}")
        except Exception as exc:
            counts["error"] += 1
            errors += 1
            print(f"  {label}: [error] {exc}")

    print(
        f"\n汇总："
        f" 需迁移 {counts['would_migrate'] + counts['migrated']}"
        f" · 已迁移 {counts['migrated']}"
        f" · 无需变更 {counts['unchanged']}"
        f" · 校验通过 {counts['verify_ok']}"
        f" · 校验失败 {counts['verify_fail']}"
        f" · 空文件 {counts['skipped_empty']}"
        f" · 失败 {counts['error']}"
    )
    if total_split_entries:
        print(f"多 box 条目合计：{total_split_entries}")
    if dry_run and not args.verify_only and counts["would_migrate"]:
        print("\n确认无误后加 --write 执行写入。")

    return 2 if errors or counts["verify_fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
