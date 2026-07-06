#!/usr/bin/env python3
"""将扁平机位目录迁移到 rtmpose-t/ 模型层下，并更新 record_id 引用。

用法:
  python scripts/data/migrate_pose_model_tiers.py --dry-run
  python scripts/data/migrate_pose_model_tiers.py
  python scripts/data/migrate_pose_model_tiers.py --tier rtmpose-t
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config_loader import is_pose_model_tier, resolve_app_paths

SKIP_NAMES = frozenset({"annotations", "archive"})


_RECORD_REF_KEYS = frozenset({"record_id", "pose_file", "pose_url", "manifest_url"})


def _rewrite_record_id(val: str, old_prefix: str, new_prefix: str) -> str | None:
    if val == old_prefix or val.startswith(old_prefix + "/"):
        return new_prefix + val[len(old_prefix) :]
    return None


def _rewrite_record_refs(obj, old_prefix: str, new_prefix: str) -> bool:
    changed = False
    if isinstance(obj, dict):
        for key, val in list(obj.items()):
            if isinstance(val, str) and key in _RECORD_REF_KEYS:
                rewritten = _rewrite_record_id(val, old_prefix, new_prefix)
                if rewritten is not None:
                    obj[key] = rewritten
                    changed = True
            elif isinstance(val, (dict, list)):
                if _rewrite_record_refs(val, old_prefix, new_prefix):
                    changed = True
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, str):
                rewritten = _rewrite_record_id(item, old_prefix, new_prefix)
                if rewritten is not None:
                    obj[i] = rewritten
                    changed = True
            elif isinstance(item, (dict, list)):
                if _rewrite_record_refs(item, old_prefix, new_prefix):
                    changed = True
    return changed


def _patch_json_file(path: Path, old_prefix: str, new_prefix: str, *, dry_run: bool) -> bool:
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not _rewrite_record_refs(data, old_prefix, new_prefix):
        return False
    if not dry_run:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def migrate(*, tier: str, dry_run: bool) -> dict[str, int]:
    paths = resolve_app_paths()
    stats = {"moved_json": 0, "moved_video": 0, "patched_meta": 0, "skipped": 0}

    tier_json = paths.json_dir / tier
    tier_video = paths.video_dir / tier
    if not dry_run:
        tier_json.mkdir(parents=True, exist_ok=True)
        tier_video.mkdir(parents=True, exist_ok=True)

    for src in sorted(paths.json_dir.iterdir(), key=lambda p: p.name):
        if not src.is_dir() or src.name in SKIP_NAMES or src.name.startswith("."):
            continue
        if is_pose_model_tier(src.name):
            stats["skipped"] += 1
            continue
        dest = tier_json / src.name
        if dest.exists():
            print(f"⚠️ 跳过（目标已存在）: {src.name} → {tier}/{src.name}")
            stats["skipped"] += 1
            continue
        print(f"📦 json: {src.name} → {tier}/{src.name}")
        if not dry_run:
            shutil.move(str(src), str(dest))
        stats["moved_json"] += 1

        old_prefix = src.name
        new_prefix = f"{tier}/{src.name}"
        if not dry_run:
            for meta_path in dest.glob("*.meta.json"):
                if _patch_json_file(meta_path, old_prefix, new_prefix, dry_run=False):
                    stats["patched_meta"] += 1
            for pkg in dest.iterdir():
                if not pkg.is_dir():
                    continue
                manifest = pkg / "manifest.json"
                if _patch_json_file(manifest, old_prefix, new_prefix, dry_run=False):
                    stats["patched_meta"] += 1
                review = pkg / "event_review.json"
                _patch_json_file(review, old_prefix, new_prefix, dry_run=False)
            batch_files = list(dest.glob("_batch_*.json"))
            for bf in batch_files:
                _patch_json_file(bf, old_prefix, new_prefix, dry_run=False)

    for src in sorted(paths.video_dir.iterdir(), key=lambda p: p.name):
        if not src.is_dir() or src.name.startswith("."):
            continue
        if is_pose_model_tier(src.name):
            stats["skipped"] += 1
            continue
        dest = tier_video / src.name
        if dest.exists():
            print(f"⚠️ 跳过 video（目标已存在）: {src.name}")
            stats["skipped"] += 1
            continue
        print(f"🎬 video: {src.name} → {tier}/{src.name}")
        if not dry_run:
            shutil.move(str(src), str(dest))
        stats["moved_video"] += 1

    return stats


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="迁移采集数据到 rtmpose-{t,s,m} 模型层目录")
    p.add_argument("--tier", default="rtmpose-t", help="目标模型层目录名（默认 rtmpose-t）")
    p.add_argument("--dry-run", action="store_true", help="仅预览，不移动文件")
    args = p.parse_args(argv)
    tier = str(args.tier or "rtmpose-t").strip().lower()
    if not is_pose_model_tier(tier):
        print(f"❌ 无效 tier: {tier}", file=sys.stderr)
        return 2
    stats = migrate(tier=tier, dry_run=args.dry_run)
    mode = "[dry-run] " if args.dry_run else ""
    print(
        f"\n{mode}完成：json 机位 {stats['moved_json']}，video 机位 {stats['moved_video']}，"
        f"修补 meta {stats['patched_meta']}，跳过 {stats['skipped']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
