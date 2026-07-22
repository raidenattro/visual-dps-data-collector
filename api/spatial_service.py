"""地面标定 API 业务逻辑。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from api.annotate_service import normalize_pose_tier
from api.record_service import locate_record_by_id
from config_loader import AppPaths, resolve_app_paths
from model_assets import VIDEO_EXTENSIONS
from pose_store import TIMELINE_FILE, load_manifest
from spatial_pose.calibration import (
    calibration_path_for_slug,
    compute_and_update_config,
    load_calibration,
    load_calibration_json,
    prepare_spatial_config_for_save,
    save_calibration,
)
from spatial_pose.grid import grid_segments_image
from spatial_pose.schema import empty_spatial_config, normalize_spatial_config
from spatial_pose.volume_calibration import compute_volume_computed_fields


def _spatial_preview_extras(config: dict[str, Any], cal=None) -> dict[str, Any]:
    computed = config.get("computed") if isinstance(config.get("computed"), dict) else {}
    vol = config.get("volume") if isinstance(config.get("volume"), dict) else {}
    extras: dict[str, Any] = {
        "volume_wireframe_segments": computed.get("volume_wireframe_segments") or [],
        "column_lines_image": computed.get("column_lines_image") or [],
        "layer_lines_image": computed.get("layer_lines_image") or {},
        "volume_rmse_px": computed.get("volume_rmse_px"),
        "face_rmse_px": computed.get("face_rmse_px") or {},
    }
    if vol.get("enabled") and not extras["volume_wireframe_segments"]:
        try:
            vol_computed = compute_volume_computed_fields(config, cal=cal)
            if vol_computed:
                extras["volume_wireframe_segments"] = vol_computed.get("volume_wireframe_segments") or []
                extras["column_lines_image"] = vol_computed.get("column_lines_image") or []
                extras["layer_lines_image"] = vol_computed.get("layer_lines_image") or {}
                extras["volume_rmse_px"] = vol_computed.get("volume_rmse_px")
                extras["face_rmse_px"] = vol_computed.get("face_rmse_px") or {}
        except Exception:
            pass
    return extras


def _bucket_has_videos(bucket: Path) -> bool:
    if not bucket.is_dir():
        return False
    return any(
        p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS and not p.name.startswith(".")
        for p in bucket.iterdir()
    )


def list_spatial_camera_slugs(
    paths: AppPaths,
    *,
    pose_tier: str = "rtmpose-m",
) -> list[str]:
    """列出可用于标定的机位 slug（video 目录 + 已有 spatial JSON）。"""
    tier = normalize_pose_tier(pose_tier)
    slugs: set[str] = set()
    tier_root = paths.video_dir / tier
    if tier_root.is_dir():
        for child in sorted(tier_root.iterdir(), key=lambda p: p.name.lower()):
            if child.is_dir() and _bucket_has_videos(child):
                slugs.add(child.name)
    if paths.spatial_dir.is_dir():
        for path in paths.spatial_dir.glob("*.json"):
            name = path.stem.strip()
            if name and name != "template":
                slugs.add(name)
    return sorted(slugs)


def preview_calibration_payload(
    camera_slug: str,
    body: dict[str, Any],
    *,
    paths: AppPaths | None = None,
    infer_width: int | None = None,
    infer_height: int | None = None,
) -> dict[str, Any]:
    """计算单应与网格预览，不落盘。"""
    paths = paths or resolve_app_paths()
    slug = str(camera_slug or "").strip()
    if not slug:
        raise ValueError("camera_slug 不能为空")
    norm = normalize_spatial_config(body, camera_slug=slug)
    cal = None
    grid_segments: list[dict[str, Any]] = []
    ground_rmse = None
    try:
        norm, cal = prepare_spatial_config_for_save(
            norm,
            infer_width=infer_width,
            infer_height=infer_height,
        )
        if cal is not None:
            grid_segments = grid_segments_image(cal)
            ground_rmse = cal.ground_control_rmse_px
    except ValueError:
        from spatial_pose.volume_calibration import compute_volume_computed_fields

        vol_computed = compute_volume_computed_fields(norm, cal=None)
        if vol_computed:
            norm.setdefault("computed", {}).update(vol_computed)

    extras = _spatial_preview_extras(norm, cal=cal)
    return {
        "camera_slug": slug,
        "ground_control_rmse_px": ground_rmse,
        "grid_segments": grid_segments,
        "config": norm,
        **extras,
    }


def get_calibration_payload(camera_slug: str, *, paths: AppPaths | None = None) -> dict[str, Any]:
    paths = paths or resolve_app_paths()
    slug = str(camera_slug or "").strip()
    if not slug:
        raise ValueError("camera_slug 不能为空")
    data = load_calibration_json(paths.spatial_dir, slug)
    if data is None:
        return empty_spatial_config(slug)
    return data


def save_calibration_payload(
    camera_slug: str,
    body: dict[str, Any],
    *,
    paths: AppPaths | None = None,
    infer_width: int | None = None,
    infer_height: int | None = None,
) -> dict[str, Any]:
    paths = paths or resolve_app_paths()
    slug = str(camera_slug or "").strip()
    if not slug:
        raise ValueError("camera_slug 不能为空")
    norm = normalize_spatial_config(body, camera_slug=slug)
    norm, cal = prepare_spatial_config_for_save(
        norm,
        infer_width=infer_width,
        infer_height=infer_height,
    )
    out_path = calibration_path_for_slug(paths.spatial_dir, slug)
    save_calibration(out_path, norm)
    payload = dict(norm)
    payload["grid_segments"] = grid_segments_image(cal) if cal is not None else []
    extras = _spatial_preview_extras(norm, cal=cal)
    payload.update({k: extras[k] for k in ("volume_wireframe_segments", "column_lines_image", "layer_lines_image", "volume_rmse_px", "face_rmse_px") if k in extras})
    return payload


def calibration_for_infer(
    camera_slug: str,
    *,
    infer_width: int,
    infer_height: int,
    paths: AppPaths | None = None,
) -> dict[str, Any] | None:
    paths = paths or resolve_app_paths()
    cal = load_calibration(
        paths.spatial_dir,
        camera_slug,
        infer_width=infer_width,
        infer_height=infer_height,
        require_enabled=True,
    )
    if cal is None:
        return None
    payload = {
        "camera_slug": cal.camera_slug,
        "enabled": cal.enabled,
        "ground_control_rmse_px": cal.ground_control_rmse_px,
        "image_to_ground_homography": cal.h_image_to_world.tolist(),
        "ground_to_image_homography": cal.h_world_to_image.tolist(),
        "visualization": cal.visualization(),
        "grid_segments": grid_segments_image(cal),
        "infer_width": cal.infer_width,
        "infer_height": cal.infer_height,
        "config": cal.config,
    }
    vol = cal.config.get("volume") if isinstance(cal.config.get("volume"), dict) else {}
    if vol.get("enabled"):
        payload["volume"] = vol
        payload["ground_columns"] = cal.config.get("ground_columns")
        payload["shelf_faces"] = cal.config.get("shelf_faces")
        extras = _spatial_preview_extras(cal.config, cal=cal)
        payload["volume_wireframe_segments"] = extras.get("volume_wireframe_segments") or []
        payload["column_lines_image"] = extras.get("column_lines_image") or []
        payload["layer_lines_image"] = extras.get("layer_lines_image") or {}
        payload["volume_rmse_px"] = extras.get("volume_rmse_px")
    return payload


def _infer_size_from_record(locator) -> tuple[int, int]:
    manifest = load_manifest(locator)
    infer_w = int(manifest.get("infer_width") or 0)
    infer_h = int(manifest.get("infer_height") or 0)
    if infer_w > 0 and infer_h > 0:
        return infer_w, infer_h
    timeline_path = locator.record_dir / TIMELINE_FILE
    if timeline_path.is_file():
        try:
            import pyarrow.parquet as pq

            table = pq.read_table(timeline_path, columns=["infer_width", "infer_height"])
            if table.num_rows > 0:
                rows = table.slice(0, 1).to_pylist()
                row = rows[0] if rows else {}
                infer_w = int(row.get("infer_width") or 0)
                infer_h = int(row.get("infer_height") or 0)
                if infer_w > 0 and infer_h > 0:
                    return infer_w, infer_h
        except Exception:
            pass
    return 0, 0


def record_spatial_context(record_id: str) -> dict[str, Any]:
    locator = locate_record_by_id(record_id)
    if not locator:
        raise FileNotFoundError("记录不存在")
    manifest = load_manifest(locator)
    spatial_meta = manifest.get("spatial") if isinstance(manifest.get("spatial"), dict) else {}
    camera_slug = str(manifest.get("camera_slug") or "").strip()
    if not camera_slug:
        parts = record_id.replace("\\", "/").split("/")
        if len(parts) >= 2:
            camera_slug = parts[-2]
    infer_w = int(manifest.get("infer_width") or 0)
    infer_h = int(manifest.get("infer_height") or 0)
    if infer_w <= 0 or infer_h <= 0:
        infer_w, infer_h = _infer_size_from_record(locator)
    if infer_w <= 0 and manifest.get("frames"):
        fr0 = manifest["frames"][0] if isinstance(manifest.get("frames"), list) else {}
        if isinstance(fr0, dict):
            infer_w = int(fr0.get("infer_width") or 0)
            infer_h = int(fr0.get("infer_height") or 0)

    cal_payload = None
    if camera_slug and infer_w > 0 and infer_h > 0:
        cal_payload = calibration_for_infer(
            camera_slug,
            infer_width=infer_w,
            infer_height=infer_h,
        )
    return {
        "record_id": record_id,
        "camera_slug": camera_slug,
        "infer_width": infer_w,
        "infer_height": infer_h,
        "spatial": spatial_meta,
        "calibration": cal_payload,
    }


def load_record_wrist_face_payload(record_id: str) -> dict[str, Any]:
    """读取手腕侧面 Y×Z 轨迹 sidecar（wrist_face.parquet），供 Left/Right Map。"""
    locator = locate_record_by_id(record_id)
    if not locator:
        raise FileNotFoundError("记录不存在")
    from wrist_face_store import WRIST_FACE_FILE, load_wrist_face_rows, playback_payload_by_hand

    rows = load_wrist_face_rows(locator.path)
    by_hand = playback_payload_by_hand(rows)
    return {
        "record_id": record_id,
        "storage": WRIST_FACE_FILE,
        "source": "wrist_face" if (locator.path / WRIST_FACE_FILE).is_file() else "none",
        "count": len(rows),
        "left_count": len(by_hand.get("left") or []),
        "right_count": len(by_hand.get("right") or []),
        "left": by_hand.get("left") or [],
        "right": by_hand.get("right") or [],
    }


def load_record_floor_foot_payload(record_id: str) -> dict[str, Any]:
    """读取足部轨迹 sidecar（floor_foot.parquet），供回放 Ground Map。"""
    locator = locate_record_by_id(record_id)
    if not locator:
        raise FileNotFoundError("记录不存在")
    from floor_foot_store import FLOOR_FOOT_FILE, load_floor_foot_rows, playback_payload_from_rows

    rows = load_floor_foot_rows(locator.path, allow_legacy_timeline=True)
    return {
        "record_id": record_id,
        "storage": FLOOR_FOOT_FILE,
        "source": "floor_foot" if (locator.path / FLOOR_FOOT_FILE).is_file() else "legacy_timeline",
        "count": len(rows),
        "rows": playback_payload_from_rows(rows),
    }
