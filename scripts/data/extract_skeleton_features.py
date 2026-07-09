#!/usr/bin/env python3
"""提取全骨骼速度特征与碰撞段运动统计（无需重跑 RTMPose）。

输出（每条 v2 记录包内）：
  skeleton_velocity.parquet        — 每帧 × 每人：17 点速度 + 躯干/全身聚合
  skeleton_motion_segments.parquet   — 碰撞段 + 段内躯干/下肢运动统计

用法（项目根目录）:
  python scripts/data/extract_skeleton_features.py
  python scripts/data/extract_skeleton_features.py --tier rtmpose-m
  python scripts/data/extract_skeleton_features.py --tier rtmpose-m --camera 2-7-2
  python scripts/data/extract_skeleton_features.py --record rtmpose-m/2-7-2/clip_0001_...
  python scripts/data/extract_skeleton_features.py --tier rtmpose-m --skip-existing
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config_loader import parse_record_path_segments, resolve_app_paths, resolve_config_path
from pose_store import iter_active_records, meta_sidecar_path

from api.record_service import locate_record_by_id
from api.skeleton_features_service import extract_skeleton_features_for_record


def _record_matches_filters(
    record_id: str,
    *,
    tier: str,
    camera: str,
) -> bool:
    parsed_tier, parsed_slug, _ = parse_record_path_segments(record_id)
    if tier and (parsed_tier or "") != tier:
        return False
    if camera:
        cam = camera.strip()
        if parsed_slug == cam:
            return True
        sidecar = meta_sidecar_path(resolve_app_paths().json_dir, record_id)
        if sidecar.is_file():
            try:
                meta = json.loads(sidecar.read_text(encoding="utf-8"))
                if str(meta.get("camera_slug") or "") == cam:
                    return True
            except json.JSONDecodeError:
                pass
        return False
    return True


def _collect_targets(args: argparse.Namespace) -> list[str]:
    paths = resolve_app_paths()
    if args.record:
        rid = str(args.record).strip().replace("\\", "/")
        loc = locate_record_by_id(rid)
        if not loc:
            print(f"记录不存在: {rid}", file=sys.stderr)
            return []
        return [rid]

    targets: list[str] = []
    for loc in iter_active_records(paths.json_dir, pose_tier=args.tier or None):
        if _record_matches_filters(loc.record_id, tier=args.tier or "", camera=args.camera or ""):
            targets.append(loc.record_id)
    return sorted(targets)


def _append_export_rows(export_dir: Path, result: dict) -> None:
    export_dir.mkdir(parents=True, exist_ok=True)
    summary_path = export_dir / "extract_skeleton_summary.jsonl"
    line = {
        "record_id": result.get("record_id"),
        "status": result.get("status"),
        "velocity_count": result.get("velocity_count"),
        "motion_segment_count": result.get("motion_segment_count"),
        "segment_count": result.get("segment_count"),
        "box_count": result.get("box_count"),
        "velocity_path": result.get("velocity_path"),
        "motion_segments_path": result.get("motion_segments_path"),
        "annotation": result.get("annotation"),
        "annotation_source": result.get("annotation_source"),
        "error": result.get("error"),
    }
    with open(summary_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="提取全骨骼速度特征（无需重跑模型）")
    parser.add_argument("--tier", default="", help="模型层，如 rtmpose-m")
    parser.add_argument("--camera", default="", help="机位 slug 过滤，如 2-7-2")
    parser.add_argument("--record", default="", help="仅处理指定 record_id")
    parser.add_argument("--skip-existing", action="store_true", help="已有特征文件则跳过")
    parser.add_argument("--max-gap-frames", type=int, default=1, help="碰撞段合并允许的最大间隙帧数")
    parser.add_argument("--aggregate-only", action="store_true", help="仅写聚合列（不含 vx/vy 明细）")
    parser.add_argument("--dry-run", action="store_true", help="只列出将处理的记录")
    parser.add_argument("--export-dir", default="", help="写入批处理汇总 JSONL")
    args = parser.parse_args()

    resolve_config_path(None)
    targets = _collect_targets(args)
    if not targets:
        print("未找到匹配记录")
        return 1

    if args.dry_run:
        print(f"将处理 {len(targets)} 条记录:")
        for rid in targets:
            print(f"  {rid}")
        return 0

    export_dir = Path(args.export_dir).resolve() if args.export_dir else None
    if export_dir:
        summary = export_dir / "extract_skeleton_summary.jsonl"
        if summary.is_file():
            summary.unlink()

    ok = skip = fail = 0
    for rid in targets:
        locator = locate_record_by_id(rid)
        if not locator:
            print(f"{rid}: fail 记录不存在")
            fail += 1
            continue
        try:
            result = extract_skeleton_features_for_record(
                locator,
                max_gap_frames=args.max_gap_frames,
                skip_if_exists=args.skip_existing,
                include_keypoint_detail=not args.aggregate_only,
            )
            st = result.get("status")
            if st == "skipped":
                skip += 1
                print(f"{rid}: skip 已存在特征文件")
            else:
                ok += 1
                print(
                    f"{rid}: ok 速度行 {result.get('velocity_count')} "
                    f"运动段 {result.get('motion_segment_count')} "
                    f"货框 {result.get('box_count')} "
                    f"标注 {result.get('annotation_source') or result.get('annotation') or '—'}"
                )
            if export_dir:
                _append_export_rows(export_dir, result)
        except Exception as exc:
            fail += 1
            err_result = {"record_id": rid, "status": "error", "error": str(exc)}
            print(f"{rid}: fail {exc}")
            if export_dir:
                _append_export_rows(export_dir, err_result)

    print(f"\n完成：成功 {ok}，跳过 {skip}，失败 {fail}，共 {len(targets)} 条")
    if export_dir:
        print(f"汇总: {export_dir / 'extract_skeleton_summary.jsonl'}")

    return 0 if fail == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
