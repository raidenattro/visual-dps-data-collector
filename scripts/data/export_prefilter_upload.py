#!/usr/bin/env python3
"""从记录包 timeline 只读重算前置速度过滤碰撞，导出上传评估目录。

不写回记录包；唯一输出 localdata/export/rule-speed-prefilter-prod-test/

用法（项目根目录）:
  python scripts/data/export_prefilter_upload.py
  python scripts/data/export_prefilter_upload.py --threshold 50
  python scripts/data/evaluate_inference_upload.py \\
    --dirs localdata/export/rule-baseline-local-prod-test \\
             localdata/export/rule-speed-prefilter-prod-test --in-place
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.data.evaluate_combo1_segment_filter import _ensure_cv2_point_polygon_test

_ensure_cv2_point_polygon_test()

from config_loader import resolve_config_path
from event_engine.speed_gated_collision import SpeedGateConfig, recompute_prefilter_upload_frames
from scripts.data.upload_export_common import (
    ALARM_COOLDOWN,
    ALARM_MIN_CONSECUTIVE,
    MANIFEST_NAME,
    POSE_FRAME_INTERVAL,
    build_output_manifest,
    load_baseline_manifest,
    process_record_upload_export,
)

DEFAULT_BASELINE_MANIFEST = ROOT / "localdata/export/rule-baseline-prod-test/_manifest.json"
DEFAULT_OUTPUT = ROOT / "localdata/export/rule-speed-prefilter-prod-test"
DEFAULT_THRESHOLD = 60.0


def main() -> int:
    parser = argparse.ArgumentParser(description="前置速度过滤：只读 timeline 重算并导出 upload 目录")
    parser.add_argument("--baseline-manifest", type=Path, default=DEFAULT_BASELINE_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--feature", default="lower_mean_speed")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--pose-frame-interval", type=int, default=POSE_FRAME_INTERVAL)
    parser.add_argument("--alarm-min", type=int, default=ALARM_MIN_CONSECUTIVE)
    parser.add_argument("--alarm-cooldown", type=int, default=ALARM_COOLDOWN)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    resolve_config_path(None)
    baseline_manifest_path = args.baseline_manifest.resolve()
    baseline_dir = baseline_manifest_path.parent
    output_dir = args.output_dir.resolve()

    baseline_manifest = load_baseline_manifest(baseline_manifest_path)
    records = list(baseline_manifest.get("records") or [])
    if not records:
        print("baseline manifest 无 records")
        return 2

    speed_gate = SpeedGateConfig(
        feature=args.feature,
        max_threshold=args.threshold,
        fail_open=True,
    )

    recompute_kwargs = {
        "speed_gate": speed_gate,
        "alarm_min_consecutive_frames": args.alarm_min,
        "alarm_cooldown_frames": args.alarm_cooldown,
    }

    if args.dry_run:
        print(f"将处理 {len(records)} 条记录（只读 timeline，不写回记录包）")
        print(f"输出: {output_dir}")
        print(f"规则: 帧级 {speed_gate.feature} ≤ {speed_gate.max_threshold}")
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for entry in records:
        rid = str(entry.get("record_id") or "")
        res = process_record_upload_export(
            entry,
            baseline_dir=baseline_dir,
            output_dir=output_dir,
            recompute_fn=recompute_prefilter_upload_frames,
            recompute_kwargs=recompute_kwargs,
            pose_frame_interval=args.pose_frame_interval,
        )
        results.append(res)
        if res.get("status") == "ok":
            print(f"{rid}: exported={res.get('frame_count_exported')} picking={res.get('picking_frame_count')}")
        else:
            print(f"{rid}: ERROR {res.get('error')}")

    out_manifest = build_output_manifest(
        baseline_manifest,
        baseline_dir=baseline_dir,
        results=results,
        params_patch={
            "collision_engine": "speed_gated_box_human_det_infer",
            "pose_frame_interval": args.pose_frame_interval,
            "alarm_min_consecutive_frames": args.alarm_min,
            "alarm_cooldown_frames": args.alarm_cooldown,
            "speed_filter": {
                "enabled": True,
                "stage": "prefilter",
                "feature": speed_gate.feature,
                "max_threshold": speed_gate.max_threshold,
                "fail_open": speed_gate.fail_open,
                "writeback_timeline": False,
            },
        },
    )
    (output_dir / MANIFEST_NAME).write_text(
        json.dumps(out_manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    ok_n = out_manifest.get("exported_count", 0)
    err_n = out_manifest.get("error_count", 0)
    summary = out_manifest.get("summary") or {}
    print(
        f"\n完成: {ok_n}/{len(records)} ok, {err_n} errors\n"
        f"picking帧合计: {summary.get('picking_frames')}\n"
        f"输出: {output_dir}"
    )
    return 0 if err_n == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
