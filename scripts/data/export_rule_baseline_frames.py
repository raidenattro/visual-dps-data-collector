#!/usr/bin/env python3
"""导出现场规则 baseline 逐帧 JSON（28 条优质样本，内存重算，不写 timeline）。

每 clip 一个 JSON 文件，帧字段含 rule_collisions / rule_alarm_collisions；
is_picking = 本帧是否有 rule 告警；picking_prob / predicted_box_tokens 暂空。

用法（项目根目录）:
  python scripts/data/export_rule_baseline_frames.py --dry-run
  python scripts/data/export_rule_baseline_frames.py
  python scripts/data/export_rule_baseline_frames.py \\
    --out-dir localdata/export/rule-baseline-prod \\
    --pose-frame-interval 2 --alarm-min 3 --cooldown 0
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


def _ensure_cv2_point_polygon_test() -> None:
    existing = sys.modules.get("cv2")
    if existing is not None and hasattr(existing, "pointPolygonTest"):
        return

    def _ray_point_in_contour(x: float, y: float, contour) -> bool:
        try:
            import numpy as np

            arr = np.asarray(contour, dtype=float)
            if arr.ndim == 3:
                arr = arr.reshape(-1, 2)
            elif arr.ndim == 2 and arr.shape[1] != 2:
                arr = arr.reshape(-1, 2)
            poly = [(float(px), float(py)) for px, py in arr]
        except Exception:
            poly = []
            for pt in contour or []:
                if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                    poly.append((float(pt[0]), float(pt[1])))
        if len(poly) < 3:
            return False
        inside = False
        n = len(poly)
        for i in range(n):
            x1, y1 = poly[i]
            x2, y2 = poly[(i + 1) % n]
            if ((y1 > y) != (y2 > y)) and (x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-12) + x1):
                inside = not inside
        return inside

    class _Cv2Shim:
        @staticmethod
        def pointPolygonTest(contour, pt, measure_dist):  # noqa: N802
            x, y = float(pt[0]), float(pt[1])
            return 1.0 if _ray_point_in_contour(x, y, contour) else -1.0

    sys.modules["cv2"] = _Cv2Shim()


_ensure_cv2_point_polygon_test()

from config_loader import parse_record_path_segments, resolve_app_paths, resolve_config_path
from event_engine.annotation_boxes import load_scaled_boxes
from event_engine.box_identity import box_id_from_token
from event_engine.collision_sim import (
    filter_pose_inference_frames,
    simulate_frame_events_from_frames,
    stored_pose_frame_interval,
)
from pose_store import load_all_frames, load_manifest

from api.accuracy_service import resolve_annotation_for_accuracy_record
from api.record_service import locate_record_by_id
from scripts.data.analyze_wrist_feature_discrimination import (
    DEFAULT_CAMERAS,
    DEFAULT_REVIEW_STATUS,
    DEFAULT_TAGS,
    DEFAULT_TIER,
    _collect_record_ids,
    _parse_csv_list,
    _parse_tags,
)


def _infer_size_from_frames(frames: list[dict[str, Any]], manifest: dict[str, Any]) -> tuple[int, int]:
    infer_w = int(manifest.get("infer_width") or 0)
    infer_h = int(manifest.get("infer_height") or 0)
    if infer_w > 0 and infer_h > 0:
        return infer_w, infer_h
    for fr in frames:
        if not isinstance(fr, dict):
            continue
        w = int(fr.get("infer_width") or 0)
        h = int(fr.get("infer_height") or 0)
        if w > 0 and h > 0:
            return w, h
    return 640, 480


def build_export_token_lookup(boxes: list[dict[str, Any]]) -> dict[str, str]:
    """box_id → 导出 token（优先 shelf_code:box_id）。"""
    lookup: dict[str, str] = {}
    for box in boxes:
        if not isinstance(box, dict):
            continue
        box_id = str(box.get("box_id") or box.get("id") or "").strip()
        if not box_id:
            continue
        shelf = str(box.get("shelf_code") or "").strip()
        lookup[box_id] = f"{shelf}:{box_id}" if shelf else f"Box_{box_id}"
    return lookup


def export_box_tokens(tokens: list[str], lookup: dict[str, str]) -> list[str]:
    """将 Box_{id} 转为导出格式并去重排序。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in tokens or []:
        box_id = box_id_from_token(str(raw).strip())
        if not box_id:
            continue
        text = lookup.get(box_id, f"Box_{box_id}")
        if text not in seen:
            seen.add(text)
            out.append(text)
    out.sort()
    return out


