#!/usr/bin/env python3
"""仅用 81 货架单标注重算 clip_0013，与 output.jsonl 对比（不合并 reflection）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _ensure_cv2_shim() -> None:
    existing = sys.modules.get("cv2")
    if existing is not None and hasattr(existing, "pointPolygonTest"):
        return

    def _ray_point_in_contour(x: float, y: float, contour) -> bool:
        import numpy as np

        arr = np.asarray(contour, dtype=float)
        if arr.ndim == 3:
            arr = arr.reshape(-1, 2)
        poly = [(float(px), float(py)) for px, py in arr]
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


def main() -> int:
    _ensure_cv2_shim()

    from config_loader import resolve_config_path
    from event_engine.annotation_boxes import load_scaled_boxes_from_config
    from event_engine.box_identity import box_id_from_token, parse_collision_token
    from event_engine.collision_sim import simulate_frame_events_infer_collision
    from pose_store import load_all_frames, load_manifest
    from api.record_service import locate_record_by_id

    resolve_config_path(None)

    record_id = "rtmpose-m/1-1-1/clip_0013_start_00-11-22_rtmpose_m"
    ann_path = Path(r"D:\work\workspace\git-repo\ShelfPickSense\data\data28\Train\record_001\annotation.json")
    jsonl_path = Path(r"D:\work\workspace\git-repo\ShelfPickSense\outputs\output.jsonl")
    export_path = ROOT / "localdata/export/rule-baseline-prod-test/clip_0013_start_00-11-22_rtmpose_m.json"

    locator = locate_record_by_id(record_id)
    if not locator:
        print(f"记录不存在: {record_id}", file=sys.stderr)
        return 1

    manifest = load_manifest(locator)
    all_frames = load_all_frames(locator)

    infer_w = int(manifest.get("infer_width") or 0)
    infer_h = int(manifest.get("infer_height") or 0)
    if infer_w <= 0 or infer_h <= 0:
        for fr in all_frames:
            infer_w = int(fr.get("infer_width") or 0)
            infer_h = int(fr.get("infer_height") or 0)
            if infer_w > 0 and infer_h > 0:
                break
    if infer_w <= 0 or infer_h <= 0:
        infer_w, infer_h = 640, 480

    ann = json.loads(ann_path.read_text(encoding="utf-8"))
    boxes = load_scaled_boxes_from_config(ann, infer_w, infer_h)
    shelf81_ids = {
        str(b.get("box_id") or b.get("id") or "").strip()
        for shelf in (ann.get("shelves") or [])
        if isinstance(shelf, dict) and str(shelf.get("shelf_code") or "").strip() == "81"
        for b in (shelf.get("boxes") or [])
        if isinstance(b, dict)
    }
    shelf81_ids.discard("")
    fps = float(manifest.get("fps") or 15.0)

    events, stats = simulate_frame_events_infer_collision(
        all_frames,
        boxes,
        pose_frame_interval=2,
        alarm_min_consecutive_frames=3,
        alarm_cooldown_frames=0,
        video_fps=fps,
    )

    def shelf81_box_ids(tokens: list[str] | None) -> list[str]:
        out: set[str] = set()
        for raw in tokens or []:
            shelf, box_id = parse_collision_token(str(raw))
            if shelf == "81" and box_id:
                out.add(box_id)
        return sorted(out)

    def row_from_jsonl(d: dict) -> dict:
        alarm_ids = shelf81_box_ids(d.get("collision_alarm_collisions"))
        return {
            "coll": shelf81_box_ids(d.get("collision_collisions")),
            "alarm": alarm_ids,
            "pick": bool(alarm_ids),
        }

    def row_from_recompute(ev: dict) -> dict:
        alarm_ids = shelf81_box_ids(ev.get("alarm_collisions"))
        return {
            "coll": shelf81_box_ids(ev.get("collisions")),
            "alarm": alarm_ids,
            "pick": bool(alarm_ids),
        }

    def row_from_export(d: dict) -> dict:
        coll_ids = sorted({
            bid for bid in (box_id_from_token(str(t)) for t in (d.get("rule_collisions") or []))
            if bid in shelf81_ids
        })
        alarm_ids = sorted({
            bid for bid in (box_id_from_token(str(t)) for t in (d.get("rule_alarm_collisions") or []))
            if bid in shelf81_ids
        })
        return {"coll": coll_ids, "alarm": alarm_ids, "pick": bool(alarm_ids)}

    jl_rows = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    by_j = {d["frame_idx"]: d for d in jl_rows}
    by_r = {ev["frame_idx"]: ev for ev in events}
    by_e = {d["frame_idx"]: d for d in json.loads(export_path.read_text(encoding="utf-8"))}

    common = sorted(set(by_j) & set(by_r))
    coll_diff_re: list[int] = []
    pick_diff_re: list[int] = []
    coll_diff_ex: list[int] = []
    pick_diff_ex: list[int] = []

    for fi in common:
        j = row_from_jsonl(by_j[fi])
        r = row_from_recompute(by_r[fi])
        e = row_from_export(by_e.get(fi, {}))
        if j["coll"] != r["coll"] or j["alarm"] != r["alarm"]:
            coll_diff_re.append(fi)
        if j["pick"] != r["pick"]:
            pick_diff_re.append(fi)
        if j["coll"] != e["coll"] or j["alarm"] != e["alarm"]:
            coll_diff_ex.append(fi)
        if j["pick"] != e["pick"]:
            pick_diff_ex.append(fi)

    print("=== 81 货架单标注（data28/record_001）重算 vs output.jsonl ===")
    print(f"annotation: {ann_path}")
    print(f"infer size: {infer_w}x{infer_h}, boxes: {len(boxes)}")
    print(f"skeleton range: {stats['min_frame']}-{stats['max_frame']}, frames: {len(events)}")
    print(f"common frames: {len(common)}")
    print(f"collision/alarm diff (81 box_id): {len(coll_diff_re)}")
    print(f"is_picking diff (81 only): {len(pick_diff_re)}")
    print(f"jsonl picking (81 alarm): {sum(1 for d in jl_rows if shelf81_box_ids(d.get('collision_alarm_collisions')))}")
    print(f"recompute picking (81 alarm): {sum(1 for ev in events if shelf81_box_ids(ev.get('alarm_collisions')))}")
    print()
    print("=== 现有 export（MAP_6 合并标注）vs jsonl，仅看 81 货框 ===")
    print(f"collision/alarm diff: {len(coll_diff_ex)}")
    print(f"is_picking diff (81 only): {len(pick_diff_ex)}")
    print()

    if coll_diff_re:
        print("重算差异样例（前 10 帧）:")
        for fi in coll_diff_re[:10]:
            j = row_from_jsonl(by_j[fi])
            r = row_from_recompute(by_r[fi])
            print(f"  frame {fi}: jsonl coll={j['coll']} alarm={j['alarm']} pick={j['pick']}")
            print(f"             recompute coll={r['coll']} alarm={r['alarm']} pick={r['pick']}")
    else:
        print("81 货架单标注重算与 jsonl 在 collision/alarm 上完全一致。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
