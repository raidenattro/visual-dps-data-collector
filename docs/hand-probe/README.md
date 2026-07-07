# 手臂延长探针 A/B 实验

对比 **wrist（手腕）** baseline 与 **hand_extended（手臂延长探针）** 在同一 skeleton、reflection 标注下的准确率。内存重算，不写 timeline。

脚本：[`scripts/data/compare_hand_probe_accuracy.py`](../../scripts/data/compare_hand_probe_accuracy.py)

## 全量 cohort（28 条）

现场参数：`interval=2`，`alarm_min=3`，`cooldown=0`；标签单人+无遮挡；已复核+有标真。

| 报告 | α | 生成时间 (UTC) | 标注版本 | 标真段合计 | 手腕 recall | 延长 recall | 延长 FP Δ |
|------|---|----------------|----------|------------|-------------|-------------|-----------|
| [alpha01](hand-probe-ab-alpha01-rtmpose-m.md) | 0.1 | 2026-07-07 | **修订后** | 161 | 91.30% | 91.30% | +12 |
| [alpha02](hand-probe-ab-alpha02-rtmpose-m.md) | 0.2 | 2026-07-03 | 修订前 | 168 | 89.29% | 88.69% | +45 |
| [alpha03](hand-probe-ab-alpha03-rtmpose-m.md) | 0.3 | 2026-07-03 | 修订前 | 168 | 89.29% | 85.71% | +146 |
| [prod-params](hand-probe-ab-prod-params-rtmpose-m.md) | 0.4 | 2026-07-03 | 修订前 | 168 | 89.29% | 82.74% | +259 |

JSON：`docs/json/hand-probe-ab-{alpha01,alpha02,alpha03,prod-params}-rtmpose-m.json`

> **待办**：在修订后标注下重跑 α=0.2/0.3/0.4，可与 alpha01 同期对比。

## 1-1-1 子集（2 条）

机位 1-1-1 仅 `clip_0013`、`clip_0025`；参数同上。

| 报告 | α |
|------|---|
| [alpha020](1-1-1/hand-probe-ab-1-1-1-rtmpose-m-alpha020.md) | 0.2 |
| [alpha030](1-1-1/hand-probe-ab-1-1-1-rtmpose-m-alpha030.md) | 0.3 |
| [alpha040](1-1-1/hand-probe-ab-1-1-1-rtmpose-m-alpha040.md) | 0.4 |

标注修订前后对比专文：[1-1-1/annotation-revision-comparison.md](1-1-1/annotation-revision-comparison.md)

## 重跑命令

```bash
# 全量 8 机位
python scripts/data/compare_hand_probe_accuracy.py \
  --tier rtmpose-m --tags "单人,无遮挡" \
  --cameras "1-1-1,1-2-1,2-2-2,2-3-1,2-4-1,2-5-1,2-6-1,2-7-2" \
  --pose-frame-interval 2 --alarm-min 3 --cooldown 0 \
  --extension-ratio 0.2 \
  --out docs/hand-probe/hand-probe-ab-alpha02-rtmpose-m.md

# 仅 1-1-1 两条
python scripts/data/compare_hand_probe_accuracy.py \
  --cameras 1-1-1 --extension-ratio 0.2 \
  --out docs/hand-probe/1-1-1/hand-probe-ab-1-1-1-rtmpose-m-alpha020.md
```

## 指标口径

- **漏报 FN**：标真段级，段内无匹配 `alarm_collisions` 计 1
- **误报 FP**：token 级，每个未被任一段 GT 覆盖的 `(frame, box_token)` 计 1
- **召回 recall**：TP / (TP + FN)，按段
- 货位来源：reflection `1-1组-1` → **81.json + 82.json**（优先 `localdata/json/rtmpose-m/annotations/`）
