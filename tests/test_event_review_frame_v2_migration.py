"""Lossless event-review schema v2 conversion tests."""

from __future__ import annotations

import unittest

from event_review_frame_v2 import (
    EVENT_REVIEW_SCHEMA_V2,
    LegacyReviewMigrationRequired,
    SOURCE_EXPLICIT_CONFIRMED,
    SOURCE_LEGACY_FRAME_EVENT,
    SOURCE_LEGACY_PER_BOX,
    detect_review_format,
    frame_v2_entry_to_playback_row,
    legacy_entry_to_binding,
    load_verified_items_for_write,
    migrate_verified_true_to_frame_v2,
    upsert_binding,
    verify_frame_v2_verified_true,
)
from scripts.data.migrate_event_review_to_frame_v2 import (
    _candidate_payload,
    resolve_record_filter,
)


class MigrateRecordFilterTest(unittest.TestCase):
    RECORD_IDS = [
        "rtmpose-m/2-5-1-(2)/00000001088000200_seg01_24-00_to_25-45_rtmpose_m",
        "rtmpose-m/2-5-1-(2)/00000001088000200_seg01_12-33_to_21-40_rtmpose_m",
        "rtmpose-l/2-5-1-(2)/00000001088000200_seg01_24-00_to_25-45_rtmpose_l",
    ]

    def test_empty_filter_keeps_every_record(self) -> None:
        self.assertEqual(resolve_record_filter(self.RECORD_IDS, ""), self.RECORD_IDS)

    def test_exact_record_id_wins_over_substring(self) -> None:
        target = self.RECORD_IDS[0]
        self.assertEqual(resolve_record_filter(self.RECORD_IDS, target), [target])

    def test_unique_substring_resolves_to_one_record(self) -> None:
        self.assertEqual(
            resolve_record_filter(self.RECORD_IDS, "seg01_12-33_to_21-40"),
            [self.RECORD_IDS[1]],
        )

    def test_ambiguous_substring_refuses_to_guess(self) -> None:
        # 同一视频段在两个 pose tier 下都有记录，必须报错而不是随便挑一条替用户改数据。
        with self.assertRaises(ValueError) as ctx:
            resolve_record_filter(self.RECORD_IDS, "seg01_24-00_to_25-45")
        self.assertIn("匹配到多条记录", str(ctx.exception))

    def test_unmatched_filter_raises(self) -> None:
        with self.assertRaises(ValueError):
            resolve_record_filter(self.RECORD_IDS, "not-a-record")


