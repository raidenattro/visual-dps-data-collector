"""与 visual-dps event-worker 对齐的碰撞检测（离线视频采集）。"""

from event_engine.annotation_boxes import flatten_annotation_boxes, load_scaled_boxes
from event_engine.box_identity import box_collision_token, parse_collision_token
from event_engine.collision import CollisionProcessor, PersonTrackAssigner
from event_engine.collision_methods import (
    COLLISION_METHOD_HAND_STATE,
    COLLISION_METHOD_WRIST_POINT,
    DEFAULT_COLLISION_METHOD,
    HandStateCollisionProcessor,
    build_collision_params,
    create_collision_processor,
    default_collision_params,
    normalize_collision_method,
)

__all__ = [
    "CollisionProcessor",
    "HandStateCollisionProcessor",
    "PersonTrackAssigner",
    "COLLISION_METHOD_HAND_STATE",
    "COLLISION_METHOD_WRIST_POINT",
    "DEFAULT_COLLISION_METHOD",
    "build_collision_params",
    "box_collision_token",
    "create_collision_processor",
    "default_collision_params",
    "normalize_collision_method",
    "parse_collision_token",
    "flatten_annotation_boxes",
    "load_scaled_boxes",
]
