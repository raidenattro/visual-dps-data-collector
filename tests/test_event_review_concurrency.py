from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import pose_store
import review_store
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
        self.assertEqual(saved["verified_true"], [verified_entry])

        review_path = pose_store.event_review_path(self.locator)
        with review_path.open(encoding="utf-8") as f:
            self.assertIsInstance(json.load(f), dict)

    def test_zero_event_refresh_preserves_legacy_annotation_without_status(self) -> None:
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

        self.assertEqual(refreshed["verified_true"], [entry])
        self.assertNotEqual(
            refreshed["status"],
            pose_store.REVIEW_STATUS_NO_COLLISION,
        )

    def test_same_frame_boxes_keep_independent_verified_state(self) -> None:
        marked = {
            "event_type": "collision",
            "frame_idx": 1272,
            "source_frame_idx": 1272,
            "box_tokens": ["Box_2015"],
            "confirmed_box_tokens": ["Box_2015"],
            "person_id": 0,
        }
        pose_store.save_event_review(
            self.locator,
            [marked],
            status=pose_store.REVIEW_STATUS_IN_PROGRESS,
            event_total=2,
        )
        events = [
            {
                "event_type": "collision",
                "frame_idx": 1272,
                "source_frame_idx": 1272,
                "box_tokens": ["Box_2014"],
            },
            {
                "event_type": "collision",
                "frame_idx": 1272,
                "source_frame_idx": 1272,
                "box_tokens": ["Box_2015"],
            },
        ]

        enriched = pose_store.enrich_events_with_review(events, self.locator)

        self.assertFalse(enriched[0]["verified_true"])
        self.assertTrue(enriched[1]["verified_true"])
        self.assertEqual(enriched[1]["person_id"], 0)


if __name__ == "__main__":
    unittest.main()
