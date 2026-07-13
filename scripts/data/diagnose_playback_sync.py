#!/usr/bin/env python3
"""诊断单条记录的「视频时间 ↔ 骨架帧」对齐情况。"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.record_service import locate_record_by_id, video_path_for_record
from pose_store import load_manifest, load_timeline_index


def _ffprobe(path: Path) -> dict | None:
    ff = shutil.which("ffprobe")
    if not ff or not path.is_file():
        return None
    cmd = [
        ff,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,r_frame_rate,avg_frame_rate,nb_frames,duration",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        return json.loads(subprocess.check_output(cmd, text=True))
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        return None


def frame_at_time_formula(time_sec: float, fps: float, total: int) -> int:
    if total <= 0:
        return 0
    return min(total, max(1, math.floor(max(0.0, time_sec) * fps) + 1))


def frame_at_time_timeline(time_sec: float, rows: list[dict]) -> int:
    if not rows:
        return 0
    t = max(0.0, time_sec)
    if t <= float(rows[0].get("timestamp_sec") or 0):
        return int(rows[0].get("source_frame_idx") or rows[0].get("frame_idx") or 0)
    last = rows[-1]
    if t >= float(last.get("timestamp_sec") or 0):
        return int(last.get("source_frame_idx") or last.get("frame_idx") or 0)
    lo, hi = 0, len(rows) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if float(rows[mid].get("timestamp_sec") or 0) <= t:
            lo = mid + 1
        else:
            hi = mid - 1
    hit = rows[max(0, hi)]
    return int(hit.get("source_frame_idx") or hit.get("frame_idx") or 0)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="诊断回放视频与骨架帧对齐")
    p.add_argument("record_id", help="如 rtmpose-m/1-1-1-(3)/clip_rtmpose_m")
    args = p.parse_args(argv)

    loc = locate_record_by_id(args.record_id)
    if not loc:
        print(f"❌ 记录不存在: {args.record_id}", file=sys.stderr)
        return 2

    manifest = load_manifest(loc)
    rows = load_timeline_index(loc)
    fps = float(manifest.get("fps") or 0) or 25.0
    total = int(manifest.get("frame_count") or len(rows) or 0)
    mism_idx = sum(
        1
        for r in rows
        if int(r.get("frame_idx") or 0) != int(r.get("source_frame_idx") or 0)
    )

    print(f"记录: {args.record_id}")
    print(f"路径: {loc.path}")
    print(f"帧数: {total}  timeline: {len(rows)}  fps(manifest): {fps:.6f}")
    print(f"infer: {manifest.get('infer_width')}×{manifest.get('infer_height')}")
    print(f"frame_idx≠source_frame_idx: {mism_idx}")

    vp = video_path_for_record(args.record_id)
    if vp and vp.is_file():
        print(f"视频: {vp}  ({vp.stat().st_size} bytes)")
        probe = _ffprobe(vp)
        if probe:
            stream = (probe.get("streams") or [{}])[0]
            fmt = probe.get("format") or {}
            print(
                "ffprobe:",
                f"{stream.get('width')}×{stream.get('height')}",
                f"r_fps={stream.get('r_frame_rate')}",
                f"avg_fps={stream.get('avg_frame_rate')}",
                f"nb_frames={stream.get('nb_frames')}",
                f"dur={fmt.get('duration') or stream.get('duration')}",
            )
    else:
        print("⚠️ 未找到配套视频")

    if rows:
        last_ts = float(rows[-1].get("timestamp_sec") or 0)
        print(f"timeline 末帧 ts: {last_ts:.6f}s  估算时长(帧/fps): {total / fps:.3f}s")

    sample_times = [0.0, 1.0, 10.0, 30.0, 60.0]
    if rows:
        sample_times.append(float(rows[-1].get("timestamp_sec") or 0))
    if vp:
        probe = _ffprobe(vp)
        if probe:
            dur = float((probe.get("format") or {}).get("duration") or 0)
            if dur > 0:
                sample_times.extend([dur * 0.25, dur * 0.5, dur * 0.9, dur])

    seen: set[float] = set()
    print("\n时间采样 (formula vs timeline):")
    max_diff = 0
    for t in sorted(sample_times):
        t = round(t, 3)
        if t in seen:
            continue
        seen.add(t)
        f_formula = frame_at_time_formula(t, fps, total)
        f_timeline = frame_at_time_timeline(t, rows)
        diff = f_formula - f_timeline
        max_diff = max(max_diff, abs(diff))
        flag = "  ⚠️" if diff else ""
        print(f"  t={t:7.3f}s  formula={f_formula:5d}  timeline={f_timeline:5d}  Δ={diff:+d}{flag}")

    if max_diff > 0:
        print(f"\n结论: manifest fps 公式与 timeline 最大偏差 {max_diff} 帧；回放应优先 timeline（已修复）。")
    else:
        print("\n结论: 数据层时间轴一致；若仍不同步，请硬刷新回放页 (Ctrl+F5) 后重试。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
