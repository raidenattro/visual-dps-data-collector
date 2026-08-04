from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

import pose_store
import video_transcode


class LongVideoPreviewPlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"source")
        self.config = {
            "video": {
                "transcode_height": 480,
                "preview_min_frames": 10_000,
                "preview_cache_dir": str(self.root / "cache"),
                "preview_workers": 1,
            }
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_all_videos_at_threshold_need_preview_regardless_of_height(self) -> None:
        with (
            patch.object(video_transcode, "load_config_file", return_value=self.config),
            patch.object(video_transcode, "read_video_height", return_value=1080),
            patch.object(video_transcode, "read_video_frame_count", return_value=9_999),
        ):
            short_plan = video_transcode._preview_plan(self.source)
        self.assertFalse(short_plan["needs_transcode"])
        self.assertTrue(short_plan["use_original"])

        with (
            patch.object(video_transcode, "load_config_file", return_value=self.config),
            patch.object(video_transcode, "read_video_height", return_value=1080),
            patch.object(video_transcode, "read_video_frame_count", return_value=10_000),
        ):
            long_plan = video_transcode._preview_plan(self.source)
        self.assertTrue(long_plan["needs_transcode"])
        self.assertFalse(long_plan["use_original"])
        self.assertNotEqual(long_plan["preview"].parent, self.source.parent)
        self.assertTrue(long_plan["preview"].is_relative_to(self.root / "cache"))

        for source_height in (360, 480):
            with (
                patch.object(video_transcode, "load_config_file", return_value=self.config),
                patch.object(video_transcode, "read_video_height", return_value=source_height),
                patch.object(video_transcode, "read_video_frame_count", return_value=10_000),
            ):
                same_or_lower_plan = video_transcode._preview_plan(self.source)
                status = video_transcode._status_dict_from_plan(
                    same_or_lower_plan,
                    status="transcoding",
                    progress=0,
                )
            self.assertTrue(same_or_lower_plan["needs_transcode"])
            self.assertFalse(same_or_lower_plan["use_original"])
            self.assertEqual(status["preview_height"], 480)

    def test_gop_is_derived_from_source_fps(self) -> None:
        source = Path(video_transcode.__file__).read_text(encoding="utf-8")
        self.assertIn("gop_frames = max(1, int(round(source_fps)))", source)
        self.assertIn('"-fps_mode",\n        "passthrough"', source)
        self.assertIn("setpts=PTS-STARTPTS", source)


class PlaybackArrowCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.record_dir = Path(self.temp_dir.name) / "record"
        self.record_dir.mkdir()
        timeline = pa.table(
            {
                "frame_idx": [1, 2, 3, 4],
                "source_frame_idx": [1, 2, 3, 4],
                "timestamp_sec": [0.0, 0.04, 0.08, 0.12],
                "infer_width": [640] * 4,
                "infer_height": [480] * 4,
                "collisions": [[], ["Box_1"], [], []],
                "alarm_collisions": [[], [], [], []],
            }
        )
        skeleton = pa.table(
            {
                "frame_idx": [2, 4],
                "source_frame_idx": [2, 4],
                "person_id": [0, 0],
                "keypoints": [[], []],
                "det_bbox": [[], []],
            }
        )
        pq.write_table(timeline, self.record_dir / pose_store.TIMELINE_FILE)
        pq.write_table(skeleton, self.record_dir / pose_store.SKELETON_FILE)
        self.locator = pose_store.RecordLocator(
            record_id="rtmpose-m/camera/record",
            storage=pose_store.STORAGE_V2_PARQUET,
            path=self.record_dir,
        )
        with pose_store._playback_cache_lock:
            pose_store._playback_cache.clear()

    def tearDown(self) -> None:
        with pose_store._playback_cache_lock:
            pose_store._playback_cache.clear()
        self.temp_dir.cleanup()

    def test_frames_are_sliced_from_one_cached_arrow_load(self) -> None:
        original = pq.read_table
        calls: list[str] = []

        def counted(path, *args, **kwargs):
            calls.append(Path(path).name)
            return original(path, *args, **kwargs)

        with patch.object(pq, "read_table", side_effect=counted):
            first = pose_store.load_frames_range(
                self.locator, from_frame_idx=2, to_frame_idx=3
            )
            second = pose_store.load_frames_range(
                self.locator, from_frame_idx=3, to_frame_idx=4
            )

        self.assertEqual([row["frame_idx"] for row in first], [2, 3])
        self.assertEqual([row["frame_idx"] for row in second], [3, 4])
        self.assertEqual(calls.count(pose_store.TIMELINE_FILE), 1)
        self.assertEqual(calls.count(pose_store.SKELETON_FILE), 1)
        self.assertTrue(pose_store.playback_cache_is_warm(self.locator, "tables"))


if __name__ == "__main__":
    unittest.main()
