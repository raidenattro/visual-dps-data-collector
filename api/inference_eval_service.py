"""上传推测结果 JSON 与人工标真对比评估。

评估规则：is_picking=true 视为碰撞告警；货框取自 rule_alarm_collisions，
若无则回退 rule_collisions；box_id 按 event_engine.box_identity 做兼容匹配。
"""

from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config_loader import AppPaths, parse_record_path_segments, resolve_app_paths
from event_engine.box_identity import canonical_box_token
from pose_store import (
    REVIEW_STATUS_COMPLETED,
    REVIEW_STATUS_NO_COLLISION,
    locate_record,
)
from review_store import canonical_event_review_path, resolve_review_context

from api.accuracy_service import (
    _allowed_record_ids_for_tags,
    _load_event_review,
    build_ground_truth_segments,
    evaluate_segments,
    parse_accuracy_tag_filter,
)
from api.eval_diagnostics import build_clip_diagnostics
from api.eval_run_store import save_eval_run

MANIFEST_FILE = "_manifest.json"


def parse_inference_json(raw: Any) -> list[dict[str, Any]]:
    """解析单 clip 推测 JSON（根节点为帧数组）。"""
    if not isinstance(raw, list):
        raise ValueError("推测 JSON 根节点须为数组")
    frames: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            frames.append(item)
    if not frames:
        raise ValueError("推测 JSON 无有效帧条目")
    return frames


def load_inference_json_file(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 JSON: {exc}") from exc
    return parse_inference_json(data)


def extract_record_id_from_frames(frames: list[dict[str, Any]]) -> str:
    """从帧列表提取 record_id（须唯一）。"""
    ids: set[str] = set()
    for fr in frames:
        rid = str(fr.get("record_id") or "").strip()
        if rid:
            ids.add(rid)
    if not ids:
        return ""
    if len(ids) > 1:
        raise ValueError(f"单文件含多个 record_id: {sorted(ids)}")
    return next(iter(ids))


def _box_tokens_from_picking_frame(fr: dict[str, Any]) -> list[str]:
    """从 is_picking 帧提取货框 token（告警优先，否则碰撞）。"""
    tokens: list[str] = []
    seen: set[str] = set()
    for field in ("rule_alarm_collisions", "rule_collisions"):
        for raw in fr.get(field) or []:
            canon = canonical_box_token(str(raw).strip())
            if canon and canon not in seen:
                seen.add(canon)
                tokens.append(canon)
        if tokens:
            break
    return tokens


def count_rule_collisions_from_frames(frames: list[dict[str, Any]]) -> int:
    """统计上传推测 JSON 中 rule_collisions 条目数。"""
    return len(extract_rule_collisions_from_frames(frames))


def extract_rule_collisions_from_frames(
    frames: list[dict[str, Any]],
) -> list[tuple[int, str]]:
    """上传推测：提取非告警碰撞 (frame_idx, box_token)。"""
    from event_engine.box_identity import token_matches_any

    out: list[tuple[int, str]] = []
    for fr in frames:
        fi = int(fr.get("frame_idx") or 0)
        if fi <= 0:
            continue
        alarm_tokens = _box_tokens_from_picking_frame(fr) if fr.get("is_picking") else []
        for raw in fr.get("rule_collisions") or []:
            token = canonical_box_token(str(raw).strip())
            if not token:
                continue
            if alarm_tokens and token_matches_any(token, alarm_tokens):
                continue
            out.append((fi, token))
    return out


def extract_picking_alarms_from_frames(
    frames: list[dict[str, Any]],
) -> list[tuple[int, str]]:
    """is_picking=true 的帧转为 (frame_idx, box_token) 告警列表。"""
    alarms: list[tuple[int, str]] = []
    for fr in frames:
        if not fr.get("is_picking"):
            continue
        fi = int(fr.get("frame_idx") or 0)
        tokens = _box_tokens_from_picking_frame(fr)
        if tokens:
            for token in tokens:
                alarms.append((fi, token))
        else:
            # 有告警标记但无货框，仍计为一次推测（无法匹配标真 → 误报）
            alarms.append((fi, ""))
    return alarms


def _precision_proxy(detected: int, false_alarms: int) -> float | None:
    denom = detected + false_alarms
    if denom <= 0:
        return None
    return round(detected / denom, 4)


def _normalized_review_status(review: dict[str, Any] | None) -> str:
    return str((review or {}).get("status") or "").strip().lower()


@dataclass
class UploadClipInput:
    upload_file: str
    frames: list[dict[str, Any]]
    record_id: str = ""


def load_upload_manifest(dir_path: Path) -> dict[str, Any] | None:
    manifest_path = dir_path / MANIFEST_FILE
    if not manifest_path.is_file():
        return None
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def discover_upload_json_files(
    root: Path,
    *,
    manifest: dict[str, Any] | None = None,
) -> list[Path]:
    """发现目录下待评估的 clip JSON（支持子目录，排除 manifest）。"""
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"目录不存在: {root}")

    if manifest and isinstance(manifest.get("records"), list):
        paths: list[Path] = []
        for row in manifest["records"]:
            if not isinstance(row, dict):
                continue
            rel = str(row.get("file") or "").strip()
            if not rel:
                continue
            candidate = root / rel
            if candidate.is_file():
                paths.append(candidate)
        if paths:
            return sorted(paths, key=lambda p: p.name.lower())

    return sorted(
        (p for p in root.rglob("*.json") if p.name != MANIFEST_FILE),
        key=lambda p: str(p.relative_to(root)).lower(),
    )


