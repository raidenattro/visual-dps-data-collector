"""标注 ROI 有效性：以人工复核 verified_true 为范本，对比告警检测结果。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config_loader import AppPaths, camera_storage_slug, resolve_app_paths
from pose_store import (
    REVIEW_STATUS_COMPLETED,
    REVIEW_STATUS_NO_COLLISION,
    extract_confirmed_box_tokens,
    iter_active_records,
    load_timeline,
    locate_record,
)
from review_store import EVENT_REVIEW_FILE, canonical_event_review_path, resolve_review_context

from annotation_store import (
    annotation_dir_for_source,
    annotation_path_for_video_stem,
    materialize_tier_annotation_from_master,
    resolve_video_stem_from_record,
)
from api.annotate_service import normalize_pose_tier
from api.collision_recompute_service import recompute_record_collisions
from api.record_service import meta_path_for_record, resolve_annotation_path_for_record
from event_engine.box_identity import (
    canonical_box_token,
    canonicalize_box_token_list,
    token_matches_any,
)
from record_tag_store import normalize_tag_name, record_ids_with_all_tags


def parse_accuracy_tag_filter(raw: Any) -> list[str]:
    """解析准确率标签筛选（逗号分隔或列表）；多条须同时命中。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        parts = [str(x) for x in raw]
    else:
        parts = str(raw).replace("，", ",").split(",")
    names: list[str] = []
    seen: set[str] = set()
    for part in parts:
        text = str(part or "").strip()
        if not text:
            continue
        name = normalize_tag_name(text)
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def _allowed_record_ids_for_tags(tags: list[str]) -> set[str] | None:
    if not tags:
        return None
    return record_ids_with_all_tags(tags)


def _ground_truth_tokens(entry: dict[str, Any]) -> list[str]:
    """人工范本货框：优先 confirmed_box_tokens，否则 box_tokens（均为 Box_{box_id}）。"""
    confirmed = extract_confirmed_box_tokens(entry)
    if confirmed:
        return confirmed
    raw = entry.get("box_tokens")
    if not isinstance(raw, list):
        return []
    return canonicalize_box_token_list([str(t).strip() for t in raw if str(t).strip()])


def _gt_token_key(tokens: list[str]) -> tuple[str, ...]:
    return tuple(canonicalize_box_token_list(tokens))


@dataclass
class GroundTruthSegment:
    gt_tokens: tuple[str, ...]
    frame_start: int
    frame_end: int
    entry_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "gt_tokens": list(self.gt_tokens),
            "frame_start": self.frame_start,
            "frame_end": self.frame_end,
            "entry_count": self.entry_count,
        }


def build_ground_truth_segments(verified_true: list[dict[str, Any]]) -> list[GroundTruthSegment]:
    """将 verified_true 按连续相同范本货框合并为时间段。"""
    entries = [e for e in verified_true if isinstance(e, dict)]
    entries.sort(key=lambda e: int(e.get("frame_idx") or e.get("source_frame_idx") or 0))

    segments: list[GroundTruthSegment] = []
    current: GroundTruthSegment | None = None

    for entry in entries:
        tokens = _ground_truth_tokens(entry)
        if not tokens:
            continue
        key = _gt_token_key(tokens)
        frame = int(entry.get("frame_idx") or entry.get("source_frame_idx") or 0)
        if current and current.gt_tokens == key:
            current.frame_end = max(current.frame_end, frame)
            current.entry_count += 1
        else:
            if current:
                segments.append(current)
            current = GroundTruthSegment(
                gt_tokens=key,
                frame_start=frame,
                frame_end=frame,
                entry_count=1,
            )
    if current:
        segments.append(current)
    return segments


def _extract_alarms(timeline: list[dict[str, Any]]) -> list[tuple[int, str]]:
    """从 timeline 提取全部告警 (frame_idx, box_token)。"""
    out: list[tuple[int, str]] = []
    for row in timeline:
        fi = int(row.get("frame_idx") or 0)
        for raw in row.get("alarm_collisions") or []:
            token = canonical_box_token(str(raw).strip())
            if token:
                out.append((fi, token))
    return out


