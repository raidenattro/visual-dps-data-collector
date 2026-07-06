"""评估 run 落盘：localdata/eval_runs/{eval_id}/。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config_loader import AppPaths, resolve_app_paths

EVAL_RUNS_DIR_NAME = "eval_runs"
EVAL_RUN_SCHEMA = 1
MANIFEST_FILE = "manifest.json"
SUMMARY_FILE = "summary.json"
CLIPS_DIR = "clips"


def eval_runs_root(paths: AppPaths | None = None) -> Path:
    app_paths = paths or resolve_app_paths()
    return (app_paths.base_localdata / EVAL_RUNS_DIR_NAME).resolve()


def new_eval_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{stamp}_{uuid.uuid4().hex[:8]}"


def record_id_to_clip_filename(record_id: str) -> str:
    safe = str(record_id or "").strip().replace("\\", "/").replace("/", "__")
    return f"{safe or 'unknown'}.json"


def clip_filename_to_record_id(filename: str) -> str:
    stem = Path(filename).stem
    parts = stem.split("__")
    if len(parts) >= 3 and parts[0].startswith("rtmpose"):
        return "/".join(parts)
    return stem.replace("__", "/")


def save_eval_run(
    result: dict[str, Any],
    *,
    eval_mode: str,
    paths: AppPaths | None = None,
    eval_id: str | None = None,
    extra_manifest: dict[str, Any] | None = None,
) -> str:
    """将批量评估结果写入 eval_runs，返回 eval_id。"""
    app_paths = paths or resolve_app_paths()
    run_id = eval_id or new_eval_id()
    root = eval_runs_root(app_paths) / run_id
    clips_dir = root / CLIPS_DIR
    clips_dir.mkdir(parents=True, exist_ok=True)

    clips = result.get("clips") or []
    clip_index: list[dict[str, Any]] = []
    for clip in clips:
        if not isinstance(clip, dict):
            continue
        record_id = str(clip.get("record_id") or "").strip()
        fname = record_id_to_clip_filename(record_id) if record_id else f"clip_{len(clip_index)}.json"
        clip_path = clips_dir / fname
        clip_path.write_text(json.dumps(clip, ensure_ascii=False, indent=2), encoding="utf-8")
        clip_index.append({
            "record_id": record_id,
            "file": fname,
            "status": clip.get("status"),
            "upload_file": clip.get("upload_file"),
            "review_key": clip.get("review_key"),
            "clip": clip.get("clip"),
        })

    manifest: dict[str, Any] = {
        "schema": EVAL_RUN_SCHEMA,
        "eval_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "eval_mode": eval_mode,
        "source": result.get("source") or eval_mode,
        "rules": result.get("rules") or {},
        "params": {
            "pose_tier": result.get("pose_tier"),
            "camera_label": result.get("camera_label"),
            "camera_slug": result.get("camera_slug"),
            "upload_label": result.get("upload_label"),
            "tag_filter": (result.get("summary") or {}).get("tag_filter"),
        },
        "clip_count": len(clip_index),
        "clips": clip_index,
    }
    if extra_manifest:
        manifest["extra"] = extra_manifest

    summary = dict(result.get("summary") or {})
    summary["eval_id"] = run_id

    (root / MANIFEST_FILE).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (root / SUMMARY_FILE).write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return run_id


def list_eval_runs(paths: AppPaths | None = None, *, limit: int = 50) -> list[dict[str, Any]]:
    root = eval_runs_root(paths)
    if not root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower(), reverse=True):
        if not child.is_dir():
            continue
        manifest_path = child / MANIFEST_FILE
        summary_path = child / SUMMARY_FILE
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            summary = (
                json.loads(summary_path.read_text(encoding="utf-8"))
                if summary_path.is_file()
                else {}
            )
        except (OSError, json.JSONDecodeError):
            continue
        rows.append({
            "eval_id": manifest.get("eval_id") or child.name,
            "created_at": manifest.get("created_at"),
            "eval_mode": manifest.get("eval_mode"),
            "source": manifest.get("source"),
            "clip_count": manifest.get("clip_count"),
            "evaluated": summary.get("evaluated"),
            "recall": summary.get("recall"),
            "false_alarms": summary.get("false_alarms"),
            "params": manifest.get("params"),
        })
        if len(rows) >= max(1, int(limit)):
            break
    return rows


def load_eval_run(eval_id: str, paths: AppPaths | None = None) -> dict[str, Any]:
    root = eval_runs_root(paths) / str(eval_id).strip()
    if not root.is_dir():
        raise FileNotFoundError(f"评估 run 不存在: {eval_id}")
    manifest = json.loads((root / MANIFEST_FILE).read_text(encoding="utf-8"))
    summary_path = root / SUMMARY_FILE
    summary = (
        json.loads(summary_path.read_text(encoding="utf-8"))
        if summary_path.is_file()
        else {}
    )
    clips_meta = manifest.get("clips") or []
    clip_summaries: list[dict[str, Any]] = []
    for row in clips_meta:
        if not isinstance(row, dict):
            continue
        fname = str(row.get("file") or "").strip()
        clip_path = root / CLIPS_DIR / fname
        if not clip_path.is_file():
            clip_summaries.append({**row, "status": row.get("status") or "error", "error": "clip 文件缺失"})
            continue
        try:
            clip = json.loads(clip_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            clip_summaries.append({**row, "status": "error", "error": "clip 解析失败"})
            continue
        from api.eval_diagnostics import clip_summary_from_full

        clip_summaries.append(clip_summary_from_full(clip))
    return {
        "eval_id": eval_id,
        "manifest": manifest,
        "summary": summary,
        "clips": clip_summaries,
    }


def load_eval_clip(eval_id: str, record_id: str, paths: AppPaths | None = None) -> dict[str, Any]:
    root = eval_runs_root(paths) / str(eval_id).strip()
    clip_path = root / CLIPS_DIR / record_id_to_clip_filename(record_id)
    if not clip_path.is_file():
        raise FileNotFoundError(f"评估 clip 不存在: {eval_id} / {record_id}")
    return json.loads(clip_path.read_text(encoding="utf-8"))
