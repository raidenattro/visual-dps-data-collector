#!/usr/bin/env python3
"""本仓库 CollisionProcessor 重算 baseline，导出上传评估目录。

不写回记录包；输出 localdata/export/rule-baseline-local-prod-test/
与外部 rule-baseline-prod-test 参数对齐（interval=2, alarm_min=3, cooldown=0），
便于与前置/后置过滤在同引擎下对比。

用法（项目根目录）:
  python scripts/data/export_baseline_upload.py
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
from event_engine.speed_gated_collision import recompute_baseline_upload_frames
from scripts.data.upload_export_common import (
    ALARM_COOLDOWN,
    ALARM_MIN_CONSECUTIVE,
    MANIFEST_NAME,
    POSE_FRAME_INTERVAL,
    build_output_manifest,
    load_baseline_manifest,
    process_record_upload_export,
)

DEFAULT_REF_MANIFEST = ROOT / "localdata/export/rule-baseline-prod-test/_manifest.json"
DEFAULT_OUTPUT = ROOT / "localdata/export/rule-baseline-local-prod-test"


def main() -> int:
    parser = argparse.ArgumentParser(description="本仓库 baseline 碰撞重算并导出 upload 目录")
    parser.add_argument("--ref-manifest", type=Path, default=DEFAULT_REF_MANIFEST,
                        help="参考 manifest（取 record 列表与抽帧 frame_idx）")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--pose-frame-interval", type=int, default=POSE_FRAME_INTERVAL)
    parser.add_argument("--alarm-min", type=int, default=ALARM_MIN_CONSECUTIVE)
    parser.add_argument("--alarm-cooldown", type=int, default=ALARM_COOLDOWN)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    resolve_config_path(None)
    ref_manifest_path = args.ref_manifest.resolve()
    ref_dir = ref_manifest_path.parent
    output_dir = args.output_dir.resolve()

    ref_manifest = load_baseline_manifest(ref_manifest_path)
    records = list(ref_manifest.get("records") or [])
    if not records:
        print("参考 manifest 无 records")
        return 2

    recompute_kwargs = {
        "alarm_min_consecutive_frames": args.alarm_min,
        "alarm_cooldown_frames": args.alarm_cooldown,
    }

    if args.dry_run:
        print(f"将处理 {len(records)} 条记录（只读 timeline，不写回记录包）")
        print(f"输出: {output_dir}")
        print(f"引擎: LocalBaselineCollisionProcessor cooldown={args.alarm_cooldown}")
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for entry in records:
        rid = str(entry.get("record_id") or "")
        res = process_record_upload_export(
            entry,
            baseline_dir=ref_dir,
            output_dir=output_dir,
            recompute_fn=recompute_baseline_upload_frames,
            recompute_kwargs=recompute_kwargs,
            pose_frame_interval=args.pose_frame_interval,
        )
        results.append(res)
        if res.get("status") == "ok":
            print(f"{rid}: exported={res.get('frame_count_exported')} picking={res.get('picking_frame_count')}")
        else:
            print(f"{rid}: ERROR {res.get('error')}")

    out_manifest = build_output_manifest(
        ref_manifest,
        baseline_dir=ref_dir,
        results=results,
        params_patch={
            "baseline_type": "rule_local_recompute",
            "collision_engine": "local_collision_processor",
            "pose_frame_interval": args.pose_frame_interval,
            "alarm_min_consecutive_frames": args.alarm_min,
            "alarm_cooldown_frames": args.alarm_cooldown,
            "writeback_timeline": False,
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
