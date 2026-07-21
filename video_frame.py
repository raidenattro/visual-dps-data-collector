"""从视频提取标注用首帧。"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import cv2

from collect_core import _compute_infer_resolution, validate_video_path


def _resize_frame_to_infer(
    frame,
    *,
    source_w: int,
    source_h: int,
    infer_width: int = 0,
    infer_height: int = 0,
) -> tuple[Any, int, int]:
    """将帧缩放到与采集推理一致的分辨率（与 collect_core 等比逻辑对齐）。"""
    h, w = frame.shape[:2]
    if infer_width > 0 and infer_height > 0:
        if w == infer_width and h == infer_height:
            return frame, w, h
        resized = cv2.resize(
            frame,
            (int(infer_width), int(infer_height)),
            interpolation=cv2.INTER_AREA,
        )
        return resized, int(infer_width), int(infer_height)
    if infer_height > 0:
        target_w, target_h, need = _compute_infer_resolution(source_w, source_h, infer_height)
        if need:
            resized = cv2.resize(
                frame,
                (target_w, target_h),
                interpolation=cv2.INTER_AREA,
            )
            return resized, target_w, target_h
    return frame, w, h


def _resize_frame_if_needed(frame, *, max_width: int) -> tuple[Any, int, int]:
    h, w = frame.shape[:2]
    if max_width > 0 and w > max_width:
        scale = max_width / float(w)
        frame = cv2.resize(
            frame,
            (int(round(w * scale)), int(round(h * scale))),
            interpolation=cv2.INTER_AREA,
        )
        h, w = frame.shape[:2]
    return frame, w, h


def extract_frame_jpeg(
    video_path: str | Path,
    frame_index: int = 0,
    *,
    max_width: int = 0,
    infer_width: int = 0,
    infer_height: int = 0,
) -> tuple[bytes, int, int, int, int, int]:
    """按帧索引提取 JPEG；返回 (jpeg, w, h, total, source_w, source_h)。"""
    path = validate_video_path(video_path)
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {path}")
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fi = max(0, int(frame_index))
        if total > 0 and fi >= total:
            raise RuntimeError(f"帧索引 {fi} 超出范围（视频共 {total} 帧）")
        if fi > 0:
            cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
        ret, frame = cap.read()
        if not ret or frame is None:
            label = "首帧" if fi == 0 else f"第 {fi + 1} 帧"
            raise RuntimeError(f"无法读取视频{label}: {path}")
        source_h, source_w = frame.shape[:2]
        if infer_width > 0 or infer_height > 0:
            frame, w, h = _resize_frame_to_infer(
                frame,
                source_w=source_w,
                source_h=source_h,
                infer_width=infer_width,
                infer_height=infer_height,
            )
        else:
            frame, w, h = _resize_frame_if_needed(frame, max_width=max_width)
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        if not ok:
            raise RuntimeError("帧 JPEG 编码失败")
        return buf.tobytes(), w, h, total, int(source_w), int(source_h)
    finally:
        cap.release()


def extract_first_frame_jpeg(
    video_path: str | Path,
    *,
    max_width: int = 0,
    infer_width: int = 0,
    infer_height: int = 0,
) -> tuple[bytes, int, int]:
    jpeg, w, h, _total, _sw, _sh = extract_frame_jpeg(
        video_path,
        0,
        max_width=max_width,
        infer_width=infer_width,
        infer_height=infer_height,
    )
    return jpeg, w, h


def frame_base64_at_index(
    video_path: str | Path,
    frame_index: int = 0,
    *,
    max_width: int = 0,
    infer_width: int = 0,
    infer_height: int = 0,
) -> dict:
    jpeg, w, h, total, source_w, source_h = extract_frame_jpeg(
        video_path,
        frame_index,
        max_width=max_width,
        infer_width=infer_width,
        infer_height=infer_height,
    )
    fi = max(0, int(frame_index))
    return {
        "image": base64.b64encode(jpeg).decode("ascii"),
        "width": w,
        "height": h,
        "source_width": source_w,
        "source_height": source_h,
        "frame_index": fi,
        "frame_count": total,
    }


def first_frame_base64(
    video_path: str | Path,
    *,
    max_width: int = 0,
    infer_width: int = 0,
    infer_height: int = 0,
) -> dict:
    return frame_base64_at_index(
        video_path,
        0,
        max_width=max_width,
        infer_width=infer_width,
        infer_height=infer_height,
    )
