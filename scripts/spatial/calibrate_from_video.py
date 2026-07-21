#!/usr/bin/env python3
"""在机位视频帧上交互标注 10 个地面控制点，生成 spatial 标定 JSON。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cv2
import numpy as np

from config_loader import load_config_file, resolve_app_paths, spatial_enabled
from spatial_pose.calibration import (
    calibration_path_for_slug,
    compute_and_update_config,
    save_calibration,
)
from spatial_pose.preview import write_calibration_preview
from spatial_pose.schema import EXPECTED_CONTROL_POINTS, empty_spatial_config

# 控制点顺序说明（与 handoff world 点顺序一致）
POINT_LABELS = [
    "远-左", "远-右",
    "远2-左", "远2-右",
    "中-左", "中-右",
    "近2-左", "近2-右",
    "近-左", "近-右",
]


def _resolve_video(args: argparse.Namespace, paths) -> Path:
    if args.video:
        p = Path(args.video)
        if not p.is_file():
            raise FileNotFoundError(f"视频不存在: {p}")
        return p.resolve()
    if not args.camera_slug:
        raise ValueError("请指定 --video 或 --camera-slug")
    from api.annotate_service import resolve_camera_video_bucket

    tier = args.pose_tier or "rtmpose-m"
    hit = resolve_camera_video_bucket(paths, args.camera_slug, pose_tier=tier)
    if not hit:
        raise FileNotFoundError(
            f"机位 {args.camera_slug!r} 在 video_dir/{tier}/ 下无视频"
        )
    _slug, video_path = hit
    return video_path.resolve()


def _read_frame(video_path: Path, frame_index: int) -> tuple[np.ndarray, int, int]:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {video_path}")
    try:
        if frame_index > 0:
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ok, frame = cap.read()
        if not ok or frame is None:
            raise RuntimeError(f"无法读取帧 {frame_index}")
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or frame.shape[1])
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or frame.shape[0])
        return frame, w, h
    finally:
        cap.release()


def _interactive_pick_points(frame: np.ndarray) -> list[list[float]]:
    points: list[tuple[float, float]] = []
    display = frame.copy()

    def _ redraw() -> None:
        nonlocal display
        display = frame.copy()
        for i, (x, y) in enumerate(points):
            cv2.circle(display, (int(x), int(y)), 7, (20, 20, 245), -1, cv2.LINE_AA)
            label = POINT_LABELS[i] if i < len(POINT_LABELS) else f"P{i+1}"
            cv2.putText(
                display,
                label,
                (int(x) + 8, int(y) - 8),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (255, 255, 255),
                1,
                cv2.LINE_AA,
            )
        hint = f"已点 {len(points)}/{EXPECTED_CONTROL_POINTS} | 左键加点 右键撤销 u 撤销 q 完成"
        cv2.putText(display, hint, (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (80, 235, 255), 2)

    def _on_mouse(event, x, y, _flags, _param) -> None:
        if event == cv2.EVENT_LBUTTONDOWN and len(points) < EXPECTED_CONTROL_POINTS:
            points.append((float(x), float(y)))
            _redraw()
        elif event == cv2.EVENT_RBUTTONDOWN and points:
            points.pop()
            _redraw()

    win = "spatial_calibrate"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(win, _on_mouse)
    _redraw()
    while True:
        cv2.imshow(win, display)
        key = cv2.waitKey(20) & 0xFF
        if key in (ord("q"), ord("Q"), 13) and len(points) == EXPECTED_CONTROL_POINTS:
            break
        if key in (ord("u"), ord("U"), 8) and points:
            points.pop()
            _redraw()
        if key == 27:
            cv2.destroyAllWindows()
            raise SystemExit("用户取消标定")
    cv2.destroyAllWindows()
    return [[p[0], p[1]] for p in points]


def main() -> None:
    parser = argparse.ArgumentParser(description="机位地面单应标定（10 控制点）")
    parser.add_argument("--camera-slug", default="", help="机位 slug，如 1-1-1")
    parser.add_argument("--video", default="", help="标定用视频路径")
    parser.add_argument("--pose-tier", default="rtmpose-m", help="解析机位视频时的 tier")
    parser.add_argument("--frame", type=int, default=0, help="标定帧序号（0=首帧）")
    parser.add_argument("--aisle-width-m", type=float, default=2.0)
    parser.add_argument("--marker-spacing-m", type=float, default=2.4)
    parser.add_argument("--marker-pairs", type=int, default=5)
    parser.add_argument("--output", default="", help="输出 JSON（默认 spatial_dir/slug.json）")
    parser.add_argument("--preview", default="", help="预览 PNG 路径")
    args = parser.parse_args()

    slug = str(args.camera_slug or "").strip()
    if not slug:
        slug = Path(args.video).stem if args.video else ""
    if not slug:
        raise SystemExit("需要 --camera-slug 或 --video")

    cfg = load_config_file()
    paths = resolve_app_paths(cfg)
    paths.spatial_dir.mkdir(parents=True, exist_ok=True)

    video_path = _resolve_video(args, paths)
    frame, width, height = _read_frame(video_path, args.frame)
    print(f"标定视频: {video_path}")
    print(f"分辨率: {width}x{height}  帧: {args.frame}")
    print("请按顺序点击 10 个地面控制点（远→近，每行左/右）")

    image_points = _interactive_pick_points(frame)
    spatial_cfg = empty_spatial_config(slug)
    spatial_cfg["physical"] = {
        "aisle_width_m": args.aisle_width_m,
        "marker_spacing_m": args.marker_spacing_m,
        "marker_pairs": args.marker_pairs,
    }
    spatial_cfg["calibration"] = {
        "resolution": [width, height],
        "image_points_px": image_points,
    }
    cal = compute_and_update_config(spatial_cfg, infer_width=width, infer_height=height)

    out_json = Path(args.output) if args.output else calibration_path_for_slug(paths.spatial_dir, slug)
    save_calibration(out_json, cal.config)
    preview_path = Path(args.preview) if args.preview else out_json.with_suffix(".preview.png")
    pts = np.array(image_points, dtype=np.float64)
    write_calibration_preview(frame, cal, preview_path, image_points=pts)

    print(f"已保存标定: {out_json}")
    print(f"RMSE: {cal.ground_control_rmse_px:.2f} px")
    print(f"预览图: {preview_path}")
    if not spatial_enabled(cfg):
        print("提示: config.json spatial.enabled=false，采集时不会写入 floor_xy")


if __name__ == "__main__":
    main()
