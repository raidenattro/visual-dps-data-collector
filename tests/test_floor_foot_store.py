"""floor_foot sidecar 存储测试。"""

from __future__ import annotations

from floor_foot_store import (
    floor_foot_row_from_frame,
    floor_foot_rows_from_frames,
    legacy_floor_rows_from_timeline,
    playback_payload_from_rows,
    strip_floor_from_timeline_row,
)


def test_floor_foot_row_from_frame():
    row = floor_foot_row_from_frame(
        {
            "frame_idx": 10,
            "timestamp_sec": 0.5,
            "foot_uv_px": [100.0, 200.0],
            "floor_xy_m": [0.5, 1.2],
            "foot_person_id": 0,
        }
    )
    assert row is not None
    assert row["frame_idx"] == 10
    assert row["foot_u_px"] == 100.0
    assert row["floor_x_m"] == 0.5


def test_legacy_timeline_migration():
    legacy = legacy_floor_rows_from_timeline(
        [{"frame_idx": 1, "foot_u_px": 1.0, "foot_v_px": 2.0, "floor_x_m": 0.1, "floor_y_m": 0.2}]
    )
    assert len(legacy) == 1
    payload = playback_payload_from_rows(legacy)
    assert payload[0]["foot_uv_px"] == [1.0, 2.0]


def test_strip_timeline_floor():
    clean = strip_floor_from_timeline_row(
        {"frame_idx": 1, "floor_x_m": 0.1, "collisions": ["Box_1"]}
    )
    assert "floor_x_m" not in clean
    assert clean["collisions"] == ["Box_1"]


def test_rows_from_frames_skips_empty():
    rows = floor_foot_rows_from_frames([{"frame_idx": 1}, {"frame_idx": 2, "floor_xy_m": [0.0, 0.0]}])
    assert len(rows) == 1
    assert rows[0]["frame_idx"] == 2
