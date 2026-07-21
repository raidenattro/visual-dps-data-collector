"""足部 floor_xy 轨迹 sidecar 存储（与 timeline.parquet 分离）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

FLOOR_FOOT_FILE = "floor_foot.parquet"

TIMELINE_FLOOR_COLUMNS = frozenset(
    {
        "foot_u_px",
        "foot_v_px",
        "floor_x_m",
        "floor_y_m",
        "raw_floor_x_m",
        "raw_floor_y_m",
    }
)


def _require_pyarrow():
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("缺少 pyarrow") from exc
    return pa, pq


def floor_foot_path(record_dir: Path) -> Path:
    return Path(record_dir) / FLOOR_FOOT_FILE


def floor_foot_empty_schema() -> dict[str, Any]:
    pa, _ = _require_pyarrow()
    return {
        "frame_idx": pa.array([], type=pa.int32()),
        "source_frame_idx": pa.array([], type=pa.int32()),
        "timestamp_sec": pa.array([], type=pa.float64()),
        "person_id": pa.array([], type=pa.int32()),
        "person_track_id": pa.array([], type=pa.int32()),
        "foot_u_px": pa.array([], type=pa.float64()),
        "foot_v_px": pa.array([], type=pa.float64()),
        "floor_x_m": pa.array([], type=pa.float64()),
        "floor_y_m": pa.array([], type=pa.float64()),
        "raw_floor_x_m": pa.array([], type=pa.float64()),
        "raw_floor_y_m": pa.array([], type=pa.float64()),
        "trail_segment_id": pa.array([], type=pa.int32()),
    }


def floor_foot_row_from_frame(frame: dict[str, Any]) -> dict[str, Any] | None:
    """从帧 dict 提取 floor 行；无有效脚点时返回 None。"""
    if not isinstance(frame, dict):
        return None
    foot_uv = frame.get("foot_uv_px")
    floor_xy = frame.get("floor_xy_m")
    raw_floor = frame.get("raw_floor_xy_m")
    has_foot = isinstance(foot_uv, (list, tuple)) and len(foot_uv) >= 2
    has_floor = isinstance(floor_xy, (list, tuple)) and len(floor_xy) >= 2
    if not has_foot and not has_floor:
        return None

    row: dict[str, Any] = {
        "frame_idx": int(frame.get("frame_idx") or 0),
        "source_frame_idx": int(frame.get("source_frame_idx") or frame.get("frame_idx") or 0),
        "timestamp_sec": float(frame.get("timestamp_sec") or 0.0),
        "person_id": int(frame.get("foot_person_id") if frame.get("foot_person_id") is not None else -1),
        "person_track_id": int(frame.get("foot_person_track_id") or 0),
        "foot_u_px": None,
        "foot_v_px": None,
        "floor_x_m": None,
        "floor_y_m": None,
        "raw_floor_x_m": None,
        "raw_floor_y_m": None,
        "trail_segment_id": int(frame.get("foot_trail_segment_id") or 0),
    }
    if has_foot:
        row["foot_u_px"] = float(foot_uv[0])
        row["foot_v_px"] = float(foot_uv[1])
    if has_floor:
        row["floor_x_m"] = float(floor_xy[0])
        row["floor_y_m"] = float(floor_xy[1])
    if isinstance(raw_floor, (list, tuple)) and len(raw_floor) >= 2:
        row["raw_floor_x_m"] = float(raw_floor[0])
        row["raw_floor_y_m"] = float(raw_floor[1])
    return row


def floor_foot_rows_from_frames(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for fr in frames:
        if not isinstance(fr, dict):
            continue
        row = floor_foot_row_from_frame(fr)
        if row and row.get("frame_idx", 0) > 0:
            rows.append(row)
    return rows


def write_floor_foot_parquet(record_dir: Path, rows: list[dict[str, Any]]) -> None:
    pa, pq = _require_pyarrow()
    record_dir = Path(record_dir)
    path = floor_foot_path(record_dir)
    if rows:
        pq.write_table(pa.Table.from_pylist(rows), path, compression="zstd")
        return
    pq.write_table(pa.table(floor_foot_empty_schema()), path, compression="zstd")


def read_floor_foot_parquet(record_dir: Path) -> list[dict[str, Any]]:
    _, pq = _require_pyarrow()
    path = floor_foot_path(record_dir)
    if not path.is_file():
        return []
    return pq.read_table(path).to_pylist()


def floor_foot_index(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        fid = int(row.get("frame_idx") or 0)
        if fid > 0:
            out[fid] = row
    return out


def merge_floor_row_into_frame(frame: dict[str, Any], row: dict[str, Any] | None) -> None:
    if not row:
        frame.pop("foot_uv_px", None)
        frame.pop("floor_xy_m", None)
        frame.pop("raw_floor_xy_m", None)
        frame.pop("foot_person_id", None)
        frame.pop("foot_person_track_id", None)
        return
    if row.get("foot_u_px") is not None and row.get("foot_v_px") is not None:
        frame["foot_uv_px"] = [float(row["foot_u_px"]), float(row["foot_v_px"])]
    else:
        frame.pop("foot_uv_px", None)
    if row.get("floor_x_m") is not None and row.get("floor_y_m") is not None:
        frame["floor_xy_m"] = [float(row["floor_x_m"]), float(row["floor_y_m"])]
    else:
        frame.pop("floor_xy_m", None)
    if row.get("raw_floor_x_m") is not None and row.get("raw_floor_y_m") is not None:
        frame["raw_floor_xy_m"] = [float(row["raw_floor_x_m"]), float(row["raw_floor_y_m"])]
    else:
        frame.pop("raw_floor_xy_m", None)
    if row.get("person_id") is not None:
        frame["foot_person_id"] = int(row["person_id"])
    if row.get("person_track_id") is not None:
        frame["foot_person_track_id"] = int(row["person_track_id"])


def legacy_floor_rows_from_timeline(timeline_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从旧版 timeline 列迁移读取 floor 数据。"""
    rows: list[dict[str, Any]] = []
    for tl in timeline_rows:
        if not isinstance(tl, dict):
            continue
        fid = int(tl.get("frame_idx") or 0)
        if fid <= 0:
            continue
        has_foot = tl.get("foot_u_px") is not None and tl.get("foot_v_px") is not None
        has_floor = tl.get("floor_x_m") is not None and tl.get("floor_y_m") is not None
        if not has_foot and not has_floor:
            continue
        row: dict[str, Any] = {
            "frame_idx": fid,
            "source_frame_idx": int(tl.get("source_frame_idx") or fid),
            "timestamp_sec": float(tl.get("timestamp_sec") or 0.0),
            "person_id": -1,
            "person_track_id": 0,
            "foot_u_px": float(tl["foot_u_px"]) if has_foot else None,
            "foot_v_px": float(tl["foot_v_px"]) if has_foot else None,
            "floor_x_m": float(tl["floor_x_m"]) if has_floor else None,
            "floor_y_m": float(tl["floor_y_m"]) if has_floor else None,
            "raw_floor_x_m": float(tl["raw_floor_x_m"]) if tl.get("raw_floor_x_m") is not None else None,
            "raw_floor_y_m": float(tl["raw_floor_y_m"]) if tl.get("raw_floor_y_m") is not None else None,
        }
        rows.append(row)
    return rows


