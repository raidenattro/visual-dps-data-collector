# 前置过滤消融：ankle@max80 + triple90 单条件拆分

> 生成时间：2026-07-13  
> 分支：`try/speed-filter`  
> 样本：28 条（单人 · 无遮挡 · 已复核 · 有标真）  
> 对照：`localdata/export/rule-speed-prefilter-ankle-max80-triple90-prod-test`  
> eval_id（对照）：`20260713_062811_2cad403d`

## 1. 实验目的

triple90 由三条手部角度 **AND** 组成。用户怀疑条件 2（肘角）、条件 3（腕抬升）与条件 1（臂躯干角）信息重叠、单独价值有限。本实验在 **相同速度门控** `ankle_max_speed > 80` 下，分别只保留一条豁免条件，与完整 triple90 对比 FP/FN。

**共同参数**：无站立约束（对齐 `triple90-prod-test`）、`pose_frame_interval=2`、`alarm_min=3`、`cooldown=0`。

| 编号 | 特征 | 阈值 | 几何含义 |
|------|------|------|----------|
| 1 | `arm_torso_angle_max` | ≥90° | 上臂相对躯干张开（∠髋心-肩-肘） |
| 2 | `elbow_angle_mean` | ≥150° | 左右肘伸直程度（∠肩-肘-腕均值） |
| 3 | `wrist_elevation_angle_max` | ≥60° | 肩→腕相对向下方向的抬升角 |

门控（单条件版）：

```
block = (ankle_max_speed > 80) AND NOT(该单条件满足)
```

完整 triple90：

```
block = (ankle_max_speed > 80) AND NOT(条件1 AND 条件2 AND 条件3)
```

## 2. 导出目录与 eval_id

| 实验 | 规则 | 目录 | eval_id |
|------|------|------|---------|
| 对照 | ankle@80 + triple90 | `rule-speed-prefilter-ankle-max80-triple90-prod-test` | `20260713_062811_2cad403d` |
| A | ankle@80 + **仅条件1** | `rule-speed-prefilter-ankle-max80-armtorso90-prod-test` | `20260713_062808_b6f38d9c` |
| B | ankle@80 + **仅条件2** | `rule-speed-prefilter-ankle-max80-elbow150-prod-test` | `20260713_062809_01f34f76` |
| C | ankle@80 + **仅条件3** | `rule-speed-prefilter-ankle-max80-wristelev60-prod-test` | `20260713_062810_472b46eb` |

## 3. 系统级准确率（28-clip）

| 指标 | triple90（对照） | 仅 arm_torso≥90 | 仅 elbow≥150 | 仅 wrist_elev≥60 |
|------|------------------|-----------------|--------------|------------------|
| 标真段 | 156 | 156 | 156 | 156 |
| TP | 145 | 145 | **146** | 145 |
| FN | 11 | 11 | **10** | 11 |
| FP | **290** | 317 | 343 | 314 |
| 召回率 | 92.95% | 92.95% | **93.59%** | 92.95% |
| 精确率代理¹ | **33.33%** | 31.41% | 29.84% | 31.61% |

¹ 精确率代理 = TP / (TP + FP)。

### 相对 triple90 的净变化

| 实验 | ΔFP | ΔFN | Δ召回 | 评价 |
|------|-----|-----|-------|------|
| 仅 arm_torso≥90 | **+27** | 0 | — | FP 变差，无 FN 收益 |
| 仅 elbow≥150 | **+53** | **−1** | +0.64% | 挽回 1 段 FN（2-7-2 clip_0013），但 FP 增幅最大 |
| 仅 wrist_elev≥60 | **+24** | 0 | — | FP 略差于 triple90，无 FN 收益 |

**结论**：在 28-clip 上，**完整 triple90 AND 的 FP 最低（290）**；拆成任一单条件均不能同时保持或改善 FP。条件 2 单独使用虽少 1 段漏报，但误报净增 53 次，得不偿失。条件 1 与条件 3 单独使用效果接近，均弱于 triple90。

## 4. 与 triple90 对比明细

自动对比报告（experiment vs baseline=triple90）：

