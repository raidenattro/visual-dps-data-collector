#!/usr/bin/env python3
"""评估 Combo1 + 躯干运动速度组合过滤效果。

对比三组告警：
  1. 原始告警（alarm_min=5，无段过滤）
  2. Combo1 段过滤（fc/dur/disp）
  3. Combo1 + 躯干速度上限（拒绝走过误报）

用法（项目根目录）:
  python scripts/data/evaluate_combo1_motion_filter.py
  python scripts/data/evaluate_combo1_motion_filter.py --max-torso-speed-p50 50
  python scripts/data/evaluate_combo1_motion_filter.py --skip-extract
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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
from scripts.data.evaluate_combo1_segment_filter import (
    COMBO1_MAX_DISP_PER_FRAME,
    COMBO1_MIN_DURATION,
    COMBO1_MIN_FRAMES,
    MotionComboRule,
    _evaluate_record,
)
from scripts.train.segment_filter_core import aggregate_metrics, combo1_motion_rule, combo1_rule

from api.record_service import locate_record_by_id
from api.skeleton_features_service import extract_skeleton_features_for_record


def _render_markdown(
    *,
    tier: str,
    tags: list[str],
    cameras: list[str],
    raw_rows: list[dict[str, Any]],
    combo1_rows: list[dict[str, Any]],
    motion_rows: list[dict[str, Any]],
    motion_rule: MotionComboRule,
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    raw = aggregate_metrics([r for r in raw_rows if r.get("status") == "ok"])
    c1 = aggregate_metrics([r for r in combo1_rows if r.get("status") == "ok"])
    mot = aggregate_metrics([r for r in motion_rows if r.get("status") == "ok"])

    fa_raw = int(raw.get("false_alarms") or 0)
    fa_c1 = int(c1.get("false_alarms") or 0)
    fa_mot = int(mot.get("false_alarms") or 0)
    tp_raw = int(raw.get("detected") or 0)
    tp_c1 = int(c1.get("detected") or 0)
    tp_mot = int(mot.get("detected") or 0)

    def fp_drop(base: int, cur: int) -> str:
        if base <= 0:
            return "—"
        return f"{round((base - cur) / base * 100, 1)}%"

    def tp_loss(base: int, cur: int) -> str:
        if base <= 0:
            return "—"
        return f"{round((base - cur) / base * 100, 1)}%"

    lines = [
        "# Combo1 + 躯干速度组合过滤评估",
        "",
        f"> 生成时间：{now}  ",
        f"> 模型层：`{tier}`  ",
        f"> 记录标签：{', '.join(tags)}  ",
        f"> 机位：{', '.join(cameras)}  ",
        f"> Combo1：fc≥{COMBO1_MIN_FRAMES}, dur≥{COMBO1_MIN_DURATION}, dpf≤{COMBO1_MAX_DISP_PER_FRAME}  ",
        f"> 运动规则：{motion_rule.label}  ",
        "",
        "## 汇总对比",
        "",
        "| 指标 | 原始告警 | Combo1 | Combo1+运动 |",
        "|------|----------|--------|---------------|",
        f"| 记录数 | {raw.get('records', 0)} | {c1.get('records', 0)} | {mot.get('records', 0)} |",
        f"| 标真段数 | {raw.get('gt_segments', 0)} | {c1.get('gt_segments', 0)} | {mot.get('gt_segments', 0)} |",
        f"| 检出 TP | {tp_raw} | {tp_c1} | {tp_mot} |",
        f"| 误报 FP | {fa_raw} | {fa_c1} | {fa_mot} |",
        f"| 召回率 | {raw.get('recall', '—')} | {c1.get('recall', '—')} | {mot.get('recall', '—')} |",
        f"| 精确率代理 | {raw.get('precision_proxy', '—')} | {c1.get('precision_proxy', '—')} | {mot.get('precision_proxy', '—')} |",
        "",
        "## 相对 Combo1 的变化",
        "",
        f"- 误报 FP 下降：{fp_drop(fa_c1, fa_mot)}（Combo1 {fa_c1} → Combo1+运动 {fa_mot}）",
        f"- 标真 TP 损失：{tp_loss(tp_c1, tp_mot)}（Combo1 {tp_c1} → Combo1+运动 {tp_mot}）",
        "",
        "## 说明",
        "",
        "- 运动段数据来自 `skeleton_motion_segments.parquet`（需先运行 extract_skeleton_features）。",
        "- `torso_speed_p50` 超过上限的碰撞段将被拒绝，用于过滤人走过货位误报。",
        "- 段内无运动特征时跳过运动过滤（保留 Combo1 结果）。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Combo1 + 躯干速度组合过滤评估")
    parser.add_argument("--tier", default=DEFAULT_TIER)
    parser.add_argument("--tags", default=",".join(DEFAULT_TAGS))
    parser.add_argument("--cameras", default=",".join(DEFAULT_CAMERAS))
    parser.add_argument("--review-status", default=DEFAULT_REVIEW_STATUS)
    parser.add_argument("--alarm-min", type=int, default=5)
    parser.add_argument("--alarm-cooldown", type=int, default=6)
    parser.add_argument("--max-torso-speed-p50", type=float, default=50.0)
    parser.add_argument("--max-lower-mean-speed-p50", type=float, default=0.0, help="0=不启用")
    parser.add_argument("--skip-extract", action="store_true")
    parser.add_argument(
        "--out",
        default=str(ROOT / "docs" / "combo1-motion-filter-rtmpose-m.md"),
    )
    args = parser.parse_args()

    resolve_config_path(None)
    tags = _parse_tags(args.tags)
    cameras = _parse_csv_list(args.cameras)
    record_ids = _collect_record_ids(
        tier=args.tier,
        cameras=set(cameras),
        tags=tags,
        review_status=args.review_status or None,
        has_verified=True,
    )
    if not record_ids:
        print("未找到符合条件记录")
        return 1

    lower_cap = float(args.max_lower_mean_speed_p50) if float(args.max_lower_mean_speed_p50) > 0 else None
    motion_rule = combo1_motion_rule(
        max_torso_speed_p50=float(args.max_torso_speed_p50),
        max_lower_mean_speed_p50=lower_cap,
    )
    combo1 = combo1_rule()

    raw_rows: list[dict[str, Any]] = []
    combo1_rows: list[dict[str, Any]] = []
    motion_rows: list[dict[str, Any]] = []

    for rid in record_ids:
        if not args.skip_extract:
            loc = locate_record_by_id(rid)
            if loc:
                try:
                    extract_skeleton_features_for_record(loc, skip_if_exists=True)
                except Exception as exc:
                    print(f"{rid}: 特征提取失败 {exc}")

        raw_rows.append(
            _evaluate_record(
                rid,
                alarm_min=args.alarm_min,
                alarm_cooldown=args.alarm_cooldown,
                apply_combo=False,
            )
        )
        combo1_rows.append(
            _evaluate_record(
                rid,
                alarm_min=args.alarm_min,
                alarm_cooldown=args.alarm_cooldown,
                combo_rule=combo1,
                apply_combo=True,
            )
        )
        motion_rows.append(
            _evaluate_record(
                rid,
                alarm_min=args.alarm_min,
                alarm_cooldown=args.alarm_cooldown,
                combo_rule=motion_rule,
                apply_combo=True,
            )
        )
        mr = motion_rows[-1]
        if mr.get("status") == "ok":
            print(
                f"{rid}: raw_fp={raw_rows[-1].get('false_alarms')} "
                f"combo1_fp={combo1_rows[-1].get('false_alarms')} "
                f"motion_fp={mr.get('false_alarms')} "
                f"tp={mr.get('detected')}"
            )

    md = _render_markdown(
        tier=args.tier,
        tags=tags,
        cameras=cameras,
        raw_rows=raw_rows,
        combo1_rows=combo1_rows,
        motion_rows=motion_rows,
        motion_rule=motion_rule,
    )
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    print(f"\n报告已写入: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
