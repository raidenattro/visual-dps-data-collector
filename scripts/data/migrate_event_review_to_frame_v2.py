#!/usr/bin/env python3
"""将 event_review.json 的 verified_true 迁移为 schema v2（逐帧 + bindings）。

用法:
  python scripts/data/migrate_event_review_to_frame_v2.py --dry-run
  python scripts/data/migrate_event_review_to_frame_v2.py
  python scripts/data/migrate_event_review_to_frame_v2.py --verify-only
  python scripts/data/migrate_event_review_to_frame_v2.py 1-1-1-_2

说明:
  - schema v2：每帧一条 verified_true，bindings 承载 (person_id, confirmed_box_tokens)
  - 默认 --dry-run；去掉后写入并在同目录备份 .bak.{timestamp}
  - 有 record locator 时尽量读取 timeline 填充 box_tokens / event_type
  - legacy 迁移规则：仅使用 confirmed_box_tokens；无 confirmed 且 box_tokens 唯一时
    视为旧版逐货框标真（单 box 事件），避免读时聚合误并
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
from event_review_frame_v2 import (
    EVENT_REVIEW_SCHEMA_V2,
    FrameV2MigrationStats,
    is_frame_v2_review,
    migrate_verified_true_to_frame_v2,
    verify_frame_v2_verified_true,
)
from pose_store import iter_active_records, load_timeline
from review_store import EVENT_REVIEW_FILE, event_review_read_paths, event_review_write_path


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _camera_slug_for_record(record_id: str, paths) -> str:
    if "/" in record_id:
        return record_id.split("/", 1)[0]
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


def _discover_review_targets(paths, slug_filter: str) -> list[tuple[Path, object | None]]:
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


def _timeline_by_frame(locator) -> dict[int, dict]:
    try:
        rows = load_timeline(locator, include_events=True)
    except (OSError, RuntimeError, ValueError):
        return {}
    out: dict[int, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            fi = int(row.get("frame_idx") or 0)
        except (TypeError, ValueError):
            continue
        if fi > 0:
            out[fi] = row
    return out


def process_review_file(
    path: Path,
    locator,
    *,
    dry_run: bool,
    verify_only: bool,
) -> tuple[str, str, FrameV2MigrationStats | None]:
    raw = _load_json(path)
    verified = raw.get("verified_true")
    if not isinstance(verified, list) or not verified:
        return "skipped_empty", "无 verified_true", None

    if verify_only:
        if is_frame_v2_review(raw):
            issues = verify_frame_v2_verified_true(verified)
            if issues:
                preview = "; ".join(issues[:3])
                if len(issues) > 3:
                    preview += f" …共 {len(issues)} 项"
                return "verify_fail", preview, None
            return "verify_ok", f"schema v2 · {len(verified)} 帧", None
        return "verify_fail", f"仍为 schema {raw.get('schema', 1)} legacy 格式", None

    if is_frame_v2_review(raw):
        issues = verify_frame_v2_verified_true(verified)
        if not issues:
            return "unchanged", f"已是 schema v2 · {len(verified)} 帧", None

    timeline_by_frame = _timeline_by_frame(locator) if locator is not None else {}
    new_verified, stats = migrate_verified_true_to_frame_v2(
        verified,
        timeline_by_frame=timeline_by_frame,
    )

    if stats.output_frame_count == 0 and stats.input_count > 0:
        return "failed", "迁移结果为空，请检查 cleared 警告", stats

    if (
        is_frame_v2_review({**raw, "verified_true": new_verified, "schema": EVENT_REVIEW_SCHEMA_V2})
        and len(new_verified) == len(verified)
        and stats.skipped_entries == 0
        and stats.deduped_bindings == 0
        and int(raw.get("schema") or 0) >= EVENT_REVIEW_SCHEMA_V2
    ):
        return "unchanged", f"{stats.input_count} 条已是 v2 帧级", stats

    note_parts = [
        f"{stats.input_count} legacy 条 → {stats.output_frame_count} 帧",
        f"bindings {stats.binding_count}",
    ]
    if stats.deduped_bindings:
        note_parts.append(f"去重 {stats.deduped_bindings}")
    if stats.skipped_entries:
        note_parts.append(f"跳过 {stats.skipped_entries}")
    if stats.cleared_entries:
        note_parts.append(f"清除 {stats.cleared_entries} 条")
    if stats.cleared_frames:
        note_parts.append(f"清除帧 {len(set(stats.cleared_frames))}")
    note = " · ".join(note_parts)

    if dry_run:
        return "would_migrate", note, stats

    updated = dict(raw)
    updated["schema"] = EVENT_REVIEW_SCHEMA_V2
    updated["verified_true"] = new_verified
    updated["updated_at"] = datetime.now(timezone.utc).isoformat()

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
        description="event_review verified_true 迁移为 schema v2（逐帧 + bindings）"
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
        help="仅校验 verified_true 是否已为 schema v2",
    )
    args = parser.parse_args()
    dry_run = not args.write
    if args.verify_only:
        dry_run = True

    resolve_config_path(None)
    paths = resolve_app_paths()
    targets = _discover_review_targets(paths, args.camera_slug.strip())

    if not targets:
        print("未找到 event_review.json")
        return 1

    counts: Counter[str] = Counter()
    print(f"{'校验' if args.verify_only else ('预览（dry-run）' if dry_run else '写入')}：共 {len(targets)} 个 review 文件\n")

    for path, locator in targets:
        status, note, stats = process_review_file(
            path,
            locator,
            dry_run=dry_run,
            verify_only=args.verify_only,
        )
        counts[status] += 1
        rel = path
        try:
            rel = path.relative_to(ROOT)
        except ValueError:
            pass
        print(f"[{status}] {rel}\n    {note}")
        if stats and stats.warnings:
            for warn in stats.warnings[:5]:
                print(f"    ! {warn}")
            if len(stats.warnings) > 5:
                print(f"    ! …共 {len(stats.warnings)} 条警告")

    print("\n汇总:", dict(counts))
    if dry_run and not args.verify_only and counts.get("would_migrate"):
        print("\n确认无误后加 --write 执行写入。")
    if args.verify_only:
        return 0 if counts.get("verify_fail", 0) == 0 else 2
    if counts.get("failed", 0):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
