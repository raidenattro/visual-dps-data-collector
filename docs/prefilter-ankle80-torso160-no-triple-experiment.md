# 前置过滤实验：ankle_max@80 + 肩髋踝站立豁免（无手部 triple90）

> 生成时间：2026-07-13  
> 分支：`try/speed-filter`  
> 样本：28 条（单人 · 无遮挡 · 已复核 · 有标真）  
> 导出目录：`localdata/export/rule-speed-prefilter-ankle-max80-torso160-prod-test`  
> eval_id：`20260713_055117_e1fd6851`

## 1. 规则说明

| 条件 | 参数 |
|------|------|
| 速度门控 | `ankle_max_speed > 80` → 高速时**可能**跳过手腕碰撞检测 |
| 站立约束 | 仅当 `torso_leg_angle_mean ≥ 160`（肩髋踝近似站立）时才允许 block；**蹲姿不 block** |
| 手部角度豁免 | **已删除**（不再要求 arm_torso≥90 AND elbow≥150 AND wrist_elev≥60） |

门控逻辑（伪代码）：

```
block = (ankle_max_speed > 80) AND (torso_leg_angle_mean >= 160)
```

对齐参数：`pose_frame_interval=2`，`alarm_min=3`，`cooldown=0`。

### 与上一版（ankle@80 + triple90 + torso160）差异

| 项 | triple90 + torso160 | **本实验（仅 torso160）** |
|----|----------------------|---------------------------|
| 手部 triple AND 豁免 | 有 | **无** |
| 蹲姿豁免 | 有 | 有 |
| 规则复杂度 | 高 | **低** |

## 2. 系统级准确率

| 指标 | baseline-local | triple90+torso160 | **本实验** |
|------|----------------|-------------------|------------|
| 标真段 | 156 | 156 | 156 |
| TP | 147 | 146 | **146** |
| FN | 9 | 10 | **10** |
| FP | 429 | 311 | **297** |
| 召回率 | 94.23% | 93.59% | **93.59%** |
| 精确率代理¹ | 25.52% | 31.94% | **32.96%** |

¹ 精确率代理 = TP / (TP + FP)。

### 解读

- 相对 **baseline-local**：误报 **429 → 297（−132）**，零新增误报；漏报 **9 → 10（+1 段）**。
- 相对 **triple90+torso160**：误报 **311 → 297（−14）**，漏报 **不变（10 段）**，召回 **不变**。
- **删去手部 triple90 豁免后，FP 进一步下降且未增加 FN**，规则更简单，效果优于带 triple90 的版本。

## 3. 相对 baseline 新增漏报（仅 1 段）

| clip | 机位 | 帧区间 | 标真货框 | 说明 |
|------|------|--------|----------|------|
| `clip_0013_start_00-42-48` | 2-7-2 | 159–169 | Box_3105 | 蹲姿取货、手腕点置信度低导致碰撞链断裂（与 triple90 版相同 FN） |

## 4. 相对 triple90+torso160 消除的误报（14 次，无新增）

主要来自走动/快速经过场景，手部 triple 豁免曾放行、本实验正确 block：

| clip | 机位 | 消除帧（示例） |
|------|------|----------------|
| `clip_0006_start_00-41-26` | 2-2-2 | 457, 459 |
| `clip_0008_start_00-30-49` | 2-5-1 | 5575, 5577 |
| `clip_0013_start_00-11-22` | 1-1-1 | 3979 |
| `clip_0020_start_00-48-44` | 2-2-2 | 65, 67, 75 |
| `clip_0028_start_00-33-12` | 1-2-1 | 543, 545, 551, 553, 555, 557 |

## 5. 复现命令

```bash
# 导出（项目根目录）
python scripts/data/export_prefilter_triple_angle_upload.py \
  --speed-feature ankle_max_speed --speed-threshold 80 \
  --stance-feature torso_leg_angle_mean --stance-threshold 160 \
  --no-triple-exempt \
  --output-dir localdata/export/rule-speed-prefilter-ankle-max80-torso160-prod-test

# 评估
python scripts/data/evaluate_inference_upload.py \
  --dir localdata/export/rule-speed-prefilter-ankle-max80-torso160-prod-test --in-place

# 对比 baseline
python scripts/data/compare_export_false_alarms.py \
  --baseline localdata/export/rule-baseline-local-prod-test \
  --experiment localdata/export/rule-speed-prefilter-ankle-max80-torso160-prod-test

# 对比 triple90+torso160
python scripts/data/compare_export_false_alarms.py \
  --baseline localdata/export/rule-speed-prefilter-ankle-max80-triple90-torso160-prod-test \
  --experiment localdata/export/rule-speed-prefilter-ankle-max80-torso160-prod-test
```

## 6. 结论与建议

| 结论 | 说明 |
|------|------|
| **推荐简化** | 在当前 28-clip 上，`ankle@80 + 肩髋踝≥160` 优于叠加 triple90 |
| FP 收益 | 比 triple90 版再减 14 次误报，无 FN 代价 |
| 残留 FN | 10 段与 triple90 版相同；蹲姿+手腕低置信仍是主因 |
| 下一步 | 碰撞层手腕缺失补偿 / 蹲姿段专用策略，而非恢复手部角度豁免 |

## 7. 产出文件

| 路径 | 说明 |
|------|------|
| `localdata/export/rule-speed-prefilter-ankle-max80-torso160-prod-test/` | 导出包 + accuracy_report |
| `localdata/export/compare_*_vs_rule-baseline-local-prod-test.md` | 相对 baseline 对比 |
| `localdata/export/compare_*_vs_rule-speed-prefilter-ankle-max80-triple90-torso160-prod-test.md` | 相对 triple90 对比 |
| `localdata/eval_runs/20260713_055117_e1fd6851/` | eval run 落盘 |
