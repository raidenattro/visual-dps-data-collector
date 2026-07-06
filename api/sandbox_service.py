"""碰撞沙盒：仅读写 upload/sandbox/{session_id}，绝不修改正式记录。"""

from __future__ import annotations

import json
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from annotation_store import validate_annotation_payload
from config_loader import resolve_app_paths
from event_engine.annotation_boxes import load_scaled_boxes
from event_engine.collision_sim import (
    filter_pose_inference_frames,
    simulate_frame_events_from_frames,
    stored_pose_frame_interval,
)
from event_engine.wrist_hits import ProbeMode
from pose_store import RecordLocator, load_all_frames, load_manifest

from api.collision_variants_service import (
    _infer_size_from_frames,
    _timeline_row_from_event,
    _video_fps,
    write_collision_variant_timeline,
)
from api.record_service import locate_record_by_id, resolve_annotation_path_for_record

SANDBOX_SCHEMA = 1
META_FILE = "meta.json"
ANNOTATION_FILE = "annotation.json"
TIMELINE_FILE = "timeline.parquet"
SESSION_ID_RE = re.compile(r"^[a-f0-9]{8,32}$")
DEFAULT_TTL_HOURS = 72


def sandbox_root() -> Path:
    paths = resolve_app_paths()
    root = (paths.upload_dir / "sandbox").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _validate_session_id(session_id: str) -> str:
    sid = str(session_id or "").strip().lower()
    if not SESSION_ID_RE.fullmatch(sid):
        raise ValueError(f"无效 session_id: {session_id!r}")
    return sid


def _session_dir(session_id: str) -> Path:
    sid = _validate_session_id(session_id)
    root = sandbox_root()
    path = (root / sid).resolve()
    if not str(path).startswith(str(root)):
        raise ValueError("沙盒路径越界")
    return path


def _assert_writable_session_dir(session_id: str) -> Path:
    path = _session_dir(session_id)
    path.mkdir(parents=True, exist_ok=True)
    resolved = path.resolve()
    root = sandbox_root().resolve()
    if not str(resolved).startswith(str(root)):
        raise ValueError("沙盒写入路径被拒绝")
    return resolved


def _read_meta(session_dir: Path) -> dict[str, Any]:
    meta_path = session_dir / META_FILE
    if not meta_path.is_file():
        raise FileNotFoundError("沙盒 session 不存在")
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("沙盒 meta 格式无效")
    return data


def _write_meta(session_dir: Path, meta: dict[str, Any]) -> None:
    meta["updated_at"] = _utc_now_iso()
    (session_dir / META_FILE).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _collision_params_from_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    collision = manifest.get("collision") if isinstance(manifest.get("collision"), dict) else {}
    inference = manifest.get("inference") if isinstance(manifest.get("inference"), dict) else {}
    return {
        "alarm_min_consecutive_frames": max(
            1, int(collision.get("alarm_min_consecutive_frames") or inference.get("alarm_min_consecutive_frames") or 3)
        ),
        "alarm_cooldown_frames": max(
            0, int(collision.get("alarm_cooldown_frames") or inference.get("alarm_cooldown_frames") or 0)
        ),
        "probe_mode": str(collision.get("probe_mode") or "wrist"),
        "extension_ratio": float(collision.get("extension_ratio") or 0.3),
        "pose_frame_interval": max(1, int(inference.get("pose_frame_interval") or 1)),
    }


def _source_locator(record_id: str) -> RecordLocator:
    locator = locate_record_by_id(record_id)
    if not locator:
        raise FileNotFoundError(f"源记录不存在: {record_id}")
    return locator


def _copy_source_annotation(locator: RecordLocator, session_dir: Path) -> dict[str, Any]:
    ann_path = resolve_annotation_path_for_record(locator.record_id, locator=locator)
    dest = session_dir / ANNOTATION_FILE
    if ann_path and ann_path.is_file():
        shutil.copy2(ann_path, dest)
        return {
            "copied": True,
            "source_annotation": ann_path.name,
            "source_path": str(ann_path),
        }
    # 无外部标注时尝试 manifest 内嵌
    manifest = load_manifest(locator)
    embedded = manifest.get("annotation")
    if isinstance(embedded, dict) and embedded.get("boxes"):
        payload = {"boxes": embedded.get("boxes"), "source_info": embedded.get("source_info") or {}}
        dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"copied": True, "source_annotation": "manifest.embedded", "source_path": None}
    raise FileNotFoundError(f"源记录无可用货框标注: {locator.record_id}")