def load_floor_foot_rows(record_dir: Path, *, allow_legacy_timeline: bool = True) -> list[dict[str, Any]]:
    """优先读 sidecar；无文件时可回退旧 timeline 列。"""
    record_dir = Path(record_dir)
    rows = read_floor_foot_parquet(record_dir)
    if rows:
        return rows
    if not allow_legacy_timeline:
        return []
    from pose_store import TIMELINE_FILE

    timeline_path = record_dir / TIMELINE_FILE
    if not timeline_path.is_file():
        return []
    _, pq = _require_pyarrow()
    table = pq.read_table(timeline_path)
    col_names = set(table.column_names)
    if not col_names.intersection(TIMELINE_FLOOR_COLUMNS):
        return []
    return legacy_floor_rows_from_timeline(table.to_pylist())


def row_to_playback_payload(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "frame_idx": int(row.get("frame_idx") or 0),
        "source_frame_idx": int(row.get("source_frame_idx") or row.get("frame_idx") or 0),
        "timestamp_sec": float(row.get("timestamp_sec") or 0.0),
        "person_id": int(row.get("person_id") if row.get("person_id") is not None else -1),
        "person_track_id": int(row.get("person_track_id") or 0),
    }
    if row.get("foot_u_px") is not None and row.get("foot_v_px") is not None:
        out["foot_uv_px"] = [float(row["foot_u_px"]), float(row["foot_v_px"])]
    if row.get("floor_x_m") is not None and row.get("floor_y_m") is not None:
        out["floor_xy_m"] = [float(row["floor_x_m"]), float(row["floor_y_m"])]
    if row.get("raw_floor_x_m") is not None and row.get("raw_floor_y_m") is not None:
        out["raw_floor_xy_m"] = [float(row["raw_floor_x_m"]), float(row["raw_floor_y_m"])]
    if row.get("trail_segment_id") is not None:
        out["trail_segment_id"] = int(row["trail_segment_id"])
    return out


