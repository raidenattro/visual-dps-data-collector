#!/usr/bin/env python3
"""批量生成碰撞变体 sidecar（wrist + hand_extended α=0.1/0.2/0.3/0.4）。

用法（项目根目录）:
  python scripts/data/build_collision_variants.py --dry-run
  python scripts/data/build_collision_variants.py
  python scripts/data/build_collision_variants.py --skip-existing
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config_loader import resolve_config_path
from api.collision_variants_service import build_collision_variants_for_record
from api.record_service import locate_record_by_id
from scripts.data.eval_dataset import (
    DEFAULT_CAMERAS,
    DEFAULT_REVIEW_STATUS,
    DEFAULT_TAGS,
    DEFAULT_TIER,
    collect_record_ids,
    parse_csv_list,
    parse_tags,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="批量生成碰撞变体 timeline sidecar")
    parser.add_argument("--tier", default=DEFAULT_TIER)
    parser.add_argument("--tags", default=",".join(DEFAULT_TAGS))
    parser.add_argument("--cameras", default=",".join(DEFAULT_CAMERAS))
    parser.add_argument("--pose-frame-interval", type=int, default=2)
    parser.add_argument("--alarm-min", type=int, default=3)
    parser.add_argument("--cooldown", type=int, default=0)
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--record", default="", help="仅处理单条 record_id")
    parser.add_argument(
        "--summary-out",
        default=str(ROOT / "localdata" / "export" / "collision_variants_build_summary.json"),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    resolve_config_path(None)
    tags = parse_tags(args.tags)
    cameras = parse_csv_list(args.cameras)

    if args.record.strip():
        record_ids = [args.record.strip()]
    else:
        record_ids = collect_record_ids(
            tier=args.tier,
            cameras=set(cameras),
            tags=tags,
            review_status=DEFAULT_REVIEW_STATUS,
            has_verified=True,
        )

    if not record_ids:
        print("未找到匹配记录", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"将处理 {len(record_ids)} 条记录")
        print(
            f"参数: interval={args.pose_frame_interval}, min={args.alarm_min}, "
            f"cooldown={args.cooldown}"
        )
        for rid in record_ids:
            print(f"  {rid}")
        return 0

    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for rid in record_ids:
        locator = locate_record_by_id(rid)
        if not locator:
            errors.append({"record_id": rid, "error": "记录不存在"})
            continue
        try:
            result = build_collision_variants_for_record(
                locator,
                pose_frame_interval=args.pose_frame_interval,
                alarm_min_consecutive_frames=args.alarm_min,
                alarm_cooldown_frames=args.cooldown,
                skip_if_exists=args.skip_existing,
            )
            results.append(result)
            status = result.get("status")
            if status == "skipped":
                print(f"⊘ {rid.split('/')[-1]}: 已存在，跳过")
            else:
                alarm_info = ", ".join(
                    f"{v['variant']}={v['alarm_frame_count']}"
                    for v in (result.get("variants") or [])
                )
                print(
                    f"✓ {rid.split('/')[-1]}: {result.get('frame_count_used')} 帧 "
                    f"({alarm_info})"
                )
        except (OSError, ValueError, FileNotFoundError, RuntimeError) as exc:
            errors.append({"record_id": rid, "error": str(exc)})
            print(f"✗ {rid}: {exc}", file=sys.stderr)

    summary = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "params": {
            "pose_frame_interval": args.pose_frame_interval,
            "alarm_min_consecutive_frames": args.alarm_min,
            "alarm_cooldown_frames": args.cooldown,
        },
        "tier": args.tier,
        "tags": tags,
        "cameras": cameras,
        "record_count": len(record_ids),
        "ok_count": sum(1 for r in results if r.get("status") == "ok"),
        "skipped_count": sum(1 for r in results if r.get("status") == "skipped"),
        "error_count": len(errors),
        "results": results,
        "errors": errors,
    }

    out_path = Path(args.summary_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n汇总: {out_path}")
    print(
        f"完成: ok={summary['ok_count']} skipped={summary['skipped_count']} "
        f"errors={summary['error_count']}"
    )
    return 0 if not errors or results else 1


if __name__ == "__main__":
    raise SystemExit(main())
