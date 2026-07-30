from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import pose_store
import review_store
from api.routes import http as http_routes
from config_loader import resolve_app_paths


class EventReviewConcurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.paths = resolve_app_paths({"paths": {}}, base=self.root)
        record_path = self.paths.json_dir / "rtmpose-m" / "camera-1" / "clip.json"
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text("{}", encoding="utf-8")
        self.locator = pose_store.RecordLocator(
            record_id="rtmpose-m/camera-1/clip",
            storage=pose_store.STORAGE_V1_JSON,
            path=record_path,
        )
        self.paths_patch = patch.object(
            review_store,
            "resolve_app_paths",
            return_value=self.paths,
        )
        self.paths_patch.start()

    def tearDown(self) -> None:
        self.paths_patch.stop()
        self.temp_dir.cleanup()

    def test_event_total_refresh_cannot_overwrite_new_annotation(self) -> None:
        pose_store.save_event_review(
            self.locator,
            [],
            status=pose_store.REVIEW_STATUS_IN_PROGRESS,
            event_total=1,
        )

        cache_at_write = threading.Event()
        release_cache_write = threading.Event()
        save_started = threading.Event()
        save_finished = threading.Event()
        errors: list[BaseException] = []
        original_writer = pose_store._write_json_atomic

        def blocking_writer(path: Path, payload: dict) -> None:
            if threading.current_thread().name == "cache-thread":
                cache_at_write.set()
                if not release_cache_write.wait(timeout=5):
                    raise TimeoutError("测试未释放 event_total 缓存写入")
            original_writer(path, payload)

        verified_entry = {
            "event_type": "collision",
            "frame_idx": 1272,
            "source_frame_idx": 1272,
            "box_tokens": ["Box_2015"],
            "confirmed_box_tokens": ["Box_2015"],
            "person_id": 0,
        }

        def refresh_event_total() -> None:
            try:
                pose_store.cache_event_review_total(self.locator, 939)
            except BaseException as exc:  # pragma: no cover - 仅用于线程错误回传
                errors.append(exc)

        def save_annotation() -> None:
            save_started.set()
            try:
                pose_store.save_event_review(
                    self.locator,
                    [verified_entry],
                    status=pose_store.REVIEW_STATUS_IN_PROGRESS,
                    event_total=939,
                )
            except BaseException as exc:  # pragma: no cover - 仅用于线程错误回传
                errors.append(exc)
            finally:
                save_finished.set()

        with patch.object(pose_store, "_write_json_atomic", side_effect=blocking_writer):
            cache_thread = threading.Thread(target=refresh_event_total, name="cache-thread")
            cache_thread.start()
            self.assertTrue(cache_at_write.wait(timeout=5))

            save_thread = threading.Thread(target=save_annotation, name="annotation-thread")
            save_thread.start()
            self.assertTrue(save_started.wait(timeout=5))
            self.assertFalse(
                save_finished.wait(timeout=0.1),
                "标注保存应等待同一 review 的缓存写入完成，不能并发覆盖",
            )

            release_cache_write.set()
            cache_thread.join(timeout=5)
            save_thread.join(timeout=5)

        self.assertFalse(cache_thread.is_alive())
        self.assertFalse(save_thread.is_alive())
        self.assertEqual(errors, [])

        saved = pose_store.load_event_review(self.locator)
        self.assertEqual(saved["event_total"], 939)
        self.assertEqual(len(saved.get("verified_true") or []), 1)
        self.assertEqual(saved["verified_true"][0]["frame_idx"], 1272)
        self.assertEqual(saved["verified_true"][0]["confirmed_box_tokens"], ["Box_2015"])

        review_path = pose_store.event_review_path(self.locator)
        with review_path.open(encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertIsInstance(on_disk, dict)
        self.assertEqual(on_disk.get("schema"), 2)
        self.assertEqual(len(on_disk.get("verified_true") or []), 1)
        self.assertEqual(on_disk["verified_true"][0]["frame_idx"], 1272)
        self.assertIn("bindings", on_disk["verified_true"][0])

    def test_save_event_review_writes_v2_and_loads_back(self) -> None:
        entry = {
            "event_type": "collision",
            "frame_idx": 1272,
            "source_frame_idx": 1272,
            "box_tokens": ["Box_2015"],
            "confirmed_box_tokens": ["Box_2015"],
            "person_id": 0,
        }
        pose_store.save_event_review(
            self.locator,
            [entry],
            status=pose_store.REVIEW_STATUS_IN_PROGRESS,
            event_total=1,
        )
        loaded = pose_store.load_event_review(self.locator)
        self.assertEqual(loaded.get("schema"), 2)
        self.assertEqual(len(loaded.get("verified_true") or []), 1)
        self.assertEqual(loaded["verified_true"][0]["confirmed_box_tokens"], ["Box_2015"])
        self.assertEqual(loaded["verified_true"][0]["person_id"], 0)

        on_disk = json.loads(
            pose_store.event_review_path(self.locator).read_text(encoding="utf-8")
        )
        self.assertEqual(on_disk.get("schema"), 2)
        frame_entry = on_disk["verified_true"][0]
        self.assertEqual(frame_entry["frame_idx"], 1272)
        self.assertEqual(
            frame_entry["bindings"][0]["confirmed_box_tokens"],
            ["Box_2015"],
        )

    def test_cache_event_review_total_preserves_v2_verified_true(self) -> None:
        from event_review_frame_v2 import EVENT_REVIEW_SCHEMA_V2

        review_path = pose_store.event_review_path(self.locator)
        pose_store._write_json_atomic(
            review_path,
            {
                "schema": EVENT_REVIEW_SCHEMA_V2,
                "record_id": self.locator.record_id,
                "verified_true": [
                    {
                        "frame_idx": 1272,
                        "source_frame_idx": 1272,
                        "event_type": "collision",
                        "box_tokens": ["Box_2015"],
                        "bindings": [
                            {"confirmed_box_tokens": ["Box_2015"], "person_id": 0},
                        ],
                    }
                ],
                "event_total": 1,
            },
        )

        pose_store.cache_event_review_total(self.locator, 939)

        on_disk = json.loads(review_path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk.get("schema"), EVENT_REVIEW_SCHEMA_V2)
        self.assertEqual(on_disk.get("event_total"), 939)
        self.assertEqual(len(on_disk.get("verified_true") or []), 1)
        self.assertIn("bindings", on_disk["verified_true"][0])

        loaded = pose_store.load_event_review(self.locator)
        self.assertEqual(len(loaded.get("verified_true") or []), 1)
        self.assertEqual(loaded["verified_true"][0]["confirmed_box_tokens"], ["Box_2015"])

    def test_zero_event_refresh_preserves_on_disk_annotation_without_status(self) -> None:
        entry = {
            "event_type": "collision",
            "frame_idx": 1272,
            "source_frame_idx": 1272,
            "box_tokens": ["Box_2014"],
        }
        review_path = pose_store.event_review_path(self.locator)
        pose_store._write_json_atomic(
            review_path,
            {
                "schema": pose_store.EVENT_REVIEW_SCHEMA,
                "record_id": self.locator.record_id,
                "verified_true": [entry],
            },
        )

        refreshed = pose_store.ensure_no_collision_review_completed(
            self.locator,
            event_count=0,
        )

        self.assertNotEqual(
            refreshed["status"],
            pose_store.REVIEW_STATUS_NO_COLLISION,
        )
        with review_path.open(encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["verified_true"], [entry])

    def test_load_events_returns_one_row_for_every_frame(self) -> None:
        rows = [
            {
                "frame_idx": 1,
                "source_frame_idx": 1,
                "timestamp_sec": 0.0,
                "collisions": ["Box_2015", "Box_2014"],
                "alarm_collisions": [],
            },
            {
                "frame_idx": 2,
                "source_frame_idx": 2,
                "timestamp_sec": 0.04,
                "collisions": [],
                "alarm_collisions": [],
            },
            {
                "frame_idx": 3,
                "source_frame_idx": 3,
                "timestamp_sec": 0.08,
                "collisions": ["Box_2014", "Box_2015"],
                "alarm_collisions": ["Box_2015"],
            },
        ]

        with patch.object(pose_store, "load_timeline", return_value=rows):
            events = pose_store.load_events(self.locator)

        self.assertEqual([event["frame_idx"] for event in events], [1, 2, 3])
        self.assertEqual(events[0]["event_type"], "collision")
        self.assertEqual(events[0]["box_tokens"], ["Box_2014", "Box_2015"])
        self.assertEqual(events[1]["event_type"], "frame")
        self.assertEqual(events[1]["box_tokens"], [])
        self.assertEqual(events[2]["event_type"], "alarm")
        self.assertEqual(events[2]["box_tokens"], ["Box_2014", "Box_2015"])

    def test_v2_review_enrich_by_frame_reads_bindings_only(self) -> None:
        from event_review_frame_v2 import EVENT_REVIEW_SCHEMA_V2

        review_path = pose_store.event_review_path(self.locator)
        pose_store._write_json_atomic(
            review_path,
            {
                "schema": EVENT_REVIEW_SCHEMA_V2,
                "record_id": self.locator.record_id,
                "verified_true": [
                    {
                        "frame_idx": 1272,
                        "source_frame_idx": 1272,
                        "event_type": "collision",
                        "box_tokens": ["Box_2014", "Box_2015"],
                        "bindings": [
                            {
                                "confirmed_box_tokens": ["Box_2015"],
                                "person_id": 0,
                            }
                        ],
                    }
                ],
            },
        )
        events = [
            {
                "event_type": "collision",
                "frame_idx": 1272,
                "source_frame_idx": 1272,
                "box_tokens": ["Box_2014", "Box_2015"],
            },
        ]

        enriched = pose_store.enrich_events_with_review(events, self.locator)

        self.assertEqual(len(enriched), 1)
        self.assertTrue(enriched[0]["verified_true"])
        self.assertEqual(enriched[0]["confirmed_box_tokens"], ["Box_2015"])
        self.assertEqual(enriched[0]["person_id"], 0)

    def test_load_event_review_safely_projects_explicit_legacy_truth(self) -> None:
        pose_store._write_json_atomic(
            pose_store.event_review_path(self.locator),
            {
                "schema": pose_store.EVENT_REVIEW_SCHEMA,
                "record_id": self.locator.record_id,
                "verified_true": [
                    {
                        "event_type": "collision",
                        "frame_idx": 1272,
                        "box_tokens": ["Box_2015"],
                        "confirmed_box_tokens": ["Box_2015"],
                    }
                ],
            },
        )
        loaded = pose_store.load_event_review(self.locator)
        self.assertEqual(len(loaded["verified_true"]), 1)
        self.assertEqual(
            loaded["verified_true"][0]["confirmed_box_tokens"],
            ["Box_2015"],
        )
        self.assertFalse(loaded["migration_required"])

    def test_ambiguous_legacy_patch_returns_409_without_changing_file(self) -> None:
        path = pose_store.event_review_path(self.locator)
        payload = {
            "schema": 1,
            "record_id": self.locator.record_id,
            "verified_true": [
                {
                    "event_type": "collision",
                    "frame_idx": 1272,
                    "box_tokens": ["Box_2014"],
                }
            ],
        }
        pose_store._write_json_atomic(path, payload)
        before = path.read_bytes()

        with self.assertRaises(Exception) as raised:
            http_routes._patch_record_event_review_locked(
                self.locator.record_id,
                self.locator,
                {
                    "action": "toggle",
                    "event": {
                        "event_type": "collision",
                        "frame_idx": 1272,
                        "box_tokens": ["Box_2014"],
                    },
                    "verified_true": False,
                    "event_total": 1,
                },
            )
        self.assertEqual(getattr(raised.exception, "status_code", None), 409)
        self.assertEqual(path.read_bytes(), before)

    def test_frame_toggle_preserves_other_bindings_then_cancel_removes_only_frame(self) -> None:
        old_entries = [
            {
                "event_type": "collision",
                "frame_idx": 1272,
                "source_frame_idx": 1272,
                "box_tokens": ["Box_2014", "Box_2015"],
                "confirmed_box_tokens": ["Box_2014"],
                "person_id": 0,
            },
            {
                "event_type": "collision",
                "frame_idx": 1272,
                "source_frame_idx": 1272,
                "box_tokens": ["Box_2014", "Box_2015"],
                "confirmed_box_tokens": ["Box_2015"],
                "person_id": 1,
            },
            {
                "event_type": "collision",
                "frame_idx": 1273,
                "source_frame_idx": 1273,
                "box_tokens": ["Box_2015"],
                "confirmed_box_tokens": ["Box_2015"],
                "person_id": 0,
            },
        ]
        pose_store.save_event_review(
            self.locator,
            old_entries,
            status=pose_store.REVIEW_STATUS_IN_PROGRESS,
            event_total=2,
        )
        frame_event = {
            "event_type": "collision",
            "frame_idx": 1272,
            "source_frame_idx": 1272,
            "box_tokens": ["Box_2014", "Box_2015"],
            "confirmed_box_tokens": ["Box_2015"],
            "person_id": 0,
        }

        with patch("record_index_store.refresh_record_summary"):
            http_routes._patch_record_event_review_locked(
                self.locator.record_id,
                self.locator,
                {
                    "action": "toggle",
                    "event": frame_event,
                    "verified_true": True,
                    "event_total": 2,
                },
            )

        migrated = json.loads(
            pose_store.event_review_path(self.locator).read_text(encoding="utf-8")
        )["verified_true"]
        migrated_target = [entry for entry in migrated if entry["frame_idx"] == 1272]
        self.assertEqual(len(migrated_target), 1)
        self.assertEqual(
            migrated_target[0]["bindings"][0]["confirmed_box_tokens"],
            ["Box_2015"],
        )
        self.assertEqual(len(migrated_target[0]["bindings"]), 2)
        self.assertIn(
            {
                "confirmed_box_tokens": ["Box_2015"],
                "person_id": 1,
            },
            migrated_target[0]["bindings"],
        )
        self.assertEqual(len([entry for entry in migrated if entry["frame_idx"] == 1273]), 1)

        with patch("record_index_store.refresh_record_summary"):
            http_routes._patch_record_event_review_locked(
                self.locator.record_id,
                self.locator,
                {
                    "action": "toggle",
                    "event": frame_event,
                    "verified_true": False,
                    "event_total": 2,
                },
            )

        saved = json.loads(
            pose_store.event_review_path(self.locator).read_text(encoding="utf-8")
        )["verified_true"]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["frame_idx"], 1273)

    def test_plain_frame_can_be_marked_only_with_confirmed_box(self) -> None:
        empty_frame = {
            "event_type": "frame",
            "frame_idx": 100,
            "source_frame_idx": 100,
            "box_tokens": [],
        }
        self.assertIsNone(pose_store.normalize_review_entry(empty_frame))

        marked_frame = {
            **empty_frame,
            "confirmed_box_tokens": ["Box_2015"],
            "person_id": 0,
        }
        normalized = pose_store.normalize_review_entry(marked_frame)
        self.assertIsNotNone(normalized)
        self.assertEqual(normalized["confirmed_box_tokens"], ["Box_2015"])

        pose_store.save_event_review(
            self.locator,
            [marked_frame],
            status=pose_store.REVIEW_STATUS_IN_PROGRESS,
            event_total=1,
        )
        with patch("record_index_store.refresh_record_summary"):
            http_routes._patch_record_event_review_locked(
                self.locator.record_id,
                self.locator,
                {
                    "action": "toggle",
                    "event": empty_frame,
                    "verified_true": False,
                    "event_total": 1,
                },
            )
        self.assertEqual(
            json.loads(
                pose_store.event_review_path(self.locator).read_text(encoding="utf-8")
            )["verified_true"],
            [],
        )

    def test_range_write_includes_every_frame_and_preserves_other_binding(self) -> None:
        pose_store.save_event_review(
            self.locator,
            [
                {
                    "frame_idx": 11,
                    "source_frame_idx": 11,
                    "detected_event_types": [],
                    "detected_box_tokens": [],
                    "bindings": [
                        {
                            "confirmed_box_tokens": ["Box_2014"],
                            "person_id": 0,
                        },
                        {
                            "confirmed_box_tokens": ["Box_2099"],
                            "person_id": 1,
                        }
                    ],
                }
            ],
            status=pose_store.REVIEW_STATUS_IN_PROGRESS,
            event_total=30,
        )
        events = [
            {
                "event_type": "collision" if frame != 11 else "frame",
                "frame_idx": frame,
                "source_frame_idx": frame,
                "box_tokens": ["Box_2015"] if frame != 11 else [],
                "confirmed_box_tokens": ["Box_2015"],
                "person_id": 0,
                "person_track_id": "track-p0",
            }
            for frame in range(10, 13)
        ]

        with patch("record_index_store.refresh_record_summary"):
            response = http_routes._patch_record_event_review_locked(
                self.locator.record_id,
                self.locator,
                {
                    "action": "set_range_verified",
                    "range_start": 10,
                    "range_end": 12,
                    "events": events,
                    "event_total": 30,
                },
            )

        body = json.loads(response.body)
        self.assertEqual(body["range_applied_count"], 3)
        saved = json.loads(
            pose_store.event_review_path(self.locator).read_text(encoding="utf-8")
        )["verified_true"]
        by_frame = {entry["frame_idx"]: entry for entry in saved}
        self.assertEqual(sorted(by_frame), [10, 11, 12])
        for frame in range(10, 13):
            self.assertIn(
                {
                    "confirmed_box_tokens": ["Box_2015"],
                    "person_id": 0,
                    "person_track_id": "track-p0",
                },
                by_frame[frame]["bindings"],
            )
        self.assertIn(
            {
                "confirmed_box_tokens": ["Box_2099"],
                "person_id": 1,
            },
            by_frame[11]["bindings"],
        )
        self.assertEqual(len(by_frame[11]["bindings"]), 2)

    def test_range_write_rejects_missing_frame_without_changing_file(self) -> None:
        pose_store.save_event_review(
            self.locator,
            [],
            status=pose_store.REVIEW_STATUS_IN_PROGRESS,
            event_total=30,
        )
        path = pose_store.event_review_path(self.locator)
        before = path.read_bytes()
        events = [
            {
                "event_type": "frame",
                "frame_idx": frame,
                "source_frame_idx": frame,
                "box_tokens": [],
                "confirmed_box_tokens": ["Box_2015"],
                "person_id": 0,
            }
            for frame in (20, 22)
        ]

        with self.assertRaisesRegex(Exception, "缺少帧"):
            http_routes._patch_record_event_review_locked(
                self.locator.record_id,
                self.locator,
                {
                    "action": "set_range_verified",
                    "range_start": 20,
                    "range_end": 22,
                    "events": events,
                    "event_total": 30,
                },
            )
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
