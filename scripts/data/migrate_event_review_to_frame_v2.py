#!/usr/bin/env python3
"""Audit and migrate event_review.json to the lossless frame-v2 schema.

The default is read-only.  Legacy schema 1 is ambiguous, so callers must
choose a source profile.  Candidate output never overwrites source data.
In-place replacement is a separate, explicit operation and always creates a
backup first.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config_loader import resolve_app_paths, resolve_config_path
from event_review_frame_v2 import (
    EVENT_REVIEW_SCHEMA_V2,
    LEGACY_SOURCE_FORMATS,
    SOURCE_EXPLICIT_CONFIRMED,
    detect_review_format,
    is_frame_v2_review,
    migrate_verified_true_to_frame_v2,
    verify_frame_v2_verified_true,
)
from pose_store import iter_active_records, load_timeline
from review_store import EVENT_REVIEW_FILE, event_review_read_paths


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp.replace(path)


def _discover(paths, camera_slug: str) -> list[tuple[Path, Any | None]]:
    locators_by_path: dict[str, Any] = {}
    for locator in iter_active_records(paths.json_dir):
        bucket = locator.record_id.split("/", 1)[0] if "/" in locator.record_id else ""
        if camera_slug and bucket != camera_slug:
            continue
        for path in event_review_read_paths(locator, paths):
            locators_by_path[str(path.resolve())] = locator

    targets: dict[str, tuple[Path, Any | None]] = {}
    if paths.review_dir.is_dir():
        for path in paths.review_dir.rglob(EVENT_REVIEW_FILE):
            try:
                rel = path.relative_to(paths.review_dir)
            except ValueError:
                continue
            if camera_slug and (not rel.parts or rel.parts[0] != camera_slug):
                continue
            key = str(path.resolve())
            targets[key] = (path, locators_by_path.get(key))
    for key, locator in locators_by_path.items():
        path = Path(key)
        if path.is_file():
            targets.setdefault(key, (path, locator))
    return [targets[key] for key in sorted(targets)]


def _timeline(locator: Any | None) -> dict[int, dict[str, Any]]:
    if locator is None:
        return {}
    try:
        rows = load_timeline(locator, include_events=True)
    except (OSError, RuntimeError, ValueError):
        return {}
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            frame_idx = int(row.get("frame_idx"))
        except (TypeError, ValueError):
            continue
        out[frame_idx] = row
    return out


def _candidate_payload(
    raw: dict[str, Any],
    *,
    source_format: str,
    timeline_by_frame: dict[int, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    verified = raw.get("verified_true")
    verified_list = verified if isinstance(verified, list) else []
    converted, stats = migrate_verified_true_to_frame_v2(
        verified_list,
        timeline_by_frame=timeline_by_frame,
        source_format=source_format,
    )
    payload = dict(raw)
    payload["schema"] = EVENT_REVIEW_SCHEMA_V2
    payload["verified_true"] = converted
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    payload["migration"] = {
        "source_schema": raw.get("schema", 1),
        "source_format": source_format,
        "source_detected_format": detect_review_format(raw),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "stats": stats.to_dict(),
    }
    if stats.unresolved_items:
        payload["unresolved_legacy"] = stats.unresolved_items
    elif "unresolved_legacy" not in raw:
        payload.pop("unresolved_legacy", None)
    return payload, stats.to_dict()


def _relative_candidate_path(path: Path, review_root: Path) -> Path:
    try:
        return path.resolve().relative_to(review_root.resolve())
    except ValueError:
        return Path(path.parent.name) / path.name


def main() -> int:
    parser = argparse.ArgumentParser(
        description="审计并迁移 event_review 到逐帧 schema v2（默认只读）"
    )
    parser.add_argument("camera_slug", nargs="?", default="")
    parser.add_argument(
        "--source-format",
        choices=sorted(LEGACY_SOURCE_FORMATS),
        default=SOURCE_EXPLICIT_CONFIRMED,
        help=(
            "旧格式语义；默认只信 confirmed_box_tokens。"
            "legacy-per-box / legacy-frame-event 必须由人工确认后选择"
        ),
    )
    parser.add_argument(
        "--candidate-dir",
        type=Path,
        help="把迁移候选写入独立目录，不修改源文件",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="原地替换；先生成 .bak 时间戳备份",
    )
    parser.add_argument(
        "--allow-unresolved",
        action="store_true",
        help="允许候选/替换包含 unresolved_legacy；默认替换时禁止",
    )
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--report", type=Path, help="写出 JSON 审计报告")
    args = parser.parse_args()

    if args.replace and args.candidate_dir:
        parser.error("--replace 与 --candidate-dir 不能同时使用")

    resolve_config_path(None)
    paths = resolve_app_paths()
    targets = _discover(paths, args.camera_slug.strip())
    if not targets:
        print("未找到 event_review.json")
        return 1

    report_items: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    for path, locator in targets:
        raw = _load_json(path)
        detected = detect_review_format(raw)
        item: dict[str, Any] = {
            "path": str(path),
            "detected_format": detected,
            "source_format": args.source_format,
        }
        if args.verify_only:
            issues = (
                verify_frame_v2_verified_true(raw.get("verified_true") or [])
                if is_frame_v2_review(raw)
                else [f"仍是 {detected}"]
            )
            status = "verify_ok" if not issues else "verify_fail"
            item.update({"status": status, "issues": issues})
            counts[status] += 1
            report_items.append(item)
            print(f"[{status}] {path} {issues[:2]}")
            continue

        candidate, stats = _candidate_payload(
            raw,
            source_format=args.source_format,
            timeline_by_frame=_timeline(locator),
        )
        unresolved = int(stats.get("unresolved_entries") or 0)
        item["stats"] = stats
        item["unresolved_legacy_preserved"] = unresolved

        if args.replace and unresolved and not args.allow_unresolved:
            status = "blocked_unresolved"
            item["status"] = status
            counts[status] += 1
            report_items.append(item)
            print(f"[{status}] {path} unresolved={unresolved}，源文件未改")
            continue

        if args.candidate_dir:
            destination = (
                args.candidate_dir
                / _relative_candidate_path(path, paths.review_dir)
            )
            _write_json(destination, candidate)
            status = "candidate_written"
            item["candidate_path"] = str(destination)
        elif args.replace:
            backup = path.with_name(f"{path.name}.bak.{timestamp}")
            shutil.copy2(path, backup)
            _write_json(path, candidate)
            status = "replaced"
            item["backup_path"] = str(backup)
        else:
            status = "audit_only"

        item["status"] = status
        counts[status] += 1
        report_items.append(item)
        print(
            f"[{status}] {path} "
            f"rows={stats['input_count']} frames={stats['output_frame_count']} "
            f"bindings={stats['binding_count']} unresolved={unresolved}"
        )

    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": (
            "verify"
            if args.verify_only
            else "replace"
            if args.replace
            else "candidate"
            if args.candidate_dir
            else "audit"
        ),
        "source_format": args.source_format,
        "counts": dict(counts),
        "items": report_items,
    }
    if args.report:
        _write_json(args.report, report)
        print(f"报告: {args.report}")
    print("汇总:", dict(counts))

    if counts.get("verify_fail") or counts.get("blocked_unresolved"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
