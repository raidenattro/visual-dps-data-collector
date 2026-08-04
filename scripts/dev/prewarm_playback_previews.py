#!/usr/bin/env python3
"""Prebuild local playback previews without changing source videos."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.record_service import (  # noqa: E402
    record_playback_frame_contract,
    video_path_for_record,
)
from config_loader import resolve_app_paths  # noqa: E402
from pose_store import iter_active_records  # noqa: E402
from video_transcode import build_preview_video_sync  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Prebuild derived fast-start playback previews")
    parser.add_argument("--pose-tier", default="rtmpose-m")
    parser.add_argument("--record-id", action="append", default=[])
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    requested = {str(value).strip() for value in args.record_id if str(value).strip()}
    records = iter_active_records(resolve_app_paths().json_dir, pose_tier=args.pose_tier)
    if requested:
        records = [locator for locator in records if locator.record_id in requested]

    failures = 0
    for index, locator in enumerate(records, start=1):
        source = video_path_for_record(locator.record_id)
        if source is None or not source.is_file():
            failures += 1
            print(f"[{index}/{len(records)}] missing video: {locator.record_id}")
            continue
        contract = record_playback_frame_contract(locator.record_id, source)
        if not contract.get("ok"):
            failures += 1
            print(
                f"[{index}/{len(records)}] frame contract failed: {locator.record_id}: "
                + json.dumps(contract, ensure_ascii=False)
            )
            continue
        result = build_preview_video_sync(source, force=args.force)
        print(
            f"[{index}/{len(records)}] {locator.record_id}: "
            + json.dumps(result, ensure_ascii=False, default=str)
        )
        if result.get("status") != "ready":
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
