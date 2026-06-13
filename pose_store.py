"""Pose 记录存储：schema v2 分包 Parquet；兼容 v1 单体 JSON。"""

from __future__ import annotations

import json
import shutil
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

_review_write_locks: dict[str, threading.Lock] = {}
_review_write_locks_mu = threading.Lock()


@contextmanager
def event_review_write_lock(record_id: str):
    """同一 record 的 event_review 写入串行化，避免并发 PATCH toggle 互相覆盖。"""
    rid = str(record_id or "").strip()
    with _review_write_locks_mu:
        lock = _review_write_locks.setdefault(rid, threading.Lock())
    lock.acquire()
    try:
        yield
    finally:
        lock.release()

from model_assets import COCO17_KEYPOINT_NAMES

SCHEMA_V2 = 2
MANIFEST_FILE = "manifest.json"
TIMELINE_FILE = "timeline.parquet"
SKELETON_FILE = "skeleton.parquet"
EVENT_REVIEW_FILE = "event_review.json"
EVENT_REVIEW_SCHEMA = 1
REVIEW_STATUS_COMPLETED = "completed"
REVIEW_STATUS_IN_PROGRESS = "in_progress"
REVIEW_STATUS_NOT_STARTED = "not_started"
REVIEW_STATUS_NO_COLLISION = "no_collision"
REVIEW_STATUS_LABELS = {
    REVIEW_STATUS_COMPLETED: "已复核",
    REVIEW_STATUS_IN_PROGRESS: "复核中",
    REVIEW_STATUS_NOT_STARTED: "未复核",
    REVIEW_STATUS_NO_COLLISION: "无碰撞",
}
REVIEW_STATUS_TERMINAL = frozenset({REVIEW_STATUS_COMPLETED, REVIEW_STATUS_NO_COLLISION})
STORAGE_V2_PARQUET = "v2_parquet"
STORAGE_V1_JSON = "v1_json"


@dataclass(frozen=True)
class RecordLocator:
    record_id: str
    storage: str
    path: Path


def is_v2_package(path: Path) -> bool:
    return path.is_dir() and (path / MANIFEST_FILE).is_file()


