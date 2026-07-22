"""左右手腕侧面 Y×Z 轨迹 sidecar（Left/Right Map）。"""

from __future__ import annotations

from typing import Any, Literal

from pathlib import Path

WRIST_FACE_FILE = "wrist_face.parquet"
HandSide = Literal["left", "right"]


def _require_pyarrow():
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("缺少 pyarrow") from exc
    return pa, pq


def wrist_face_path(record_dir: Path) -> Path:
    return Path(record_dir) / WRIST_FACE_FILE


def wrist_face_empty_schema() -> dict[str, Any]:
    pa, _ = _require_pyarrow()
    return {
        "frame_idx": pa.array([], type=pa.int32()),
        "source_frame_idx": pa.array([], type=pa.int32()),
        "timestamp_sec": pa.array([], type=pa.float64()),
        "hand": pa.array([], type=pa.string()),
        "person_id": pa.array([], type=pa.int32()),
        "person_track_id": pa.array([], type=pa.int32()),
        "wrist_u_px": pa.array([], type=pa.float64()),
        "wrist_v_px": pa.array([], type=pa.float64()),
        "face_y_m": pa.array([], type=pa.float64()),
        "face_z_m": pa.array([], type=pa.float64()),
        "column": pa.array([], type=pa.int32()),
        "layer": pa.array([], type=pa.int32()),
        "trail_segment_id": pa.array([], type=pa.int32()),
    }


def wrist_face_row_from_volume(
    frame: dict[str, Any],
    hand: HandSide,
    vol: Any,
    *,
    person_id: int = -1,
    person_track_id: int = 0,
) -> dict[str, Any] | None:
    """从 VolumeProjectionResult 构建 sidecar 行。"""
    if vol is None or not getattr(vol, "wrist_uv_px", None):
        return None
    if vol.face_y_m is None or vol.face_z_m is None:
        return None
    uv = vol.wrist_uv_px
    row: dict[str, Any] = {
        "frame_idx": int(frame.get("frame_idx") or 0),
        "source_frame_idx": int(frame.get("source_frame_idx") or frame.get("frame_idx") or 0),
        "timestamp_sec": float(frame.get("timestamp_sec") or 0.0),
        "hand": hand,
        "person_id": int(person_id),
        "person_track_id": int(person_track_id),
        "wrist_u_px": float(uv[0]),
        "wrist_v_px": float(uv[1]),
        "face_y_m": float(vol.face_y_m),
        "face_z_m": float(vol.face_z_m),
        "column": int(vol.column) if vol.column is not None else None,
        "layer": int(vol.layer) if vol.layer is not None else None,
        "trail_segment_id": 0,
    }
    return row


def write_wrist_face_parquet(record_dir: Path, rows: list[dict[str, Any]]) -> None:
    pa, pq = _require_pyarrow()
    record_dir = Path(record_dir)
    path = wrist_face_path(record_dir)
    if rows:
        pq.write_table(pa.Table.from_pylist(rows), path, compression="zstd")
        return
    pq.write_table(pa.table(wrist_face_empty_schema()), path, compression="zstd")


def read_wrist_face_parquet(record_dir: Path) -> list[dict[str, Any]]:
    _, pq = _require_pyarrow()
    path = wrist_face_path(record_dir)
    if not path.is_file():
        return []
    return pq.read_table(path).to_pylist()


def load_wrist_face_rows(record_dir: Path) -> list[dict[str, Any]]:
    return read_wrist_face_parquet(record_dir)


def assign_wrist_trail_segment_ids(
    rows: list[dict[str, Any]],
    *,
    jump_threshold_m: float = 1.4,
    max_frame_gap: int = 25,
    max_uv_jump_px: float = 100.0,
) -> list[dict[str, Any]]:
    """按 hand 分组分配 trail_segment_id。"""
    if not rows:
        return rows
    out: list[dict[str, Any]] = []
    for hand in ("left", "right"):
        subset = [r for r in rows if str(r.get("hand")) == hand]
        if not subset:
            continue
        sorted_rows = sorted(subset, key=lambda r: int(r.get("frame_idx") or 0))
        segment_id = 0
        prev: dict[str, Any] | None = None
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
                elif prev.get("face_y_m") is not None and item.get("face_y_m") is not None:
                    dy = float(item["face_y_m"]) - float(prev["face_y_m"])
                    dz = float(item["face_z_m"]) - float(prev["face_z_m"])
                    if (dy * dy + dz * dz) ** 0.5 > jump_threshold_m:
                        segment_id += 1
                elif prev.get("wrist_u_px") is not None and item.get("wrist_u_px") is not None:
                    du = float(item["wrist_u_px"]) - float(prev["wrist_u_px"])
                    dv = float(item["wrist_v_px"]) - float(prev["wrist_v_px"])
                    if (du * du + dv * dv) ** 0.5 > max_uv_jump_px:
                        segment_id += 1
            item["trail_segment_id"] = segment_id
            out.append(item)
            prev = item
    out.sort(key=lambda r: (int(r.get("frame_idx") or 0), str(r.get("hand") or "")))
    return out


def row_to_playback_payload(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "frame_idx": int(row.get("frame_idx") or 0),
        "source_frame_idx": int(row.get("source_frame_idx") or row.get("frame_idx") or 0),
        "timestamp_sec": float(row.get("timestamp_sec") or 0.0),
        "hand": str(row.get("hand") or ""),
        "person_id": int(row.get("person_id") if row.get("person_id") is not None else -1),
        "person_track_id": int(row.get("person_track_id") or 0),
    }
    if row.get("wrist_u_px") is not None and row.get("wrist_v_px") is not None:
        out["wrist_uv_px"] = [float(row["wrist_u_px"]), float(row["wrist_v_px"])]
    if row.get("face_y_m") is not None and row.get("face_z_m") is not None:
        out["face_yz_m"] = [float(row["face_y_m"]), float(row["face_z_m"])]
    if row.get("column") is not None:
        out["column"] = int(row["column"])
    if row.get("layer") is not None:
        out["layer"] = int(row["layer"])
    if row.get("trail_segment_id") is not None:
        out["trail_segment_id"] = int(row["trail_segment_id"])
    return out


def wrist_face_rows_from_frames(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从采集帧上的 wrist_face_sidecar 字段收集 sidecar 行。"""
    rows: list[dict[str, Any]] = []
    for fr in frames:
        if not isinstance(fr, dict):
            continue
        extra = fr.get("wrist_face_sidecar")
        if isinstance(extra, list):
            for row in extra:
                if isinstance(row, dict) and int(row.get("frame_idx") or 0) > 0:
                    rows.append(row)
    return rows


def playback_payload_by_hand(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    left: list[dict[str, Any]] = []
    right: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        payload = row_to_playback_payload(row)
        hand = str(row.get("hand") or "")
        if hand == "left":
            left.append(payload)
        elif hand == "right":
            right.append(payload)
    left.sort(key=lambda r: int(r.get("frame_idx") or 0))
    right.sort(key=lambda r: int(r.get("frame_idx") or 0))
    return {"left": left, "right": right}
