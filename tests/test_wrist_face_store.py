"""wrist_face sidecar 存储测试。"""

from __future__ import annotations

from wrist_face_store import (
    assign_wrist_trail_segment_ids,
    playback_payload_by_hand,
    wrist_face_row_from_volume,
)


class _FakeVol:
    wrist_uv_px = [100.0, 200.0]
    face_y_m = 1.0
    face_z_m = 0.5
    column = 2
    layer = 3


def test_wrist_face_row_from_volume():
    row = wrist_face_row_from_volume({"frame_idx": 5, "timestamp_sec": 1.0}, "left", _FakeVol(), person_id=0)
    assert row is not None
    assert row["hand"] == "left"
    assert row["face_y_m"] == 1.0


def test_playback_payload_by_hand():
    rows = [
        {"frame_idx": 1, "hand": "left", "face_y_m": 1.0, "face_z_m": 0.5, "wrist_u_px": 1.0, "wrist_v_px": 2.0},
        {"frame_idx": 2, "hand": "right", "face_y_m": 1.2, "face_z_m": 0.6, "wrist_u_px": 3.0, "wrist_v_px": 4.0},
    ]
    payload = playback_payload_by_hand(rows)
    assert len(payload["left"]) == 1
    assert len(payload["right"]) == 1
    assert payload["left"][0]["face_yz_m"] == [1.0, 0.5]


def test_assign_wrist_trail_segment_ids():
    rows = assign_wrist_trail_segment_ids(
        [
            {"frame_idx": 1, "hand": "left", "face_y_m": 0.0, "face_z_m": 0.0, "wrist_u_px": 0.0, "wrist_v_px": 0.0},
            {"frame_idx": 2, "hand": "left", "face_y_m": 0.1, "face_z_m": 0.0, "wrist_u_px": 1.0, "wrist_v_px": 0.0},
        ]
    )
    assert rows[0]["trail_segment_id"] == 0
    assert rows[1]["trail_segment_id"] == 0