class EventReviewFrameV2MigrationTest(unittest.TestCase):
    def test_explicit_confirmed_is_only_safe_default_truth(self) -> None:
        binding, warning = legacy_entry_to_binding(
            {
                "event_type": "collision",
                "frame_idx": 1272,
                "box_tokens": ["Box_2014"],
                "confirmed_box_tokens": ["Box_2015"],
                "person_id": 0,
            },
            source_format=SOURCE_EXPLICIT_CONFIRMED,
        )
        self.assertIsNone(warning)
        self.assertEqual(binding["confirmed_box_tokens"], ["Box_2015"])
        self.assertEqual(binding["person_id"], 0)

    def test_default_never_guesses_single_detected_box_is_truth(self) -> None:
        row = {
            "event_type": "collision",
            "frame_idx": 1272,
            "box_tokens": ["Box_2014"],
            "person_id": 0,
        }
        binding, warning = legacy_entry_to_binding(
            row,
            source_format=SOURCE_EXPLICIT_CONFIRMED,
        )
        self.assertIsNone(binding)
        self.assertIn("不能猜测", warning)

        converted, stats = migrate_verified_true_to_frame_v2([row])
        self.assertEqual(converted, [])
        self.assertEqual(stats.unresolved_entries, 1)
        self.assertEqual(stats.unresolved_items, [row])
        self.assertEqual(stats.cleared_entries, 0)

    def test_per_box_profile_requires_one_box_and_converts(self) -> None:
        rows = [
            {
                "event_type": "collision",
                "frame_idx": 10,
                "box_tokens": ["Box_1"],
                "person_id": 0,
            },
            {
                "event_type": "collision",
                "frame_idx": 10,
                "box_tokens": ["Box_2"],
                "person_id": 1,
            },
        ]
        converted, stats = migrate_verified_true_to_frame_v2(
            rows,
            source_format=SOURCE_LEGACY_PER_BOX,
        )
        self.assertEqual(stats.unresolved_entries, 0)
        self.assertEqual(len(converted), 1)
        self.assertEqual(len(converted[0]["bindings"]), 2)

    def test_frame_event_profile_preserves_multi_box_as_one_binding(self) -> None:
        converted, stats = migrate_verified_true_to_frame_v2(
            [
                {
                    "event_type": "collision",
                    "frame_idx": 20,
                    "box_tokens": ["Box_1", "Box_2"],
                    "person_id": 0,
                }
            ],
            source_format=SOURCE_LEGACY_FRAME_EVENT,
        )
        self.assertEqual(stats.unresolved_entries, 0)
        self.assertEqual(
            converted[0]["bindings"][0]["confirmed_box_tokens"],
            ["Box_1", "Box_2"],
        )

    def test_same_frame_explicit_rows_keep_both_people(self) -> None:
        rows = [
            {
                "event_type": "collision",
                "frame_idx": 1272,
                "box_tokens": ["Box_2014", "Box_2015"],
                "confirmed_box_tokens": ["Box_2014"],
                "person_id": 0,
            },
            {
                "event_type": "collision",
                "frame_idx": 1272,
                "box_tokens": ["Box_2014", "Box_2015"],
                "confirmed_box_tokens": ["Box_2015"],
                "person_id": 1,
            },
        ]
        converted, stats = migrate_verified_true_to_frame_v2(rows)
        self.assertEqual(stats.binding_count, 2)
        self.assertEqual(len(converted), 1)
        self.assertEqual(
            {
                (binding["person_id"], tuple(binding["confirmed_box_tokens"]))
                for binding in converted[0]["bindings"]
            },
            {(0, ("Box_2014",)), (1, ("Box_2015",))},
        )

    def test_v2_roundtrip_does_not_dedupe_bindings_by_detection_signature(self) -> None:
        frame = {
            "frame_idx": 1272,
            "source_frame_idx": 1272,
            "detected_event_types": ["collision"],
            "detected_box_tokens": ["Box_2014", "Box_2015"],
            "bindings": [
                {"person_id": 0, "confirmed_box_tokens": ["Box_2014"]},
                {"person_id": 1, "confirmed_box_tokens": ["Box_2015"]},
            ],
        }
        raw = {"schema": EVENT_REVIEW_SCHEMA_V2, "verified_true": [frame]}
        loaded = load_verified_items_for_write(raw)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(len(loaded[0]["bindings"]), 2)
        playback = frame_v2_entry_to_playback_row(loaded[0])
        self.assertEqual(playback["confirmed_box_tokens"], ["Box_2014", "Box_2015"])
        self.assertNotIn("person_id", playback)

    def test_upsert_one_person_preserves_other_person(self) -> None:
        bindings = [
            {"person_id": 0, "confirmed_box_tokens": ["Box_2014"]},
            {"person_id": 1, "confirmed_box_tokens": ["Box_2015"]},
        ]
        updated = upsert_binding(
            bindings,
            {"person_id": 0, "confirmed_box_tokens": ["Box_2016"]},
        )
        self.assertEqual(len(updated), 2)
        self.assertIn(
            {"person_id": 1, "confirmed_box_tokens": ["Box_2015"]},
            updated,
        )
        self.assertIn(
            {"person_id": 0, "confirmed_box_tokens": ["Box_2016"]},
            updated,
        )

    def test_upsert_can_upgrade_person_binding_with_track_without_duplication(self) -> None:
        bindings = [
            {"person_id": 0, "confirmed_box_tokens": ["Box_2014"]},
            {"person_id": 1, "confirmed_box_tokens": ["Box_2015"]},
        ]
        updated = upsert_binding(
            bindings,
            {
                "person_id": 0,
                "person_track_id": "track-p0",
                "confirmed_box_tokens": ["Box_2016"],
            },
        )
        self.assertEqual(len(updated), 2)
        self.assertIn(
            {
                "person_id": 0,
                "person_track_id": "track-p0",
                "confirmed_box_tokens": ["Box_2016"],
            },
            updated,
        )
        self.assertIn(
            {"person_id": 1, "confirmed_box_tokens": ["Box_2015"]},
            updated,
        )

    def test_mixed_legacy_is_detected_and_write_is_blocked(self) -> None:
        raw = {
            "schema": 1,
            "verified_true": [
                {
                    "event_type": "collision",
                    "frame_idx": 1,
                    "box_tokens": ["Box_1"],
                    "confirmed_box_tokens": ["Box_1"],
                },
                {
                    "event_type": "collision",
                    "frame_idx": 2,
                    "box_tokens": ["Box_2"],
                },
            ],
        }
        self.assertEqual(detect_review_format(raw), "mixed-legacy")
        with self.assertRaises(LegacyReviewMigrationRequired):
            load_verified_items_for_write(raw)

        candidate, stats = _candidate_payload(
            raw,
            source_format=SOURCE_EXPLICIT_CONFIRMED,
            timeline_by_frame={},
        )
        self.assertEqual(stats["unresolved_entries"], 1)
        self.assertEqual(candidate["verified_true"][0]["frame_idx"], 1)
        self.assertEqual(candidate["unresolved_legacy"], [raw["verified_true"][1]])

    def test_verify_rejects_duplicate_frame_records(self) -> None:
        frame = {
            "frame_idx": 1,
            "source_frame_idx": 1,
            "detected_event_types": [],
            "detected_box_tokens": [],
            "bindings": [{"confirmed_box_tokens": ["Box_1"]}],
        }
        issues = verify_frame_v2_verified_true([frame, frame])
        self.assertTrue(any("出现多条" in issue for issue in issues))


if __name__ == "__main__":
    unittest.main()
