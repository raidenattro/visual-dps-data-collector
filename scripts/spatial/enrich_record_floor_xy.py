#!/usr/bin/env python3
"""对已有 v2 记录从 skeleton 踝点重算 floor_xy，写入 floor_foot.parquet sidecar。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config_loader import load_config_file, resolve_app_paths, spatial_enabled
from floor_foot_store import (
    FLOOR_FOOT_FILE,
    floor_foot_rows_from_frames,
    rewrite_timeline_without_floor,
    write_floor_foot_parquet,
)
from pose_store import (
    TIMELINE_FILE,
    locate_record,
    _timeline_row_from_frame,
    _assemble_frames_from_tables,
)
from spatial_pose.calibration import load_calibration
from spatial_pose.floor_projection import FloorSmoothState, pick_primary_person, project_foot_for_frame


def _require_pyarrow():
    try:
        import pyarrow as pa  # noqa: F401
        import pyarrow.parquet as pq  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("缺少 pyarrow") from exc
    import pyarrow as pa
    import pyarrow.parquet as pq

    return pa, pq


def enrich_record(record_dir: Path, *, spatial_dir: Path, force: bool = False) -> str:
    record_dir = record_dir.resolve()
    manifest_path = record_dir / "manifest.json"
    if not manifest_path.is_file():
        return "skip: 无 manifest"
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    camera_slug = str(manifest.get("camera_slug") or "").strip()
    if not camera_slug:
        parts = record_dir.parts
        if len(parts) >= 2:
            camera_slug = parts[-2]
    if not camera_slug:
        return "skip: 无 camera_slug"

    infer_w = int(manifest.get("infer_width") or 0)
    infer_h = int(manifest.get("infer_height") or 0)
    if infer_w <= 0 or infer_h <= 0:
        infer_w = int(manifest.get("annotation", {}).get("annotation_size", {}).get("width") or 0)
        infer_h = int(manifest.get("annotation", {}).get("annotation_size", {}).get("height") or 0)
    if infer_w <= 0 or infer_h <= 0:
        return "skip: 无 infer 尺寸"

    cal = load_calibration(
        spatial_dir,
        camera_slug,
        infer_width=infer_w,
        infer_height=infer_h,
        require_enabled=True,
    )
    if cal is None:
        return f"skip: 无 spatial 标定 ({camera_slug})"

    timeline_path = record_dir / TIMELINE_FILE
    skeleton_path = record_dir / "skeleton.parquet"
    if not timeline_path.is_file() or not skeleton_path.is_file():
        return "skip: 缺少 parquet"

    pa, pq = _require_pyarrow()
    timeline_rows = pq.read_table(timeline_path).to_pylist()
    skeleton_rows = pq.read_table(skeleton_path).to_pylist()
    frames = _assemble_frames_from_tables(timeline_rows, skeleton_rows, floor_by_frame={})

    smooth = FloorSmoothState.from_calibration(cal)
    updated = 0
    for frame in frames:
        floor = project_foot_for_frame(cal, frame.get("persons") or [], smooth)
        frame.pop("foot_uv_px", None)
        frame.pop("raw_floor_xy_m", None)
        frame.pop("floor_xy_m", None)
        frame.pop("foot_person_id", None)
        frame.pop("foot_person_track_id", None)
        if floor.foot_uv_px is not None:
            frame["foot_uv_px"] = floor.foot_uv_px
        if floor.raw_floor_xy_m is not None:
            frame["raw_floor_xy_m"] = floor.raw_floor_xy_m
        if floor.floor_xy_m is not None:
            frame["floor_xy_m"] = floor.floor_xy_m
        person = pick_primary_person(frame.get("persons") or [])
        if person is not None:
            frame["foot_person_id"] = int(person.get("person_id") if person.get("person_id") is not None else -1)
            if person.get("person_track_id") is not None:
                frame["foot_person_track_id"] = int(person["person_track_id"])
        if floor.floor_xy_m or floor.foot_uv_px:
            updated += 1

    floor_rows = floor_foot_rows_from_frames(frames)
    write_floor_foot_parquet(record_dir, floor_rows)

    clean_timeline = [_timeline_row_from_frame(fr) for fr in frames]
    pq.write_table(pa.Table.from_pylist(clean_timeline), timeline_path, compression="zstd")
    rewrite_timeline_without_floor(record_dir)

    manifest["spatial"] = cal.manifest_summary()
    files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    files["floor_foot"] = FLOOR_FOOT_FILE
    manifest["files"] = files
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return f"ok: {record_dir.name} floor_foot={len(floor_rows)} updated_frames={updated} rmse={cal.ground_control_rmse_px:.2f}px"


def main() -> None:
    parser = argparse.ArgumentParser(description="离线补算 floor_xy 到 floor_foot.parquet")
    parser.add_argument("target", help="record 目录或 record_id")
    parser.add_argument("--json-dir", default="", help="覆盖 json_dir")
    args = parser.parse_args()

    if not spatial_enabled():
        print("警告: config spatial.enabled=false，仍将尝试补算")

    cfg = load_config_file()
    paths = resolve_app_paths(cfg)
    if args.json_dir:
        paths = resolve_app_paths({**cfg, "paths": {**cfg.get("paths", {}), "json_dir": args.json_dir}})

    target = Path(args.target)
    if target.is_dir():
        print(enrich_record(target, spatial_dir=paths.spatial_dir))
        return

    locator = locate_record(paths.json_dir, str(args.target))
    if not locator:
        raise SystemExit(f"找不到记录: {args.target}")
    print(enrich_record(locator.path, spatial_dir=paths.spatial_dir))


if __name__ == "__main__":
    main()