def load_upload_clips_from_dir(
    dir_path: Path,
    *,
    root: Path | None = None,
) -> tuple[list[UploadClipInput], dict[str, Any] | None]:
    """从目录加载全部 clip 推测 JSON。"""
    root = (root or dir_path).resolve()
    manifest = load_upload_manifest(root)
    files = discover_upload_json_files(root, manifest=manifest)
    if not files:
        raise ValueError(f"目录下无 clip JSON: {root}")

    clips: list[UploadClipInput] = []
    for path in files:
        frames = load_inference_json_file(path)
        record_id = extract_record_id_from_frames(frames)
        try:
            rel_name = str(path.relative_to(root))
        except ValueError:
            rel_name = path.name
        clips.append(
            UploadClipInput(
                upload_file=rel_name,
                frames=frames,
                record_id=record_id,
            )
        )
    return clips, manifest


def _resolve_review_for_record(
    paths: AppPaths,
    record_id: str,
) -> tuple[str, dict[str, Any] | None, str | None]:
    locator = locate_record(paths.json_dir, record_id)
    if not locator:
        return "", None, f"未找到 pose 记录: {record_id}"

    review_key, _, _ = resolve_review_context(locator, paths)
    review_path = canonical_event_review_path(paths, review_key)
    review = _load_event_review(review_path)
    if not review:
        return review_key, None, "复核 JSON 不存在"
    return review_key, review, None


