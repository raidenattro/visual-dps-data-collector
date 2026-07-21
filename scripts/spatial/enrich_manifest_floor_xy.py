#!/usr/bin/env python3
"""按 upload manifest 批量补算 floor_xy，并可选回写 export JSON。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import importlib.util

from config_loader import load_config_file, resolve_app_paths
from floor_foot_store import load_floor_foot_rows
from pose_store import locate_record

_ENRICH_MOD_PATH = ROOT / "scripts" / "spatial" / "enrich_record_floor_xy.py"
_spec = importlib.util.spec_from_file_location("enrich_record_floor_xy", _ENRICH_MOD_PATH)
if _spec is None or _spec.loader is None:
    raise RuntimeError(f"无法加载 {_ENRICH_MOD_PATH}")
_enrich_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_enrich_mod)
enrich_record = _enrich_mod.enrich_record


def _load_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _floor_by_frame_idx(record_dir: Path) -> dict[int, dict[str, Any]]:
    rows = load_floor_foot_rows(record_dir, allow_legacy_timeline=True)
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        fid = int(row.get("frame_idx") or 0)
        if fid <= 0:
            continue
        item: dict[str, Any] = {}
        if row.get("floor_x_m") is not None and row.get("floor_y_m") is not None:
            item["floor_x_m"] = float(row["floor_x_m"])
            item["floor_y_m"] = float(row["floor_y_m"])
        if row.get("raw_floor_x_m") is not None and row.get("raw_floor_y_m") is not None:
            item["raw_floor_x_m"] = float(row["raw_floor_x_m"])
            item["raw_floor_y_m"] = float(row["raw_floor_y_m"])
        if row.get("foot_u_px") is not None and row.get("foot_v_px") is not None:
            item["foot_u_px"] = float(row["foot_u_px"])
            item["foot_v_px"] = float(row["foot_v_px"])
        if item:
            out[fid] = item
    return out


def _patch_export_json(export_path: Path, floor_map: dict[int, dict[str, Any]]) -> tuple[int, int]:
    if not export_path.is_file() or not floor_map:
        return 0, 0
    rows = json.loads(export_path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        return 0, 0
    patched = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        fid = int(row.get("frame_idx") or 0)
        hit = floor_map.get(fid)
        if not hit:
            continue
        row.update(hit)
        patched += 1
    export_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return patched, len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="按 manifest 批量补算 floor_xy")
    parser.add_argument(
        "--manifest",
        default="localdata/export/rule-baseline-local-prod-test/_manifest.json",
        help="upload manifest（含 records[].record_id / file / path）",
    )
    parser.add_argument(
        "--export-dir",
        default="",
        help="export 目录（默认 manifest 所在目录）；补算成功后回写 clip JSON",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    manifest_path = (ROOT / args.manifest).resolve() if not Path(args.manifest).is_absolute() else Path(args.manifest)
    if not manifest_path.is_file():
        raise SystemExit(f"manifest 不存在: {manifest_path}")

    export_dir = Path(args.export_dir).resolve() if args.export_dir else manifest_path.parent
    manifest = _load_manifest(manifest_path)
    records = list(manifest.get("records") or [])
    if not records:
        print("manifest 无 records")
        return 2

    cfg = load_config_file()
    paths = resolve_app_paths(cfg)
    spatial_dir = paths.spatial_dir

    ok = skip = err = 0
    export_patched_total = 0
    for entry in records:
        rid = str(entry.get("record_id") or "").strip()
        if not rid:
            skip += 1
            print("skip: 空 record_id")
            continue
        locator = locate_record(paths.json_dir, rid)
        if not locator:
            err += 1
            print(f"ERROR {rid}: 本地记录不存在")
            continue
        if args.dry_run:
            slug = str(entry.get("camera_slug") or "").strip()
            cal_path = spatial_dir / f"{slug}.json"
            print(f"DRY {rid} camera={slug} calib={cal_path.is_file()}")
            continue

        msg = enrich_record(locator.path, spatial_dir=spatial_dir)
        print(f"{rid}: {msg}")
        if msg.startswith("ok:"):
            ok += 1
            export_path = export_dir / str(entry.get("file") or "")
            if not export_path.is_file():
                path_field = str(entry.get("path") or "").strip()
                if path_field:
                    export_path = Path(path_field)
            floor_map = _floor_by_frame_idx(locator.path)
            patched, total = _patch_export_json(export_path, floor_map)
            if patched:
                print(f"  export patched {patched}/{total} -> {export_path.name}")
                export_patched_total += patched
        else:
            skip += 1

    print(
        f"\n完成: ok={ok} skip={skip} err={err} total={len(records)} "
        f"export_floor_rows={export_patched_total}"
    )
    return 0 if err == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