def is_v1_json(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() == ".json" and not path.name.endswith(".meta.json")


def record_id_from_path(path: Path) -> str:
    return path.stem if path.is_file() else path.name


def meta_sidecar_path(json_dir: Path, record_id: str) -> Path:
    rid = str(record_id or "").strip().replace("\\", "/")
    if "/" in rid:
        parent, name = rid.rsplit("/", 1)
        return json_dir / parent / f"{name}.meta.json"
    return json_dir / f"{rid}.meta.json"


def _locate_in_dir(
    json_dir: Path,
    record_id: str,
    *,
    include_archive: bool = True,
) -> RecordLocator | None:
    rid = str(record_id or "").strip().replace("\\", "/")
    if not rid:
        return None

    pkg = json_dir / rid
    if is_v2_package(pkg):
        return RecordLocator(rid, STORAGE_V2_PARQUET, pkg)

    direct = json_dir / f"{rid}.json"
    if is_v1_json(direct):
        return RecordLocator(rid, STORAGE_V1_JSON, direct)

    for p in json_dir.glob("*.json"):
        if p.name.endswith(".meta.json"):
            continue
        if p.stem == rid or p.stem.startswith(rid + "_"):
            return RecordLocator(p.stem, STORAGE_V1_JSON, p)

    if not include_archive:
        return None

    archive = json_dir / "archive"
    if not archive.is_dir():
        return None

    arch_pkg = archive / rid
    if is_v2_package(arch_pkg):
        return RecordLocator(rid, STORAGE_V2_PARQUET, arch_pkg)

    arch_json = archive / f"{rid}.json"
    if is_v1_json(arch_json):
        return RecordLocator(rid, STORAGE_V1_JSON, arch_json)

    for p in archive.glob("*.json"):
        if p.name.endswith(".meta.json"):
            continue
        if p.stem == rid or p.stem.startswith(rid + "_"):
            return RecordLocator(p.stem, STORAGE_V1_JSON, p)
    return None


def locate_record(json_dir: Path, record_id: str, *, include_archive: bool = True) -> RecordLocator | None:
    rid = str(record_id or "").strip().replace("\\", "/")
    if not rid:
        return None
    # 支持 rtmpose-t/1-2-1/foo 等多级路径
    direct = _locate_in_dir(json_dir, rid, include_archive=include_archive)
    if direct:
        return RecordLocator(rid, direct.storage, direct.path)
    if "/" in rid:
        bucket, name = rid.split("/", 1)
        found = _locate_in_dir(json_dir / bucket, name, include_archive=include_archive)
        if found:
            return RecordLocator(rid, found.storage, found.path)
    return None


def iter_active_records(json_dir: Path, *, pose_tier: str | None = None) -> list[RecordLocator]:
    from config_loader import is_pose_model_tier

    items: list[RecordLocator] = []
    seen: set[str] = set()
    reserved = {"archive", "annotations"}
    tier_filter = str(pose_tier or "").strip().lower() or None

    def _skip_json_file(path: Path) -> bool:
        if path.name.endswith(".meta.json"):
            return True
        if path.name.startswith("_batch_"):
            return True
        return False

    def _add(loc: RecordLocator) -> None:
        if loc.record_id not in seen:
            seen.add(loc.record_id)
            items.append(loc)

    def _scan_camera_dir(cam_dir: Path, id_prefix: str) -> None:
        if not cam_dir.is_dir():
            return
        for child in cam_dir.iterdir():
            if _skip_json_file(child):
                continue
            if child.is_dir() and is_v2_package(child):
                rid = f"{id_prefix}/{child.name}" if id_prefix else child.name
                _add(RecordLocator(rid, STORAGE_V2_PARQUET, child))
            elif is_v1_json(child):
                rid = f"{id_prefix}/{child.stem}" if id_prefix else child.stem
                _add(RecordLocator(rid, STORAGE_V1_JSON, child))

    for p in sorted(json_dir.iterdir(), key=lambda x: x.name):
        if p.name in reserved or p.name.startswith("."):
            continue
        if is_pose_model_tier(p.name):
            if tier_filter and p.name != tier_filter:
                continue
            for cam_dir in sorted(p.iterdir(), key=lambda x: x.name):
                if cam_dir.name in reserved or cam_dir.name.startswith("."):
                    continue
                _scan_camera_dir(cam_dir, f"{p.name}/{cam_dir.name}")
            continue
        if tier_filter:
            # 已按模型层过滤时跳过旧版扁平机位目录
            continue
        if p.is_dir() and is_v2_package(p):
            _add(RecordLocator(p.name, STORAGE_V2_PARQUET, p))
            continue
        if is_v1_json(p) and not _skip_json_file(p):
            _add(RecordLocator(p.stem, STORAGE_V1_JSON, p))
            continue
        if p.is_dir():
            _scan_camera_dir(p, p.name)

    items.sort(key=lambda loc: loc.path.stat().st_mtime, reverse=True)
    return items


def _require_pyarrow():
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq

        return pa, pq
    except ImportError as exc:
        raise RuntimeError("缺少 pyarrow，请执行: pip install pyarrow") from exc


def _kpt_column_names() -> list[str]:
    cols: list[str] = []
    for i in range(len(COCO17_KEYPOINT_NAMES)):
        cols.extend([f"kpt_{i}_x", f"kpt_{i}_y", f"kpt_{i}_score"])
    return cols


def _person_row_to_skeleton(
    frame: dict[str, Any],
    person: dict[str, Any],
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "frame_idx": int(frame.get("frame_idx") or 0),
        "source_frame_idx": int(frame.get("source_frame_idx") or frame.get("frame_idx") or 0),
        "timestamp_sec": float(frame.get("timestamp_sec") or 0.0),
        "person_id": int(person.get("person_id") if person.get("person_id") is not None else -1),
        "person_track_id": person.get("person_track_id"),
    }
    bbox = person.get("bbox") or [None, None, None, None]
    if isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
        row["bbox_x1"] = float(bbox[0]) if bbox[0] is not None else None
        row["bbox_y1"] = float(bbox[1]) if bbox[1] is not None else None
        row["bbox_x2"] = float(bbox[2]) if bbox[2] is not None else None
        row["bbox_y2"] = float(bbox[3]) if bbox[3] is not None else None
    else:
        row["bbox_x1"] = row["bbox_y1"] = row["bbox_x2"] = row["bbox_y2"] = None

    kpts = person.get("keypoints") or []
    for i in range(len(COCO17_KEYPOINT_NAMES)):
        if i < len(kpts) and isinstance(kpts[i], (list, tuple)) and len(kpts[i]) >= 2:
            kp = kpts[i]
            row[f"kpt_{i}_x"] = round(float(kp[0]), 2)
            row[f"kpt_{i}_y"] = round(float(kp[1]), 2)
            row[f"kpt_{i}_score"] = round(float(kp[2]), 4) if len(kp) > 2 else None
        else:
            row[f"kpt_{i}_x"] = row[f"kpt_{i}_y"] = row[f"kpt_{i}_score"] = None
    return row


def _timeline_row_from_frame(frame: dict[str, Any]) -> dict[str, Any]:
    return {
        "frame_idx": int(frame.get("frame_idx") or 0),
        "source_frame_idx": int(frame.get("source_frame_idx") or frame.get("frame_idx") or 0),
        "timestamp_sec": float(frame.get("timestamp_sec") or 0.0),
        "infer_width": int(frame.get("infer_width") or 0),
        "infer_height": int(frame.get("infer_height") or 0),
        "collisions": list(frame.get("collisions") or []),
        "alarm_collisions": list(frame.get("alarm_collisions") or []),
    }


def _build_manifest_from_collect(data: dict[str, Any], record_id: str) -> dict[str, Any]:
    frames = data.get("frames") or []
    infer_w = infer_h = 0
    if frames and isinstance(frames[0], dict):
        infer_w = int(frames[0].get("infer_width") or 0)
        infer_h = int(frames[0].get("infer_height") or 0)

    manifest: dict[str, Any] = {
        k: v
        for k, v in data.items()
        if k != "frames"
    }
    manifest.update(
        {
            "schema": SCHEMA_V2,
            "kind": data.get("kind") or "pose_collect_video",
            "record_id": record_id,
            "storage": STORAGE_V2_PARQUET,
            "infer_width": infer_w,
            "infer_height": infer_h,
            "frame_count": len(frames),
            "files": {
                "timeline": TIMELINE_FILE,
                "skeleton": SKELETON_FILE,
            },
        }
    )
    return manifest


def write_timeline_parquet(record_dir: Path, frames: list[dict[str, Any]]) -> None:
    """仅重写 timeline.parquet（碰撞重算时保留 skeleton.parquet）。"""
    pa, pq = _require_pyarrow()
    record_dir = Path(record_dir)
    timeline_rows = [_timeline_row_from_frame(fr) for fr in frames if isinstance(fr, dict)]
    if timeline_rows:
        pq.write_table(pa.Table.from_pylist(timeline_rows), record_dir / TIMELINE_FILE, compression="zstd")
        return
    pq.write_table(
        pa.table(
            {
                "frame_idx": pa.array([], type=pa.int32()),
                "source_frame_idx": pa.array([], type=pa.int32()),
                "timestamp_sec": pa.array([], type=pa.float64()),
                "infer_width": pa.array([], type=pa.int32()),
                "infer_height": pa.array([], type=pa.int32()),
                "collisions": pa.array([], type=pa.list_(pa.string())),
                "alarm_collisions": pa.array([], type=pa.list_(pa.string())),
            }
        ),
        record_dir / TIMELINE_FILE,
        compression="zstd",
    )


def patch_v2_manifest(record_dir: Path, updates: dict[str, Any]) -> dict[str, Any]:
    """合并更新 manifest.json 并写回。"""
    record_dir = Path(record_dir)
    path = record_dir / MANIFEST_FILE
    if not path.is_file():
        raise FileNotFoundError(f"manifest 不存在: {path}")
    with open(path, encoding="utf-8") as f:
        manifest = json.load(f)
    if not isinstance(manifest, dict):
        raise ValueError("manifest 根节点必须是 object")
    manifest.update(updates)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return manifest


def write_v2_package(record_dir: Path, data: dict[str, Any], *, record_id: str | None = None) -> Path:
    """将采集结果写入分包 Parquet 目录。"""
    pa, pq = _require_pyarrow()

    record_dir = Path(record_dir)
    record_dir.mkdir(parents=True, exist_ok=True)
    rid = record_id or record_dir.name

    frames = data.get("frames") or []
    timeline_rows = [_timeline_row_from_frame(fr) for fr in frames if isinstance(fr, dict)]
    skeleton_rows: list[dict[str, Any]] = []
    for fr in frames:
        if not isinstance(fr, dict):
            continue
        persons = fr.get("persons") or []
        if not persons:
            continue
        for person in persons:
            if isinstance(person, dict):
                skeleton_rows.append(_person_row_to_skeleton(fr, person))

    if timeline_rows:
        pq.write_table(pa.Table.from_pylist(timeline_rows), record_dir / TIMELINE_FILE, compression="zstd")
    else:
        pq.write_table(
            pa.table(
                {
                    "frame_idx": pa.array([], type=pa.int32()),
                    "source_frame_idx": pa.array([], type=pa.int32()),
                    "timestamp_sec": pa.array([], type=pa.float64()),
                    "infer_width": pa.array([], type=pa.int32()),
                    "infer_height": pa.array([], type=pa.int32()),
                    "collisions": pa.array([], type=pa.list_(pa.string())),
                    "alarm_collisions": pa.array([], type=pa.list_(pa.string())),
                }
            ),
            record_dir / TIMELINE_FILE,
            compression="zstd",
        )

    skel_cols = [
        "frame_idx",
        "source_frame_idx",
        "timestamp_sec",
        "person_id",
        "person_track_id",
        "bbox_x1",
        "bbox_y1",
        "bbox_x2",
        "bbox_y2",
        *_kpt_column_names(),
    ]
    if skeleton_rows:
        pq.write_table(pa.Table.from_pylist(skeleton_rows), record_dir / SKELETON_FILE, compression="zstd")
    else:
        empty = {c: [] for c in skel_cols}
        pq.write_table(pa.table(empty), record_dir / SKELETON_FILE, compression="zstd")

    manifest = _build_manifest_from_collect(data, rid)
    with open(record_dir / MANIFEST_FILE, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return record_dir.resolve()


def load_manifest(locator: RecordLocator) -> dict[str, Any]:
    if locator.storage == STORAGE_V2_PARQUET:
        path = locator.path / MANIFEST_FILE
        if not path.is_file():
            raise FileNotFoundError(f"manifest 不存在: {path}")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}

    with open(locator.path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("pose JSON 根节点必须是 object")
    return data


def _skeleton_row_to_person(row: dict[str, Any]) -> dict[str, Any]:
    keypoints: list[list[float | None]] = []
    for i in range(len(COCO17_KEYPOINT_NAMES)):
        x = row.get(f"kpt_{i}_x")
        y = row.get(f"kpt_{i}_y")
        s = row.get(f"kpt_{i}_score")
        if x is None or y is None:
            keypoints.append([None, None, None])
        else:
            keypoints.append([float(x), float(y), float(s) if s is not None else 0.0])

    person: dict[str, Any] = {
        "person_id": int(row.get("person_id") if row.get("person_id") is not None else 0),
        "keypoints": keypoints,
    }
    ptid = row.get("person_track_id")
    if ptid is not None:
        person["person_track_id"] = int(ptid)
    bbox = [row.get("bbox_x1"), row.get("bbox_y1"), row.get("bbox_x2"), row.get("bbox_y2")]
    if any(v is not None for v in bbox):
        person["bbox"] = [float(v) if v is not None else 0.0 for v in bbox]
    return person


def _assemble_frames_from_tables(
    timeline_rows: list[dict[str, Any]],
    skeleton_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    skel_by_frame: dict[int, list[dict[str, Any]]] = {}
    for row in skeleton_rows:
        fid = int(row.get("frame_idx") or 0)
        skel_by_frame.setdefault(fid, []).append(_skeleton_row_to_person(row))

    frames: list[dict[str, Any]] = []
    for tl in timeline_rows:
        fid = int(tl.get("frame_idx") or 0)
        frame: dict[str, Any] = {
            "frame_idx": fid,
            "source_frame_idx": int(tl.get("source_frame_idx") or fid),
            "timestamp_sec": float(tl.get("timestamp_sec") or 0.0),
            "infer_width": int(tl.get("infer_width") or 0),
            "infer_height": int(tl.get("infer_height") or 0),
            "persons": skel_by_frame.get(fid, []),
            "collisions": list(tl.get("collisions") or []),
            "alarm_collisions": list(tl.get("alarm_collisions") or []),
        }
        frames.append(frame)
    return frames


def _read_parquet_table(path: Path):
    _, pq = _require_pyarrow()
    if not path.is_file():
        return []
    table = pq.read_table(path)
    return table.to_pylist()


def load_frames_range(
    locator: RecordLocator,
    *,
    from_frame_idx: int = 1,
    to_frame_idx: int | None = None,
) -> list[dict[str, Any]]:
    if locator.storage == STORAGE_V1_JSON:
        data = load_manifest(locator)
        frames = data.get("frames") or []
        lo = max(1, int(from_frame_idx))
        hi = int(to_frame_idx) if to_frame_idx is not None else 10**9
        return [fr for fr in frames if isinstance(fr, dict) and lo <= int(fr.get("frame_idx") or 0) <= hi]

    if locator.storage != STORAGE_V2_PARQUET:
        return []

    pa, pq = _require_pyarrow()
    lo = max(1, int(from_frame_idx))
    hi = int(to_frame_idx) if to_frame_idx is not None else 10**9

    timeline_path = locator.path / TIMELINE_FILE
    skeleton_path = locator.path / SKELETON_FILE
    if not timeline_path.is_file():
        return []

    timeline_table = pq.read_table(
        timeline_path,
        filters=[("frame_idx", ">=", lo), ("frame_idx", "<=", hi)],
    )
    timeline_rows = timeline_table.to_pylist()

    skeleton_rows: list[dict[str, Any]] = []
    if skeleton_path.is_file():
        skeleton_table = pq.read_table(
            skeleton_path,
            filters=[("frame_idx", ">=", lo), ("frame_idx", "<=", hi)],
        )
        skeleton_rows = skeleton_table.to_pylist()

    return _assemble_frames_from_tables(timeline_rows, skeleton_rows)


def load_all_frames(locator: RecordLocator) -> list[dict[str, Any]]:
    if locator.storage == STORAGE_V1_JSON:
        data = load_manifest(locator)
        return list(data.get("frames") or [])

    timeline_rows = _read_parquet_table(locator.path / TIMELINE_FILE)
    skeleton_rows = _read_parquet_table(locator.path / SKELETON_FILE)
    return _assemble_frames_from_tables(timeline_rows, skeleton_rows)


def load_pose_document(locator: RecordLocator, *, include_frames: bool = True) -> dict[str, Any]:
    """加载与 v1 pose.json 兼容的完整文档（导出/离线分析用）。"""
    if locator.storage == STORAGE_V1_JSON:
        return load_manifest(locator)

    manifest = load_manifest(locator)
    doc = dict(manifest)
    if include_frames:
        doc["frames"] = load_all_frames(locator)
    else:
        doc.pop("frames", None)
    return doc


def load_timeline(locator: RecordLocator, *, include_events: bool = False) -> list[dict[str, Any]]:
    """轻量时间轴（回放索引用；可选含 collisions / alarm_collisions）。"""
    if locator.storage == STORAGE_V1_JSON:
        data = load_manifest(locator)
        rows: list[dict[str, Any]] = []
        for fr in data.get("frames") or []:
            if not isinstance(fr, dict):
                continue
            row = {
                "frame_idx": int(fr.get("frame_idx") or 0),
                "source_frame_idx": int(fr.get("source_frame_idx") or fr.get("frame_idx") or 0),
                "timestamp_sec": float(fr.get("timestamp_sec") or 0.0),
                "infer_width": int(fr.get("infer_width") or 0),
                "infer_height": int(fr.get("infer_height") or 0),
            }
            if include_events:
                row["collisions"] = list(fr.get("collisions") or [])
                row["alarm_collisions"] = list(fr.get("alarm_collisions") or [])
            rows.append(row)
        return rows

    rows_raw = _read_parquet_table(locator.path / TIMELINE_FILE)
    out: list[dict[str, Any]] = []
    for r in rows_raw:
        if not isinstance(r, dict):
            continue
        row = {
            "frame_idx": int(r.get("frame_idx") or 0),
            "source_frame_idx": int(r.get("source_frame_idx") or r.get("frame_idx") or 0),
            "timestamp_sec": float(r.get("timestamp_sec") or 0.0),
            "infer_width": int(r.get("infer_width") or 0),
            "infer_height": int(r.get("infer_height") or 0),
        }
        if include_events:
            row["collisions"] = list(r.get("collisions") or [])
            row["alarm_collisions"] = list(r.get("alarm_collisions") or [])
        out.append(row)
    return out


def load_events(locator: RecordLocator) -> list[dict[str, Any]]:
    """碰撞/告警事件列表（每帧每类型一条，供回放跳转）。"""
    rows = load_timeline(locator, include_events=True)
    events: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: int(r.get("frame_idx") or 0)):
        ts = float(row.get("timestamp_sec") or 0.0)
        fi = int(row.get("frame_idx") or 0)
        sfi = int(row.get("source_frame_idx") or fi)
        alarms = [str(t) for t in (row.get("alarm_collisions") or []) if str(t).strip()]
        collisions = [str(t) for t in (row.get("collisions") or []) if str(t).strip()]
        if alarms:
            events.append(
                {
                    "event_type": "alarm",
                    "frame_idx": fi,
                    "source_frame_idx": sfi,
                    "timestamp_sec": ts,
                    "box_tokens": alarms,
                }
            )
        coll_only = [t for t in collisions if t not in set(alarms)]
        if coll_only:
            events.append(
                {
                    "event_type": "collision",
                    "frame_idx": fi,
                    "source_frame_idx": sfi,
                    "timestamp_sec": ts,
                    "box_tokens": coll_only,
                }
            )
    return events


def event_signature(event_type: str, frame_idx: int, box_tokens: list[Any] | None) -> str:
    """事件唯一键（与前端 eventRowKey 一致）。"""
    tokens = sorted(str(t).strip() for t in (box_tokens or []) if str(t).strip())
    return f"{str(event_type or '').strip()}:{int(frame_idx)}:{','.join(tokens)}"


def event_review_path(locator: RecordLocator) -> Path:
    if locator.storage == STORAGE_V2_PARQUET:
        return locator.path / EVENT_REVIEW_FILE
    return locator.path.with_suffix(".event_review.json")


def events_to_verified_entries(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """将 load_events 结果转为 event_review.verified_true 条目（去重）。"""
    verified: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ev in events:
        if not isinstance(ev, dict):
            continue
        norm = normalize_review_entry(ev)
        if not norm:
            continue
        sig = event_signature(norm["event_type"], norm["frame_idx"], norm["box_tokens"])
        if sig in seen:
            continue
        seen.add(sig)
        verified.append(norm)
    return verified


def normalize_review_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    event_type = str(entry.get("event_type") or "").strip()
    if event_type not in ("alarm", "collision"):
        return None
    try:
        frame_idx = int(entry.get("frame_idx") or 0)
    except (TypeError, ValueError):
        return None
    if frame_idx < 0:
        return None
    tokens = [str(t).strip() for t in (entry.get("box_tokens") or []) if str(t).strip()]
    if not tokens:
        return None
    try:
        source_frame_idx = int(entry.get("source_frame_idx") or frame_idx)
    except (TypeError, ValueError):
        source_frame_idx = frame_idx
    out: dict[str, Any] = {
        "event_type": event_type,
        "frame_idx": frame_idx,
        "source_frame_idx": source_frame_idx,
        "box_tokens": tokens,
    }
    confirmed = str(entry.get("confirmed_box_token") or "").strip()
    if confirmed:
        out["confirmed_box_token"] = confirmed
    return out


def load_event_review_raw(locator: RecordLocator) -> dict[str, Any]:
    """仅读取 event_review.json 原始内容（列表接口用，不做 verified 规范化）。"""
    path = event_review_path(locator)
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def load_event_review(locator: RecordLocator) -> dict[str, Any]:
    """加载人工复核结果（标为真的碰撞/告警）。"""
    path = event_review_path(locator)
    empty = {
        "schema": EVENT_REVIEW_SCHEMA,
        "record_id": locator.record_id,
        "verified_true": [],
        "status": "",
        "completed_at": "",
        "event_total": None,
    }
    if not path.is_file():
        return empty
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return empty
    if not isinstance(raw, dict):
        return empty

    verified: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw.get("verified_true") or []:
        norm = normalize_review_entry(item if isinstance(item, dict) else {})
        if not norm:
            continue
        sig = event_signature(norm["event_type"], norm["frame_idx"], norm["box_tokens"])
        if sig in seen:
            continue
        seen.add(sig)
        verified.append(norm)

    event_total_raw = raw.get("event_total")
    try:
        event_total = int(event_total_raw) if event_total_raw is not None else None
    except (TypeError, ValueError):
        event_total = None

    return {
        "schema": int(raw.get("schema") or EVENT_REVIEW_SCHEMA),
        "record_id": str(raw.get("record_id") or locator.record_id),
        "updated_at": str(raw.get("updated_at") or ""),
        "status": str(raw.get("status") or "").strip().lower(),
        "completed_at": str(raw.get("completed_at") or ""),
        "event_total": event_total,
        "verified_true": verified,
    }


def persisted_event_review_status(review: dict[str, Any]) -> str:
    """event_review.json 中已落盘的 status 字段（不含推断）。"""
    return str(review.get("status") or "").strip().lower()


def is_persisted_review_terminal(review: dict[str, Any]) -> bool:
    return persisted_event_review_status(review) in REVIEW_STATUS_TERMINAL


def resolve_event_review_status(
    review: dict[str, Any],
    *,
    event_count: int | None = None,
) -> str:
    """未复核 / 复核中 / 已复核 / 无碰撞（展示用，含 event_total 推断）。"""
    status = persisted_event_review_status(review)
    if status == REVIEW_STATUS_NO_COLLISION:
        return REVIEW_STATUS_NO_COLLISION
    if status == REVIEW_STATUS_COMPLETED:
        return REVIEW_STATUS_COMPLETED
    if status == REVIEW_STATUS_IN_PROGRESS:
        return REVIEW_STATUS_IN_PROGRESS
    if event_count == 0 and not review.get("verified_true"):
        return REVIEW_STATUS_NO_COLLISION
    if review.get("event_total") == 0 and not review.get("verified_true"):
        return REVIEW_STATUS_NO_COLLISION
    if review.get("verified_true") or review.get("updated_at"):
        return REVIEW_STATUS_IN_PROGRESS
    return REVIEW_STATUS_NOT_STARTED


def collect_result_has_skeleton(data: dict[str, Any]) -> bool:
    """采集结果中是否包含至少一帧人体骨架。"""
    for fr in data.get("frames") or []:
        if not isinstance(fr, dict):
            continue
        if fr.get("persons"):
            return True
    return False


def record_has_skeleton_data(
    locator: RecordLocator,
    meta: dict[str, Any] | None = None,
) -> bool:
    """记录是否含人体骨架（优先读 meta.has_skeleton，否则查 Parquet/JSON）。"""
    if isinstance(meta, dict) and "has_skeleton" in meta:
        return bool(meta.get("has_skeleton"))
    if locator.storage == STORAGE_V2_PARQUET:
        skel_path = locator.path / SKELETON_FILE
        if not skel_path.is_file():
            return False
        try:
            _, pq = _require_pyarrow()
            return int(pq.read_metadata(skel_path).num_rows or 0) > 0
        except Exception:
            return False
    try:
        data = load_manifest(locator)
    except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError):
        return False
    return collect_result_has_skeleton(data)


def ensure_no_collision_review_completed(
    locator: RecordLocator,
    *,
    event_count: int | None = None,
) -> dict[str, Any]:
    """无碰撞/告警事件时持久化 event_review（status=no_collision）。"""
    if event_count is None:
        try:
            event_count = len(load_events(locator))
        except (RuntimeError, OSError, ValueError):
            return load_event_review(locator)
    if event_count > 0:
        return cache_event_review_total(locator, event_count)

    review = load_event_review(locator)
    if is_persisted_review_terminal(review):
        return review
    if (
        persisted_event_review_status(review) == REVIEW_STATUS_IN_PROGRESS
        and review.get("verified_true")
    ):
        return review

    save_event_review(locator, [], status=REVIEW_STATUS_NO_COLLISION, event_total=0)
    return load_event_review(locator)


def cache_event_review_total(locator: RecordLocator, event_total: int) -> dict[str, Any]:
    """仅缓存事件总数（不改变复核状态），避免列表反复扫描 timeline。"""
    review = load_event_review(locator)
    total = max(0, int(event_total))
    if review.get("event_total") == total:
        return review

    payload: dict[str, Any] = {
        "schema": EVENT_REVIEW_SCHEMA,
        "record_id": locator.record_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "verified_true": list(review.get("verified_true") or []),
        "event_total": total,
    }
    st = str(review.get("status") or "").strip().lower()
    if st:
        payload["status"] = st
    if review.get("completed_at"):
        payload["completed_at"] = review.get("completed_at")

    path = event_review_path(locator)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return load_event_review(locator)


def event_review_status_label(status: str) -> str:
    return REVIEW_STATUS_LABELS.get(str(status or "").strip().lower(), REVIEW_STATUS_LABELS[REVIEW_STATUS_NOT_STARTED])


def load_verified_true_signatures(locator: RecordLocator) -> set[str]:
    review = load_event_review(locator)
    out: set[str] = set()
    for item in review.get("verified_true") or []:
        if not isinstance(item, dict):
            continue
        out.add(
            event_signature(
                str(item.get("event_type") or ""),
                int(item.get("frame_idx") or 0),
                item.get("box_tokens"),
            )
        )
    return out


def save_event_review(
    locator: RecordLocator,
    verified_true: list[dict[str, Any]] | None = None,
    *,
    status: str | None = None,
    event_total: int | None = None,
) -> Path:
    """保存人工复核结果到记录目录 event_review.json。"""
    existing = load_event_review(locator)
    if verified_true is None:
        normalized = list(existing.get("verified_true") or [])
    else:
        normalized = []
        seen: set[str] = set()
        for item in verified_true:
            norm = normalize_review_entry(item if isinstance(item, dict) else {})
            if not norm:
                continue
            sig = event_signature(norm["event_type"], norm["frame_idx"], norm["box_tokens"])
            if sig in seen:
                continue
            seen.add(sig)
            normalized.append(norm)
        normalized.sort(
            key=lambda e: (
                int(e.get("frame_idx") or 0),
                str(e.get("event_type") or ""),
                ",".join(e.get("box_tokens") or []),
            )
        )

    payload: dict[str, Any] = {
        "schema": EVENT_REVIEW_SCHEMA,
        "record_id": locator.record_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "verified_true": normalized,
    }

    prev_status = resolve_event_review_status(existing)
    if event_total is not None:
        payload["event_total"] = max(0, int(event_total))
    elif existing.get("event_total") is not None:
        payload["event_total"] = existing.get("event_total")

    if status is not None:
        st = str(status).strip().lower()
        payload["status"] = st
        if st == REVIEW_STATUS_COMPLETED:
            payload["completed_at"] = datetime.now(timezone.utc).isoformat()
        elif st == REVIEW_STATUS_NO_COLLISION:
            payload.pop("completed_at", None)
        elif st == REVIEW_STATUS_IN_PROGRESS:
            payload.pop("completed_at", None)
        elif existing.get("completed_at") and st != REVIEW_STATUS_COMPLETED:
            payload.pop("completed_at", None)
    elif prev_status in REVIEW_STATUS_TERMINAL:
        payload["status"] = prev_status
        if prev_status == REVIEW_STATUS_COMPLETED and existing.get("completed_at"):
            payload["completed_at"] = existing.get("completed_at")
    elif normalized or verified_true is not None:
        payload["status"] = REVIEW_STATUS_IN_PROGRESS
    elif existing.get("status"):
        payload["status"] = existing.get("status")
        if existing.get("completed_at"):
            payload["completed_at"] = existing.get("completed_at")

    path = event_review_path(locator)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path.resolve()


def load_verified_review_by_signature(locator: RecordLocator) -> dict[str, dict[str, Any]]:
    """已标真复核条目，按 event_signature 索引（含 confirmed_box_token）。"""
    review = load_event_review(locator)
    out: dict[str, dict[str, Any]] = {}
    for item in review.get("verified_true") or []:
        if not isinstance(item, dict):
            continue
        sig = event_signature(
            str(item.get("event_type") or ""),
            int(item.get("frame_idx") or 0),
            item.get("box_tokens"),
        )
        out[sig] = item
    return out


def enrich_events_with_review(
    events: list[dict[str, Any]],
    locator: RecordLocator,
) -> list[dict[str, Any]]:
    verified_by_sig = load_verified_review_by_signature(locator)
    out: list[dict[str, Any]] = []
    for ev in events:
        row = dict(ev)
        sig = event_signature(
            str(ev.get("event_type") or ""),
            int(ev.get("frame_idx") or 0),
            ev.get("box_tokens"),
        )
        review_item = verified_by_sig.get(sig)
        row["verified_true"] = review_item is not None
        confirmed = str((review_item or {}).get("confirmed_box_token") or "").strip()
        if confirmed:
            row["confirmed_box_token"] = confirmed
        out.append(row)
    return out


def load_pose_header(locator: RecordLocator) -> dict[str, Any]:
    """加载不含 frames 的头部（Web manifest / 列表）。"""
    if locator.storage == STORAGE_V1_JSON:
        data = load_manifest(locator)
        header = {k: v for k, v in data.items() if k != "frames"}
        header.setdefault("schema", 1)
        header.setdefault("storage", STORAGE_V1_JSON)
        header.setdefault("record_id", locator.record_id)
        return header

    manifest = load_manifest(locator)
    header = dict(manifest)
    header.pop("frames", None)
    return header


def convert_v1_json_to_v2_package(json_path: Path, record_dir: Path | None = None) -> Path:
    """将 v1 单体 JSON 转为 v2 Parquet 包；成功后删除原 JSON。"""
    json_path = Path(json_path)
    if not is_v1_json(json_path):
        raise ValueError(f"不是 v1 pose JSON: {json_path}")

    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("pose JSON 根节点必须是 object")

    rid = json_path.stem
    out_dir = record_dir or json_path.parent / rid
    if out_dir.exists() and out_dir.is_dir() and is_v2_package(out_dir):
        shutil.rmtree(out_dir)
    write_v2_package(out_dir, data, record_id=rid)

    sidecar = json_path.with_suffix(".meta.json")
    new_sidecar = meta_sidecar_path(json_path.parent, rid)
    if sidecar.is_file() and sidecar != new_sidecar:
        if new_sidecar.is_file():
            sidecar.unlink()
        else:
            sidecar.replace(new_sidecar)

    json_path.unlink()
    return out_dir.resolve()


def migrate_v1_json_dir(json_dir: Path) -> list[Path]:
    """扫描目录，将所有 v1 JSON 转为 v2 包。"""
    migrated: list[Path] = []
    for p in sorted(json_dir.glob("*.json")):
        if p.name.endswith(".meta.json"):
            continue
        if not is_v1_json(p):
            continue
        out = convert_v1_json_to_v2_package(p)
        migrated.append(out)
    return migrated


def delete_record(
    json_dir: Path,
    locator: RecordLocator,
    *,
    video_path: Path | None = None,
) -> dict[str, Any]:
    """删除一条历史记录（骨架包/JSON、sidecar meta、配套视频；不删 annotations 目录标注）。"""
    record_id = locator.record_id
    deleted: list[str] = []

    if video_path and video_path.is_file():
        video_path.unlink()
        deleted.append(str(video_path.resolve()))

    sidecar = meta_sidecar_path(json_dir, record_id)
    if sidecar.is_file():
        sidecar.unlink()
        deleted.append(str(sidecar.resolve()))

    review_path = event_review_path(locator)
    if review_path.is_file():
        review_path.unlink()
        deleted.append(str(review_path.resolve()))

    if locator.path.is_file():
        legacy_sidecar = locator.path.with_suffix(".meta.json")
        if legacy_sidecar.is_file() and legacy_sidecar.resolve() != sidecar.resolve():
            legacy_sidecar.unlink()
            deleted.append(str(legacy_sidecar.resolve()))
        locator.path.unlink()
        deleted.append(str(locator.path.resolve()))
    elif locator.path.is_dir():
        shutil.rmtree(locator.path)
        deleted.append(str(locator.path.resolve()))

    legacy_ann = json_dir / f"{record_id}_annotation.json"
    if legacy_ann.is_file():
        legacy_ann.unlink()
        deleted.append(str(legacy_ann.resolve()))

    return {
        "status": "deleted",
        "record_id": record_id,
        "deleted_paths": deleted,
    }