def evaluate_uploaded_clip(
    paths: AppPaths,
    clip: UploadClipInput,
    *,
    allowed_ids: set[str] | None = None,
    tag_filter: list[str] | None = None,
) -> dict[str, Any]:
    """评估单个上传 clip（is_picking + 货框匹配）。"""
    base = {
        "upload_file": clip.upload_file,
        "record_id": clip.record_id,
        "review_key": "",
    }

    record_id = str(clip.record_id or "").strip()
    if not record_id:
        return {**base, "status": "error", "error": "JSON 缺少 record_id"}

    if allowed_ids is not None and record_id not in allowed_ids:
        return {
            **base,
            "status": "excluded",
            "error": f"记录标签未同时包含：{', '.join(tag_filter or [])}",
        }

    review_key, review, review_err = _resolve_review_for_record(paths, record_id)
    base["review_key"] = review_key
    if review_err:
        return {**base, "status": "error", "error": review_err}

    review_status = _normalized_review_status(review)
    if review_status == REVIEW_STATUS_NO_COLLISION:
        return {**base, "status": "excluded", "error": "无碰撞复核，不参与评估"}
    if review_status != REVIEW_STATUS_COMPLETED:
        return {
            **base,
            "status": "excluded",
            "error": f"复核未完成（status={review_status or 'not_started'}）",
        }

    verified = review.get("verified_true") if isinstance(review, dict) else []
    if not isinstance(verified, list) or not verified:
        return {**base, "status": "skipped", "error": "无 verified_true 范本数据"}

    segments = build_ground_truth_segments([e for e in verified if isinstance(e, dict)])
    if not segments:
        return {**base, "status": "skipped", "error": "verified_true 无有效货框范本"}

    alarms = extract_picking_alarms_from_frames(clip.frames)
    collisions = extract_rule_collisions_from_frames(clip.frames)
    metrics = evaluate_segments(segments, alarms)
    diagnostics = build_clip_diagnostics(
        segments,
        alarms,
        metrics,
        collisions=collisions,
        source_label="上传推测 · is_picking",
        collision_count=len(collisions),
        verified_count=len(verified),
    )
    picking_frame_count = len({
        int(fr.get("frame_idx") or 0)
        for fr in clip.frames
        if fr.get("is_picking")
    })

    _, slug, clip_name = parse_record_path_segments(record_id)

    return {
        **base,
        "record_id": record_id,
        "clip": clip_name or clip.upload_file,
        "camera_slug": slug or "",
        "source_video": str((review or {}).get("source_video") or ""),
        "status": "ok",
        "gt_segments": metrics["gt_segments"],
        "detected": metrics["detected"],
        "missed": metrics["missed"],
        "false_alarms": metrics["false_alarms"],
        "recall": metrics["recall"],
        "miss_rate": metrics["miss_rate"],
        "alarm_count": len(alarms),
        "collision_count": count_rule_collisions_from_frames(clip.frames),
        "picking_frame_count": picking_frame_count,
        "verified_entry_count": len(verified),
        "precision_proxy": _precision_proxy(metrics["detected"], metrics["false_alarms"]),
        "diagnostics": diagnostics,
        "missed_segments": diagnostics["missed_segments"],
        "false_alarm_samples": diagnostics["false_alarms"],
    }


def aggregate_upload_clip_results(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in rows if r.get("status") == "ok"]
    totals = {
        "gt_segments": sum(int(r.get("gt_segments") or 0) for r in ok),
        "detected": sum(int(r.get("detected") or 0) for r in ok),
        "missed": sum(int(r.get("missed") or 0) for r in ok),
        "false_alarms": sum(int(r.get("false_alarms") or 0) for r in ok),
        "alarm_count": sum(int(r.get("alarm_count") or 0) for r in ok),
    }
    evaluated = sum(1 for r in rows if r.get("status") == "ok")
    skipped = sum(1 for r in rows if r.get("status") == "skipped")
    errors = sum(1 for r in rows if r.get("status") == "error")
    excluded = sum(1 for r in rows if r.get("status") == "excluded")

    total_seg = totals["gt_segments"]
    d = totals["detected"]
    summary = {
        "clip_count": len(rows),
        "evaluated": evaluated,
        "skipped": skipped,
        "errors": errors,
        "excluded": excluded,
        "tag_filtered": excluded,
        **totals,
        "recall": round(d / total_seg, 4) if total_seg else None,
        "miss_rate": round(totals["missed"] / total_seg, 4) if total_seg else None,
        "precision_proxy": _precision_proxy(d, totals["false_alarms"]),
    }
    return summary


