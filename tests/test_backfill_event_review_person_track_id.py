"""event_review 人员追踪 ID 回填测试。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import pose_store
from scripts.data.backfill_event_review_person_track_id import (
    backfill_review_payload,
    review_frame_ids,
)


class BackfillEventReviewPersonTrackIdTest(unittest.TestCase):
    def test_backfills_v2_bindings_and_legacy_rows_without_overwriting(self) -> None:
        review = {
            "schema": 2,
            "verified_true": [
                {
                    "frame_idx": 10,
                    "bindings": [
                        {
                            "person_id": 0,
                            "confirmed_box_tokens": ["Box_1"],
                        },
                        {
                            "person_id": 1,
                            "person_track_id": "202",
                            "confirmed_box_tokens": ["Box_2"],
                        },
                    ],
                },
                {
                    "frame_idx": 11,
                    "person_id": 0,
                    "confirmed_box_tokens": ["Box_3"],
                },
            ],
        }
        tracks = {
            10: {0: "101", 1: "999"},
            11: {0: "101"},
        }

        stats = backfill_review_payload(review, tracks)

        self.assertEqual(review_frame_ids(review), [10, 11])
        self.assertEqual(
            review["verified_true"][0]["bindings"][0]["person_track_id"],
            "101",
        )
        self.assertEqual(
            review["verified_true"][0]["bindings"][1]["person_track_id"],
            "202",
        )
        self.assertEqual(review["verified_true"][1]["person_track_id"], "101")
        self.assertEqual(stats.added, 2)
        self.assertEqual(stats.conflicts, 1)

    def test_missing_identity_or_skeleton_track_is_not_guessed(self) -> None:
        review = {
            "verified_true": [
                {
                    "frame_idx": 20,
                    "bindings": [
                        {"confirmed_box_tokens": ["Box_1"]},
                        {
                            "person_id": 7,
                            "confirmed_box_tokens": ["Box_2"],
                        },
                    ],
                }
            ]
        }

        stats = backfill_review_payload(review, {20: {0: "100"}})

        self.assertEqual(stats.added, 0)
        self.assertEqual(stats.missing_person_id, 1)
        self.assertEqual(stats.missing_skeleton_track, 1)
        self.assertNotIn(
            "person_track_id",
            review["verified_true"][0]["bindings"][1],
        )

    def test_save_layer_backfills_missing_tracks_from_skeleton(self) -> None:
        entries = [
            {
                "frame_idx": 30,
                "bindings": [
                    {
                        "person_id": 0,
                        "confirmed_box_tokens": ["Box_1"],
                    },
                    {
                        "person_id": 1,
                        "person_track_id": "kept",
                        "confirmed_box_tokens": ["Box_2"],
                    },
                ],
            }
        ]
        frames = [
            {
                "frame_idx": 30,
                "persons": [
                    {"person_id": 0, "person_track_id": 0},
                    {"person_id": 1, "person_track_id": 9},
                ],
            }
        ]

        with patch.object(pose_store, "load_frames_range", return_value=frames):
            added = pose_store.backfill_verified_person_track_ids(None, entries)

        self.assertEqual(added, 1)
        self.assertEqual(entries[0]["bindings"][0]["person_track_id"], "0")
        self.assertEqual(entries[0]["bindings"][1]["person_track_id"], "kept")


if __name__ == "__main__":
    unittest.main()
