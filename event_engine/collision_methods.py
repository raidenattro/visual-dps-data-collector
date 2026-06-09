"""碰撞方法工厂：保留旧手腕点逻辑，并提供同手同箱状态机逻辑。"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import cv2

from event_engine.box_identity import box_collision_token
from event_engine.collision import CollisionProcessor, PersonTrackAssigner

COLLISION_METHOD_WRIST_POINT = "wrist_point"
COLLISION_METHOD_HAND_STATE = "hand_state"
DEFAULT_COLLISION_METHOD = COLLISION_METHOD_WRIST_POINT


def normalize_collision_method(method: str | None) -> str:
    key = str(method or "").strip().lower().replace("-", "_")
    aliases = {
        "": DEFAULT_COLLISION_METHOD,
        "legacy": COLLISION_METHOD_WRIST_POINT,
        "point": COLLISION_METHOD_WRIST_POINT,
        "wrist": COLLISION_METHOD_WRIST_POINT,
        "wrist_point": COLLISION_METHOD_WRIST_POINT,
        "state": COLLISION_METHOD_HAND_STATE,
        "hand": COLLISION_METHOD_HAND_STATE,
        "hand_state": COLLISION_METHOD_HAND_STATE,
    }
    return aliases.get(key, key)


def default_collision_params(method: str | None = None) -> dict[str, Any]:
    method_key = normalize_collision_method(method)
    if method_key == COLLISION_METHOD_HAND_STATE:
        return {
            "method": COLLISION_METHOD_HAND_STATE,
            "enter_window_frames": 6,
            "enter_min_hits": 3,
            "enter_timeout_frames": 12,
            "exit_window_frames": 8,
            "exit_min_releases": 5,
            "exit_timeout_frames": 20,
            "max_inside_frames": 75,
            "cooldown_frames": 30,
            "hit_threshold": 0.55,
            "box_margin": 0.15,
            "wrist_score_min": 0.45,
            "elbow_score_min": 0.35,
            "jump_max": 0.45,
            "forearm_min_ratio": 0.5,
            "forearm_max_ratio": 1.8,
            "near_edge_ratio": 0.05,
        }
    return {
        "method": COLLISION_METHOD_WRIST_POINT,
        "alarm_min_consecutive_frames": 3,
        "alarm_cooldown_frames": 6,
    }


def build_collision_params(
    method: str | None = None,
    params: dict[str, Any] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    method_key = normalize_collision_method(
        method or (params.get("method") if isinstance(params, dict) else None)
    )
    out = default_collision_params(method_key)
    if isinstance(params, dict):
        out.update({k: v for k, v in params.items() if v is not None})
    out.update({k: v for k, v in overrides.items() if v is not None})
    out["method"] = method_key
    return coerce_collision_params(out)


def coerce_collision_params(params: dict[str, Any]) -> dict[str, Any]:
    method = normalize_collision_method(params.get("method"))
    out = dict(params)
    out["method"] = method
    for grouped_key in (COLLISION_METHOD_WRIST_POINT, COLLISION_METHOD_HAND_STATE):
        if grouped_key != method and isinstance(out.get(grouped_key), dict):
            out.pop(grouped_key, None)

    def _int(name: str, default: int, min_v: int = 1) -> None:
        try:
            out[name] = max(min_v, int(out.get(name, default)))
        except (TypeError, ValueError):
            out[name] = default

    def _float(name: str, default: float, min_v: float = 0.0) -> None:
        try:
            out[name] = max(min_v, float(out.get(name, default)))
        except (TypeError, ValueError):
            out[name] = default

    if method == COLLISION_METHOD_HAND_STATE:
        defaults = default_collision_params(method)
        for name in (
            "enter_window_frames",
            "enter_min_hits",
            "enter_timeout_frames",
            "exit_window_frames",
            "exit_min_releases",
            "exit_timeout_frames",
            "max_inside_frames",
            "cooldown_frames",
        ):
            _int(name, int(defaults[name]))
        for name in (
            "hit_threshold",
            "box_margin",
            "wrist_score_min",
            "elbow_score_min",
            "jump_max",
            "forearm_min_ratio",
            "forearm_max_ratio",
            "near_edge_ratio",
        ):
            _float(name, float(defaults[name]))
        out["enter_min_hits"] = min(out["enter_min_hits"], out["enter_window_frames"])
        out["exit_min_releases"] = min(out["exit_min_releases"], out["exit_window_frames"])
        return out

    defaults = default_collision_params(method)
    _int("alarm_min_consecutive_frames", int(defaults["alarm_min_consecutive_frames"]))
    _int("alarm_cooldown_frames", int(defaults["alarm_cooldown_frames"]))
    return out


def create_collision_processor(
    boxes: list[dict[str, Any]],
    *,
    method: str | None = None,
    params: dict[str, Any] | None = None,
    alarm_min_consecutive_frames: int | None = None,
    alarm_cooldown_frames: int | None = None,
    video_fps: float = 25.0,
):
    cfg = build_collision_params(
        method,
        params,
        alarm_min_consecutive_frames=alarm_min_consecutive_frames,
        alarm_cooldown_frames=alarm_cooldown_frames,
    )
    if cfg["method"] == COLLISION_METHOD_HAND_STATE:
        return HandStateCollisionProcessor(boxes, video_fps=video_fps, **cfg)
    return CollisionProcessor(
        boxes,
        alarm_min_consecutive_frames=cfg["alarm_min_consecutive_frames"],
        alarm_cooldown_frames=cfg["alarm_cooldown_frames"],
        video_fps=video_fps,
    )


def collision_processor_method(processor: Any) -> str:
    return getattr(processor, "method", COLLISION_METHOD_WRIST_POINT)


def collision_processor_params(processor: Any) -> dict[str, Any]:
    if hasattr(processor, "params"):
        return dict(getattr(processor, "params"))
    return {}


@dataclass
class HandSignal:
    obs: str
    token: str = ""
    score: float = 0.0


@dataclass
class HandRuntime:
    prev_wrist: tuple[float, float] | None = None
    forearm_len: float | None = None


@dataclass
class HandEventState:
    state: str = "IDLE"
    token: str = ""
    started_frame: int = 0
    state_frame: int = 0
    cooldown_until: int = 0
    history: deque[HandSignal] = field(default_factory=lambda: deque(maxlen=16))


def _pt(kpts: list, idx: int) -> tuple[float, float, float] | None:
    if idx >= len(kpts):
        return None
    kp = kpts[idx]
    if not isinstance(kp, (list, tuple)) or len(kp) < 3:
        return None
    try:
        return float(kp[0]), float(kp[1]), float(kp[2])
    except (TypeError, ValueError):
        return None


def _segment_intersects_polygon(
    p1: tuple[float, float],
    p2: tuple[float, float],
    polygon: list[list[float]],
) -> bool:
    if len(polygon) < 3:
        return False
    if _point_in_poly(p1, polygon) or _point_in_poly(p2, polygon):
        return True
    for i, a in enumerate(polygon):
        b = polygon[(i + 1) % len(polygon)]
        if _segments_intersect(p1, p2, (float(a[0]), float(a[1])), (float(b[0]), float(b[1]))):
            return True
    return False


def _point_in_poly(point: tuple[float, float], polygon: list[list[float]]) -> bool:
    x, y = point
    inside = False
    for i, a in enumerate(polygon):
        b = polygon[(i + 1) % len(polygon)]
        xi, yi = float(a[0]), float(a[1])
        xj, yj = float(b[0]), float(b[1])
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi
            if x < x_cross:
                inside = not inside
    return inside


def _contour_from_points(points: list[list[float]]):
    import numpy as np

    return np.asarray(points, dtype="float32").reshape((-1, 1, 2))


def _polygon_area(points: list[list[float]]) -> float:
    area = 0.0
    for i, a in enumerate(points):
        b = points[(i + 1) % len(points)]
        area += float(a[0]) * float(b[1]) - float(b[0]) * float(a[1])
    return abs(area) / 2.0


def _point_polygon_edge_distance(point: tuple[float, float], polygon: list[list[float]]) -> float:
    best = 1e18
    for i, a in enumerate(polygon):
        b = polygon[(i + 1) % len(polygon)]
        best = min(best, _point_segment_distance(point, (float(a[0]), float(a[1])), (float(b[0]), float(b[1]))))
    return best


def _point_segment_distance(point, a, b) -> float:
    px, py = point
    ax, ay = a
    bx, by = b
    dx = bx - ax
    dy = by - ay
    denom = dx * dx + dy * dy
    if denom <= 1e-12:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denom))
    x = ax + t * dx
    y = ay + t * dy
    return math.hypot(px - x, py - y)


def _segments_intersect(a, b, c, d) -> bool:
    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    def on_segment(p, q, r):
        return (
            min(p[0], r[0]) - 1e-6 <= q[0] <= max(p[0], r[0]) + 1e-6
            and min(p[1], r[1]) - 1e-6 <= q[1] <= max(p[1], r[1]) + 1e-6
        )

    o1 = orient(a, b, c)
    o2 = orient(a, b, d)
    o3 = orient(c, d, a)
    o4 = orient(c, d, b)
    if (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0):
        return True
    return (
        abs(o1) <= 1e-6 and on_segment(a, c, b)
        or abs(o2) <= 1e-6 and on_segment(a, d, b)
        or abs(o3) <= 1e-6 and on_segment(c, a, d)
        or abs(o4) <= 1e-6 and on_segment(c, b, d)
    )


class HandStateCollisionProcessor:
    """同手同箱进入-离开闭环状态机。

    每只手先锁定一个货箱，确认稳定进入后等待离开锁定箱；完成闭环才输出 alarm。
    """

    method = COLLISION_METHOD_HAND_STATE

    def __init__(
        self,
        boxes: list[dict[str, Any]],
        *,
        method: str = COLLISION_METHOD_HAND_STATE,
        enter_window_frames: int = 6,
        enter_min_hits: int = 3,
        enter_timeout_frames: int = 12,
        exit_window_frames: int = 8,
        exit_min_releases: int = 5,
        exit_timeout_frames: int = 20,
        max_inside_frames: int = 75,
        cooldown_frames: int = 30,
        hit_threshold: float = 0.55,
        box_margin: float = 0.15,
        wrist_score_min: float = 0.45,
        elbow_score_min: float = 0.35,
        jump_max: float = 0.45,
        forearm_min_ratio: float = 0.5,
        forearm_max_ratio: float = 1.8,
        near_edge_ratio: float = 0.05,
        video_fps: float = 25.0,
        **_: Any,
    ):
        self.boxes = boxes
        self.params = coerce_collision_params(
            {
                "method": method,
                "enter_window_frames": enter_window_frames,
                "enter_min_hits": enter_min_hits,
                "enter_timeout_frames": enter_timeout_frames,
                "exit_window_frames": exit_window_frames,
                "exit_min_releases": exit_min_releases,
                "exit_timeout_frames": exit_timeout_frames,
                "max_inside_frames": max_inside_frames,
                "cooldown_frames": cooldown_frames,
                "hit_threshold": hit_threshold,
                "box_margin": box_margin,
                "wrist_score_min": wrist_score_min,
                "elbow_score_min": elbow_score_min,
                "jump_max": jump_max,
                "forearm_min_ratio": forearm_min_ratio,
                "forearm_max_ratio": forearm_max_ratio,
                "near_edge_ratio": near_edge_ratio,
            }
        )
        self.video_fps = max(1.0, float(video_fps))
        self.person_assigner = PersonTrackAssigner(max_match_dist=220.0, stale_sec=1.2)
        self._states: dict[str, HandEventState] = {}
        self._runtime: dict[str, HandRuntime] = {}
        self._box_items = [self._prepare_box(b) for b in boxes]
        self._box_items = [b for b in self._box_items if b is not None]

    def _prepare_box(self, box: dict[str, Any]) -> dict[str, Any] | None:
        token = box_collision_token(box)
        contour = box.get("orig_contour")
        pts = box.get("video_polygon") or []
        if not token or contour is None or not isinstance(pts, list) or len(pts) < 3:
            return None
        try:
            poly = [[float(p[0]), float(p[1])] for p in pts if len(p) >= 2]
        except (TypeError, ValueError):
            return None
        if len(poly) < 3:
            return None
        area = _polygon_area(poly)
        return {
            "token": token,
            "contour": contour,
            "poly": poly,
            "scale": math.sqrt(max(1.0, area)),
        }

    def process(self, pose_frame: dict) -> dict:
        frame_idx = int(pose_frame.get("frame_idx") or pose_frame.get("source_frame_idx") or 0)
        now_ts = frame_idx / self.video_fps if self.video_fps > 0 else 0.0
        persons = pose_frame.get("persons") or pose_frame.get("skeletons") or []
        skeletons_data = []
        used_track_ids: set[int] = set()
        current_hits: set[str] = set()
        alarms: set[str] = set()

        for person in persons:
            if not isinstance(person, dict):
                continue
            kpts = person.get("keypoints") or []
            track_id = self._assign_track(person, kpts, now_ts, used_track_ids)
            skel = dict(person)
            skel["person_track_id"] = track_id
            skeletons_data.append(skel)
            if len(kpts) < 11:
                continue
            for hand, shoulder_i, elbow_i, wrist_i in (
                ("left", 5, 7, 9),
                ("right", 6, 8, 10),
            ):
                key = f"{track_id}:{hand}"
                signal = self._observe_hand(key, kpts, shoulder_i, elbow_i, wrist_i)
                active, alarm = self._update_state(key, signal, frame_idx)
                current_hits.update(active)
                alarms.update(alarm)

        return {
            "collisions": sorted(current_hits),
            "alarm_collisions": sorted(alarms),
            "skeletons": skeletons_data,
            "frame_idx": frame_idx,
        }

    def _assign_track(self, person: dict, kpts: list, now_ts: float, used: set[int]) -> int:
        existing = person.get("person_track_id")
        if existing is not None:
            try:
                tid = int(existing)
                used.add(tid)
                return tid
            except (TypeError, ValueError):
                pass
        l = _pt(kpts, 5)
        r = _pt(kpts, 6)
        if l and r and l[2] > 0.2 and r[2] > 0.2:
            ax = (l[0] + r[0]) / 2.0
            ay = (l[1] + r[1]) / 2.0
        else:
            pts = [p for p in kpts if isinstance(p, (list, tuple)) and len(p) >= 2]
            ax = sum(float(p[0]) for p in pts) / len(pts) if pts else 0.0
            ay = sum(float(p[1]) for p in pts) / len(pts) if pts else 0.0
        return self.person_assigner.assign(ax, ay, now_ts=now_ts, occupied_track_ids=used)

    def _observe_hand(self, hand_key: str, kpts: list, shoulder_i: int, elbow_i: int, wrist_i: int) -> HandSignal:
        shoulder = _pt(kpts, shoulder_i)
        elbow = _pt(kpts, elbow_i)
        wrist = _pt(kpts, wrist_i)
        if not elbow or not wrist:
            return HandSignal("UNKNOWN")
        rt = self._runtime.setdefault(hand_key, HandRuntime())
        wrist_xy = (wrist[0], wrist[1])
        elbow_xy = (elbow[0], elbow[1])
        person_scale = self._person_scale(kpts)
        forearm_len = max(1e-6, math.hypot(wrist[0] - elbow[0], wrist[1] - elbow[1]))
        prev_len = rt.forearm_len
        jump_norm = (
            math.hypot(wrist[0] - rt.prev_wrist[0], wrist[1] - rt.prev_wrist[1]) / person_scale
            if rt.prev_wrist
            else 0.0
        )
        len_ratio = forearm_len / prev_len if prev_len and prev_len > 1e-6 else 1.0
        low_quality = wrist[2] < self.params["wrist_score_min"] or elbow[2] < self.params["elbow_score_min"]
        jump_bad = jump_norm > self.params["jump_max"]
        limb_unstable = not (
            self.params["forearm_min_ratio"] <= len_ratio <= self.params["forearm_max_ratio"]
        )
        if shoulder and shoulder[2] > 0.2:
            angle_bad = False
        else:
            angle_bad = False
        quality_bad = low_quality or jump_bad or limb_unstable or angle_bad

        if not low_quality and not jump_bad:
            rt.prev_wrist = wrist_xy
            rt.forearm_len = forearm_len if prev_len is None else prev_len * 0.75 + forearm_len * 0.25

        if low_quality:
            return HandSignal("UNKNOWN")

        scored: list[tuple[str, float]] = []
        for box in self._box_items:
            wrist_in = _point_in_poly(wrist_xy, box["poly"])
            forearm_hit = _segment_intersects_polygon(elbow_xy, wrist_xy, box["poly"])
            dist = _point_polygon_edge_distance(wrist_xy, box["poly"])
            near = dist <= box["scale"] * self.params["near_edge_ratio"]
            score = (
                0.55 * float(wrist_in)
                + 0.30 * float(forearm_hit)
                + 0.15 * float(near)
                - 0.40 * float(jump_bad)
                - 0.30 * float(low_quality)
                - 0.30 * float(limb_unstable)
            )
            scored.append((box["token"], score))

        if not scored:
            return HandSignal("NO_HIT")
        scored.sort(key=lambda x: x[1], reverse=True)
        best_token, best_score = scored[0]
        second_score = scored[1][1] if len(scored) > 1 else -1.0
        if quality_bad:
            return HandSignal("UNKNOWN")
        if best_score >= self.params["hit_threshold"] and best_score - second_score >= self.params["box_margin"]:
            return HandSignal("HIT", best_token, best_score)
        if best_score >= self.params["hit_threshold"] and best_score - second_score < self.params["box_margin"]:
            return HandSignal("UNKNOWN", best_token, best_score)
        return HandSignal("NO_HIT", "", best_score)

    def _person_scale(self, kpts: list) -> float:
        xs = [float(k[0]) for k in kpts if isinstance(k, (list, tuple)) and len(k) >= 3 and float(k[2]) > 0.2]
        ys = [float(k[1]) for k in kpts if isinstance(k, (list, tuple)) and len(k) >= 3 and float(k[2]) > 0.2]
        if not xs or not ys:
            return 100.0
        return max(20.0, max(max(xs) - min(xs), max(ys) - min(ys)))

    def _update_state(self, key: str, signal: HandSignal, frame_idx: int) -> tuple[set[str], set[str]]:
        st = self._states.setdefault(key, HandEventState())
        st.history.append(signal)
        active: set[str] = set()
        alarms: set[str] = set()

        if st.state == "COOLDOWN":
            if frame_idx >= st.cooldown_until:
                st.state = "IDLE"
                st.token = ""
            else:
                return active, alarms

        if st.state == "IDLE":
            if signal.obs == "HIT" and signal.token:
                st.state = "ENTER_PENDING"
                st.token = signal.token
                st.started_frame = frame_idx
                st.state_frame = frame_idx
                active.add(st.token)
            return active, alarms

        if st.state == "ENTER_PENDING":
            if st.token:
                active.add(st.token)
            if self._confirm_enter(st):
                st.state = "INSIDE"
                st.state_frame = frame_idx
                active.add(st.token)
            elif frame_idx - st.state_frame > self.params["enter_timeout_frames"]:
                self._reset_state(st)
            return active, alarms

        if st.state == "INSIDE":
            active.add(st.token)
            if frame_idx - st.state_frame > self.params["max_inside_frames"]:
                self._reset_state(st)
                return set(), set()
            if signal.obs != "UNKNOWN" and not (signal.obs == "HIT" and signal.token == st.token):
                st.state = "EXIT_PENDING"
                st.state_frame = frame_idx
            return active, alarms

        if st.state == "EXIT_PENDING":
            active.add(st.token)
            if signal.obs == "HIT" and signal.token == st.token:
                st.state = "INSIDE"
                st.state_frame = frame_idx
                return active, alarms
            if self._confirm_exit(st):
                alarms.add(st.token)
                active.add(st.token)
                st.state = "COOLDOWN"
                st.cooldown_until = frame_idx + self.params["cooldown_frames"]
                st.state_frame = frame_idx
                return active, alarms
            if frame_idx - st.state_frame > self.params["exit_timeout_frames"]:
                self._reset_state(st)
                return set(), set()
        return active, alarms

    def _confirm_enter(self, st: HandEventState) -> bool:
        win = list(st.history)[-self.params["enter_window_frames"] :]
        same = sum(1 for s in win if s.obs == "HIT" and s.token == st.token)
        other = sum(1 for s in win if s.obs == "HIT" and s.token and s.token != st.token)
        return same >= self.params["enter_min_hits"] and other <= 1

    def _confirm_exit(self, st: HandEventState) -> bool:
        win = list(st.history)[-self.params["exit_window_frames"] :]
        releases = sum(
            1
            for s in win
            if s.obs != "UNKNOWN" and not (s.obs == "HIT" and s.token == st.token)
        )
        return releases >= self.params["exit_min_releases"]

    def _reset_state(self, st: HandEventState) -> None:
        st.state = "IDLE"
        st.token = ""
        st.started_frame = 0
        st.state_frame = 0
        st.cooldown_until = 0
        st.history.clear()
