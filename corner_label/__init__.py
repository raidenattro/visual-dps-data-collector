"""视频机位标签 OCR 与 reflection → 标注 JSON 自动对应。"""

from corner_label.ocr import CornerRoi, default_corner_roi, default_ocr_engine, read_corner_label_from_video
from corner_label.reflection import ReflectionMap, load_reflection
from corner_label.resolve import ResolveResult, resolve_annotation_for_video

__all__ = [
    "CornerRoi",
    "ReflectionMap",
    "ResolveResult",
    "default_corner_roi",
    "default_ocr_engine",
    "load_reflection",
    "read_corner_label_from_video",
    "resolve_annotation_for_video",
]
