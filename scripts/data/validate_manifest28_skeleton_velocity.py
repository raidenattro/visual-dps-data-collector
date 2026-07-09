#!/usr/bin/env python3
"""[已废弃] 早期验证脚本，含 Combo1 组合评估，报告易误导。

请改用纯速度验证：
  python scripts/data/validate_baseline28_subsampled_velocity.py
报告输出：docs/skeleton-velocity-speed-filter.md
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 必须在 collision 导入前安装 cv2 shim
from scripts.data.evaluate_combo1_segment_filter import _ensure_cv2_point_polygon_test

_ensure_cv2_point_polygon_test()

from config_loader import resolve_config_path
from scripts.data.analyze_skeleton_velocity_discrimination import (
    TORSO_THRESHOLDS,
    LOWER_THRESHOLDS,
    RATIO_THRESHOLDS,
    SkeletonPool,
    _analyze_record,
    _best_f1,
    _merge_pool,
    _pool_summary,
    _threshold_scan,
    _threshold_scan_le,
)
from scripts.data.evaluate_combo1_segment_filter import (
    COMBO1_MAX_DISP_PER_FRAME,
    COMBO1_MIN_DURATION,
    COMBO1_MIN_FRAMES,
    MotionComboRule,
    _evaluate_record,
    _precision_proxy,
)
from scripts.train.segment_filter_core import combo1_rule

MANIFEST = ROOT / "localdata/export/rule-baseline-prod-test/_manifest.json"
OUT_JSON = ROOT / "localdata/export/rule-baseline-prod-test/skeleton_velocity_validation.json"
OUT_MD = ROOT / "docs/_deprecated_skeleton_velocity_manifest28.md"


def _load_manifest_ids() -> list[str]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return [r["record_id"] for r in manifest.get("records") or []]


def _feature_ranking(pool: dict[str, Any]) -> list[dict[str, Any]]:
    """按标真 vs 误报段 F1 排序各特征。"""
    scans = [
        ("torso_speed_p50", pool.get("torso_p50_false_alarm_scan") or [], "le"),
        ("lower_mean_speed_p50", pool.get("lower_p50_false_alarm_scan") or [], "le"),
        ("body_mean_speed_p50", _threshold_scan_le(
            pool["body_mean_speed_p50_seg"]["gt_overlap_seg"].get("n") and [],
            [], []
        ), "le"),
        ("wrist_torso_ratio_p50", pool.get("ratio_p50_false_alarm_scan") or [], "ge"),
    ]
    # 重新从 pool 原始数据计算 body scan
    from scripts.data.analyze_skeleton_velocity_discrimination import _analyze_record as _  # noqa

    rows: list[dict[str, Any]] = []
    for name, scan, direction in [
        ("torso_speed_p50", pool.get("torso_p50_false_alarm_scan") or [], "le"),
        ("lower_mean_speed_p50", pool.get("lower_p50_false_alarm_scan") or [], "le"),
        ("wrist_torso_ratio_p50", pool.get("ratio_p50_false_alarm_scan") or [], "ge"),
    ]:
        best = _best_f1(scan)
        gt = pool
        seg_key = {
            "torso_speed_p50": "torso_speed_p50_seg",
            "lower_mean_speed_p50": "lower_mean_speed_p50_seg",
            "wrist_torso_ratio_p50": "wrist_torso_ratio_p50_seg",
        }[name]
        gt_s = pool[seg_key]["gt_overlap_seg"]
        fa_s = pool[seg_key]["false_alarm_seg"]
        rows.append({
            "feature": name,
            "direction": direction,
            "gt_p50": gt_s.get("p50"),
            "false_alarm_p50": fa_s.get("p50"),
            "gt_mean": gt_s.get("mean"),
            "false_alarm_mean": fa_s.get("mean"),
            "best_threshold": best.get("threshold") if best else None,
            "best_f1": best.get("f1") if best else None,
            "best_precision": best.get("precision") if best else None,
            "best_recall": best.get("recall") if best else None,
        })
    rows.sort(key=lambda r: (r.get("best_f1") or 0), reverse=True)
    return rows


def _combo_motion_grid(record_ids: list[str]) -> list[dict[str, Any]]:
    """Combo1 + 躯干速度上限网格搜索。"""
    combo1 = combo1_rule()
    results: list[dict[str, Any]] = []

    # 先算 Combo1 baseline
    c1_rows = []
    raw_rows = []
    for rid in record_ids:
        raw_rows.append(_evaluate_record(rid, alarm_min=5, alarm_cooldown=6, apply_combo=False))
        c1_rows.append(
            _evaluate_record(
                rid, alarm_min=5, alarm_cooldown=6, combo_rule=combo1, apply_combo=True
            )
        )

    def agg(rows: list[dict[str, Any]]) -> dict[str, int]:
        ok = [r for r in rows if r.get("status") == "ok"]
        return {
            "detected": sum(int(r.get("detected") or 0) for r in ok),
            "false_alarms": sum(int(r.get("false_alarms") or 0) for r in ok),
            "gt_segments": sum(int(r.get("gt_segments") or 0) for r in ok),
        }

    raw_agg = agg(raw_rows)
    c1_agg = agg(c1_rows)

    for torso_t in [20, 30, 40, 50, 60, 80, 100, 120, 150, 200]:
        rule = MotionComboRule(
            combo_id=5,
            min_frames=COMBO1_MIN_FRAMES,
            min_duration=COMBO1_MIN_DURATION,
            max_disp_per_frame=COMBO1_MAX_DISP_PER_FRAME,
            max_displacement=None,
            max_torso_speed_p50=float(torso_t),
            max_lower_mean_speed_p50=None,
        )
        mot_rows = []
        for rid in record_ids:
            mot_rows.append(
                _evaluate_record(
                    rid,
                    alarm_min=5,
                    alarm_cooldown=6,
                    combo_rule=rule,
                    apply_combo=True,
                )
            )
        mot_agg = agg(mot_rows)
        tp_loss = (
            (c1_agg["detected"] - mot_agg["detected"]) / c1_agg["detected"]
            if c1_agg["detected"] > 0
            else None
        )
        fp_drop = (
            (c1_agg["false_alarms"] - mot_agg["false_alarms"]) / c1_agg["false_alarms"]
            if c1_agg["false_alarms"] > 0
            else None
        )
        results.append({
            "max_torso_speed_p50": torso_t,
            "detected": mot_agg["detected"],
            "false_alarms": mot_agg["false_alarms"],
            "tp_loss_vs_combo1": round(tp_loss, 4) if tp_loss is not None else None,
            "fp_drop_vs_combo1": round(fp_drop, 4) if fp_drop is not None else None,
            "precision_proxy": _precision_proxy(mot_agg["detected"], mot_agg["false_alarms"]),
        })

    # lower_mean_speed grid with best torso
    best_torso = max(
        (r for r in results if (r.get("tp_loss_vs_combo1") or 0) <= 0.05),
        key=lambda r: (r.get("fp_drop_vs_combo1") or 0),
        default=results[0] if results else None,
    )
    lower_results: list[dict[str, Any]] = []
    if best_torso:
        torso_t = best_torso["max_torso_speed_p50"]
        for lower_t in [30, 50, 80, 100, 120, 150]:
            rule = MotionComboRule(
                combo_id=6,
                min_frames=COMBO1_MIN_FRAMES,
                min_duration=COMBO1_MIN_DURATION,
                max_disp_per_frame=COMBO1_MAX_DISP_PER_FRAME,
                max_displacement=None,
                max_torso_speed_p50=float(torso_t),
                max_lower_mean_speed_p50=float(lower_t),
            )
            mot_rows = [
                _evaluate_record(
                    rid, alarm_min=5, alarm_cooldown=6, combo_rule=rule, apply_combo=True
                )
                for rid in record_ids
            ]
            mot_agg = agg(mot_rows)
            fp_drop = (
                (c1_agg["false_alarms"] - mot_agg["false_alarms"]) / c1_agg["false_alarms"]
                if c1_agg["false_alarms"] > 0
                else None
            )
            tp_loss = (
                (c1_agg["detected"] - mot_agg["detected"]) / c1_agg["detected"]
                if c1_agg["detected"] > 0
                else None
            )
            lower_results.append({
                "max_torso_speed_p50": torso_t,
                "max_lower_mean_speed_p50": lower_t,
                "detected": mot_agg["detected"],
                "false_alarms": mot_agg["false_alarms"],
                "tp_loss_vs_combo1": round(tp_loss, 4) if tp_loss is not None else None,
                "fp_drop_vs_combo1": round(fp_drop, 4) if fp_drop is not None else None,
            })

    return {
        "raw": raw_agg,
        "combo1": c1_agg,
        "torso_grid": results,
        "lower_grid": lower_results,
        "best_torso_row": best_torso,
    }


def _render_md(
    *,
    record_ids: list[str],
    pool: dict[str, Any],
    ranking: list[dict[str, Any]],
    combo: dict[str, Any],
) -> str:
    lines = [
        "# 全骨骼速度特征验证（rule-baseline-prod-test 28 条）",
        "",
        f"- 记录数：{len(record_ids)}",
        f"- 标真重叠碰撞段：{pool.get('segments_gt_overlap')}",
        f"- 误报碰撞段：{pool.get('segments_false_alarm')}",
        "",
        "## 1. 段级特征区分度排名（标真 vs 误报）",
        "",
        "| 特征 | 标真 P50 | 误报 P50 | 最佳阈值 | F1 | 精确率 | 召回率 |",
        "|------|----------|----------|----------|-----|--------|--------|",
    ]
    for r in ranking:
        th = r.get("best_threshold")
        th_str = f"≤{th}" if r["direction"] == "le" else f"≥{th}"
        lines.append(
            f"| {r['feature']} | {r.get('gt_p50','—')} | {r.get('false_alarm_p50','—')} | "
            f"{th_str} | {r.get('best_f1','—')} | {r.get('best_precision','—')} | {r.get('best_recall','—')} |"
        )

    best = ranking[0] if ranking else {}
    lines.extend([
        "",
        "## 2. 推荐过滤特征",
        "",
        f"**首选特征：`{best.get('feature', '—')}`**",
        "",
        f"- 过滤方向：{'段内速度 ≤ 阈值保留（拒绝高速度走过误报）' if best.get('direction') == 'le' else '段内比值 ≥ 阈值保留'}",
        f"- 建议段级阈值：{best.get('best_threshold', '—')}",
        f"- 段级区分 F1：{best.get('best_f1', '—')}（对比手腕 speed F1≈0.17）",
        "",
        "## 3. Combo1 + 运动速度告警级评估",
        "",
        f"- 原始告警：TP={combo['raw']['detected']} FP={combo['raw']['false_alarms']}",
        f"- Combo1：TP={combo['combo1']['detected']} FP={combo['combo1']['false_alarms']}",
        "",
        "### torso_speed_p50 上限网格（Combo1 基础上）",
        "",
        "| torso_p50 ≤ | TP | FP | FP下降(vs Combo1) | TP损失(vs Combo1) | 精确率代理 |",
        "|-------------|-----|-----|------------------|------------------|------------|",
    ])
    for row in combo.get("torso_grid") or []:
        lines.append(
            f"| {row['max_torso_speed_p50']} | {row['detected']} | {row['false_alarms']} | "
            f"{row.get('fp_drop_vs_combo1','—')} | {row.get('tp_loss_vs_combo1','—')} | "
            f"{row.get('precision_proxy','—')} |"
        )

    best_combo = combo.get("best_torso_row") or {}
    lines.extend([
        "",
        "## 4. 最终推荐规则",
        "",
        "```",
        f"Combo1 (fc≥4, dur≥0.20, dpf≤2.5)",
        f"AND torso_speed_p50 ≤ {best_combo.get('max_torso_speed_p50', 'TBD')}",
        "```",
        "",
        f"- 预期 FP 下降（相对 Combo1）：{best_combo.get('fp_drop_vs_combo1', '—')}",
        f"- 预期 TP 损失（相对 Combo1）：{best_combo.get('tp_loss_vs_combo1', '—')}",
        "",
    ])
    if combo.get("lower_grid"):
        lines.extend([
            "### 叠加 lower_mean_speed_p50",
            "",
            "| lower_p50 ≤ | TP | FP | FP下降 | TP损失 |",
            "|-------------|-----|-----|--------|--------|",
        ])
        for row in combo["lower_grid"]:
            lines.append(
                f"| {row['max_lower_mean_speed_p50']} | {row['detected']} | {row['false_alarms']} | "
                f"{row.get('fp_drop_vs_combo1','—')} | {row.get('tp_loss_vs_combo1','—')} |"
            )
    return "\n".join(lines) + "\n"


def main() -> int:
    resolve_config_path(None)
    record_ids = _load_manifest_ids()
    pool_acc = SkeletonPool()
    per_record: list[dict[str, Any]] = []

    for rid in record_ids:
        result = _analyze_record(rid)
        if not result or result.get("error"):
            print(f"{rid}: skip {result.get('error') if result else '无结果'}")
            continue
        per_record.append({k: v for k, v in result.items() if k != "_pool"})
        _merge_pool(pool_acc, result["_pool"])
        print(
            f"{rid}: gt_seg={result.get('segments_gt_overlap')} "
            f"fa_seg={result.get('segments_false_alarm')}"
        )

    pool = _pool_summary(pool_acc)

    # 段级特征排名
    ranking: list[dict[str, Any]] = []
    for name, scan_key, direction in [
        ("torso_speed_p50", "torso_p50_false_alarm_scan", "le"),
        ("lower_mean_speed_p50", "lower_p50_false_alarm_scan", "le"),
        ("torso_speed_max", None, "le"),
        ("wrist_torso_ratio_p50", "ratio_p50_false_alarm_scan", "ge"),
    ]:
        if scan_key:
            scan = pool.get(scan_key) or []
        else:
            # torso_speed_max: compute from pool lists
            from scripts.data.analyze_skeleton_velocity_discrimination import _stats
            gt_vals = pool_acc.torso_max_gt_seg
            fa_vals = pool_acc.torso_max_false_alarm_seg
            scan = _threshold_scan_le(gt_vals, fa_vals, TORSO_THRESHOLDS)
        best = _best_f1(scan)
        seg_map = {
            "torso_speed_p50": "torso_speed_p50_seg",
            "lower_mean_speed_p50": "lower_mean_speed_p50_seg",
            "torso_speed_max": "torso_speed_max_seg",
            "wrist_torso_ratio_p50": "wrist_torso_ratio_p50_seg",
        }
        sk = seg_map[name]
        ranking.append({
            "feature": name,
            "direction": direction,
            "gt_p50": pool[sk]["gt_overlap_seg"].get("p50"),
            "false_alarm_p50": pool[sk]["false_alarm_seg"].get("p50"),
            "best_threshold": best.get("threshold") if best else None,
            "best_f1": best.get("f1") if best else None,
            "best_precision": best.get("precision") if best else None,
            "best_recall": best.get("recall") if best else None,
        })
    ranking.sort(key=lambda r: (r.get("best_f1") or 0), reverse=True)

    combo = _combo_motion_grid(record_ids)

    payload = {
        "manifest": str(MANIFEST),
        "record_count": len(record_ids),
        "record_ids": record_ids,
        "pool": pool,
        "feature_ranking": ranking,
        "combo_evaluation": combo,
        "recommendation": {
            "feature": ranking[0]["feature"] if ranking else None,
            "threshold": ranking[0].get("best_threshold") if ranking else None,
            "segment_f1": ranking[0].get("best_f1") if ranking else None,
            "combo_rule": combo.get("best_torso_row"),
        },
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text(
        _render_md(record_ids=record_ids, pool=pool, ranking=ranking, combo=combo),
        encoding="utf-8",
    )

    print("\n=== 特征排名 ===")
    for r in ranking:
        print(
            f"{r['feature']}: gt_p50={r.get('gt_p50')} fa_p50={r.get('false_alarm_p50')} "
            f"best_f1={r.get('best_f1')} threshold={r.get('best_threshold')}"
        )
    print(f"\n推荐: {ranking[0]['feature']} ≤ {ranking[0].get('best_threshold')}")
    print(f"JSON: {OUT_JSON}")
    print(f"MD: {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