def create_sandbox_session(record_id: str) -> dict[str, Any]:
    """从正式记录 fork 沙盒 session（只复制 annotation，不复制 pose/timeline）。"""
    rid = str(record_id or "").strip().replace("\\", "/")
    if not rid:
        raise ValueError("record_id 不能为空")

    locator = _source_locator(rid)
    manifest = load_manifest(locator)
    params = _collision_params_from_manifest(manifest)

    session_id = uuid.uuid4().hex[:12]
    session_dir = _assert_writable_session_dir(session_id)
    ann_info = _copy_source_annotation(locator, session_dir)

    meta = {
        "schema": SANDBOX_SCHEMA,
        "session_id": session_id,
        "source_record_id": locator.record_id,
        "created_at": _utc_now_iso(),
        "updated_at": _utc_now_iso(),
        "params": params,
        "annotation": ann_info,
        "recomputed": False,
        "frame_count": 0,
        "alarm_frame_count": 0,
        "collision_frame_count": 0,
        "event_count": 0,
    }
    _write_meta(session_dir, meta)

    return {
        "session_id": session_id,
        "source_record_id": locator.record_id,
        "params": params,
        "annotation": ann_info,
        "recomputed": False,
    }


def list_sandbox_sessions() -> list[dict[str, Any]]:
    root = sandbox_root()
    out: list[dict[str, Any]] = []
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not child.is_dir():
            continue
        if not SESSION_ID_RE.fullmatch(child.name):
            continue
        try:
            meta = _read_meta(child)
            out.append(_session_summary(meta, child))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
    return out


def _session_summary(meta: dict[str, Any], session_dir: Path) -> dict[str, Any]:
    has_timeline = (session_dir / TIMELINE_FILE).is_file()
    return {
        "session_id": meta.get("session_id"),
        "source_record_id": meta.get("source_record_id"),
        "created_at": meta.get("created_at"),
        "updated_at": meta.get("updated_at"),
        "params": meta.get("params") or {},
        "recomputed": bool(meta.get("recomputed")) and has_timeline,
        "frame_count": int(meta.get("frame_count") or 0),
        "event_count": int(meta.get("event_count") or 0),
        "alarm_frame_count": int(meta.get("alarm_frame_count") or 0),
        "collision_frame_count": int(meta.get("collision_frame_count") or 0),
    }


def get_sandbox_session(session_id: str) -> dict[str, Any]:
    session_dir = _session_dir(session_id)
    if not session_dir.is_dir():
        raise FileNotFoundError("沙盒 session 不存在")
    meta = _read_meta(session_dir)
    summary = _session_summary(meta, session_dir)
    summary["has_annotation"] = (session_dir / ANNOTATION_FILE).is_file()
    summary["has_timeline"] = (session_dir / TIMELINE_FILE).is_file()
    return summary


