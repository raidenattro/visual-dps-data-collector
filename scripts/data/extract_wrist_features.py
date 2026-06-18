#!/usr/bin/env python3
"""提取手腕速度与碰撞段位移特征（无需重跑 RTMPose）。

输出（每条 v2 记录包内）：
  wrist_velocity.parquet   — 每帧 × 每人 × 左/右手腕速度
  wrist_box_segments.parquet — 每次手腕进/出货框的碰撞段（含进入/离开坐标与位移）

用法（项目根目录）:
  python scripts/data/extract_wrist_features.py
  python scripts/data/extract_wrist_features.py --tier rtmpose-m
  python scripts/data/extract_wrist_features.py --tier rtmpose-m --camera 2-7-2
  python scripts/data/extract_wrist_features.py --record rtmpose-m/2-7-2/clip_0001_...
  python scripts/data/extract_wrist_features.py --tier rtmpose-m --skip-existing
  python scripts/data/extract_wrist_features.py --tier rtmpose-m --export-dir localdata/features/export
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
from api.wrist_features_service import extract_wrist_features_for_record


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
    """将单条记录结果追加到汇总 JSONL（便于批处理查看）。"""
    export_dir.mkdir(parents=True, exist_ok=True)
    summary_path = export_dir / "extract_summary.jsonl"
    line = {
        "record_id": result.get("record_id"),
        "status": result.get("status"),
        "velocity_count": result.get("velocity_count"),
        "segment_count": result.get("segment_count"),
        "box_count": result.get("box_count"),
        "velocity_path": result.get("velocity_path"),
        "segments_path": result.get("segments_path"),
        "annotation": result.get("annotation"),
        "error": result.get("error"),
    }
    with open(summary_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def _merge_parquet_exports(export_dir: Path) -> None:
    """合并本次批处理产出的碰撞段 parquet。"""
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        print("跳过 parquet 合并：未安装 pyarrow", file=sys.stderr)
        return

    summary_path = export_dir / "extract_summary.jsonl"
    if not summary_path.is_file():
        return

    tables = []
    for line in summary_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("status") not in ("ok", "skipped"):
            continue
        seg_path = Path(str(row.get("segments_path") or ""))
        if not seg_path.is_file():
            continue
        t = pq.read_table(seg_path)
        df = t.to_pandas()
        df.insert(0, "record_id", str(row.get("record_id") or ""))
        tables.append(pa.Table.from_pandas(df, preserve_index=False))

    if not tables:
        return

    merged = pa.concat_tables(tables)
    out = export_dir / "all_wrist_box_segments.parquet"
    pq.write_table(merged, out, compression="zstd")
    print(f"已合并碰撞段: {out} ({merged.num_rows} 行)")


def main() -> int:
    parser = argparse.ArgumentParser(description="提取手腕速度与碰撞段特征（无需重跑模型）")
    parser.add_argument("--tier", default="", help="模型层，如 rtmpose-m")
    parser.add_argument("--camera", default="", help="机位 slug 过滤，如 2-7-2")
    parser.add_argument("--record", default="", help="仅处理指定 record_id")
    parser.add_argument("--skip-existing", action="store_true", help="已有特征文件则跳过")
    parser.add_argument("--max-gap-frames", type=int, default=1, help="碰撞段合并允许的最大间隙帧数")
    parser.add_argument("--dry-run", action="store_true", help="只列出将处理的记录")
    parser.add_argument(
        "--export-dir",
        default="",
        help="写入批处理汇总 JSONL，并尝试合并 all_wrist_box_segments.parquet",
    )
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
        summary = export_dir / "extract_summary.jsonl"
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
            result = extract_wrist_features_for_record(
                locator,
                max_gap_frames=args.max_gap_frames,
                skip_if_exists=args.skip_existing,
            )
            st = result.get("status")
            if st == "skipped":
                skip += 1
                print(f"{rid}: skip 已存在特征文件")
            else:
                ok += 1
                print(
                    f"{rid}: ok 速度行 {result.get('velocity_count')} "
                    f"碰撞段 {result.get('segment_count')} "
                    f"货框 {result.get('box_count')}"
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
        _merge_parquet_exports(export_dir)
        print(f"汇总: {export_dir / 'extract_summary.jsonl'}")

    return 0 if fail == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
