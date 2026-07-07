# 文档索引

本目录存放评估报告、实验记录与项目说明。Markdown 报告与 `docs/json/` 中同名 JSON 成对存放（由 `scripts/data/report_paths.py` 约定）。

## 目录结构

| 目录 | 内容 |
|------|------|
| [`hand-probe/`](hand-probe/) | 手腕 vs 手臂延长探针 A/B 实验（`compare_hand_probe_accuracy.py`） |
| [`alarm-min/`](alarm-min/) | `alarm_min` 阈值扫描与准确率对比 |
| [`segment-filter/`](segment-filter/) | 段级过滤 combo 实验（`evaluate_combo1_segment_filter.py`） |
| [`features/`](features/) | 手腕特征区分度分析 |
| [`guides/`](guides/) | 前端说明、数据迁移等操作文档 |
| [`daily/`](daily/) | 工作日报 |
| [`json/`](json/) | 各报告结构化数据（机器可读） |
| [`view/`](view/) | 图表 SVG（如 alarm-min 散点图） |

## 常用脚本 → 输出路径

```bash
# 手臂延长探针 A/B（默认 α=0.4，28 条优质样本）
python scripts/data/compare_hand_probe_accuracy.py \
  --out docs/hand-probe/hand-probe-ab-prod-params-rtmpose-m.md

# 全量 α 扫描示例
python scripts/data/compare_hand_probe_accuracy.py \
  --extension-ratio 0.1 \
  --out docs/hand-probe/hand-probe-ab-alpha01-rtmpose-m.md

# 手腕特征区分度
python scripts/data/analyze_wrist_feature_discrimination.py \
  --out docs/features/wrist-features-discrimination-rtmpose-m.md

# alarm_min 散点（SVG → docs/view/，MD → docs/alarm-min/）
python scripts/data/plot_alarm_min_disp_fc_scatter.py \
  --out docs/alarm-min/alarm-min5-disp-fc-scatter-rtmpose-m

# 段过滤 combo（默认 docs/segment-filter/comboN-...）
python scripts/data/evaluate_combo1_segment_filter.py --combo 2
```

## 实验 cohort（优质 28 条）约定

与 [`scripts/data/eval_dataset.py`](../scripts/data/eval_dataset.py) 一致：

- 模型 tier：`rtmpose-m`
- 标签：`单人,无遮挡`（AND）
- 复核：已复核 + 有标真（`verified_true`）
- 机位：8 个（1-1-1, 1-2-1, 2-2-2, 2-3-1, 2-4-1, 2-5-1, 2-6-1, 2-7-2）
- 现场模拟：`pose_frame_interval=2`，`alarm_min=3`，`alarm_cooldown=0`

## 标注版本说明

2026-07-04 前后存在两版标注 / 标真：

| 版本 | 全量标真段合计 | 代表报告 |
|------|----------------|----------|
| **修订前**（2026-07-03） | 168 | `hand-probe/hand-probe-ab-alpha02-rtmpose-m.md` 等 |
| **修订后**（2026-07-04 起） | 161 | `hand-probe/hand-probe-ab-alpha01-rtmpose-m.md`；1-1-1 子集见 [`hand-probe/1-1-1/`](hand-probe/1-1-1/) |

主要差异：`clip_0013` 标真段 61 → 54（review 侧 `verified_true` 变更，非货位 JSON）。跨版本对比时以漏报/误报绝对数为主，recall 分母不同。

## 相关链接

- 脚本索引：[`scripts/README.md`](../scripts/README.md)
- 手臂延长实验详情：[`hand-probe/README.md`](hand-probe/README.md)
- 前端与复核：[`guides/frontend-guide.md`](guides/frontend-guide.md)