def load_sandbox_annotation(session_id: str) -> dict[str, Any]:
    session_dir = _session_dir(session_id)
    ann_path = session_dir / ANNOTATION_FILE
    if not ann_path.is_file():
        raise FileNotFoundError("沙盒内无 annotation.json")
    data = json.loads(ann_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("annotation.json 须为 JSON 对象")
    return data


def update_sandbox_annotation(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("请求体须为 JSON 对象")
    boxes, err = validate_annotation_payload(payload)
    if err:
        raise ValueError(err)
    session_dir = _assert_writable_session_dir(session_id)
    if not (session_dir / META_FILE).is_file():
        raise FileNotFoundError("沙盒 session 不存在")
    meta = _read_meta(session_dir)
    clean = dict(payload)
    clean["boxes"] = boxes
    (session_dir / ANNOTATION_FILE).write_text(
        json.dumps(clean, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    meta["annotation_edited"] = True
    meta["recomputed"] = False
    _write_meta(session_dir, meta)
    return {"session_id": session_id, "box_count": len(boxes), "status": "saved"}


def recompute_sandbox_collisions(
    session_id: str,
    *,
    alarm_min_consecutive_frames: int | None = None,
    alarm_cooldown_frames: int | None = None,
    probe_mode: str | None = None,
    extension_ratio: float | None = None,
    pose_frame_interval: int | None = None,
) -> dict[str, Any]:
    session_dir = _assert_writable_session_dir(session_id)
    meta = _read_meta(session_dir)
    source_record_id = str(meta.get("source_record_id") or "").strip()
    if not source_record_id:
        raise ValueError("沙盒 meta 缺少 source_record_id")

    locator = _source_locator(source_record_id)
    manifest = load_manifest(locator)
    all_frames = load_all_frames(locator)
    if not all_frames:
        raise ValueError("源记录无帧数据")

    ann_path = session_dir / ANNOTATION_FILE
    if not ann_path.is_file():
        raise FileNotFoundError("沙盒内无 annotation.json，请先创建或保存标注")

    infer_w, infer_h = _infer_size_from_frames(all_frames, manifest)
    boxes = load_scaled_boxes(ann_path, infer_w, infer_h)
    if not boxes:
        raise ValueError("沙盒标注无有效货框")

    params = dict(meta.get("params") or _collision_params_from_manifest(manifest))
    if alarm_min_consecutive_frames is not None:
        params["alarm_min_consecutive_frames"] = max(1, int(alarm_min_consecutive_frames))
    if alarm_cooldown_frames is not None:
        params["alarm_cooldown_frames"] = max(0, int(alarm_cooldown_frames))
    if probe_mode is not None:
        params["probe_mode"] = str(probe_mode).strip()
    if extension_ratio is not None:
        params["extension_ratio"] = float(extension_ratio)
    if pose_frame_interval is not None:
        params["pose_frame_interval"] = max(1, int(pose_frame_interval))

    pm: ProbeMode = "hand_extended" if params.get("probe_mode") == "hand_extended" else "wrist"
    stored_interval = stored_pose_frame_interval(manifest)
    interval = max(1, int(params.get("pose_frame_interval") or 1))
    frames = filter_pose_inference_frames(
        all_frames,
        interval,
        stored_interval=stored_interval,
    )
    fps = _video_fps(manifest)

    events = simulate_frame_events_from_frames(
        frames,
        boxes,
        alarm_min_consecutive_frames=int(params["alarm_min_consecutive_frames"]),
        alarm_cooldown_frames=int(params["alarm_cooldown_frames"]),
        video_fps=fps,
        probe_mode=pm,
        extension_ratio=float(params.get("extension_ratio") or 0.3),
    )
    rows = [_timeline_row_from_event(fr, ev) for fr, ev in zip(frames, events)]
    timeline_path = session_dir / TIMELINE_FILE
    write_collision_variant_timeline(timeline_path, rows)

    alarm_frames = sum(1 for r in rows if r.get("alarm_collisions"))
    collision_frames = sum(1 for r in rows if r.get("collisions"))
    event_count = _count_events_from_rows(rows)

    meta["params"] = params
    meta["recomputed"] = True
    meta["frame_count"] = len(rows)
    meta["alarm_frame_count"] = alarm_frames
    meta["collision_frame_count"] = collision_frames
    meta["event_count"] = event_count
    meta["recomputed_at"] = _utc_now_iso()
    _write_meta(session_dir, meta)

    return {
        "session_id": session_id,
        "source_record_id": source_record_id,
        "params": params,
        "frame_count": len(rows),
        "alarm_frame_count": alarm_frames,
        "collision_frame_count": collision_frames,
        "event_count": event_count,
        "status": "recomputed",
    }


def _count_events_from_rows(rows: list[dict[str, Any]]) -> int:
    from event_engine.box_identity import canonicalize_box_token_list

    n = 0
    for row in rows:
        alarms = canonicalize_box_token_list(row.get("alarm_collisions") or [])
        collisions = canonicalize_box_token_list(row.get("collisions") or [])
        if alarms:
            n += 1
        alarm_set = set(alarms)
        if any(t not in alarm_set for t in collisions):
            n += 1
    return n


def load_sandbox_timeline(session_id: str, *, include_events: bool = True) -> list[dict[str, Any]]:
    session_dir = _session_dir(session_id)
    path = session_dir / TIMELINE_FILE
    if not path.is_file():
        raise FileNotFoundError("沙盒尚未重算碰撞，请先执行 recompute")

    from api.collision_variants_service import _require_pyarrow

    _, pq = _require_pyarrow()
    rows = pq.read_table(path).to_pylist()
    out: list[dict[str, Any]] = []
    for r in rows:
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


def load_sandbox_events(session_id: str) -> list[dict[str, Any]]:
    from event_engine.box_identity import canonicalize_box_token_list

    rows = load_sandbox_timeline(session_id, include_events=True)
    events: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: int(r.get("frame_idx") or 0)):
        ts = float(row.get("timestamp_sec") or 0.0)
        fi = int(row.get("frame_idx") or 0)
        sfi = int(row.get("source_frame_idx") or fi)
        alarms = canonicalize_box_token_list(row.get("alarm_collisions") or [])
        collisions = canonicalize_box_token_list(row.get("collisions") or [])
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
        alarm_set = set(alarms)
        coll_only = [t for t in collisions if t not in alarm_set]
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


def delete_sandbox_session(session_id: str) -> dict[str, Any]:
    session_dir = _session_dir(session_id)
    if session_dir.is_dir():
        shutil.rmtree(session_dir, ignore_errors=True)
    return {"status": "deleted", "session_id": session_id}


def delete_all_sandbox_sessions() -> dict[str, Any]:
    root = sandbox_root()
    removed = 0
    if root.is_dir():
        for child in list(root.iterdir()):
            if child.is_dir() and SESSION_ID_RE.fullmatch(child.name):
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
    return {"status": "cleared", "removed_count": removed}


def cleanup_expired_sandbox_sessions(*, ttl_hours: float = DEFAULT_TTL_HOURS) -> dict[str, Any]:
    root = sandbox_root()
    if not root.is_dir():
        return {"status": "ok", "removed_count": 0, "ttl_hours": ttl_hours}
    now = datetime.now(timezone.utc)
    removed = 0
    for child in list(root.iterdir()):
        if not child.is_dir() or not SESSION_ID_RE.fullmatch(child.name):
            continue
        try:
            meta = _read_meta(child)
            ts = _parse_iso(str(meta.get("updated_at") or meta.get("created_at") or ""))
            if ts is None:
                continue
            age_h = (now - ts.astimezone(timezone.utc)).total_seconds() / 3600.0
            if age_h >= float(ttl_hours):
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        except (OSError, json.JSONDecodeError, ValueError):
            continue
    return {"status": "ok", "removed_count": removed, "ttl_hours": ttl_hours}
