"""Downstream consumers must use bindings, never detection boxes as truth."""

from __future__ import annotations

import unittest

from api.accuracy_service import build_ground_truth_segments
from export_pose_xlsx import _human_verified_label, _verified_lookup_from_review


class EventReviewDownstreamTest(unittest.TestCase):
    def setUp(self) -> None:
        self.review = {
            "schema": 2,
            "verified_true": [
                {
                    "frame_idx": 10,
                    "source_frame_idx": 10,
                    "event_type": "collision",
                    "box_tokens": ["Box_2014"],
                    "bindings": [
                        {
                            "person_id": 0,
                            "confirmed_box_tokens": ["Box_2015"],
                        },
                        {
                            "person_id": 1,
                            "confirmed_box_tokens": ["Box_2016"],
                        },
                    ],
                },
                {
                    "frame_idx": 11,
                    "source_frame_idx": 11,
                    "event_type": "collision",
                    "box_tokens": ["Box_2014"],
                    "bindings": [
                        {
                            "person_id": 0,
                            "confirmed_box_tokens": ["Box_2015", "Box_2016"],
                        }
                    ],
                },
                {
                    "frame_idx": 13,
                    "source_frame_idx": 13,
                    "event_type": "collision",
                    "box_tokens": ["Box_2014"],
                    "bindings": [
                        {
                            "person_id": 0,
                            "confirmed_box_tokens": ["Box_2015", "Box_2016"],
                        }
                    ],
                },
            ],
        }

    def test_accuracy_uses_binding_union_and_respects_frame_gaps(self) -> None:
        segments = build_ground_truth_segments(self.review["verified_true"])
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0].gt_tokens, ("Box_2015", "Box_2016"))
        self.assertEqual((segments[0].frame_start, segments[0].frame_end), (10, 11))
        self.assertEqual((segments[1].frame_start, segments[1].frame_end), (13, 13))

    def test_excel_lookup_uses_confirmed_not_detected_boxes(self) -> None:
        lookup = _verified_lookup_from_review(self.review)
        self.assertEqual(
            _human_verified_label(
                lookup,
                event_type_zh="碰撞",
                frame_idx=10,
                token="Box_2015",
                person_id=0,
            ),
            "是",
        )
        self.assertEqual(
            _human_verified_label(
                lookup,
                event_type_zh="碰撞",
                frame_idx=10,
                token="Box_2014",
                person_id=0,
            ),
            "未复核",
        )
        self.assertEqual(
            _human_verified_label(
                lookup,
                event_type_zh="碰撞",
                frame_idx=10,
                token="Box_2016",
                person_id=0,
            ),
            "未复核",
        )
        self.assertEqual(
            _human_verified_label(
                lookup,
                event_type_zh="碰撞",
                frame_idx=10,
                token="Box_2016",
                person_id=1,
            ),
            "是",
        )


if __name__ == "__main__":
    unittest.main()
