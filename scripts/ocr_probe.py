#!/usr/bin/env python3
"""单视频 OCR 探针（与 Web 同一 Python 环境）。用法:
  python scripts/ocr_probe.py --video path/to.mp4
  python scripts/ocr_probe.py --video path/to.mp4 --engine paddle
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config_loader import load_config_file, project_root, resolve_app_paths, resolve_config_path
from corner_label.ocr import CornerRoi, default_corner_roi, default_ocr_engine, read_corner_label_from_video
from corner_label.reflection import load_reflection
from corner_label.resolve import resolve_annotation_for_video


def main() -> int:
    p = argparse.ArgumentParser(description="OCR 机位标签探针")
    p.add_argument("--video", required=True, help="视频路径")
    p.add_argument("--engine", default="", help="paddle / easy / auto")
    p.add_argument("--reflection", default="", help="reflection.json，默认仓库根目录")
    p.add_argument("--resolve", action="store_true", help="尝试匹配 annotation JSON")
    p.add_argument(
        "--roi",
        default="",
        help="覆盖 config：x0,y0,x1,y1 比例，如 0.72,0.86,1,0.98",
    )
    args = p.parse_args()

    video = Path(args.video).resolve()
    if not video.is_file():
        print(f"视频不存在: {video}", file=sys.stderr)
        return 1

    engine = str(args.engine or "").strip().lower() or default_ocr_engine()
    roi = default_corner_roi()
    if str(args.roi or "").strip():
        parts = [float(x.strip()) for x in str(args.roi).split(",")]
        if len(parts) != 4:
            print("ROI 须为 4 个数: x0,y0,x1,y1", file=sys.stderr)
            return 1
        roi = CornerRoi(x0=parts[0], y0=parts[1], x1=parts[2], y1=parts[3])
    label, meta = read_corner_label_from_video(video, engine=engine, roi=roi)
    print(json.dumps({"corner_label": label, "meta": meta}, ensure_ascii=False, indent=2))

    if not args.resolve or not label:
        return 0 if label else 2

    ref_path = Path(args.reflection) if args.reflection else project_root() / "reflection.json"
    if not ref_path.is_file():
        print(f"缺少 {ref_path}", file=sys.stderr)
        return 3

    paths = resolve_app_paths()
    resolved = resolve_annotation_for_video(
        video,
        reflection=load_reflection(ref_path),
        annotations_dir=paths.annotation_dir,
        ocr_engine=engine,
    )
    print(
        json.dumps(
            {
                "corner_label": resolved.corner_label,
                "annotation_ids": resolved.annotation_ids,
                "sources": [str(p) for p in resolved.source_annotation_paths],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
