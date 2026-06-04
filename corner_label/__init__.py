"""机位标识与 reflection → 标注 JSON 对应。"""

from corner_label.reflection import ReflectionMap, load_reflection, normalize_corner_label
from corner_label.resolve import ResolveResult, resolve_annotation_for_camera

__all__ = [
    "ReflectionMap",
    "ResolveResult",
    "load_reflection",
    "normalize_corner_label",
    "resolve_annotation_for_camera",
]
