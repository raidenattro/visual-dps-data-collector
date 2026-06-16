"""回放记录列表摘要索引：缓存于 localdata/data.db，加速按模型层筛选。"""

from __future__ import annotations

import json
from typing import Any

from config_loader import AppPaths, resolve_app_paths
from pose_store import (
    REVIEW_STATUS_COMPLETED,
    REVIEW_STATUS_IN_PROGRESS,
    REVIEW_STATUS_NO_COLLISION,
    REVIEW_STATUS_NOT_STARTED,
    REVIEW_STATUS_TERMINAL,
    iter_active_records,
)
from record_tag_store import _utc_now, get_db, init_data_store

REVIEW_STATUS_FILTERS = frozenset(
    {
        REVIEW_STATUS_COMPLETED,
        REVIEW_STATUS_NO_COLLISION,
        REVIEW_STATUS_IN_PROGRESS,
        REVIEW_STATUS_NOT_STARTED,
        "reviewed",
    }
)


def _review_fields(summary: dict[str, Any]) -> tuple[str, int]:
    status = str(summary.get("event_review_status") or REVIEW_STATUS_NOT_STARTED).strip().lower()
    if status not in {
        REVIEW_STATUS_COMPLETED,
        REVIEW_STATUS_NO_COLLISION,
        REVIEW_STATUS_IN_PROGRESS,
        REVIEW_STATUS_NOT_STARTED,
    }:
        status = REVIEW_STATUS_NOT_STARTED
    try:
        verified_count = max(0, int(summary.get("event_review_verified_count") or 0))
    except (TypeError, ValueError):
        verified_count = 0
    return status, verified_count


def _source_fingerprint(locator, paths: AppPaths) -> tuple[float, str]:
    from api.record_service import meta_path_for_record
    from review_store import event_review_read_paths

    mtime = float(locator.path.stat().st_mtime)
    parts = [str(int(mtime))]
    sidecar = meta_path_for_record(locator.record_id, locator)
    if sidecar.is_file():
        parts.append(str(int(sidecar.stat().st_mtime)))
    for review_path in event_review_read_paths(locator, paths):
        if review_path.is_file():
            parts.append(str(int(review_path.stat().st_mtime)))
            break
    return mtime, ":".join(parts)


def _summary_to_row(locator, summary: dict[str, Any], source_mtime: float, fingerprint: str) -> tuple:
    rid = str(summary.get("record_id") or locator.record_id)
    tier = str(summary.get("pose_model_tier") or "").strip()
    camera_slug = str(summary.get("camera_slug") or "").strip()
    review_status, verified_count = _review_fields(summary)
    payload = dict(summary)
    payload.pop("tags", None)
    return (
        rid,
        tier,
        camera_slug,
        source_mtime,
        fingerprint,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        _utc_now(),
        review_status,
        verified_count,
    )


def _upsert_sql() -> str:
    return """
        INSERT INTO record_index (
            record_id, pose_model_tier, camera_slug,
            source_mtime, source_fingerprint, summary_json, indexed_at,
            event_review_status, event_review_verified_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(record_id) DO UPDATE SET
            pose_model_tier = excluded.pose_model_tier,
            camera_slug = excluded.camera_slug,
            source_mtime = excluded.source_mtime,
            source_fingerprint = excluded.source_fingerprint,
            summary_json = excluded.summary_json,
            indexed_at = excluded.indexed_at,
            event_review_status = excluded.event_review_status,
            event_review_verified_count = excluded.event_review_verified_count
    """


def sync_record_summaries(paths: AppPaths | None = None, pose_tier: str | None = None) -> int:
    """增量同步指定模型层的列表摘要，返回本次刷新的记录数。"""
    from api.record_service import record_summary_for_list

    paths = paths or resolve_app_paths()
    init_data_store()
    locators = iter_active_records(paths.json_dir, pose_tier=pose_tier)
    current_ids = {loc.record_id for loc in locators}
    refreshed = 0
    upsert = _upsert_sql()

    with get_db() as conn:
        for locator in locators:
            source_mtime, fingerprint = _source_fingerprint(locator, paths)
            row = conn.execute(
                "SELECT source_fingerprint FROM record_index WHERE record_id = ?",
                (locator.record_id,),
            ).fetchone()
            if row and str(row["source_fingerprint"]) == fingerprint:
                continue
            summary = record_summary_for_list(locator, paths)
            conn.execute(upsert, _summary_to_row(locator, summary, source_mtime, fingerprint))
            refreshed += 1

        if pose_tier:
            rows = conn.execute(
                "SELECT record_id FROM record_index WHERE pose_model_tier = ?",
                (pose_tier,),
            ).fetchall()
            for row in rows:
                rid = str(row["record_id"])
                if rid not in current_ids:
                    conn.execute("DELETE FROM record_index WHERE record_id = ?", (rid,))
        conn.commit()
    return refreshed


def refresh_record_summary(record_id: str, paths: AppPaths | None = None) -> bool:
    """单条记录摘要失效后重建（复核状态变更等）。"""
    from api.record_service import locate_record_by_id, record_summary_for_list

    rid = str(record_id or "").strip()
    if not rid:
        return False
    paths = paths or resolve_app_paths()
    locator = locate_record_by_id(rid, include_archive=False)
    if not locator:
        delete_record_index(rid)
        return False
    init_data_store()
    source_mtime, fingerprint = _source_fingerprint(locator, paths)
    summary = record_summary_for_list(locator, paths)
    with get_db() as conn:
        conn.execute(_upsert_sql(), _summary_to_row(locator, summary, source_mtime, fingerprint))
        conn.commit()
    return True


