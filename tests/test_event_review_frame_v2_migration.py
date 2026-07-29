"""event_review schema v2 迁移逻辑测试。"""

from __future__ import annotations

import unittest

from event_review_frame_v2 import (
    EVENT_REVIEW_SCHEMA_V2,
    is_frame_v2_review,
    legacy_entry_to_binding,
    migrate_verified_true_to_frame_v2,
    verify_frame_v2_verified_true,
)


class EventReviewFrameV2MigrationTest(unittest.TestCase):
    def test_legacy_single_box_without_confirmed_becomes_binding(self) -> None:
        binding, warn = legacy_entry_to_binding(
            {
                "event_type": "collision",
                "frame_idx": 1272,
                "box_tokens": ["Box_2015"],
            }
        )
        self.assertIsNone(warn)
        self.assertEqual(binding["confirmed_box_tokens"], ["Box_2015"])

    def test_legacy_with_confirmed_does_not_use_other_box_tokens(self) -> None:
        binding, warn = legacy_entry_to_binding(
            {
                "event_type": "collision",
                "frame_idx": 165,
                "box_tokens": ["Box_2013"],
                "confirmed_box_tokens": ["Box_2014"],
                "person_id": 0,
            }
        )
        self.assertIsNone(warn)
        self.assertEqual(binding["confirmed_box_tokens"], ["Box_2014"])
        self.assertEqual(binding["person_id"], 0)

    def test_same_frame_two_legacy_entries_become_two_bindings(self) -> None:
        legacy = [
            {
                "event_type": "collision",
                "frame_idx": 1272,
                "box_tokens": ["Box_2014"],
                "person_id": 0,
            },
            {
                "event_type": "collision",
                "frame_idx": 1272,
                "box_tokens": ["Box_2015"],
                "confirmed_box_tokens": ["Box_2015"],
                "person_id": 1,
            },
        ]
        timeline = {
            1272: {
                "frame_idx": 1272,
                "collisions": ["Box_2014", "Box_2015"],
                "alarm_collisions": [],
            }
        }
        out, stats = migrate_verified_true_to_frame_v2(legacy, timeline_by_frame=timeline)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["frame_idx"], 1272)
        bindings = out[0]["bindings"]
        self.assertEqual(len(bindings), 2)
        confirmed_sets = {tuple(b["confirmed_box_tokens"]) for b in bindings}
        self.assertIn(("Box_2014",), confirmed_sets)
        self.assertIn(("Box_2015",), confirmed_sets)
        self.assertEqual(stats.output_frame_count, 1)
        self.assertEqual(stats.binding_count, 2)

    def test_no_confirmed_multi_box_legacy_is_skipped_not_merged(self) -> None:
        legacy = [
            {
                "event_type": "collision",
                "frame_idx": 900,
                "box_tokens": ["Box_2014", "Box_2015"],
            }
        ]
        out, stats = migrate_verified_true_to_frame_v2(legacy)
        self.assertEqual(out, [])
        self.assertEqual(stats.skipped_entries, 1)
        self.assertIn(900, stats.ambiguous_frames)

    def test_v2_review_detected(self) -> None:
        raw = {
            "schema": EVENT_REVIEW_SCHEMA_V2,
            "verified_true": [
                {
                    "frame_idx": 10,
                    "event_type": "collision",
                    "box_tokens": ["Box_1"],
                    "bindings": [{"confirmed_box_tokens": ["Box_1"], "person_id": 0}],
                }
            ],
        }
        self.assertTrue(is_frame_v2_review(raw))
        self.assertEqual(verify_frame_v2_verified_true(raw["verified_true"]), [])


if __name__ == "__main__":
    unittest.main()