def _segment_detected(segment: GroundTruthSegment, alarms: list[tuple[int, str]]) -> bool:
    gt_tokens = list(segment.gt_tokens)
    for frame, token in alarms:
        if segment.frame_start <= frame <= segment.frame_end and token_matches_any(
            token, gt_tokens
        ):
            return True
    return False


def _alarm_covered_by_segment(frame: int, token: str, segment: GroundTruthSegment) -> bool:
    if not (segment.frame_start <= frame <= segment.frame_end):
        return False
    return token_matches_any(token, list(segment.gt_tokens))


def evaluate_segments(
    segments: list[GroundTruthSegment],
    alarms: list[tuple[int, str]],
) -> dict[str, Any]:
    """按规则统计：段内出现匹配告警=成功，否则漏报；未覆盖的告警=误报。"""
    segment_results: list[dict[str, Any]] = []
    detected = 0
    missed = 0

    for seg in segments:
        ok = _segment_detected(seg, alarms)
        if ok:
            detected += 1
        else:
            missed += 1
        segment_results.append({
            **seg.to_dict(),
            "detected": ok,
        })

    false_alarms = 0
    false_alarm_samples: list[dict[str, Any]] = []
    for frame, token in alarms:
        covered = any(_alarm_covered_by_segment(frame, token, seg) for seg in segments)
        if not covered:
            false_alarms += 1
            if len(false_alarm_samples) < 20:
                false_alarm_samples.append({"frame_idx": frame, "box_token": token})

    total = len(segments)
    recall = (detected / total) if total else None
    miss_rate = (missed / total) if total else None

    return {
        "gt_segments": total,
        "detected": detected,
        "missed": missed,
        "false_alarms": false_alarms,
        "recall": recall,
        "miss_rate": miss_rate,
        "segment_details": segment_results,
        "false_alarm_samples": false_alarm_samples,
    }


