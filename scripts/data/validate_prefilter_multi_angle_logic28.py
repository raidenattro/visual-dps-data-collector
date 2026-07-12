#!/usr/bin/env python3
"""多角度组合豁免实验：knee@65 / lower@60 + 2 角度 AND/OR 逻辑。

参数对齐 manifest：pose_frame_interval=2, alarm_min=3, cooldown=0。

用法（项目根目录）:
  python scripts/data/validate_prefilter_multi_angle_logic28.py
"""

from __future__ import annotations

import itertools
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.data.evaluate_combo1_segment_filter import _ensure_cv2_point_polygon_test

_ensure_cv2_point_polygon_test()

from config_loader import resolve_app_paths, resolve_config_path
from event_engine.speed_gated_collision import SpeedGateConfig, recompute_prefilter_upload_frames
from scripts.data.analyze_skeleton_velocity_discrimination import _float_or_none
from scripts.data.validate_prefilter_joint_angle28 import (
    ALARM_COOLDOWN,
    ALARM_MIN,
    LOCAL_BASELINE_DIR,
    POSE_FRAME_INTERVAL,
    _evaluate_frames,
    _false_alarm_frames,
    _gt_overlap_frames,
    _load_manifest_records,
    _prepare_record,
    _recompute_with_row_gate,
)
from api.inference_eval_service import aggregate_upload_clip_results

OUT_JSON = ROOT / "docs/json/prefilter-multi-angle-logic-experiment.json"
OUT_MD = ROOT / "docs/prefilter-multi-angle-logic-speed-gate.md"

SPEED_BASES = (
    ("knee_ankle_mean_speed", 65.0, "knee@65"),
    ("lower_mean_speed", 60.0, "lower@60"),
)

# 参与组合的角度特征及候选阈（基于前期筛选收窄）
ANGLE_THRESHOLDS: dict[str, list[float]] = {
    "arm_torso_angle_max": [65, 70, 75, 80, 85, 90],
    "elbow_angle_mean": [130, 140, 150, 160],
    "wrist_elevation_angle_max": [40, 50, 60, 70],
    "elbow_waist_angle_max": [120, 130, 140, 150],
    "arm_torso_angle_mean": [40, 50, 60, 70],
}

# 2 角度组合对（特征A, 特征B）
ANGLE_PAIRS: tuple[tuple[str, str], ...] = (
    ("arm_torso_angle_max", "elbow_angle_mean"),
    ("arm_torso_angle_max", "wrist_elevation_angle_max"),
    ("arm_torso_angle_max", "elbow_waist_angle_max"),
    ("elbow_angle_mean", "wrist_elevation_angle_max"),
    ("elbow_waist_angle_max", "wrist_elevation_angle_max"),
    ("arm_torso_angle_mean", "elbow_angle_mean"),
)

# 3 角度 AND（小网格）
TRIPLE_AND: tuple[str, str, str] = (
    "arm_torso_angle_max",
    "elbow_angle_mean",
    "wrist_elevation_angle_max",
)
TRIPLE_THRESHOLDS: dict[str, list[float]] = {
    "arm_torso_angle_max": [70, 80, 90],
    "elbow_angle_mean": [140, 150],
    "wrist_elevation_angle_max": [50, 60],
}


def _speed_high(row: dict[str, Any], *, speed_feature: str, speed_thr: float) -> bool:
    speed = _float_or_none(row.get(speed_feature))
    return speed is not None and speed > speed_thr


def _conds_met_and(row: dict[str, Any], conds: list[tuple[str, float]]) -> bool:
    hits = 0
    for feat, thr in conds:
        v = _float_or_none(row.get(feat))
        if v is not None and v >= thr:
            hits += 1
    return hits == len(conds)


def _conds_met_or(row: dict[str, Any], conds: list[tuple[str, float]]) -> bool:
    for feat, thr in conds:
        v = _float_or_none(row.get(feat))
        if v is not None and v >= thr:
            return True
    return False


