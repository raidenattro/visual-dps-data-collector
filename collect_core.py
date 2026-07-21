"""视频骨架采集核心逻辑（CLI 与 Web 共用）。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

import cv2

from model_assets import COCO17_KEYPOINT_NAMES, RTMPOSE_VARIANTS, VIDEO_EXTENSIONS
from pose_store import STORAGE_V2_PARQUET, write_v2_package
from rtmpose_infer import PoseBatch, RTMPosePipeline

try:
    from event_engine.annotation_boxes import (
        boxes_for_json_export,
        load_annotation_config,
        load_scaled_boxes,
    )
    from event_engine.collision import CollisionProcessor
except ImportError:
    boxes_for_json_export = None  # type: ignore
    load_annotation_config = None  # type: ignore
    load_scaled_boxes = None  # type: ignore
    CollisionProcessor = None  # type: ignore

try:
    from spatial_pose.calibration import SpatialCalibration
    from spatial_pose.floor_projection import FloorSmoothState, StickyFootTracker, pick_primary_person, project_foot_for_frame
except ImportError:
    SpatialCalibration = None  # type: ignore
    FloorSmoothState = None  # type: ignore
    project_foot_for_frame = None  # type: ignore
    pick_primary_person = None  # type: ignore
    StickyFootTracker = None  # type: ignore

ProgressCallback = Callable[[int, int], None]


def parse_variant(raw: str) -> str:
    v = str(raw or "t").strip().lower()
    if v == "ms":
        v = "m"
    if v not in RTMPOSE_VARIANTS:
        raise ValueError(f"variant 必须是 {RTMPOSE_VARIANTS} 之一")
    return v


def validate_video_path(video: str | Path) -> Path:
    path = Path(video)
    if not path.is_file():
        raise FileNotFoundError(f"视频文件不存在: {video}")
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        ext_list = ", ".join(sorted(VIDEO_EXTENSIONS))
        raise ValueError(f"不支持的文件类型: {path.suffix}，仅支持 {ext_list}")
    return path


def persons_from_batch(batch: PoseBatch) -> list[dict[str, Any]]:
    persons: list[dict[str, Any]] = []
    kpts_all = batch.keypoints
    scores_all = batch.keypoint_scores
    bboxes = batch.bboxes

    for p_idx in range(batch.num_persons):
        keypoints_flat: list[list[float]] = []
        for k in range(kpts_all.shape[1]):
            x = float(kpts_all[p_idx][k][0])
            y = float(kpts_all[p_idx][k][1])
            score = float(scores_all[p_idx][k])
            keypoints_flat.append([x, y, score])

        bbox = None
        if p_idx < len(bboxes):
            bbox = [float(v) for v in bboxes[p_idx][:4]]

        persons.append(
            {
                "person_id": p_idx,
                "bbox": bbox,
                "keypoints": keypoints_flat,
            }
        )
    return persons


def _compute_infer_resolution(source_w: int, source_h: int, target_height: int) -> tuple[int, int, bool]:
    """与 visual-dps inference_service._compute_infer_resolution 一致：仅按目标高等比缩放（不放大）。"""
    target_h = max(120, int(target_height))
    if source_h <= target_h:
        infer_w = source_w
        infer_h = source_h
        resize_needed = False
    else:
        infer_h = target_h
        infer_w = int(round(source_w * (infer_h / float(source_h))))
        infer_w = max(2, infer_w - (infer_w % 2))
        infer_h = max(2, infer_h - (infer_h % 2))
        resize_needed = True
    return infer_w, infer_h, resize_needed


def _read_video_source_size(video_path: Path) -> tuple[int, int]:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {video_path}")
    try:
        src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        if src_w <= 0 or src_h <= 0:
            ret, frame = cap.read()
            if ret and frame is not None:
                src_h, src_w = frame.shape[:2]
        if src_w <= 0 or src_h <= 0:
            raise RuntimeError(f"无法读取视频分辨率: {video_path}")
        return src_w, src_h
    finally:
        cap.release()


def _resolve_collect_resize(video_path: Path, width: int, height: int) -> tuple[int, int] | None:
    """解析采集推理尺寸：宽+高均指定则固定缩放；仅高则按 visual-dps 等比缩放。"""
    if width > 0 and height > 0:
        return width, height
    if height > 0:
        src_w, src_h = _read_video_source_size(video_path)
        infer_w, infer_h, resize_needed = _compute_infer_resolution(src_w, src_h, height)
        if resize_needed:
            return infer_w, infer_h
    return None


def _throttle_frame_rate(frame_rate: float, loop_started_at: float) -> None:
    """与 visual-dps / box_human_det 一致：限制采集推理节拍（帧/秒）。"""
    if frame_rate <= 0:
        return
    frame_period_sec = 1.0 / max(1.0, float(frame_rate))
    sleep_sec = frame_period_sec - (time.perf_counter() - loop_started_at)
    if sleep_sec > 0:
        time.sleep(sleep_sec)


def _apply_floor_fields(frame_out: dict[str, Any], floor: Any) -> None:
    if floor is None:
        return
    if floor.foot_uv_px is not None:
        frame_out["foot_uv_px"] = floor.foot_uv_px
    if floor.raw_floor_xy_m is not None:
        frame_out["raw_floor_xy_m"] = floor.raw_floor_xy_m
    if floor.floor_xy_m is not None:
        frame_out["floor_xy_m"] = floor.floor_xy_m
    if getattr(floor, "trail_segment_id", None) is not None:
        frame_out["foot_trail_segment_id"] = int(floor.trail_segment_id)


def collect_from_video(
    pipeline: RTMPosePipeline,
    video_path: Path,
    *,
    resize: tuple[int, int] | None = None,
    frame_interval: int = 1,
    frame_rate: float = 0.0,
    max_frames: int | None = None,
    on_progress: ProgressCallback | None = None,
    annotation_path: str | Path | None = None,
    alarm_min_consecutive_frames: int = 3,
    alarm_cooldown_frames: int = 12,
    spatial_calibration: Any | None = None,
) -> dict[str, Any]:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {video_path}")

    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    if fps <= 0:
        fps = 15.0

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    frames_out: list[dict[str, Any]] = []
    read_idx = 0
    saved_idx = 0
    interval = max(1, int(frame_interval))

    collect_frame_rate = max(0.0, float(frame_rate))

    collision_processor: CollisionProcessor | None = None
    annotation_meta: dict[str, Any] | None = None
    infer_w_init = int(resize[0]) if resize else int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    infer_h_init = int(resize[1]) if resize else int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    if annotation_path and load_scaled_boxes and CollisionProcessor:
        ann_path = Path(annotation_path)
        if ann_path.is_file():
            scaled_boxes = load_scaled_boxes(ann_path, max(1, infer_w_init), max(1, infer_h_init))
            if scaled_boxes:
                collision_processor = CollisionProcessor(
                    scaled_boxes,
                    alarm_min_consecutive_frames=alarm_min_consecutive_frames,
                    alarm_cooldown_frames=alarm_cooldown_frames,
                    video_fps=fps,
                )
                ann_cfg = load_annotation_config(ann_path)
                annotation_meta = {
                    "source_file": ann_path.name,
                    "annotation_size": {"width": infer_w_init, "height": infer_h_init},
                    "source_annotation_size": ann_cfg.get("annotation_size"),
                    "source_info": ann_cfg.get("source_info"),
                    "shelves": ann_cfg.get("shelves"),
                    "grid_shape": ann_cfg.get("grid_shape"),
                    "boxes": boxes_for_json_export(scaled_boxes),
                    "box_count": len(scaled_boxes),
                }
                print(f"ℹ️ 碰撞检测: 已加载 {len(scaled_boxes)} 个货框（{ann_path.name}）")
            else:
                print(f"⚠️ 标注 JSON 无有效货框: {ann_path}")

    floor_smooth: Any | None = None
    floor_sticky: Any | None = None
    spatial_cal_active: Any | None = None
    if spatial_calibration is not None and FloorSmoothState and project_foot_for_frame:
        spatial_cal_active = spatial_calibration
        floor_smooth = FloorSmoothState.from_calibration(spatial_cal_active)
        if StickyFootTracker is not None:
            floor_sticky = StickyFootTracker.from_calibration(spatial_cal_active)
        print(
            f"ℹ️ 地面投射: 机位 {spatial_cal_active.camera_slug} "
            f"RMSE={spatial_cal_active.ground_control_rmse_px:.2f}px"
        )

    try:
        while True:
            loop_started_at = time.perf_counter()
            ret, frame = cap.read()
            if not ret or frame is None:
                break
            read_idx += 1
            if interval > 1 and (read_idx - 1) % interval != 0:
                if on_progress:
                    on_progress(read_idx, total_frames)
                continue

            infer_w, infer_h = frame.shape[1], frame.shape[0]
            if resize:
                infer_w, infer_h = resize
                frame = cv2.resize(frame, resize, interpolation=cv2.INTER_AREA)

            batch = pipeline.infer(frame)
            _throttle_frame_rate(collect_frame_rate, loop_started_at)
            saved_idx += 1
            ts = (read_idx - 1) / fps
            persons = persons_from_batch(batch)
            frame_out: dict[str, Any] = {
                "frame_idx": saved_idx,
                "source_frame_idx": read_idx,
                "timestamp_sec": round(ts, 6),
                "infer_width": infer_w,
                "infer_height": infer_h,
                "persons": persons,
            }
            if collision_processor is not None:
                event = collision_processor.process(
                    {"frame_idx": read_idx, "persons": persons}
                )
                frame_out["persons"] = event.get("skeletons") or persons
                frame_out["collisions"] = event.get("collisions") or []
                frame_out["alarm_collisions"] = event.get("alarm_collisions") or []
            if spatial_cal_active is not None and floor_smooth is not None:
                floor = project_foot_for_frame(
                    spatial_cal_active,
                    frame_out.get("persons") or persons,
                    floor_smooth,
                    sticky_tracker=floor_sticky,
                    frame_idx=saved_idx,
                )
                _apply_floor_fields(frame_out, floor)
                if pick_primary_person and floor.foot_uv_px:
                    person = pick_primary_person(frame_out.get("persons") or persons)
                    if person is not None:
                        frame_out["foot_person_id"] = int(
                            person.get("person_id") if person.get("person_id") is not None else -1
                        )
            frames_out.append(frame_out)
            if on_progress:
                on_progress(read_idx, total_frames)
            if max_frames is not None and saved_idx >= max_frames:
                break
    finally:
        cap.release()

    result: dict[str, Any] = {
        "schema": 1,
        "kind": "pose_collect_video",
        "model": f"rtmpose_{pipeline.variant}",
        "det_model": f"rtmdet_{pipeline.det_variant}",
        "source": str(video_path.resolve()),
        "source_video": video_path.name,
        "source_video_stem": video_path.stem,
        "source_type": "video",
        "keypoint_format": "coco17",
        "keypoint_names": list(COCO17_KEYPOINT_NAMES),
        "fps": fps,
        "total_frames": total_frames,
        "frame_interval": interval,
        "collect_frame_rate": collect_frame_rate,
        "frame_count": len(frames_out),
        "frames": frames_out,
    }
    if annotation_meta is not None:
        result["annotation"] = annotation_meta
        result["collision"] = {
            "enabled": True,
            "alarm_min_consecutive_frames": alarm_min_consecutive_frames,
            "alarm_cooldown_frames": alarm_cooldown_frames,
        }
    if spatial_cal_active is not None:
        result["spatial"] = spatial_cal_active.manifest_summary()
    return result


def write_collect_output(data: dict[str, Any], output_path: str | Path) -> Path:
    """写入采集结果：目录 → schema v2 Parquet；.json 文件 → v1 兼容。"""
    out = Path(output_path)
    if out.suffix.lower() == ".json":
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        return out.resolve()
    record_id = out.name if out.suffix == "" else out.stem
    return write_v2_package(out, data, record_id=record_id)


def write_json(data: dict[str, Any], output_path: str | Path) -> Path:
    """兼容旧调用：默认写入 v2 Parquet 包（output_path 为目录）。"""
    return write_collect_output(data, output_path)


def run_collect_job(
    *,
    video_path: Path,
    output_path: Path,
    models_dir: str,
    variant: str,
    det_variant: str = "nano",
    device: str,
    ort_backend: str,
    width: int,
    height: int,
    frame_interval: int,
    frame_rate: float = 0.0,
    max_frames: int | None = None,
    on_progress: ProgressCallback | None = None,
    annotation_path: str | Path | None = None,
    alarm_min_consecutive_frames: int = 3,
    alarm_cooldown_frames: int = 12,
    camera_slug: str = "",
    spatial_dir: str | Path | None = None,
    spatial_enabled_flag: bool = True,
) -> dict[str, Any]:
    video_path = validate_video_path(video_path)
    resize = _resolve_collect_resize(video_path, width, height)
    if resize:
        print(f"ℹ️ 推理缩放: {resize[0]}x{resize[1]}（height={height}，与 visual-dps 等比逻辑）")
    elif height > 0:
        src_w, src_h = _read_video_source_size(video_path)
        print(f"ℹ️ 推理缩放: 关闭（源 {src_w}x{src_h} 不高于目标高 {height}）")
    else:
        print("ℹ️ 推理缩放: 关闭（未指定 inference.height）")

    pipeline = RTMPosePipeline(
        variant=parse_variant(variant),
        det_variant=det_variant,
        models_dir=models_dir,
        device=device,
        backend=ort_backend,
    )

    spatial_cal = None
    slug = str(camera_slug or "").strip()
    if spatial_enabled_flag and slug and spatial_dir and SpatialCalibration is not None:
        from spatial_pose.calibration import load_calibration

        infer_w = int(resize[0]) if resize else 0
        infer_h = int(resize[1]) if resize else 0
        if infer_w <= 0 or infer_h <= 0:
            infer_w, infer_h = _read_video_source_size(video_path)
        spatial_cal = load_calibration(
            Path(spatial_dir),
            slug,
            infer_width=infer_w,
            infer_height=infer_h,
            require_enabled=True,
        )
        if spatial_cal is None:
            print(f"⚠️ 未找到可用 spatial 标定: {slug}（跳过 floor_xy）")

    t0 = time.perf_counter()
    data = collect_from_video(
        pipeline,
        video_path,
        resize=resize,
        frame_interval=frame_interval,
        frame_rate=frame_rate,
        max_frames=max_frames,
        on_progress=on_progress,
        annotation_path=annotation_path,
        alarm_min_consecutive_frames=alarm_min_consecutive_frames,
        alarm_cooldown_frames=alarm_cooldown_frames,
        spatial_calibration=spatial_cal,
    )
    data["collected_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    data["elapsed_sec"] = round(time.perf_counter() - t0, 3)
    data["storage"] = STORAGE_V2_PARQUET if Path(output_path).suffix.lower() != ".json" else "v1_json"
    if slug:
        data["camera_slug"] = slug
    write_collect_output(data, output_path)
    return data