| 实验 | 对比 MD |
|------|---------|
| arm_torso90 | `localdata/export/compare_rule-speed-prefilter-ankle-max80-armtorso90-prod-test_vs_rule-speed-prefilter-ankle-max80-triple90-prod-test.md` |
| elbow150 | `localdata/export/compare_rule-speed-prefilter-ankle-max80-elbow150-prod-test_vs_rule-speed-prefilter-ankle-max80-triple90-prod-test.md` |
| wrist_elev60 | `localdata/export/compare_rule-speed-prefilter-ankle-max80-wristelev60-prod-test_vs_rule-speed-prefilter-ankle-max80-triple90-prod-test.md` |

各目录另有 `accuracy_report.md` / `accuracy_report.json` 及 `*误报漏报情况.md`。

### 4.1 仅 elbow≥150 减少的漏报（1 段）

| clip | 机位 | 帧区间 | 标真货框 |
|------|------|--------|----------|
| `clip_0013_start_00-42-48` | 2-7-2 | 159–169 | Box_3105 |

说明：该段在 triple90 下因三条角度同时满足而被豁免 block；仅 elbow 时豁免更松，碰撞链得以恢复。但同 clip 新增 7 次误报（帧 143, 155, 157, 181, 183, 185, 191），净效果仍为 FP 恶化。

### 4.2 单条件相对 triple90 无「减少误报」

三组实验相对 triple90 **均无消除误报**（对比报告中「减少的误报」均为空），仅新增误报。说明 triple90 的 AND 组合在抑制走动类误报上具有 **叠加增益**，非任一单条件可替代。

## 5. 复现命令

```bash
# 单条件导出（三选一）
python scripts/data/export_prefilter_triple_angle_upload.py \
  --speed-feature ankle_max_speed --speed-threshold 80 \
  --single-angle-exempt arm_torso   # 或 elbow / wrist_elev

# 评估
python scripts/data/evaluate_inference_upload.py \
  --dir localdata/export/rule-speed-prefilter-ankle-max80-armtorso90-prod-test --in-place

# 与 triple90 对比
python scripts/data/compare_export_false_alarms.py \
  --baseline localdata/export/rule-speed-prefilter-ankle-max80-triple90-prod-test \
  --experiment localdata/export/rule-speed-prefilter-ankle-max80-armtorso90-prod-test
```

## 6. 建议

| 场景 | 建议 |
|------|------|
| 追求最低 FP（无站立约束） | 保留 **ankle@80 + triple90 AND** |
| 简化规则、可接受 FP 上升 | 单条件均不推荐；此前 **ankle@80 + torso160（无 triple）** 在另一实验线更优 |
| 条件 2/3 是否冗余 | 与条件 1 **不等价**：单独用时 FP 更差；AND 组合才有净收益 |
| 条件 2 单独 | 仅在对 2-7-2 FN 极敏感且可承受 +53 FP 时考虑，一般不采用 |

## 7. 双角度子集（cond12 / cond23）

在单条件消融基础上，进一步测试去掉一条后的双条件 AND：

| 实验 | 规则 | 目录 | eval_id |
|------|------|------|---------|
| cond12 | 条件1 AND 条件2（无腕抬升） | `rule-speed-prefilter-ankle-max80-armtorso90-elbow150-prod-test` | `20260713_063441_d8ff5cfb` |
| cond23 | 条件2 AND 条件3（无臂躯干） | `rule-speed-prefilter-ankle-max80-elbow150-wristelev60-prod-test` | `20260713_063442_73320113` |

| 指标 | triple90 | cond12 | cond23 |
|------|----------|--------|--------|
| FP | **290** | 298 (+8) | 291 (+1) |
| FN | 11 | 11 | 11 |
| 召回 | 92.95% | 92.95% | 92.95% |

- **cond12**：相对 triple90 多 8 次误报，无 FN 变化；说明条件3（腕抬升）对压低 FP 有可见贡献。
- **cond23**：仅多 1 次误报（`2-5-1 clip_0020` 帧 831），与 triple90 几乎等价；**条件1 在 28-clip 上边际很小**，可考虑用 cond23 简化规则（代价 +1 FP）。

```bash
python scripts/data/export_prefilter_triple_angle_upload.py \
  --speed-feature ankle_max_speed --speed-threshold 80 \
  --pair-angle-exempt cond12   # 或 cond23
```

对比 MD：

- `compare_rule-speed-prefilter-ankle-max80-armtorso90-elbow150-prod-test_vs_rule-speed-prefilter-ankle-max80-triple90-prod-test.md`
- `compare_rule-speed-prefilter-ankle-max80-elbow150-wristelev60-prod-test_vs_rule-speed-prefilter-ankle-max80-triple90-prod-test.md`
