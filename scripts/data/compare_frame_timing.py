#!/usr/bin/env python3
"""对比视频帧 PTS 与 timeline timestamp_sec。"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.record_service import locate_record_by_id, video_path_for_record
from pose_store import load_manifest, load_timeline_index


def ffprobe_frames(path: Path, limit: int = 0) -> list[dict]:
    ff = shutil.which("ffprobe")
    if not ff or not path.is_file():
        return []
    cmd = [
        ff,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_frames",
        "-show_entries",
        "frame=best_effort_timestamp_time,pkt_pts_time",
        "-of",
        "json",
        str(path),
    ]
    payload = json.loads(subprocess.check_output(cmd, text=True))
    frames = payload.get("frames") or []
    return frames[:limit] if limit > 0 else frames


def frame_ts(frame: dict) -> float:
    raw = frame.get("best_effort_timestamp_time") or frame.get("pkt_pts_time")
    return float(raw or 0.0)


def main() -> None:
    rid = sys.argv[1] if len(sys.argv) > 1 else (
        "rtmpose-m/1-1-1-(3)/00000000907002400_seg07_11-23_to_12-47_rtmpose_m"
    )
    loc = locate_record_by_id(rid)
    if not loc:
        print("record not found:", rid)
        sys.exit(1)
    m = load_manifest(loc)
    vp = video_path_for_record(rid)
    tl = load_timeline_index(loc)
    frames = ffprobe_frames(vp)

    print("record:", rid)
    print("video:", vp)
    print("manifest fps:", m.get("fps"), "frame_count:", m.get("frame_count"))
    frames = ffprobe_frames(vp)

    fps_m = float(m.get("fps") or 25.0)
    fps_n = round(fps_m) if abs(fps_m - round(fps_m)) < 0.05 else fps_m

    for i in [0, 1, 2, 99, 499, 999, 1000, 1500, 2000, len(tl) - 1]:
        if i >= len(tl) or i >= len(frames):
            continue
        pts = frame_ts(frames[i])
        tts = float(tl[i].get("timestamp_sec") or 0.0)
        fi = int(tl[i].get("frame_idx") or i + 1)
        print(
            f"  fi={fi} ffprobe_pts={pts:.6f} timeline={tts:.6f} "
            f"delta={pts - tts:.6f}"
        )

    print("\nmediaTime -> frame index (compare ffprobe nearest):")
    for mt in [5, 10, 20, 40, 60, 80]:
        idx_m = int(mt * fps_m) + 1
        idx_n = int(mt * fps_n) + 1
        best = 1
        best_d = 1e9
        for j, fr in enumerate(frames):
            d = abs(frame_ts(fr) - mt)
            if d < best_d:
                best_d = d
                best = j + 1
        print(
            f"  mt={mt:.1f}s manifest={idx_m} nominal={idx_n} "
            f"ffprobe_nearest={best} lead_nom={idx_n - best}"
        )


if __name__ == "__main__":
    main()
