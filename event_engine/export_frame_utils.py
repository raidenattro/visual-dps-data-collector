"""导出/回放共用的抽帧索引（与 upload_export_common 语义一致）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from config_loader import AppPaths, resolve_app_paths
from event_engine.speed_filter import export_frame_indices

# 28-clip 实验与 baseline-local 导出默认间隔
DEFAULT_EXPORT_POSE_FRAME_INTERVAL = 2

# baseline 导出包名（按优先级）
DEFAULT_BASELINE_EXPORT_PACKAGE_NAMES = (
    "rule-baseline-local-prod-test",
    "rule-baseline-prod-test",
)


def export_packages_root(paths: AppPaths | None = None) -> Path:
    """localdata/export 根目录（与 eval_runs_root 同类辅助函数）。"""
    app_paths = paths or resolve_app_paths()
    return (app_paths.base_localdata / "export").resolve()


def default_baseline_export_dir(
    paths: AppPaths | None = None,
    *,
    package_names: tuple[str, ...] = DEFAULT_BASELINE_EXPORT_PACKAGE_NAMES,
) -> Path | None:
    """查找带 _manifest.json 的 baseline 导出包目录。"""
    export_root = export_packages_root(paths)
    for name in package_names:
        d = export_root / name
        if (d / "_manifest.json").is_file():
            return d
    return None


def export_frame_key(frame: dict[str, Any]) -> int:
    """与 export 帧号一致：优先 source_frame_idx（视频时间轴 / upload clip frame_idx）。"""
    return int(frame.get("source_frame_idx") or frame.get("frame_idx") or 0)


def export_indices_from_timeline(
    timeline_frames: list[dict[str, Any]],
    *,
    pose_frame_interval: int = DEFAULT_EXPORT_POSE_FRAME_INTERVAL,
) -> set[int]:
    """无 baseline clip 时：按 timeline 的 export 帧键 + pose_frame_interval 生成抽帧集合。"""
    indices: list[int] = []
    for fr in timeline_frames:
        if not isinstance(fr, dict):
            continue
        idx = export_frame_key(fr)
        if idx > 0:
            indices.append(idx)
    if not indices:
        return set()
    min_idx = min(indices)
    interval = max(1, int(pose_frame_interval))
    return {i for i in indices if (i - min_idx) % interval == 0}


def _resolve_baseline_clip_path(baseline_dir: Path, entry: dict[str, Any]) -> Path | None:
    file_name = str(entry.get("file") or "").strip()
    if file_name:
        p = baseline_dir / file_name
        if p.is_file():
            return p
    path_field = str(entry.get("path") or "").strip()
    if path_field:
        p = Path(path_field)
        if p.is_file():
            return p
    clip_name = str(entry.get("clip_name") or "").strip()
    if clip_name:
        name = clip_name if clip_name.endswith(".json") else f"{clip_name}.json"
        p = baseline_dir / name
        if p.is_file():
            return p
    return None


def try_baseline_export_indices(
    record_id: str,
    *,
    baseline_dir: Path,
) -> tuple[set[int], int] | None:
    """若存在 baseline-local clip，返回其 frame_idx 集合与 manifest 中的 pose_frame_interval。"""
    manifest_path = baseline_dir / "_manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    rid = str(record_id or "").strip()
    entry: dict[str, Any] | None = None
    for row in manifest.get("records") or []:
        if isinstance(row, dict) and str(row.get("record_id") or "").strip() == rid:
            entry = row
            break
    if not entry:
        return None

    clip_path = _resolve_baseline_clip_path(baseline_dir, entry)
    if clip_path is None:
        return None

    try:
        upload_frames = json.loads(clip_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(upload_frames, list):
        return None

    indices = export_frame_indices(upload_frames)
    if not indices:
        return None

    params = manifest.get("params") or {}
    interval = max(
        1,
        int(params.get("pose_frame_interval") or DEFAULT_EXPORT_POSE_FRAME_INTERVAL),
    )
    return indices, interval


def resolve_export_indices(
    record_id: str,
    timeline_frames: list[dict[str, Any]],
    *,
    pose_frame_interval: int = DEFAULT_EXPORT_POSE_FRAME_INTERVAL,
    baseline_dir: Path | None = None,
) -> tuple[set[int], str, int]:
    """
    解析本记录用于特征计算的 export 抽帧索引。

    返回 (indices, source, pose_frame_interval)。
    source: baseline_clip | timeline_interval
    """
    if baseline_dir is None:
        baseline_dir = default_baseline_export_dir()
    if baseline_dir is not None:
        hit = try_baseline_export_indices(record_id, baseline_dir=baseline_dir)
        if hit is not None:
            indices, interval = hit
            return indices, "baseline_clip", interval

    interval = max(1, int(pose_frame_interval))
    indices = export_indices_from_timeline(
        timeline_frames,
        pose_frame_interval=interval,
    )
    return indices, "timeline_interval", interval
