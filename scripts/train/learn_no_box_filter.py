#!/usr/bin/env python3
"""无框空间特征：LOCO 网格搜索段过滤最佳参数（强特征 only）。

特征：frame_count, duration_sec, displacement, disp/fc（不含 speed、不含 box 几何）
规则：AND 阈值，在 alarm_min=5 候选告警上做段级确认

用法（项目根目录）:
  python scripts/train/learn_no_box_filter.py
  python scripts/train/learn_no_box_filter.py --out docs/train/learned-filter-no-box-rtmpose-m.md
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 触发 evaluate_combo1 内 cv2 shim
import scripts.data.evaluate_combo1_segment_filter  # noqa: F401

from config_loader import resolve_config_path
from scripts.data.analyze_wrist_feature_discrimination import (
    DEFAULT_CAMERAS,
    DEFAULT_REVIEW_STATUS,
    DEFAULT_TAGS,
    DEFAULT_TIER,
    _collect_record_ids,
    _parse_csv_list,
    _parse_tags,
)
from scripts.data.evaluate_combo1_segment_filter import ComboRule
from scripts.data.report_paths import DOCS_JSON_DIR, resolve_docs_json
from scripts.train.segment_filter_core import (
    RecordBundle,
    aggregate_metrics,
    combo1_rule,
    evaluate_bundle,
    load_record_bundle,
    rule_from_tuple,
    rule_label,
    rule_to_dict,
)

# 网格：强特征 AND（无框）
GRID_MIN_FRAMES = (3, 4, 5, 6, 7, 8, 9, 10, 12, 15)
GRID_MIN_DURATION = (0.13, 0.20, 0.27, 0.33)
GRID_MAX_DPF = (1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 6.0)
# None 表示不限制总位移
GRID_MAX_DISPLACEMENT: tuple[float | None, ...] = (None, 20.0, 30.0, 40.0, 50.0, 80.0)


@dataclass(frozen=True)
class SearchRule:
    min_frames: int
    min_duration: float
    max_disp_per_frame: float
    max_displacement: float | None

    def to_combo_rule(self) -> ComboRule:
        return rule_from_tuple(
            self.min_frames,
            self.min_duration,
            self.max_disp_per_frame,
            self.max_displacement,
        )


def _iter_grid() -> list[SearchRule]:
    out: list[SearchRule] = []
    for fc, dur, dpf, md in product(
        GRID_MIN_FRAMES,
        GRID_MIN_DURATION,
        GRID_MAX_DPF,
        GRID_MAX_DISPLACEMENT,
    ):
        out.append(SearchRule(fc, dur, dpf, md))
    return out


def _rank_key(agg: dict[str, Any], min_recall: float) -> tuple:
    """越小越好：优先满足 recall，再最小化误报，再最大化召回。"""
    recall = float(agg.get("recall") or 0.0)
    fa = int(agg.get("false_alarms") or 0)
    missed = int(agg.get("missed") or 0)
    if recall < min_recall:
        return (1, min_recall - recall, fa, missed)
    return (0, fa, missed, -recall)


def _rule_cache_key(record_id: str, sr: SearchRule) -> tuple:
    md = sr.max_displacement if sr.max_displacement is not None else -1.0
    return (record_id, sr.min_frames, sr.min_duration, sr.max_disp_per_frame, md)


def _eval_cached(
    cache: dict[tuple, dict[str, Any]],
    bundles: dict[str, RecordBundle],
    record_id: str,
    sr: SearchRule,
) -> dict[str, Any]:
    key = _rule_cache_key(record_id, sr)
    if key not in cache:
        cache[key] = evaluate_bundle(bundles[record_id], sr.to_combo_rule(), apply_filter=True)
    return cache[key]


def _aggregate_on_ids(
    cache: dict[tuple, dict[str, Any]],
    bundles: dict[str, RecordBundle],
    ids: list[str],
    sr: SearchRule,
) -> dict[str, Any]:
    rows = [_eval_cached(cache, bundles, rid, sr) for rid in ids]
    return aggregate_metrics(rows)


def _search_best_on_ids(
    bundles: dict[str, RecordBundle],
    train_ids: list[str],
    *,
    min_recall: float,
    grid: list[SearchRule],
    cache: dict[tuple, dict[str, Any]],
    log_fn: Callable[[str], None] | None = None,
    log_every: int = 0,
    early_stop_fp: int | None = None,
    fold_label: str = "",
) -> tuple[SearchRule, dict[str, Any], int]:
    """在 train_ids 上网格搜索；返回 (最优规则, 训练集聚合指标, 实际评估组数)。"""
    best_rule = grid[0]
    best_agg: dict[str, Any] = {}
    best_key: tuple | None = None
    evaluated = 0
    total = len(grid)

    for i, sr in enumerate(grid, 1):
        agg = _aggregate_on_ids(cache, bundles, train_ids, sr)
        evaluated += 1
        key = _rank_key(agg, min_recall)
        if best_key is None or key < best_key:
            best_key = key
            best_rule = sr
            best_agg = agg

        if log_fn and log_every > 0 and i % log_every == 0:
            log_fn(
                f"  {fold_label}网格 {i}/{total} 当前最优: {rule_label(best_rule.to_combo_rule())} | "
                f"train FP={best_agg.get('false_alarms', '?')} FN={best_agg.get('missed', '?')} "
                f"recall={float(best_agg.get('recall') or 0):.1%}"
            )

        # 提前终止：已满足召回约束且误报 ≤ 阈值
        if early_stop_fp is not None and best_key is not None and best_key[0] == 0:
            if int(best_agg.get("false_alarms") or 0) <= early_stop_fp:
                if log_fn:
                    log_fn(
                        f"  {fold_label}提前终止 @ {i}/{total}: "
                        f"train FP≤{early_stop_fp} 且 recall≥{min_recall:.0%} → {rule_label(best_rule.to_combo_rule())}"
                    )
                break

    return best_rule, best_agg, evaluated


def _loco_cv(
    bundles: dict[str, RecordBundle],
    record_ids: list[str],
    *,
    min_recall: float,
    grid: list[SearchRule],
    cache: dict[tuple, dict[str, Any]],
    log_fn: Callable[[str], None] | None = None,
    log_every: int = 200,
    early_stop_fp: int | None = None,
    early_stop_beat_combo1: bool = False,
) -> dict[str, Any]:
    fold_rows: list[dict[str, Any]] = []
    chosen_rules: list[SearchRule] = []
    total_evals = 0
    n_folds = len(record_ids)

    for fold_i, test_id in enumerate(record_ids, 1):
        train_ids = [rid for rid in record_ids if rid != test_id]
        test_clip = bundles[test_id].clip
        if log_fn:
            log_fn(f"\n[LOCO {fold_i}/{n_folds}] 留出: {test_clip}（训练 {len(train_ids)} 条）")

        fold_early_stop = early_stop_fp
        if early_stop_beat_combo1:
            combo1_train = aggregate_metrics(
                [evaluate_bundle(bundles[rid], combo1_rule(), apply_filter=True) for rid in train_ids]
            )
            fold_early_stop = int(combo1_train.get("false_alarms") or 0)
            if log_fn:
                log_fn(f"  提前终止阈值=combo1 训练折 FP={fold_early_stop}")

        t0 = time.perf_counter()
        best_rule, train_agg, n_eval = _search_best_on_ids(
            bundles,
            train_ids,
            min_recall=min_recall,
            grid=grid,
            cache=cache,
            log_fn=log_fn,
            log_every=log_every,
            early_stop_fp=fold_early_stop,
            fold_label=f"[折{fold_i}] ",
        )
        total_evals += n_eval
        chosen_rules.append(best_rule)

        test_row = _eval_cached(cache, bundles, test_id, best_rule)
        test_row["train_recall"] = train_agg.get("recall")
        test_row["train_false_alarms"] = train_agg.get("false_alarms")
        test_row["chosen_rule"] = rule_to_dict(best_rule.to_combo_rule())
        test_row["grid_evaluated"] = n_eval
        fold_rows.append(test_row)

        if log_fn:
            elapsed = time.perf_counter() - t0
            log_fn(
                f"[LOCO {fold_i}/{n_folds}] 完成 {elapsed:.1f}s | 选用: {rule_label(best_rule.to_combo_rule())} | "
                f"留出 FP={test_row['false_alarms']} FN={test_row['missed']} recall={test_row['recall']:.1%} | "
                f"本折网格 {n_eval}/{len(grid)}"
            )

    test_agg = aggregate_metrics(fold_rows)
    # 众数参数（各维分别取最常见）
    mdisp_key = Counter(
            r.max_displacement if r.max_displacement is not None else -1.0 for r in chosen_rules
        ).most_common(1)[0][0]
    mode_rule = SearchRule(
        min_frames=Counter(r.min_frames for r in chosen_rules).most_common(1)[0][0],
        min_duration=Counter(r.min_duration for r in chosen_rules).most_common(1)[0][0],
        max_disp_per_frame=Counter(r.max_disp_per_frame for r in chosen_rules).most_common(1)[0][0],
        max_displacement=None if mdisp_key < 0 else mdisp_key,
    )
    return {
        "folds": fold_rows,
        "test_aggregate": test_agg,
        "chosen_rules": [rule_to_dict(r.to_combo_rule()) for r in chosen_rules],
        "mode_rule": rule_to_dict(mode_rule.to_combo_rule()),
        "total_grid_evaluations": total_evals,
        "cache_hits": len(cache),
    }


def _full_grid_top(
    bundles: dict[str, RecordBundle],
    record_ids: list[str],
    *,
    min_recall: float,
    grid: list[SearchRule],
    cache: dict[tuple, dict[str, Any]],
    top_k: int = 15,
    log_fn: Callable[[str], None] | None = None,
    log_every: int = 300,
) -> list[dict[str, Any]]:
    ranked: list[tuple[tuple, SearchRule, dict[str, Any]]] = []
    total = len(grid)
    for i, sr in enumerate(grid, 1):
        agg = _aggregate_on_ids(cache, bundles, record_ids, sr)
        ranked.append((_rank_key(agg, min_recall), sr, agg))
        if log_fn and log_every > 0 and i % log_every == 0:
            log_fn(f"[全量 Top] 扫描 {i}/{total}…")
    ranked.sort(key=lambda x: x[0])
    out: list[dict[str, Any]] = []
    for key, sr, agg in ranked[:top_k]:
        out.append(
            {
                "rank_key": list(key),
                "rule": rule_to_dict(sr.to_combo_rule()),
                "rule_label": rule_label(sr.to_combo_rule()),
                "aggregate": agg,
            }
        )
    return out


def _render_markdown(
    *,
    record_ids: list[str],
    tags: list[str],
    cameras: list[str],
    min_recall: float,
    baseline_agg: dict[str, Any],
    combo1_agg: dict[str, Any],
    loco: dict[str, Any],
    global_best: SearchRule,
    global_best_agg: dict[str, Any],
    mode_agg: dict[str, Any],
    top_insample: list[dict[str, Any]],
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    loco_agg = loco["test_aggregate"]
    mode_rule = rule_from_tuple(
        loco["mode_rule"]["min_frames"],
        loco["mode_rule"]["min_duration"],
        loco["mode_rule"]["max_disp_per_frame"],
        loco["mode_rule"].get("max_displacement"),
    )
    learned_rule = global_best.to_combo_rule()

    lines = [
        "# RTMPose-M 无框强特征：LOCO 学习过滤参数",
        "",
        f"> 生成时间：{now}  ",
        f"> 样本：**{len(record_ids)}** 条（{', '.join(tags)} · 已复核 · 有标真）  ",
        f"> 机位：{', '.join(cameras)}  ",
        "> 特征：`frame_count`, `duration_sec`, `displacement`, `disp/fc`（**无** speed、**无** box 空间）  ",
        "> 基线告警：`alarm_min=5`, `cooldown=6`；段级 AND 规则确认  ",
        f"> LOCO：Leave-One-Clip-Out（28 折），训练折内选参约束 **召回 ≥ {min_recall:.0%}**  ",
        "",
        "## 1. 搜索空间",
        "",
        "| 参数 | 候选值 |",
        "|------|--------|",
        f"| `frame_count` ≥ | {', '.join(str(x) for x in GRID_MIN_FRAMES)} |",
        f"| `duration_sec` ≥ | {', '.join(str(x) for x in GRID_MIN_DURATION)} |",
        f"| `disp/fc` ≤ | {', '.join(str(x) for x in GRID_MAX_DPF)} |",
        f"| `displacement` ≤ | 无上限, {', '.join(str(int(x)) for x in GRID_MAX_DISPLACEMENT if x is not None)} |",
        "",
        f"组合总数：**{len(_iter_grid())}**（全 AND）",
        "",
        "## 2. 系统级对比（168 标真段）",
        "",
        "| 方法 | 误报 FP | 漏报 FN | 召回 | 精确率代理¹ | 告警数 |",
        "|------|---------|---------|------|-------------|--------|",
        f"| min=5 基线 | {baseline_agg['false_alarms']} | {baseline_agg['missed']} | "
        f"{baseline_agg['recall']:.1%} | {baseline_agg['precision_proxy']:.1%} | {baseline_agg['alarm_count']} |",
        f"| combo1 固定 | {combo1_agg['false_alarms']} | {combo1_agg['missed']} | "
        f"{combo1_agg['recall']:.1%} | {combo1_agg['precision_proxy']:.1%} | {combo1_agg['alarm_count']} |",
        f"| **LOCO 测试折平均**² | {loco_agg['false_alarms']} | {loco_agg['missed']} | "
        f"{loco_agg['recall']:.1%} | {loco_agg['precision_proxy']:.1%} | {loco_agg['alarm_count']} |",
        f"| LOCO 众数参数（全量重评） | {mode_agg['false_alarms']} | {mode_agg['missed']} | "
        f"{mode_agg['recall']:.1%} | {mode_agg['precision_proxy']:.1%} | {mode_agg['alarm_count']} |",
        f"| 全量网格最优³ | {global_best_agg['false_alarms']} | {global_best_agg['missed']} | "
        f"{global_best_agg['recall']:.1%} | {global_best_agg['precision_proxy']:.1%} | {global_best_agg['alarm_count']} |",
        "",
        "¹ 精确率代理 = 检出段 / (检出段 + 误报次数)  ",
        "² 每折在其余 27 条上选参，在留出 1 条上评测后汇总（**可信泛化估计**）  ",
        "³ 在全部 28 条上直接选最优（**样本内**，可能偏乐观，仅作参考）  ",
        "",
        "### LOCO 众数参数（推荐参考）",
        "",
        "```text",
        rule_label(mode_rule),
        "```",
        "",
        "### 全量网格最优参数（样本内）",
        "",
        "```text",
        rule_label(learned_rule),
        "```",
        "",
        "## 3. 相对 combo1",
        "",
    ]

    fa_delta = int(loco_agg["false_alarms"]) - int(combo1_agg["false_alarms"])
    miss_delta = int(loco_agg["missed"]) - int(combo1_agg["missed"])
    lines.append(
        f"- LOCO 平均误报 vs combo1：{combo1_agg['false_alarms']} → {loco_agg['false_alarms']}（{fa_delta:+d}）"
    )
    lines.append(
        f"- LOCO 平均漏报 vs combo1：{combo1_agg['missed']} → {loco_agg['missed']}（{miss_delta:+d}）"
    )
    lines.append(
        f"- LOCO 平均召回 vs combo1：{combo1_agg['recall']:.1%} → {loco_agg['recall']:.1%}"
    )

    lines.extend(["", "## 4. 全量网格 Top 规则（样本内）", ""])
    lines.append("| # | 规则 | FP | FN | 召回 | 精确率代理 |")
    lines.append("|---|------|----|----|------|------------|")
    for i, row in enumerate(top_insample[:10], 1):
        agg = row["aggregate"]
        lines.append(
            f"| {i} | `{row['rule_label']}` | {agg['false_alarms']} | {agg['missed']} | "
            f"{agg['recall']:.1%} | {agg['precision_proxy']:.1%} |"
        )

    lines.extend(["", "## 5. LOCO 单折明细（留出 clip）", ""])
    lines.append("| clip | 机位 | 留出折 FP | FN | 召回 | 训练折选用规则 |")
    lines.append("|------|------|-----------|-----|------|----------------|")
    for fold in loco["folds"]:
        cr = fold.get("chosen_rule") or {}
        lbl = rule_label(
            rule_from_tuple(
                cr.get("min_frames", 4),
                cr.get("min_duration", 0.2),
                cr.get("max_disp_per_frame", 2.5),
                cr.get("max_displacement"),
            )
        )
        lines.append(
            f"| `{fold.get('clip', '')}` | {fold.get('camera_slug', '')} | "
            f"{fold.get('false_alarms', 0)} | {fold.get('missed', 0)} | "
            f"{float(fold.get('recall') or 0):.0%} | `{lbl}` |"
        )

    lines.extend(
        [
            "",
            "## 6. 方法说明",
            "",
            "- 脚本：`scripts/train/learn_no_box_filter.py`",
            "- 核心：`scripts/train/segment_filter_core.py`",
            "- 选参目标：训练折上 **召回 ≥ 约束** 时 **误报最少**（同召回则漏报更少）",
            "- 下一阶段：在相同流程上增加 box 内相对空间特征（`docs/train/` 有框版）",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="无框强特征 LOCO 网格学习")
    parser.add_argument("--tier", default=DEFAULT_TIER)
    parser.add_argument("--tags", default=",".join(DEFAULT_TAGS))
    parser.add_argument("--cameras", default=",".join(DEFAULT_CAMERAS))
    parser.add_argument("--alarm-min", type=int, default=5)
    parser.add_argument("--cooldown", type=int, default=6)
    parser.add_argument("--min-recall", type=float, default=0.85, help="训练折选参最低召回")
    parser.add_argument("--log-every", type=int, default=200, help="每评估 N 组网格参数打印一次进度")
    parser.add_argument(
        "--early-stop-fp",
        type=int,
        default=None,
        help="训练折已满足召回约束且误报≤此值时提前结束本折网格（默认不提前终止）",
    )
    parser.add_argument(
        "--early-stop-beat-combo1",
        action="store_true",
        help="等同每折用 combo1 在训练折上的误报数作为 --early-stop-fp",
    )
    parser.add_argument(
        "--out",
        default=str(ROOT / "docs" / "train" / "learned-filter-no-box-rtmpose-m.md"),
    )
    parser.add_argument("--json-out", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    resolve_config_path(None)
    tags = _parse_tags(args.tags)
    cameras = _parse_csv_list(args.cameras)
    record_ids = _collect_record_ids(
        tier=args.tier,
        cameras=set(cameras),
        tags=tags,
        review_status=DEFAULT_REVIEW_STATUS,
        has_verified=True,
    )
    if not record_ids:
        print("未找到匹配记录", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"记录 {len(record_ids)} 条，网格 {len(_iter_grid())} 组")
        return 0

    print("加载记录 bundle…")
    bundles: dict[str, RecordBundle] = {}
    for rid in record_ids:
        b = load_record_bundle(
            rid, alarm_min=args.alarm_min, alarm_cooldown=args.cooldown, tier=args.tier
        )
        if b is None:
            print(f"跳过（无法加载）: {rid}", file=sys.stderr)
            continue
        bundles[rid] = b
    record_ids = [rid for rid in record_ids if rid in bundles]
    if not record_ids:
        print("无有效记录", file=sys.stderr)
        return 1

    grid = _iter_grid()
    cache: dict[tuple, dict[str, Any]] = {}
    log = print
    early_stop = args.early_stop_fp
    if args.early_stop_beat_combo1 and early_stop is not None:
        print("警告: 同时指定 --early-stop-fp 与 --early-stop-beat-combo1，以 --early-stop-fp 为准", file=sys.stderr)

    print(
        f"已加载 {len(record_ids)} 条 | 网格 {len(grid)} 组 | "
        f"召回约束≥{args.min_recall:.0%} | log_every={args.log_every}"
    )
    if early_stop is not None:
        print(f"提前终止: 训练折 FP≤{early_stop} 且 recall≥{args.min_recall:.0%}")
    elif args.early_stop_beat_combo1:
        print("提前终止: 每折训练集上优于 combo1 误报数且满足召回")

    print("\n[1/4] 基线 & combo1 …")
    t0 = time.perf_counter()
    baseline_rows = [evaluate_bundle(bundles[rid], None, apply_filter=False) for rid in record_ids]
    combo1_rows = [evaluate_bundle(bundles[rid], combo1_rule(), apply_filter=True) for rid in record_ids]
    baseline_agg = aggregate_metrics(baseline_rows)
    combo1_agg = aggregate_metrics(combo1_rows)
    print(
        f"  基线 FP={baseline_agg['false_alarms']} FN={baseline_agg['missed']} "
        f"recall={baseline_agg['recall']:.1%}"
    )
    print(
        f"  combo1 FP={combo1_agg['false_alarms']} FN={combo1_agg['missed']} "
        f"recall={combo1_agg['recall']:.1%} | {time.perf_counter() - t0:.1f}s"
    )

    print(f"\n[2/4] LOCO 交叉验证（{len(record_ids)} 折 × 最多 {len(grid)} 组）…")
    t0 = time.perf_counter()
    loco = _loco_cv(
        bundles,
        record_ids,
        min_recall=args.min_recall,
        grid=grid,
        cache=cache,
        log_fn=log,
        log_every=args.log_every,
        early_stop_fp=early_stop if not args.early_stop_beat_combo1 else None,
        early_stop_beat_combo1=args.early_stop_beat_combo1,
    )
    print(
        f"LOCO 完成 {time.perf_counter() - t0:.1f}s | "
        f"网格评估 {loco['total_grid_evaluations']} 次 | 缓存 {loco['cache_hits']} 条"
    )

    mode_r = loco["mode_rule"]
    mode_rule = rule_from_tuple(
        mode_r["min_frames"],
        mode_r["min_duration"],
        mode_r["max_disp_per_frame"],
        mode_r.get("max_displacement"),
    )

    print("\n[3/4] 全量最优规则（28 条训练）…")
    t0 = time.perf_counter()
    global_best, global_train_agg, global_n_eval = _search_best_on_ids(
        bundles,
        record_ids,
        min_recall=args.min_recall,
        grid=grid,
        cache=cache,
        log_fn=log,
        log_every=args.log_every,
        early_stop_fp=early_stop,
        fold_label="[全量] ",
    )
    global_rows = [
        evaluate_bundle(bundles[rid], global_best.to_combo_rule(), apply_filter=True) for rid in record_ids
    ]
    global_best_agg = aggregate_metrics(global_rows)
    print(
        f"  选用 {rule_label(global_best.to_combo_rule())} | 网格 {global_n_eval}/{len(grid)} | "
        f"全量 FP={global_best_agg['false_alarms']} FN={global_best_agg['missed']} "
        f"recall={global_best_agg['recall']:.1%} | {time.perf_counter() - t0:.1f}s"
    )

    mode_rows = [evaluate_bundle(bundles[rid], mode_rule, apply_filter=True) for rid in record_ids]
    mode_agg = aggregate_metrics(mode_rows)

    print("\n[4/4] 全量网格 Top-15 排名…")
    t0 = time.perf_counter()
    top_insample = _full_grid_top(
        bundles,
        record_ids,
        min_recall=args.min_recall,
        grid=grid,
        cache=cache,
        top_k=15,
        log_fn=log,
        log_every=max(args.log_every, 300),
    )
    print(f"  Top 规则已排序 | {time.perf_counter() - t0:.1f}s")

    print(
        f"LOCO 测试平均: FP={loco['test_aggregate']['false_alarms']} "
        f"FN={loco['test_aggregate']['missed']} "
        f"recall={loco['test_aggregate']['recall']:.1%}"
    )
    print(f"LOCO 众数规则: {rule_label(mode_rule)}")
    print(f"全量最优: {rule_label(global_best.to_combo_rule())}")

    md = _render_markdown(
        record_ids=record_ids,
        tags=tags,
        cameras=cameras,
        min_recall=args.min_recall,
        baseline_agg=baseline_agg,
        combo1_agg=combo1_agg,
        loco=loco,
        global_best=global_best,
        global_best_agg=global_best_agg,
        mode_agg=mode_agg,
        top_insample=top_insample,
    )
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    print(f"\n报告: {out_path}")

    json_path = resolve_docs_json(out_path, args.json_out)
    DOCS_JSON_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "min_recall_constraint": args.min_recall,
        "record_ids": record_ids,
        "grid_size": len(grid),
        "baseline": baseline_agg,
        "combo1": combo1_agg,
        "loco": loco,
        "mode_rule_full_eval": mode_agg,
        "global_best_rule": rule_to_dict(global_best.to_combo_rule()),
        "global_best_full_eval": global_best_agg,
        "top_insample": top_insample,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"JSON: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
