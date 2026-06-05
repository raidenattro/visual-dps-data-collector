#!/usr/bin/env python3
"""为已有机位目录下的采集记录补标注副本（无需重新推理）。

用法:
  python scripts/repair_batch_records.py              # 修复全部记录
  python scripts/repair_batch_records.py 2-1-3        # 仅修复机位目录 2-1-3 下记录
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config_loader import camera_storage_slug, resolve_app_paths, resolve_config_path
from pose_store import iter_active_records, meta_sidecar_path

from api.record_service import (
    locate_record_by_id,
    meta_path_for_record,
    persist_playback_annotation,
    resolve_annotation_path_for_record,
    resolve_video_stem_for_record,
)
from api.reflection_service import load_reflection_or_http, normalize_corner_label


def _camera_label_for_slug(slug: str) -> str | None:
    try:
        reflection = load_reflection_or_http()
    except Exception:
        return None
    for raw in reflection.cameras:
        label = normalize_corner_label(raw) if normalize_corner_label else str(raw).strip()
        if camera_storage_slug(label) == slug:
            return label
    return None


def _resolve_source_annotation(meta: dict, paths, camera_slug_hint: str) -> Path | None:
    cam = str(meta.get("camera_label") or "").strip()
    if not cam and camera_slug_hint:
        cam = _camera_label_for_slug(camera_slug_hint) or ""
    if cam:
        try:
            from corner_label.resolve import resolve_annotation_for_camera

            reflection = load_reflection_or_http()
            resolved = resolve_annotation_for_camera(
                normalize_corner_label(cam) if normalize_corner_label else cam,
                reflection=reflection,
                annotations_dir=paths.annotation_dir,
            )
            if resolved.annotation_path.is_file():
                return resolved.annotation_path
        except Exception:
            pass
    ann_name = str(meta.get("annotation_file") or "").strip()
    if ann_name:
        p = paths.annotation_dir / ann_name
        if p.is_file():
            return p
    return None


def repair_record(record_id: str, *, camera_slug_hint: str = "") -> str:
    locator = locate_record_by_id(record_id)
    if not locator:
        return "skip: 记录不存在"

    paths = resolve_app_paths()
    sidecar = meta_path_for_record(record_id, locator)
    meta: dict = {}
    if sidecar.is_file():
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    slug_hint = camera_slug_hint or (
        record_id.split("/", 1)[0] if "/" in record_id else str(meta.get("camera_slug") or "")
    )
    if slug_hint and not meta.get("camera_label"):
        label = _camera_label_for_slug(slug_hint)
        if label:
            meta["camera_label"] = label
            meta["camera_slug"] = slug_hint
            with open(sidecar, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)

    existing = resolve_annotation_path_for_record(record_id, locator=locator, meta=meta)
    if existing and existing.name == "annotation.json" and existing.parent == locator.path:
        return "ok: 已有包内 annotation.json"

    src = _resolve_source_annotation(meta, paths, slug_hint)
    if not src or not src.is_file():
        return "fail: 找不到机位标注（检查 reflection.json 与 annotations/）"

    video_stem = resolve_video_stem_for_record(
        record_id,
        json_dir=paths.json_dir,
        pose_path=locator.path,
        meta=meta,
    )
    source_video = str(meta.get("source_video") or "")
    dest = persist_playback_annotation(
        src,
        video_stem=video_stem,
        pose_path=locator.path,
        source_video=source_video,
    )
    if dest and dest.is_file():
        return f"ok: {video_stem} ← {src.name}"
    return "fail: 写入标注副本失败"


def main() -> int:
    resolve_config_path(None)
    paths = resolve_app_paths()
    slug_filter = str(sys.argv[1]).strip() if len(sys.argv) > 1 else ""

    targets: list[str] = []
    for loc in iter_active_records(paths.json_dir):
        rid = loc.record_id
        if slug_filter:
            bucket = rid.split("/", 1)[0] if "/" in rid else ""
            if bucket != slug_filter:
                sidecar = meta_sidecar_path(paths.json_dir, rid)
                cam_slug = ""
                if sidecar.is_file():
                    try:
                        m = json.loads(sidecar.read_text(encoding="utf-8"))
                        cam_slug = str(m.get("camera_slug") or "")
                    except json.JSONDecodeError:
                        pass
                if cam_slug != slug_filter:
                    continue
        targets.append(rid)

    if not targets:
        print(f"未找到记录（filter={slug_filter or '全部'}）")
        return 1

    ok = fail = 0
    for rid in sorted(targets):
        msg = repair_record(rid, camera_slug_hint=slug_filter)
        print(f"{rid}: {msg}")
        if msg.startswith("ok"):
            ok += 1
        elif msg.startswith("fail"):
            fail += 1

    print(f"\n完成：成功 {ok}，失败 {fail}，共 {len(targets)} 条（未重新推理）")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