def playback_payload_from_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row_to_playback_payload(r) for r in rows if isinstance(r, dict)]


def assign_trail_segment_ids(
    rows: list[dict[str, Any]],
    *,
    jump_threshold_m: float = 1.4,
    max_frame_gap: int = 25,
    max_uv_jump_px: float = 100.0,
) -> list[dict[str, Any]]:
    """按帧序为 sidecar 行分配 trail_segment_id（绘制断线用）。"""
    if not rows:
        return rows
    sorted_rows = sorted(rows, key=lambda r: int(r.get("frame_idx") or 0))
    segment_id = 0
    prev: dict[str, Any] | None = None
    out: list[dict[str, Any]] = []
    for row in sorted_rows:
        item = dict(row)
        if prev is not None:
            prev_fi = int(prev.get("frame_idx") or 0)
            fi = int(item.get("frame_idx") or 0)
            if fi - prev_fi > max_frame_gap:
                segment_id += 1
            elif (
                prev.get("person_id") is not None
                and item.get("person_id") is not None
                and int(prev.get("person_id")) >= 0
                and int(item.get("person_id")) >= 0
                and int(prev.get("person_id")) != int(item.get("person_id"))
            ):
                segment_id += 1
            elif prev.get("floor_x_m") is not None and item.get("floor_x_m") is not None:
                dx = float(item["floor_x_m"]) - float(prev["floor_x_m"])
                dy = float(item["floor_y_m"]) - float(prev["floor_y_m"])
                if (dx * dx + dy * dy) ** 0.5 > jump_threshold_m:
                    segment_id += 1
            elif prev.get("foot_u_px") is not None and item.get("foot_u_px") is not None:
                du = float(item["foot_u_px"]) - float(prev["foot_u_px"])
                dv = float(item["foot_v_px"]) - float(prev["foot_v_px"])
                if (du * du + dv * dv) ** 0.5 > max_uv_jump_px:
                    segment_id += 1
        item["trail_segment_id"] = segment_id
        out.append(item)
        prev = item
    return out


def strip_floor_from_timeline_row(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in TIMELINE_FLOOR_COLUMNS}


def rewrite_timeline_without_floor(record_dir: Path) -> bool:
    """将 timeline.parquet 中的 floor 列移除（迁移用）。"""
    from pose_store import TIMELINE_FILE

    pa, pq = _require_pyarrow()
    record_dir = Path(record_dir)
    timeline_path = record_dir / TIMELINE_FILE
    if not timeline_path.is_file():
        return False
    table = pq.read_table(timeline_path)
    if not set(table.column_names).intersection(TIMELINE_FLOOR_COLUMNS):
        return False
    clean = [strip_floor_from_timeline_row(r) for r in table.to_pylist()]
    pq.write_table(pa.Table.from_pylist(clean), timeline_path, compression="zstd")
    return True