def _load_event_review(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def build_review_key_index(paths: AppPaths, pose_tier: str) -> dict[str, str]:
    """review_key → record_id（指定模型层）。"""
    index: dict[str, str] = {}
    tier = normalize_pose_tier(pose_tier)
    for locator in iter_active_records(paths.json_dir, pose_tier=tier):
        review_key, _, _ = resolve_review_context(locator, paths)
        if review_key and review_key not in index:
            index[review_key] = locator.record_id
    return index


def _normalized_review_status(review: dict[str, Any] | None) -> str:
    return str((review or {}).get("status") or "").strip().lower()


def is_completed_review(review: dict[str, Any] | None) -> bool:
    """仅 status=completed 的复核参与准确率评估。"""
    return _normalized_review_status(review) == REVIEW_STATUS_COMPLETED


def list_evaluable_review_clips(paths: AppPaths, camera_slug: str) -> list[dict[str, Any]]:
    """已复核（completed）分片；无碰撞等其它状态不纳入测试池。"""
    return [
        c
        for c in list_review_clips_for_camera(paths, camera_slug)
        if c.get("review_status") == REVIEW_STATUS_COMPLETED
    ]


def list_review_clips_for_camera(paths: AppPaths, camera_slug: str) -> list[dict[str, Any]]:
    base = paths.review_dir / camera_slug
    if not base.is_dir():
        return []
    clips: list[dict[str, Any]] = []
    for child in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        review_path = child / EVENT_REVIEW_FILE
        if not review_path.is_file():
            continue
        review_key = f"{camera_slug}/{child.name}"
        review = _load_event_review(review_path)
        verified = review.get("verified_true") if review else []
        clips.append({
            "review_key": review_key,
            "clip_dir": child.name,
            "source_video": str((review or {}).get("source_video") or ""),
            "verified_count": len(verified) if isinstance(verified, list) else 0,
            "review_status": str((review or {}).get("status") or ""),
        })
    return clips


def list_cameras_with_review(paths: AppPaths) -> list[str]:
    if not paths.review_dir.is_dir():
        return []
    out: list[str] = []
    for child in sorted(paths.review_dir.iterdir(), key=lambda p: p.name.lower()):
        if child.is_dir() and any(child.glob(f"*/{EVENT_REVIEW_FILE}")):
            out.append(child.name)
    return out


def list_accuracy_camera_options(paths: AppPaths, reflection: Any) -> list[dict[str, Any]]:
    """reflection 机位 + 有 review 数据的 slug。"""
    review_slugs = set(list_cameras_with_review(paths))
    options: list[dict[str, Any]] = []
    for camera_label in reflection.cameras:
        slug = camera_storage_slug(camera_label)
        if slug not in review_slugs:
            continue
        clip_count = len(list_evaluable_review_clips(paths, slug))
        options.append({
            "camera_label": camera_label,
            "camera_slug": slug,
            "clip_count": clip_count,
        })
    for slug in sorted(review_slugs):
        if any(o["camera_slug"] == slug for o in options):
            continue
        clip_count = len(list_evaluable_review_clips(paths, slug))
        options.append({
            "camera_label": slug,
            "camera_slug": slug,
            "clip_count": clip_count,
        })
    return options


def evaluate_single_clip(
    paths: AppPaths,
    *,
    pose_tier: str,
    review_key: str,
    record_id: str,
    review_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    review_path = canonical_event_review_path(paths, review_key)
    review = review_data if review_data is not None else _load_event_review(review_path)
    if not review:
        return {
            "review_key": review_key,
            "record_id": record_id,
            "status": "error",
            "error": "复核 JSON 不存在",
        }

    review_status = _normalized_review_status(review)
    if review_status == REVIEW_STATUS_NO_COLLISION:
        return {
            "review_key": review_key,
            "record_id": record_id,
            "status": "excluded",
            "error": "无碰撞复核，不参与评估",
        }
    if review_status != REVIEW_STATUS_COMPLETED:
        return {
            "review_key": review_key,
            "record_id": record_id,
            "status": "excluded",
            "error": f"复核未完成（status={review_status or 'not_started'}）",
        }

    verified = review.get("verified_true")
    if not isinstance(verified, list) or not verified:
        return {
            "review_key": review_key,
            "record_id": record_id,
            "status": "skipped",
            "error": "无 verified_true 范本数据",
        }

    locator = locate_record(paths.json_dir, record_id)
    if not locator:
        return {
            "review_key": review_key,
            "record_id": record_id,
            "status": "error",
            "error": f"未找到 pose 记录: {record_id}",
        }

    timeline = load_timeline(locator, include_events=True)
    if not timeline:
        return {
            "review_key": review_key,
            "record_id": record_id,
            "status": "error",
            "error": "timeline 为空",
        }

    segments = build_ground_truth_segments(verified)
    if not segments:
        return {
            "review_key": review_key,
            "record_id": record_id,
            "status": "skipped",
            "error": "verified_true 无有效货框范本",
        }

    alarms = _extract_alarms(timeline)
    metrics = evaluate_segments(segments, alarms)
    missed_segments = [s for s in metrics["segment_details"] if not s.get("detected")]

    return {
        "review_key": review_key,
        "record_id": record_id,
        "source_video": str(review.get("source_video") or ""),
        "status": "ok",
        **{k: metrics[k] for k in ("gt_segments", "detected", "missed", "false_alarms", "recall", "miss_rate")},
        "alarm_count": len(alarms),
        "verified_entry_count": len(verified),
        "missed_segments": missed_segments[:10],
        "false_alarm_samples": metrics["false_alarm_samples"],
    }


def evaluate_camera_batch(
    paths: AppPaths,
    *,
    pose_tier: str,
    camera_label: str,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    tier = normalize_pose_tier(pose_tier)
    slug = camera_storage_slug(camera_label)
    tag_filter = list(tags or [])
    allowed_ids = _allowed_record_ids_for_tags(tag_filter)
    clips_meta = list_evaluable_review_clips(paths, slug)
    if not clips_meta:
        raise ValueError(f"机位 {camera_label!r}（{slug}）下无「已复核」的 review 分片")

    record_index = build_review_key_index(paths, tier)
    clip_results: list[dict[str, Any]] = []
    totals = {
        "gt_segments": 0,
        "detected": 0,
        "missed": 0,
        "false_alarms": 0,
    }
    evaluated = 0
    skipped = 0
    errors = 0
    tag_filtered = 0

    for meta in clips_meta:
        review_key = meta["review_key"]
        record_id = record_index.get(review_key, "")
        if not record_id:
            clip_results.append({
                "review_key": review_key,
                "record_id": "",
                "status": "error",
                "error": f"该模型层 {tier} 下无匹配 pose 记录",
            })
            errors += 1
            continue

        if allowed_ids is not None and record_id not in allowed_ids:
            clip_results.append({
                "review_key": review_key,
                "record_id": record_id,
                "status": "excluded",
                "error": f"记录标签未同时包含：{', '.join(tag_filter)}",
            })
            tag_filtered += 1
            continue

        result = evaluate_single_clip(
            paths,
            pose_tier=tier,
            review_key=review_key,
            record_id=record_id,
        )
        clip_results.append(result)
        st = result.get("status")
        if st == "ok":
            evaluated += 1
            for k in totals:
                totals[k] += int(result.get(k) or 0)
        elif st == "skipped":
            skipped += 1
        elif st == "excluded":
            pass
        else:
            errors += 1

    total_seg = totals["gt_segments"]
    summary = {
        "clip_count": len(clips_meta),
        "evaluated": evaluated,
        "skipped": skipped,
        "errors": errors,
        "tag_filter": tag_filter,
        "tag_filtered": tag_filtered,
        **totals,
        "recall": (totals["detected"] / total_seg) if total_seg else None,
        "miss_rate": (totals["missed"] / total_seg) if total_seg else None,
    }
    if total_seg:
        summary["precision_proxy"] = (
            totals["detected"] / (totals["detected"] + totals["false_alarms"])
            if (totals["detected"] + totals["false_alarms"])
            else None
        )

    return {
        "pose_tier": tier,
        "camera_label": camera_label,
        "camera_slug": slug,
        "summary": summary,
        "clips": clip_results,
        "rules": {
            "eligible": "仅复核状态为 completed（已复核）的分片参与评估",
            "excluded": "no_collision 及其它未复核状态不纳入测试、不计入统计",
            "tag_filter": "指定记录标签时，仅评估回放中同时带有全部标签的 pose 记录",
            "ground_truth": "verified_true：优先 confirmed_box_tokens，否则 box_tokens",
            "segment": "连续 verified_true 条目范本货框相同则合并为一段",
            "success": "段内 [frame_start, frame_end] 出现匹配货框的告警（alarm_collisions）",
            "miss": "段内无匹配告警记 1 次漏报",
            "false_alarm": "不在任一段时间与货框范围内的告警记 1 次误报",
        },
    }


def resolve_annotation_for_accuracy_record(
    paths: AppPaths,
    locator,
    *,
    pose_tier: str,
) -> Path | None:
    """优先模型层 annotations，再回退母本 / reflection。"""
    tier = normalize_pose_tier(pose_tier)
    sidecar = meta_path_for_record(locator.record_id, locator)
    meta = None
    if sidecar.is_file():
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = None
    video_stem = resolve_video_stem_from_record(
        locator.record_id,
        json_dir=paths.json_dir,
        pose_path=locator.path,
        meta=meta if isinstance(meta, dict) else None,
    )
    tier_dir = annotation_dir_for_source(paths, tier)
    tier_path = annotation_path_for_video_stem(video_stem, annotation_dir=tier_dir)
    if tier_path.is_file():
        return tier_path
    mat = materialize_tier_annotation_from_master(video_stem, paths=paths, source=tier)
    if mat and mat.is_file():
        return mat
    return resolve_annotation_path_for_record(
        locator.record_id,
        locator=locator,
        meta=meta if isinstance(meta, dict) else None,
    )


def recompute_camera_records_batch(
    paths: AppPaths,
    *,
    pose_tier: str,
    camera_label: str,
    alarm_min_consecutive_frames: int,
    alarm_cooldown_frames: int,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """对指定模型层 + 机位下与 review 匹配的全部记录重算碰撞/告警并写回 timeline（不改 review）。"""
    tier = normalize_pose_tier(pose_tier)
    slug = camera_storage_slug(camera_label)
    tag_filter = list(tags or [])
    allowed_ids = _allowed_record_ids_for_tags(tag_filter)
    record_index = build_review_key_index(paths, tier)
    clips_meta = list_evaluable_review_clips(paths, slug)
    if not clips_meta:
        raise ValueError(f"机位 {camera_label!r}（{slug}）下无「已复核」的 review 分片")

    alarm_min = max(1, int(alarm_min_consecutive_frames))
    alarm_cd = max(1, int(alarm_cooldown_frames))
    recomputed: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    seen_records: set[str] = set()
    tag_skipped = 0

    for meta in clips_meta:
        review_key = meta["review_key"]
        record_id = record_index.get(review_key, "")
        if not record_id or record_id in seen_records:
            continue
        seen_records.add(record_id)
        if allowed_ids is not None and record_id not in allowed_ids:
            tag_skipped += 1
            continue
        locator = locate_record(paths.json_dir, record_id)
        if not locator:
            errors.append({"record_id": record_id, "review_key": review_key, "error": "记录不存在"})
            continue
        ann_path = resolve_annotation_for_accuracy_record(paths, locator, pose_tier=tier)
        if not ann_path or not ann_path.is_file():
            errors.append({
                "record_id": record_id,
                "review_key": review_key,
                "error": "未找到可用标注 JSON",
            })
            continue
        try:
            result = recompute_record_collisions(
                locator,
                ann_path,
                video_stem=ann_path.stem,
                alarm_min_consecutive_frames=alarm_min,
                alarm_cooldown_frames=alarm_cd,
            )
            recomputed.append({**result, "review_key": review_key})
        except (OSError, ValueError, FileNotFoundError, RuntimeError) as exc:
            errors.append({
                "record_id": record_id,
                "review_key": review_key,
                "error": str(exc),
            })

    return {
        "pose_tier": tier,
        "camera_label": camera_label,
        "camera_slug": slug,
        "tag_filter": tag_filter,
        "tag_skipped": tag_skipped,
        "alarm_min_consecutive_frames": alarm_min,
        "alarm_cooldown_frames": alarm_cd,
        "record_count": len(seen_records),
        "recomputed_count": len(recomputed),
        "error_count": len(errors),
        "recomputed": recomputed,
        "errors": errors,
        "note": "复用已有骨架 keypoints，仅重算 collisions / alarm_collisions 并覆盖 timeline 与 manifest.collision；不修改 localdata/review 人工复核",
    }


def recompute_and_evaluate_camera_batch(
    paths: AppPaths,
    *,
    pose_tier: str,
    camera_label: str,
    alarm_min_consecutive_frames: int,
    alarm_cooldown_frames: int,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """先按新碰撞参数重算匹配记录，再执行准确率批量评估。"""
    recompute_result = recompute_camera_records_batch(
        paths,
        pose_tier=pose_tier,
        camera_label=camera_label,
        alarm_min_consecutive_frames=alarm_min_consecutive_frames,
        alarm_cooldown_frames=alarm_cooldown_frames,
        tags=tags,
    )
    if not recompute_result.get("recomputed_count") and recompute_result.get("error_count"):
        raise ValueError(
            recompute_result["errors"][0].get("error", "碰撞重算失败")
            if recompute_result.get("errors")
            else "无记录被重算"
        )
    eval_result = evaluate_camera_batch(
        paths,
        pose_tier=pose_tier,
        camera_label=camera_label,
        tags=tags,
    )
    eval_result["recompute"] = recompute_result
    return eval_result


def build_accuracy_context(
    paths: AppPaths,
    reflection: Any,
    *,
    pose_tier: str,
    camera_label: str,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    tier = normalize_pose_tier(pose_tier)
    slug = camera_storage_slug(camera_label)
    tag_filter = list(tags or [])
    allowed_ids = _allowed_record_ids_for_tags(tag_filter)
    clips = list_evaluable_review_clips(paths, slug)
    record_index = build_review_key_index(paths, tier)
    matched = 0
    tag_eligible = 0
    for c in clips:
        rid = record_index.get(c["review_key"], "")
        if not rid:
            continue
        matched += 1
        if allowed_ids is None or rid in allowed_ids:
            tag_eligible += 1
    return {
        "pose_tier": tier,
        "camera_label": camera_label,
        "camera_slug": slug,
        "clip_count": len(clips),
        "matched_record_count": matched,
        "tag_filter": tag_filter,
        "tag_eligible_clip_count": tag_eligible if tag_filter else matched,
        "review_filter": REVIEW_STATUS_COMPLETED,
        "clips": [
            {**c, "record_id": record_index.get(c["review_key"], "")}
            for c in clips
        ],
    }
