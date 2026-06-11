"""回放记录标签：SQLite 持久化（与 pose 文件解耦）。

数据存放在 localdata/data.db，后续可在此库扩展更多业务表。
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from config_loader import resolve_app_paths

DATA_DB_NAME = "data.db"
_LEGACY_TAGS_DB_NAME = "records_tags.db"

_TAG_MAX_LEN = 64
_TAG_PATTERN = re.compile(r"^[^\s,，]+$")

_local = threading.local()
_init_lock = threading.Lock()
_initialized_paths: set[str] = set()


def data_db_path() -> Path:
    paths = resolve_app_paths()
    return paths.base_localdata / DATA_DB_NAME


def _migrate_legacy_tag_db() -> None:
    """一次性将 records_tags.db 迁入 data.db（若新库尚不存在）。"""
    new_path = data_db_path()
    if new_path.is_file():
        return
    legacy_path = resolve_app_paths().base_localdata / _LEGACY_TAGS_DB_NAME
    if not legacy_path.is_file():
        return
    new_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(legacy_path, new_path)


def normalize_tag_name(raw: str) -> str:
    name = str(raw or "").strip()
    if not name:
        raise ValueError("标签不能为空")
    if len(name) > _TAG_MAX_LEN:
        raise ValueError(f"标签长度不能超过 {_TAG_MAX_LEN} 个字符")
    if not _TAG_PATTERN.match(name):
        raise ValueError("标签不能包含逗号或空白字符")
    return name


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS record_tags (
            record_id TEXT NOT NULL,
            tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            PRIMARY KEY (record_id, tag_id)
        );

        CREATE INDEX IF NOT EXISTS idx_record_tags_record ON record_tags(record_id);
        CREATE INDEX IF NOT EXISTS idx_record_tags_tag ON record_tags(tag_id);

        CREATE TABLE IF NOT EXISTS record_index (
            record_id TEXT PRIMARY KEY,
            pose_model_tier TEXT NOT NULL,
            camera_slug TEXT NOT NULL DEFAULT '',
            source_mtime REAL NOT NULL DEFAULT 0,
            source_fingerprint TEXT NOT NULL DEFAULT '',
            summary_json TEXT NOT NULL,
            indexed_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_record_index_tier_mtime
            ON record_index(pose_model_tier, source_mtime DESC);
        CREATE INDEX IF NOT EXISTS idx_record_index_camera
            ON record_index(camera_slug);
        """
    )
    _migrate_record_index_columns(conn)
    conn.commit()


def _migrate_record_index_columns(conn: sqlite3.Connection) -> None:
    cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(record_index)")}
    changed = False
    if "event_review_status" not in cols:
        conn.execute(
            "ALTER TABLE record_index ADD COLUMN event_review_status TEXT NOT NULL DEFAULT 'not_started'"
        )
        changed = True
    if "event_review_verified_count" not in cols:
        conn.execute(
            "ALTER TABLE record_index ADD COLUMN event_review_verified_count INTEGER NOT NULL DEFAULT 0"
        )
        changed = True
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_record_index_review_status ON record_index(event_review_status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_record_index_verified_count ON record_index(event_review_verified_count)"
    )
    if changed:
        conn.execute("UPDATE record_index SET source_fingerprint = ''")


def init_data_store(db_path: Path | None = None) -> Path:
    if db_path is None:
        _migrate_legacy_tag_db()
    path = db_path or data_db_path()
    key = str(path.resolve())
    with _init_lock:
        with _connect(path) as conn:
            _ensure_schema(conn)
        _initialized_paths.add(key)
    return path


def init_tag_store(db_path: Path | None = None) -> Path:
    return init_data_store(db_path)


@contextmanager
def get_db(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = init_data_store(db_path)
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect(path)
        _local.conn = conn
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise


_db = get_db


def _get_or_create_tag_id(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if row:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO tags (name, created_at) VALUES (?, ?)",
        (name, _utc_now()),
    )
    return int(cur.lastrowid)


def list_tags_with_counts() -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT t.name AS name, COUNT(rt.record_id) AS count
            FROM tags t
            LEFT JOIN record_tags rt ON rt.tag_id = t.id
            GROUP BY t.id
            ORDER BY t.name COLLATE NOCASE
            """
        ).fetchall()
    return [{"name": str(r["name"]), "count": int(r["count"] or 0)} for r in rows]


def get_tags_for_record(record_id: str) -> list[str]:
    rid = str(record_id or "").strip()
    if not rid:
        return []
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT t.name
            FROM record_tags rt
            JOIN tags t ON t.id = rt.tag_id
            WHERE rt.record_id = ?
            ORDER BY t.name COLLATE NOCASE
            """,
            (rid,),
        ).fetchall()
    return [str(r["name"]) for r in rows]


def get_tags_map(record_ids: list[str]) -> dict[str, list[str]]:
    ids = [str(rid or "").strip() for rid in record_ids if str(rid or "").strip()]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT rt.record_id, t.name
            FROM record_tags rt
            JOIN tags t ON t.id = rt.tag_id
            WHERE rt.record_id IN ({placeholders})
            ORDER BY rt.record_id, t.name COLLATE NOCASE
            """,
            ids,
        ).fetchall()
    out: dict[str, list[str]] = {rid: [] for rid in ids}
    for row in rows:
        out.setdefault(str(row["record_id"]), []).append(str(row["name"]))
    return out


def record_ids_with_all_tags(tag_names: list[str]) -> set[str]:
    names = []
    seen: set[str] = set()
    for raw in tag_names:
        name = normalize_tag_name(raw)
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    if not names:
        return set()
    with get_db() as conn:
        tag_ids: list[int] = []
        for name in names:
            row = conn.execute(
                "SELECT id FROM tags WHERE name = ? COLLATE NOCASE",
                (name,),
            ).fetchone()
            if not row:
                return set()
            tag_ids.append(int(row["id"]))
        placeholders = ",".join("?" for _ in tag_ids)
        rows = conn.execute(
            f"""
            SELECT record_id
            FROM record_tags
            WHERE tag_id IN ({placeholders})
            GROUP BY record_id
            HAVING COUNT(DISTINCT tag_id) = ?
            """,
            (*tag_ids, len(tag_ids)),
        ).fetchall()
    return {str(r["record_id"]) for r in rows}


def patch_record_tags(
    record_id: str,
    *,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> list[str]:
    rid = str(record_id or "").strip()
    if not rid:
        raise ValueError("record_id 无效")
    add_names = [normalize_tag_name(t) for t in (add or [])]
    remove_names = [normalize_tag_name(t) for t in (remove or [])]
    with get_db() as conn:
        for name in add_names:
            tag_id = _get_or_create_tag_id(conn, name)
            conn.execute(
                """
                INSERT OR IGNORE INTO record_tags (record_id, tag_id, created_at)
                VALUES (?, ?, ?)
                """,
                (rid, tag_id, _utc_now()),
            )
        for name in remove_names:
            conn.execute(
                """
                DELETE FROM record_tags
                WHERE record_id = ?
                  AND tag_id IN (
                    SELECT id FROM tags WHERE name = ? COLLATE NOCASE
                  )
                """,
                (rid, name),
            )
        conn.commit()
    return get_tags_for_record(rid)


def delete_record_tags(record_id: str) -> None:
    rid = str(record_id or "").strip()
    if not rid:
        return
    with get_db() as conn:
        conn.execute("DELETE FROM record_tags WHERE record_id = ?", (rid,))
        conn.execute(
            """
            DELETE FROM tags
            WHERE id NOT IN (SELECT DISTINCT tag_id FROM record_tags)
            """
        )
        conn.commit()