def list_record_summaries(
    *,
    pose_tier: str | None = None,
    offset: int = 0,
    limit: int = 0,
    allowed_ids: set[str] | None = None,
    review_status: str | None = None,
    has_verified: bool | None = None,
) -> list[dict[str, Any]]:
    """从索引读取列表摘要（按 source_mtime 倒序）。"""
    init_data_store()
    sql = "SELECT summary_json FROM record_index"
    params: list[Any] = []
    clauses: list[str] = []

    tier = str(pose_tier or "").strip().lower()
    if tier:
        clauses.append("pose_model_tier = ?")
        params.append(tier)

    review_filter = str(review_status or "").strip().lower()
    if review_filter in REVIEW_STATUS_FILTERS:
        if review_filter == "reviewed":
            placeholders = ",".join("?" for _ in REVIEW_STATUS_TERMINAL)
            clauses.append(f"event_review_status IN ({placeholders})")
            params.extend(sorted(REVIEW_STATUS_TERMINAL))
        else:
            clauses.append("event_review_status = ?")
            params.append(review_filter)

    if has_verified is True:
        clauses.append("event_review_verified_count > 0")
    elif has_verified is False:
        clauses.append("event_review_verified_count = 0")

    if allowed_ids is not None:
        if not allowed_ids:
            return []
        placeholders = ",".join("?" for _ in allowed_ids)
        clauses.append(f"record_id IN ({placeholders})")
        params.extend(sorted(allowed_ids))

    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY source_mtime DESC"

    lim = int(limit)
    off = max(0, int(offset))
    if lim > 0:
        sql += " LIMIT ?"
        params.append(lim)
        if off:
            sql += " OFFSET ?"
            params.append(off)
    elif off:
        sql += " LIMIT -1 OFFSET ?"
        params.append(off)

    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()

    items: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(str(row["summary_json"]))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            payload.setdefault("tags", [])
            items.append(payload)
    return items


def delete_record_index(record_id: str) -> None:
    rid = str(record_id or "").strip()
    if not rid:
        return
    init_data_store()
    with get_db() as conn:
        conn.execute("DELETE FROM record_index WHERE record_id = ?", (rid,))
        conn.commit()


def _review_state_from_locator(locator) -> tuple[dict[str, Any], str, int, int | None]:
    from pose_store import event_review_status_label, load_event_review, resolve_event_review_status

    review = load_event_review(locator)
    event_total = review.get("event_total")
    try:
        event_count = int(event_total) if event_total is not None else None
    except (TypeError, ValueError):
        event_count = None
    status = resolve_event_review_status(review, event_count=event_count)
    verified = review.get("verified_true")
    verified_count = len(verified) if isinstance(verified, list) else 0
    return review, status, verified_count, event_count


def _apply_review_to_summary(summary: dict[str, Any], status: str, verified_count: int, event_count: int | None) -> dict[str, Any]:
    from pose_store import event_review_status_label

    payload = dict(summary)
    payload["event_review_status"] = status
    payload["event_review_label"] = event_review_status_label(status)
    payload["event_review_verified_count"] = verified_count
    if event_count is not None:
        payload["event_review_total"] = event_count
    return payload


def import_event_reviews_to_index(
    paths: AppPaths | None = None,
    *,
    pose_tier: str | None = None,
) -> dict[str, Any]:
    """从磁盘 event_review.json 导入复核状态到 data.db（只读 JSON，不写回磁盘）。"""
    paths = paths or resolve_app_paths()
    init_data_store()
    locators = iter_active_records(paths.json_dir, pose_tier=pose_tier)

    stats = {
        "pose_tier": pose_tier or "",
        "scanned": 0,
        "updated": 0,
        "indexed_new": 0,
        "skipped_no_file": 0,
        "errors": 0,
    }

    with get_db() as conn:
        for locator in locators:
            stats["scanned"] += 1
            from review_store import event_review_read_paths

            if not event_review_read_paths(locator, paths):
                stats["skipped_no_file"] += 1
                continue
            try:
                _review, status, verified_count, event_count = _review_state_from_locator(locator)
                row = conn.execute(
                    "SELECT summary_json FROM record_index WHERE record_id = ?",
                    (locator.record_id,),
                ).fetchone()
                if not row:
                    if refresh_record_summary(locator.record_id, paths):
                        stats["indexed_new"] += 1
                        stats["updated"] += 1
                    else:
                        stats["errors"] += 1
                    continue

                try:
                    summary = json.loads(str(row["summary_json"]))
                    if not isinstance(summary, dict):
                        summary = {"record_id": locator.record_id}
                except json.JSONDecodeError:
                    summary = {"record_id": locator.record_id}

                summary = _apply_review_to_summary(summary, status, verified_count, event_count)
                conn.execute(
                    """
                    UPDATE record_index
                    SET event_review_status = ?,
                        event_review_verified_count = ?,
                        summary_json = ?,
                        indexed_at = ?
                    WHERE record_id = ?
                    """,
                    (
                        status,
                        verified_count,
                        json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
                        _utc_now(),
                        locator.record_id,
                    ),
                )
                stats["updated"] += 1
            except Exception:
                stats["errors"] += 1
        conn.commit()

    return stats
