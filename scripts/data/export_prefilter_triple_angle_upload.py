#!/usr/bin/env python3
"""导出速度门控 + 三重角度 AND 豁免前置过滤包。

默认：knee_ankle_mean_speed > 65 + arm_torso≥90 AND elbow≥150 AND wrist_elev≥60
可切换为 ankle_max_speed@80 等特征。

参数对齐 manifest：pose_frame_interval=2, alarm_min=3, cooldown=0。

用法（项目根目录）:
  python scripts/data/export_prefilter_triple_angle_upload.py
  python scripts/data/export_prefilter_triple_angle_upload.py \\
    --speed-feature ankle_max_speed --speed-threshold 80 \\
    --stance-feature torso_leg_angle_mean --stance-threshold 160 \\
    --output-dir localdata/export/rule-speed-prefilter-ankle-max80-triple90-torso160-prod-test
  python scripts/data/evaluate_inference_upload.py \\
    --dirs localdata/export/rule-speed-prefilter-knee65-triple90-prod-test --in-place
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
from event_engine.skeleton_angles import extract_subsampled_joint_angle_from_frames
from event_engine.skeleton_features import extract_subsampled_velocity_from_frames
from scripts.data.upload_export_common import (
    ALARM_COOLDOWN,
    ALARM_MIN_CONSECUTIVE,
    MANIFEST_NAME,
    POSE_FRAME_INTERVAL,
    build_output_manifest,
    load_baseline_manifest,
    process_record_upload_export,
)
from scripts.data.validate_prefilter_joint_angle28 import _recompute_with_row_gate
from scripts.data.validate_prefilter_multi_angle_logic28 import _multi_logic_gate_blocks

DEFAULT_BASELINE_MANIFEST = ROOT / "localdata/export/rule-baseline-local-prod-test/_manifest.json"
DEFAULT_OUTPUT = ROOT / "localdata/export/rule-speed-prefilter-knee65-triple90-prod-test"

DEFAULT_SPEED_FEATURE = "knee_ankle_mean_speed"
DEFAULT_SPEED_THRESHOLD = 65.0
TRIPLE_AND_CONDS: list[tuple[str, float]] = [
    ("arm_torso_angle_max", 90.0),
    ("elbow_angle_mean", 150.0),
    ("wrist_elevation_angle_max", 60.0),
]

# 站立姿态（肩-髋-踝整体夹角）：仅站立时速度门控可 block
DEFAULT_STANCE_FEATURE = "torso_leg_angle_mean"
DEFAULT_STANCE_THRESHOLD = 160.0
# ankle_max@80 + triple90 推荐导出目录后缀
ANKLE_TRIPLE90_STANCE_OUTPUT = (
    ROOT / "localdata/export/rule-speed-prefilter-ankle-max80-triple90-torso160-prod-test"
)


def _is_standing_row(row: dict[str, Any], *, stance_feat: str, stance_thr: float) -> bool:
    from scripts.data.analyze_skeleton_velocity_discrimination import _float_or_none

    v = _float_or_none(row.get(stance_feat))
    if v is None:
        return True
    return v >= stance_thr


def _triple_stance_gate_blocks(
    row: dict[str, Any],
    *,
    speed_feature: str,
    speed_threshold: float,
    triple_conds: list[tuple[str, float]],
    stance_feature: str,
    stance_threshold: float,
) -> bool:
    if not _multi_logic_gate_blocks(
        row,
        speed_feature=speed_feature,
        speed_thr=speed_threshold,
        conds=triple_conds,
        logic="and",
    ):
        return False
    if stance_feature:
        if not _is_standing_row(row, stance_feat=stance_feature, stance_thr=stance_threshold):
            return False
    return True


def _merge_velocity_angle_rows(
    velocity_rows: list[dict[str, Any]],
    angle_rows: list[dict[str, Any]],
) -> dict[tuple[int, int], dict[str, Any]]:
    merged: dict[tuple[int, int], dict[str, Any]] = {}
    for row in velocity_rows:
        key = (int(row.get("frame_idx") or 0), int(row.get("person_track_id") or 0))
        merged[key] = dict(row)
    for row in angle_rows:
        key = (int(row.get("frame_idx") or 0), int(row.get("person_track_id") or 0))
        merged.setdefault(key, {})
        merged[key].update(row)
    return merged


def recompute_triple_and_prefilter_upload_frames(
    timeline_frames: list[dict[str, Any]],
    export_frame_indices: set[int] | list[int],
    boxes: list[dict[str, Any]],
    *,
    record_id: str,
    infer_width: int,
    infer_height: int,
    video_fps: float = 25.0,
    alarm_min_consecutive_frames: int = 3,
    alarm_cooldown_frames: int = 0,
    speed_feature: str = DEFAULT_SPEED_FEATURE,
    speed_threshold: float = DEFAULT_SPEED_THRESHOLD,
    stance_feature: str = "",
    stance_threshold: float = DEFAULT_STANCE_THRESHOLD,
    **_extra: Any,
) -> list[dict[str, Any]]:
    """速度门控 + 三重角度 AND 豁免；可选站立姿态约束。"""
    velocity_rows = extract_subsampled_velocity_from_frames(
        timeline_frames,
        export_frame_indices,
        infer_width=infer_width,
        infer_height=infer_height,
        video_fps=video_fps,
    )
    angle_rows = extract_subsampled_joint_angle_from_frames(
        timeline_frames,
        export_frame_indices,
        video_fps=video_fps,
    )
    ctx = {
        "timeline_frames": timeline_frames,
        "export_indices": export_frame_indices,
        "boxes": boxes,
        "record_id": record_id,
        "merged_rows": _merge_velocity_angle_rows(velocity_rows, angle_rows),
        "fps": video_fps,
    }
    return _recompute_with_row_gate(
        ctx,
        gate_fn=lambda row: _triple_stance_gate_blocks(
            row,
            speed_feature=speed_feature,
            speed_threshold=speed_threshold,
            triple_conds=TRIPLE_AND_CONDS,
            stance_feature=str(stance_feature or "").strip(),
            stance_threshold=float(stance_threshold),
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="速度门控 + 三重角度 AND 前置过滤导出")
    parser.add_argument("--baseline-manifest", type=Path, default=DEFAULT_BASELINE_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--speed-feature", default=DEFAULT_SPEED_FEATURE)
    parser.add_argument("--speed-threshold", type=float, default=DEFAULT_SPEED_THRESHOLD)
    parser.add_argument(
        "--stance-feature",
        default=DEFAULT_STANCE_FEATURE,
        help="站立判定特征（如 knee_angle_mean）；空则禁用姿态豁免",
    )
    parser.add_argument(
        "--stance-threshold",
        type=float,
        default=DEFAULT_STANCE_THRESHOLD,
        help="站立阈：特征 >= 阈视为站立，门控可 block",
    )
    parser.add_argument("--pose-frame-interval", type=int, default=POSE_FRAME_INTERVAL)
    parser.add_argument("--alarm-min", type=int, default=ALARM_MIN_CONSECUTIVE)
    parser.add_argument("--alarm-cooldown", type=int, default=ALARM_COOLDOWN)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    speed_feature = str(args.speed_feature).strip()
    speed_threshold = float(args.speed_threshold)

    resolve_config_path(None)
    baseline_manifest_path = args.baseline_manifest.resolve()
    baseline_dir = baseline_manifest_path.parent
    output_dir = args.output_dir.resolve()

    baseline_manifest = load_baseline_manifest(baseline_manifest_path)
    records = list(baseline_manifest.get("records") or [])
    if not records:
        print("baseline manifest 无 records")
        return 2

    rule_label = (
        f"{speed_feature}≤{speed_threshold} + "
        f"arm_torso≥{TRIPLE_AND_CONDS[0][1]:.0f} AND "
        f"elbow≥{TRIPLE_AND_CONDS[1][1]:.0f} AND "
        f"wrist_elev≥{TRIPLE_AND_CONDS[2][1]:.0f}"
    )
    stance_feature = str(args.stance_feature or "").strip()
    stance_threshold = float(args.stance_threshold)
    if stance_feature:
        rule_label += f" + standing({stance_feature}≥{stance_threshold:.0f})"

    if args.dry_run:
        print(f"将处理 {len(records)} 条记录")
        print(f"输出: {output_dir}")
        print(f"规则: {rule_label}")
        return 0

    recompute_kwargs = {
        "alarm_min_consecutive_frames": args.alarm_min,
        "alarm_cooldown_frames": args.alarm_cooldown,
        "speed_feature": speed_feature,
        "speed_threshold": speed_threshold,
        "stance_feature": stance_feature,
        "stance_threshold": stance_threshold,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for entry in records:
        rid = str(entry.get("record_id") or "")
        res = process_record_upload_export(
            entry,
            baseline_dir=baseline_dir,
            output_dir=output_dir,
            recompute_fn=recompute_triple_and_prefilter_upload_frames,
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
                "feature": speed_feature,
                "max_threshold": speed_threshold,
                "fail_open": True,
                "writeback_timeline": False,
                "angle_exempt": {
                    "logic": "and",
                    "conditions": [
                        {"feature": f, "min_threshold": t} for f, t in TRIPLE_AND_CONDS
                    ],
                },
                **(
                    {
                        "stance_required": {
                            "feature": stance_feature,
                            "min_threshold": stance_threshold,
                        },
                    }
                    if stance_feature
                    else {}
                ),
            },
        },
    )
    (output_dir / MANIFEST_NAME).write_text(
        json.dumps(out_manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    ok_n = out_manifest.get("exported_count", 0)
    err_n = out_manifest.get("error_count", 0)
    print(f"\n完成: {ok_n}/{len(records)} ok, {err_n} errors")
    print(f"规则: {rule_label}")
    print(f"输出: {output_dir}")
    return 0 if err_n == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
