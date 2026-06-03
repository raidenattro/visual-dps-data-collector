"""与 visual-dps event-worker 对齐的碰撞检测（离线视频采集）。"""

from event_engine.annotation_boxes import flatten_annotation_boxes, load_scaled_boxes
from event_engine.box_identity import box_collision_token, parse_collision_token
from event_engine.collision import CollisionProcessor, PersonTrackAssigner

__all__ = [
    "CollisionProcessor",
    "PersonTrackAssigner",
    "box_collision_token",
    "parse_collision_token",
    "flatten_annotation_boxes",
    "load_scaled_boxes",
]