def _multi_logic_gate_blocks(
    row: dict[str, Any],
    *,
    speed_feature: str,
    speed_thr: float,
    conds: list[tuple[str, float]],
    logic: str,
) -> bool:
    """下肢超速且未满足豁免条件 → block。"""
    if not _speed_high(row, speed_feature=speed_feature, speed_thr=speed_thr):
        return False
    if logic == "and":
        if _conds_met_and(row, conds):
            return False
    elif logic == "or":
        if _conds_met_or(row, conds):
            return False
    return True


def _eval_combo(
    paths,
    prepared: list[dict[str, Any]],
    *,
    gate_fn: Callable[[dict[str, Any]], bool],
    label: str,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clip_results = []
    for ctx in prepared:
        frames = _recompute_with_row_gate(ctx, gate_fn=gate_fn)
        clip_results.append(
            _evaluate_frames(paths, record_id=ctx["record_id"], upload_file=ctx["upload_file"], frames=frames)
        )
    summary = aggregate_upload_clip_results(clip_results)
    out = {
        "label": label,
        "detected": summary.get("detected"),
        "missed": summary.get("missed"),
        "false_alarms": summary.get("false_alarms"),
        "recall": summary.get("recall"),
        "precision_proxy": summary.get("precision_proxy"),
    }
    if meta:
        out.update(meta)
    return out


def _eval_pure_speed(
    paths,
    prepared: list[dict[str, Any]],
    *,
    speed_feature: str,
    speed_thr: float,
    label: str,
) -> dict[str, Any]:
    clip_results = []
    for ctx in prepared:
        frames = recompute_prefilter_upload_frames(
            ctx["timeline_frames"],
            ctx["export_indices"],
            ctx["boxes"],
            record_id=ctx["record_id"],
            speed_gate=SpeedGateConfig(feature=speed_feature, max_threshold=speed_thr, fail_open=True),
            infer_width=ctx["infer_w"],
            infer_height=ctx["infer_h"],
            video_fps=ctx["fps"],
            alarm_min_consecutive_frames=ALARM_MIN,
            alarm_cooldown_frames=ALARM_COOLDOWN,
        )
        clip_results.append(
            _evaluate_frames(paths, record_id=ctx["record_id"], upload_file=ctx["upload_file"], frames=frames)
        )
    summary = aggregate_upload_clip_results(clip_results)
    return {
        "label": label,
        "detected": summary.get("detected"),
        "missed": summary.get("missed"),
        "false_alarms": summary.get("false_alarms"),
        "recall": summary.get("recall"),
        "precision_proxy": summary.get("precision_proxy"),
    }


def _screen_pair_logic(
    prepared: list[dict[str, Any]],
    *,
    speed_feature: str,
    speed_thr: float,
    feat_a: str,
    feat_b: str,
    thr_a: float,
    thr_b: float,
    logic: str,
) -> dict[str, Any]:
    """速度超阈子集上 AND/OR 豁免的标真/误报命中率。"""
    gt_hit = gt_total = fa_hit = fa_total = 0
    conds = [(feat_a, thr_a), (feat_b, thr_b)]

    for ctx in prepared:
        gt_frames = _gt_overlap_frames(ctx.get("baseline_frames") or [], ctx["gt_segments"])
        fa_frames = _false_alarm_frames(ctx.get("baseline_frames") or [], ctx["gt_segments"])
        merged = ctx["merged_rows"]

        for frame_set, is_gt in ((gt_frames, True), (fa_frames, False)):
            for fi in frame_set:
                for (fidx, _tid), row in merged.items():
                    if fidx != fi or not _speed_high(row, speed_feature=speed_feature, speed_thr=speed_thr):
                        continue
                    exempt = _conds_met_and(row, conds) if logic == "and" else _conds_met_or(row, conds)
                    if is_gt:
                        gt_total += 1
                        if exempt:
                            gt_hit += 1
                    else:
                        fa_total += 1
                        if exempt:
                            fa_hit += 1

    gt_rate = round(gt_hit / gt_total, 4) if gt_total else 0.0
    fa_rate = round(fa_hit / fa_total, 4) if fa_total else 0.0
    return {
        "feat_a": feat_a,
        "thr_a": thr_a,
        "feat_b": feat_b,
        "thr_b": thr_b,
        "logic": logic,
        "gt_exempt_rate": gt_rate,
        "fa_exempt_rate": fa_rate,
        "separation": round(gt_rate - fa_rate, 4),
    }


def _fmt_conds(conds: list[tuple[str, float]], logic: str) -> str:
    op = " AND " if logic == "and" else " OR "
    parts = [f"{f}≥{t}" for f, t in conds]
    return op.join(parts)


def _sort_key(r: dict[str, Any]) -> tuple:
    return (
        int(r.get("missed") or 9999),
        int(r.get("false_alarms") or 9999),
        -float(r.get("recall") or 0),
    )


def _build_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 多角度 AND/OR 组合豁免实验",
        "",
        f"> 生成时间：{report.get('generated_at', '')}",
        "> 脚本：`scripts/data/validate_prefilter_multi_angle_logic28.py`",
        "",
        "## 1. 参数",
        "",
        "| 参数 | 值 |",
        "|------|-----|",
        f"| pose_frame_interval | {report['params']['pose_frame_interval']} |",
        f"| alarm_min_consecutive_frames | {report['params']['alarm_min_consecutive_frames']} |",
        f"| alarm_cooldown_frames | {report['params']['alarm_cooldown_frames']} |",
        "",
        "## 2. 决策逻辑",
        "",
        "共用的速度门控：下肢速度 > 阈 → 候选 block。",
        "",
        "- **AND 豁免**：`角度A≥阈A` **且** `角度B≥阈B` → 不 block（更像伸手取货）",
        "- **OR 豁免**：`角度A≥阈A` **或** `角度B≥阈B` → 不 block（更宽松，易增 FP）",
        "- **三重 AND**：三个角度同时达标才豁免",
        "",
        "## 3. 参照方案",
        "",
        "| 方案 | TP | FP | FN | 召回率 |",
        "|------|-----|-----|-----|--------|",
    ]
    for row in report.get("references") or []:
        lines.append(
            f"| {row.get('label')} | {row.get('detected')} | {row.get('false_alarms')} | "
            f"{row.get('missed')} | {row.get('recall')} |"
        )

    ref_knee = next((r for r in (report.get("references") or []) if "knee@65 prefilter" in str(r.get("label"))), {})
    k_fp = int(ref_knee.get("false_alarms") or 298)
    k_fn = int(ref_knee.get("missed") or 12)

    for logic_name, logic_key in (("AND", "and_top"), ("OR", "or_top")):
        lines.extend([
            "",
            f"## 4. {logic_name} 逻辑 Top（FP ≤ knee@65+{report.get('fp_cap_delta', 15)}）",
            "",
            "| 方案 | TP | FP | FN | 召回 | ΔFP | ΔFN |",
            "|------|-----|-----|-----|------|-----|-----|",
        ])
        for row in report.get(logic_key) or []:
            dfp = int(row.get("false_alarms") or 0) - k_fp
            dfn = int(row.get("missed") or 0) - k_fn
            lines.append(
                f"| {row.get('label')} | {row.get('detected')} | {row.get('false_alarms')} | "
                f"{row.get('missed')} | {row.get('recall')} | {dfp:+d} | {dfn:+d} |"
            )

    lines.extend([
        "",
        "## 5. 三重 AND Top",
        "",
        "| 方案 | TP | FP | FN | 召回 | ΔFP | ΔFN |",
        "|------|-----|-----|-----|------|-----|-----|",
    ])
    for row in report.get("triple_and_top") or []:
        dfp = int(row.get("false_alarms") or 0) - k_fp
        dfn = int(row.get("missed") or 0) - k_fn
        lines.append(
            f"| {row.get('label')} | {row.get('detected')} | {row.get('false_alarms')} | "
            f"{row.get('missed')} | {row.get('recall')} | {dfp:+d} | {dfn:+d} |"
        )

    lines.extend([
        "",
        "## 6. 帧级筛选（速度超阈子集，按 separation 排序）",
        "",
        "| 速度 | 逻辑 | 条件 | 标真豁免率 | 误报豁免率 | separation |",
        "|------|------|------|------------|------------|------------|",
    ])
    for row in (report.get("pair_screens") or [])[:20]:
        cond = _fmt_conds([(row["feat_a"], row["thr_a"]), (row["feat_b"], row["thr_b"])], row["logic"])
        lines.append(
            f"| {row.get('speed_label')} | {row.get('logic', '').upper()} | {cond} | "
            f"{row.get('gt_exempt_rate')} | {row.get('fa_exempt_rate')} | {row.get('separation')} |"
        )

    best = report.get("best_overall") or {}
    lines.extend([
        "",
        "## 7. 结论",
        "",
        f"**综合优选（{best.get('criteria', '')}）**：`{best.get('label', '—')}`",
        "",
        f"- TP={best.get('detected')} FP={best.get('false_alarms')} FN={best.get('missed')} recall={best.get('recall')}",
        f"- 相对 knee@65：ΔFP={int(best.get('false_alarms') or 0) - k_fp:+d}，ΔFN={int(best.get('missed') or 0) - k_fn:+d}",
        "",
        report.get("conclusion", ""),
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    resolve_config_path(None)
    paths = resolve_app_paths()
    prepared: list[dict[str, Any]] = []
    for entry in _load_manifest_records():
        ctx = _prepare_record(entry)
        if not ctx or ctx.get("error"):
            print(f"{entry.get('record_id')}: SKIP")
            continue
        prepared.append(ctx)
        print(f"{ctx['record_id']}: ok")

    if not prepared:
        return 2

    # 参照
    print("\n=== 参照 ===")
    references = [
        _eval_combo(paths, prepared, gate_fn=lambda r: False, label="local baseline（无门控）"),
        _eval_pure_speed(paths, prepared, speed_feature="lower_mean_speed", speed_thr=60.0, label="lower@60 prefilter"),
        _eval_pure_speed(paths, prepared, speed_feature="knee_ankle_mean_speed", speed_thr=65.0, label="knee@65 prefilter"),
        _eval_combo(
            paths, prepared,
            gate_fn=lambda r: _multi_logic_gate_blocks(
                r, speed_feature="knee_ankle_mean_speed", speed_thr=65.0,
                conds=[("arm_torso_angle_max", 70.0), ("elbow_angle_mean", 150.0)], logic="and",
            ),
            label="knee@65 + 躯干70° AND 肘150°（上期对照）",
        ),
        _eval_combo(
            paths, prepared,
            gate_fn=lambda r: _multi_logic_gate_blocks(
                r, speed_feature="knee_ankle_mean_speed", speed_thr=65.0,
                conds=[("elbow_angle_mean", 150.0)], logic="or",
            ),
            label="knee@65 + 肘角≥150（单条件对照）",
        ),
    ]
    for r in references:
        print(f"  {r['label']}: TP={r['detected']} FP={r['false_alarms']} FN={r['missed']}")

    ref_knee = references[2]
    knee_fp = int(ref_knee.get("false_alarms") or 298)
    knee_fn = int(ref_knee.get("missed") or 12)
    fp_cap = knee_fp + 15

    # 帧级 pair 筛选（每对取 AND/OR 各 top 阈组合样本）
    pair_screens: list[dict[str, Any]] = []
    for speed_feature, speed_thr, speed_label in SPEED_BASES:
        for feat_a, feat_b in ANGLE_PAIRS:
            grid_a = ANGLE_THRESHOLDS.get(feat_a, [])[:4]
            grid_b = ANGLE_THRESHOLDS.get(feat_b, [])[:4]
            for logic in ("and", "or"):
                for thr_a, thr_b in itertools.product(grid_a, grid_b):
                    row = _screen_pair_logic(
                        prepared,
                        speed_feature=speed_feature,
                        speed_thr=speed_thr,
                        feat_a=feat_a,
                        feat_b=feat_b,
                        thr_a=thr_a,
                        thr_b=thr_b,
                        logic=logic,
                    )
                    row["speed_label"] = speed_label
                    pair_screens.append(row)
    pair_screens.sort(key=lambda r: (-float(r["separation"]), float(r["fa_exempt_rate"])))

    # 从筛选结果提取每对每逻辑的代表阈（separation top1 per pair+logic+speed）
    selected_combos: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for row in pair_screens:
        key = (row["speed_label"], row["logic"], row["feat_a"], row["feat_b"], row["thr_a"], row["thr_b"])
        if key in seen:
            continue
        # 每 (speed, logic, pair) 取前 3 个 separation
        bucket = (row["speed_label"], row["logic"], row["feat_a"], row["feat_b"])
        count = sum(1 for s in selected_combos if (s["speed_label"], s["logic"], s["feat_a"], s["feat_b"]) == bucket)
        if count >= 3:
            continue
        seen.add(key)
        selected_combos.append(row)

    # 全量评估：2 角度 AND/OR（筛选代表阈 + 重点对全网格）
    and_results: list[dict[str, Any]] = []
    or_results: list[dict[str, Any]] = []
    KEY_PAIRS = (
        ("arm_torso_angle_max", "elbow_angle_mean"),
        ("arm_torso_angle_max", "wrist_elevation_angle_max"),
    )

    print("\n=== 2 角度组合评估 ===")
    for speed_feature, speed_thr, speed_label in SPEED_BASES:
        for feat_a, feat_b in ANGLE_PAIRS:
            thr_pairs: set[tuple[float, float]] = set()
            for sc in selected_combos:
                if sc.get("speed_label") == speed_label and sc["feat_a"] == feat_a and sc["feat_b"] == feat_b:
                    thr_pairs.add((float(sc["thr_a"]), float(sc["thr_b"])))
            # 重点对补充小网格
            if (feat_a, feat_b) in KEY_PAIRS:
                ga = ANGLE_THRESHOLDS.get(feat_a, [])[:4]
                gb = ANGLE_THRESHOLDS.get(feat_b, [])[:3]
                thr_pairs.update(itertools.product(ga, gb))

            for logic, bucket in (("and", and_results), ("or", or_results)):
                for thr_a, thr_b in thr_pairs:
                    conds = [(feat_a, thr_a), (feat_b, thr_b)]
                    label = f"{speed_label} + {_fmt_conds(conds, logic)}"
                    res = _eval_combo(
                        paths, prepared,
                        gate_fn=lambda r, sf=speed_feature, st=speed_thr, c=conds, lg=logic: _multi_logic_gate_blocks(
                            r, speed_feature=sf, speed_thr=st, conds=c, logic=lg
                        ),
                        label=label,
                        meta={
                            "logic": logic,
                            "speed_feature": speed_feature,
                            "speed_thr": speed_thr,
                            "conds": conds,
                        },
                    )
                    bucket.append(res)

    # 三重 AND
    triple_results: list[dict[str, Any]] = []
    print("\n=== 三重 AND ===")
    for speed_feature, speed_thr, speed_label in SPEED_BASES:
        for ta in TRIPLE_THRESHOLDS["arm_torso_angle_max"]:
            for te in TRIPLE_THRESHOLDS["elbow_angle_mean"]:
                for tw in TRIPLE_THRESHOLDS["wrist_elevation_angle_max"]:
                    conds = [
                        ("arm_torso_angle_max", ta),
                        ("elbow_angle_mean", te),
                        ("wrist_elevation_angle_max", tw),
                    ]
                    label = f"{speed_label} + {_fmt_conds(conds, 'and')}"
                    res = _eval_combo(
                        paths, prepared,
                        gate_fn=lambda r, sf=speed_feature, st=speed_thr, c=conds: _multi_logic_gate_blocks(
                            r, speed_feature=sf, speed_thr=st, conds=c, logic="and"
                        ),
                        label=label,
                        meta={"logic": "triple_and", "conds": conds, "speed_feature": speed_feature},
                    )
                    triple_results.append(res)

    and_results.sort(key=_sort_key)
    or_results.sort(key=_sort_key)
    triple_results.sort(key=_sort_key)

    and_top = [r for r in and_results if int(r.get("false_alarms") or 9999) <= fp_cap][:12]
    or_top = [r for r in or_results if int(r.get("false_alarms") or 9999) <= fp_cap][:12]
    triple_and_top = triple_results[:10]

    # 召回优先：FN 最小
    all_results = and_results + or_results + triple_results
    recall_best = min(all_results, key=_sort_key)

    # FP 约束下最优
    fp_bounded = [r for r in all_results if int(r.get("false_alarms") or 9999) <= fp_cap]
    fp_best = min(fp_bounded, key=_sort_key) if fp_bounded else recall_best

    # 综合结论
    rb_fn = int(recall_best.get("missed") or 99)
    rb_fp = int(recall_best.get("false_alarms") or 9999)
    if rb_fn < knee_fn and rb_fp <= knee_fp + 30:
        conclusion = (
            f"召回优先最优为 `{recall_best.get('label')}`（FN {knee_fn}→{rb_fn}，FP {knee_fp}→{rb_fp}）。"
            f"AND 逻辑普遍比 OR 更利于压 FP；"
            f"在 FP≤{fp_cap} 约束下优选 `{fp_best.get('label')}`。"
        )
        best_overall = {**recall_best, "criteria": "召回优先（允许 FP 适度上升）"}
    else:
        conclusion = (
            f"FP 约束下优选 `{fp_best.get('label')}`（FN={fp_best.get('missed')} FP={fp_best.get('false_alarms')}）。"
            "多角度 OR 豁免易抬高误报；AND / 三重 AND 更有助于区分取货伸手与路过摆臂。"
        )
        best_overall = {**fp_best, "criteria": f"FP ≤ knee@65+{fp_cap - knee_fp}"}

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "prepared_count": len(prepared),
        "params": {
            "pose_frame_interval": POSE_FRAME_INTERVAL,
            "alarm_min_consecutive_frames": ALARM_MIN,
            "alarm_cooldown_frames": ALARM_COOLDOWN,
        },
        "references": references,
        "pair_screens": pair_screens[:40],
        "and_results": and_results,
        "or_results": or_results,
        "triple_and_results": triple_results,
        "and_top": and_top,
        "or_top": or_top,
        "triple_and_top": triple_and_top,
        "best_recall": recall_best,
        "best_fp_bounded": fp_best,
        "best_overall": best_overall,
        "fp_cap_delta": fp_cap - knee_fp,
        "conclusion": conclusion,
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_MD.write_text(_build_markdown(report), encoding="utf-8")

    print(f"\n=== 召回优先 ===\n{recall_best.get('label')}: TP={recall_best.get('detected')} "
          f"FP={rb_fp} FN={rb_fn}")
    print(f"=== FP约束 ===\n{fp_best.get('label')}: TP={fp_best.get('detected')} "
          f"FP={fp_best.get('false_alarms')} FN={fp_best.get('missed')}")
    print(f"\n结论: {conclusion}")
    print(f"MD: {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