def export_record_frames(
    record_id: str,
    *,
    pose_frame_interval: int,
    alarm_min: int,
    alarm_cooldown: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    paths = resolve_app_paths()
    locator = locate_record_by_id(record_id)
    if not locator:
        raise FileNotFoundError(f"记录不存在: {record_id}")

    manifest = load_manifest(locator)
    all_frames = load_all_frames(locator)
    if not all_frames:
        raise ValueError(f"无骨架帧: {record_id}")

    ann_path = resolve_annotation_for_accuracy_record(paths, locator, pose_tier=DEFAULT_TIER)
    if not ann_path or not ann_path.is_file():
        raise FileNotFoundError(f"无标注 JSON: {record_id}")

    infer_w, infer_h = _infer_size_from_frames(all_frames, manifest)
    boxes = load_scaled_boxes(ann_path, infer_w, infer_h)
    if not boxes:
        raise ValueError(f"标注无有效货框: {record_id}")

    token_lookup = build_export_token_lookup(boxes)
    stored_interval = stored_pose_frame_interval(manifest)
    frames = filter_pose_inference_frames(
        all_frames,
        pose_frame_interval,
        stored_interval=stored_interval,
    )

    fps = float(manifest.get("fps") or 15.0)
    if fps <= 0:
        fps = 15.0

    events = simulate_frame_events_from_frames(
        frames,
        boxes,
        alarm_min_consecutive_frames=alarm_min,
        alarm_cooldown_frames=alarm_cooldown,
        video_fps=fps,
        probe_mode="wrist",
    )

    rows: list[dict[str, Any]] = []
    picking_frames = 0
    for ev in events:
        rule_collisions = export_box_tokens(ev.get("collisions") or [], token_lookup)
        rule_alarms = export_box_tokens(ev.get("alarm_collisions") or [], token_lookup)
        is_picking = bool(rule_alarms)
        if is_picking:
            picking_frames += 1
        rows.append({
            "record_id": record_id,
            "frame_idx": int(ev.get("frame_idx") or 0),
            "is_picking": is_picking,
            "picking_prob": None,
            "predicted_box_tokens": [],
            "rule_collisions": rule_collisions,
            "rule_alarm_collisions": rule_alarms,
        })

    _, slug, _ = parse_record_path_segments(record_id)
    meta = {
        "record_id": record_id,
        "clip_name": locator.path.name,
        "camera_slug": slug,
        "frame_count_all": len(all_frames),
        "frame_count_exported": len(rows),
        "stored_pose_frame_interval": stored_interval,
        "picking_frame_count": picking_frames,
        "annotation_file": ann_path.name,
    }
    return rows, meta


def main() -> int:
    parser = argparse.ArgumentParser(description="导出现场规则 baseline 逐帧 JSON")
    parser.add_argument("--tier", default=DEFAULT_TIER)
    parser.add_argument("--tags", default=",".join(DEFAULT_TAGS))
    parser.add_argument("--cameras", default=",".join(DEFAULT_CAMERAS))
    parser.add_argument("--pose-frame-interval", type=int, default=2)
    parser.add_argument("--alarm-min", type=int, default=3)
    parser.add_argument("--cooldown", type=int, default=0)
    parser.add_argument(
        "--out-dir",
        default=str(ROOT / "localdata" / "export" / "rule-baseline-prod"),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    resolve_config_path(None)
    tags = _parse_tags(args.tags)
    cameras = _parse_csv_list(args.cameras)
    record_ids = _collect_record_ids(
        tier=args.tier,
        cameras=set(cameras),
        tags=tags,
        review_status=DEFAULT_REVIEW_STATUS,
        has_verified=True,
    )

    if not record_ids:
        print("未找到匹配记录", file=sys.stderr)
        return 1

    params = {
        "baseline_type": "rule_production_config",
        "pose_tier": args.tier,
        "probe_mode": "wrist",
        "pose_frame_interval": int(args.pose_frame_interval),
        "alarm_min_consecutive_frames": int(args.alarm_min),
        "alarm_cooldown_frames": int(args.cooldown),
        "tags": tags,
        "cameras": cameras,
    }

    if args.dry_run:
        print(f"将导出 {len(record_ids)} 条记录 → {args.out_dir}")
        print(f"参数: {params}")
        for rid in record_ids:
            print(f"  {rid}")
        return 0

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    exported: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for rid in record_ids:
        try:
            rows, meta = export_record_frames(
                rid,
                pose_frame_interval=args.pose_frame_interval,
                alarm_min=args.alarm_min,
                alarm_cooldown=args.cooldown,
            )
            clip_name = meta["clip_name"]
            out_path = out_dir / f"{clip_name}.json"
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False, indent=2)
            exported.append({
                **meta,
                "file": out_path.name,
                "path": str(out_path.resolve()),
            })
            print(
                f"✓ {clip_name}: {meta['frame_count_exported']} 帧 "
                f"(picking {meta['picking_frame_count']})"
            )
        except (OSError, ValueError, FileNotFoundError) as exc:
            errors.append({"record_id": rid, "error": str(exc)})
            print(f"✗ {rid}: {exc}", file=sys.stderr)

    manifest = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "params": params,
        "record_count": len(record_ids),
        "exported_count": len(exported),
        "error_count": len(errors),
        "records": exported,
        "errors": errors,
    }
    manifest_path = out_dir / "_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n清单: {manifest_path}")
    print(f"完成: {len(exported)}/{len(record_ids)} 条")
    return 0 if exported else 1


if __name__ == "__main__":
    raise SystemExit(main())
