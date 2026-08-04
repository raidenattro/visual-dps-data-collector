#!/usr/bin/env python3
"""按对应帧骨架回填 event_review 中缺失的 person_track_id。

默认只审计，不写盘。仅当同帧的 person_id 能唯一对应到非空
person_track_id 时才回填；已有但与骨架冲突的值只报告，不覆盖。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config_loader import resolve_app_paths
from pose_store import iter_active_records, load_frames_range
from review_store import event_review_read_paths, event_review_write_path


@dataclass
class BackfillStats:
    """单份复核数据的追踪 ID 回填统计。"""

    bindings: int = 0
    added: int = 0
    existing: int = 0
    conflicts: int = 0
    missing_person_id: int = 0
    missing_skeleton_track: int = 0


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _track_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def review_frame_ids(review: dict[str, Any]) -> list[int]:
    """收集复核内容实际涉及的帧，供骨架按需读取。"""
    frames: set[int] = set()
    for row in review.get("verified_true") or []:
        if not isinstance(row, dict):
            continue
        frame_idx = _int_or_none(row.get("frame_idx"))
        if frame_idx is not None and frame_idx >= 1:
            frames.add(frame_idx)
    return sorted(frames)


def _contiguous_ranges(frame_ids: list[int]) -> list[tuple[int, int]]:
    if not frame_ids:
        return []
    ranges: list[tuple[int, int]] = []
    start = previous = frame_ids[0]
    for frame_idx in frame_ids[1:]:
        if frame_idx == previous + 1:
            previous = frame_idx
            continue
        ranges.append((start, previous))
        start = previous = frame_idx
    ranges.append((start, previous))
    return ranges


def load_person_tracks(locator, frame_ids: list[int]) -> dict[int, dict[int, str]]:
    """读取指定帧的 raw person_id → person_track_id 映射。"""
    tracks: dict[int, dict[int, str]] = {}
    for start, end in _contiguous_ranges(frame_ids):
        for frame in load_frames_range(
            locator,
            from_frame_idx=start,
            to_frame_idx=end,
        ):
            frame_idx = _int_or_none(frame.get("frame_idx"))
            if frame_idx is None:
                continue
            people: dict[int, str] = {}
            for index, person in enumerate(frame.get("persons") or []):
                if not isinstance(person, dict):
                    continue
                person_id = _int_or_none(person.get("person_id"))
                if person_id is None:
                    person_id = index
                track_id = _track_text(person.get("person_track_id"))
                if track_id:
                    people[person_id] = track_id
            tracks[frame_idx] = people
    return tracks


def _backfill_binding(
    binding: dict[str, Any],
    frame_tracks: dict[int, str],
    stats: BackfillStats,
) -> None:
    stats.bindings += 1
    person_id = _int_or_none(binding.get("person_id"))
    if person_id is None:
        stats.missing_person_id += 1
        return

    skeleton_track = frame_tracks.get(person_id)
    existing_track = _track_text(binding.get("person_track_id"))
    if not existing_track:
        existing_track = _track_text(binding.get("track_id"))
    if existing_track:
        if skeleton_track and existing_track != skeleton_track:
            stats.conflicts += 1
        else:
            stats.existing += 1
        return
    if not skeleton_track:
        stats.missing_skeleton_track += 1
        return

    binding["person_track_id"] = skeleton_track
    stats.added += 1


def backfill_review_payload(
    review: dict[str, Any],
    tracks_by_frame: dict[int, dict[int, str]],
) -> BackfillStats:
    """原位回填 schema v2 bindings 和 legacy 扁平条目。"""
    stats = BackfillStats()
    for row in review.get("verified_true") or []:
        if not isinstance(row, dict):
            continue
        frame_idx = _int_or_none(row.get("frame_idx"))
        frame_tracks = tracks_by_frame.get(frame_idx or -1, {})
        bindings = row.get("bindings")
        if isinstance(bindings, list) and bindings:
            for binding in bindings:
                if isinstance(binding, dict):
                    _backfill_binding(binding, frame_tracks, stats)
            continue
        _backfill_binding(row, frame_tracks, stats)
    return stats


def _write_json_with_backup(
    source_path: Path,
    target_path: Path,
    payload: dict[str, Any],
) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = source_path.with_name(f"{source_path.name}.bak.{stamp}")
    shutil.copy2(source_path, backup)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = target_path.with_name(f".{target_path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, target_path)
    return backup


def _record_matches(record_id: str, camera_slug: str, record_filter: str) -> bool:
    parts = record_id.replace("\\", "/").split("/")
    if camera_slug and camera_slug not in parts[:-1]:
        return False
    return not record_filter or record_filter in record_id


def main() -> int:
    parser = argparse.ArgumentParser(
        description="从骨架回填 event_review.person_track_id（默认仅审计）"
    )
    parser.add_argument("camera_slug", nargs="?", default="", help="可选机位过滤")
    parser.add_argument("--record", default="", help="可选 record_id 子串过滤")
    parser.add_argument("--write", action="store_true", help="备份后实际写入")
    args = parser.parse_args()

    paths = resolve_app_paths()
    locators = [
        locator
        for locator in iter_active_records(paths.json_dir)
        if _record_matches(
            locator.record_id,
            str(args.camera_slug or "").strip(),
            str(args.record or "").strip(),
        )
    ]
    if not locators:
        print("未找到匹配的骨架记录。")
        return 1

    mode = "写入" if args.write else "审计（dry-run）"
    print(f"{mode}：检查 {len(locators)} 条骨架记录")
    totals: Counter[str] = Counter()
    processed_review_paths: set[Path] = set()
    errors = 0

    for locator in sorted(locators, key=lambda item: item.record_id):
        read_paths = event_review_read_paths(locator, paths)
        if not read_paths:
            totals["no_review"] += 1
            continue
        source_path = read_paths[0].resolve()
        if source_path in processed_review_paths:
            totals["duplicate_review"] += 1
            continue
        processed_review_paths.add(source_path)

        try:
            review = json.loads(source_path.read_text(encoding="utf-8"))
            if not isinstance(review, dict):
                raise ValueError("event_review 根节点不是对象")
            frame_ids = review_frame_ids(review)
            tracks = load_person_tracks(locator, frame_ids)
            stats = backfill_review_payload(review, tracks)
            totals.update(vars(stats))
            if not stats.added:
                continue

            note = (
                f"补 {stats.added}/{stats.bindings}，"
                f"冲突 {stats.conflicts}，骨架无 track {stats.missing_skeleton_track}"
            )
            if args.write:
                review["updated_at"] = datetime.now(timezone.utc).isoformat()
                target_path = event_review_write_path(locator, paths)
                backup = _write_json_with_backup(source_path, target_path, review)
                print(f"  {locator.record_id}: [已写入] {note}；备份 {backup.name}")
                totals["updated_files"] += 1
            else:
                print(f"  {locator.record_id}: [可回填] {note}")
                totals["would_update_files"] += 1
        except Exception as exc:
            errors += 1
            print(f"  {locator.record_id}: [失败] {exc}")

    print(
        "\n汇总："
        f" binding {totals['bindings']}"
        f" · 可回填/已回填 {totals['added']}"
        f" · 已有 {totals['existing']}"
        f" · 冲突 {totals['conflicts']}"
        f" · 无 person_id {totals['missing_person_id']}"
        f" · 骨架无 track {totals['missing_skeleton_track']}"
        f" · 无 review {totals['no_review']}"
        f" · 失败 {errors}"
    )
    if not args.write and totals["added"]:
        print("确认审计结果后加 --write 执行；写入前会自动备份。")
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