def evaluate_upload_batch(
    paths: AppPaths,
    clips: list[UploadClipInput],
    *,
    tags: list[str] | None = None,
    manifest: dict[str, Any] | None = None,
    upload_label: str = "",
) -> dict[str, Any]:
    tag_filter = list(tags or [])
    allowed_ids = _allowed_record_ids_for_tags(tag_filter)
    clip_results: list[dict[str, Any]] = []

    for clip in clips:
        clip_results.append(
            evaluate_uploaded_clip(
                paths,
                clip,
                allowed_ids=allowed_ids,
                tag_filter=tag_filter,
            )
        )

    summary = aggregate_upload_clip_results(clip_results)
    if tag_filter:
        summary["tag_filter"] = tag_filter

    batch_result = {
        "source": "upload",
        "upload_label": upload_label,
        "summary": summary,
        "clips": clip_results,
        "manifest": manifest,
        "rules": {
            "eligible": "仅复核状态为 completed（已复核）的分片参与评估",
            "excluded": "no_collision 及其它未复核状态不纳入测试、不计入统计",
            "tag_filter": "指定记录标签时，仅评估同时带有全部标签的 pose 记录",
            "ground_truth": "verified_true：优先 confirmed_box_tokens，否则 box_tokens",
            "segment": "连续 verified_true 条目范本货框相同则合并为一段",
            "prediction": "is_picking=true 视为碰撞告警；货框取 rule_alarm_collisions，无则 rule_collisions",
            "box_match": "货框按 box_id 兼容（如 85:4017 与 Box_4017 等价）",
            "success": "标真段内 [frame_start, frame_end] 出现 is_picking=true 且货框匹配",
            "miss": "标真段内无匹配 is_picking 告警记 1 次漏报",
            "false_alarm": "is_picking=true 但不在标真段时间+货框范围内记 1 次误报",
        },
    }
    eval_id = save_eval_run(
        batch_result,
        eval_mode="upload",
        paths=paths,
        extra_manifest={"upload_manifest": manifest} if manifest else None,
    )
    batch_result["eval_id"] = eval_id
    summary["eval_id"] = eval_id
    return batch_result


def evaluate_upload_directory(
    dir_path: Path,
    *,
    tags: list[str] | None = None,
    paths: AppPaths | None = None,
) -> dict[str, Any]:
    app_paths = paths or resolve_app_paths()
    clips, manifest = load_upload_clips_from_dir(dir_path)
    return evaluate_upload_batch(
        app_paths,
        clips,
        tags=tags,
        manifest=manifest,
        upload_label=str(dir_path),
    )


def _save_uploaded_file_items(work_dir: Path, file_items: list[tuple[str, bytes]]) -> None:
    """保存上传文件（支持文件夹相对路径）。"""
    work_dir.mkdir(parents=True, exist_ok=True)
    saved_json = 0
    for rel_path, content in file_items:
        rel = Path(str(rel_path).replace("\\", "/"))
        name = rel.name
        if not name:
            continue
        if name == MANIFEST_FILE:
            (work_dir / MANIFEST_FILE).write_bytes(content)
            continue
        if not name.lower().endswith(".json"):
            continue
        target = work_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        saved_json += 1
    if saved_json == 0 and not (work_dir / MANIFEST_FILE).is_file():
        raise ValueError("未收到有效的 .json 文件")


def cleanup_upload_dir(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def evaluate_uploaded_files(
    file_items: list[tuple[str, bytes]],
    *,
    tags: list[str] | None = None,
    keep_upload: bool = False,
) -> dict[str, Any]:
    """评估浏览器/API 上传的文件夹内 JSON。"""
    paths = resolve_app_paths()
    work_dir = paths.upload_dir / f"inference_eval_{uuid.uuid4().hex[:12]}"

    try:
        _save_uploaded_file_items(work_dir, file_items)
        clips, manifest = load_upload_clips_from_dir(work_dir)
        return evaluate_upload_batch(
            paths,
            clips,
            tags=tags,
            manifest=manifest,
            upload_label=work_dir.name,
        )
    finally:
        if not keep_upload:
            cleanup_upload_dir(work_dir)


# 供外部脚本复用标签解析
__all__ = [
    "MANIFEST_FILE",
    "discover_upload_json_files",
    "evaluate_upload_directory",
    "evaluate_uploaded_files",
    "extract_picking_alarms_from_frames",
    "load_upload_clips_from_dir",
    "parse_accuracy_tag_filter",
]
