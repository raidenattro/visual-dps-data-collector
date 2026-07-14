# 前置过滤实验：ankle@80 + triple90 + shknee140 + 站立时跳过末行货架

> 生成时间：2026-07-14  
> 分支：`try/speed-filter`  
> 样本：28 条（单人 · 无遮挡 · 已复核 · 有标真）  
> 导出目录：`localdata/export/rule-speed-prefilter-ankle-max80-triple90-shknee140-excl-bottomrow-prod-test`  
> eval_id：`20260714_102631_5a2cb454`

## 1. 背景

在上一轮实验中，`ankle_max@80 + triple90 + shoulder_hip_knee_angle_min>=140`（shknee140）已与 `torso160` 指标对齐（FN=10, FP=310）。现场仍存在 **站立 + 透视投影** 导致手腕落入货架 **末行货位** 的误报（如 `2-5-1/clip_0009` 帧 5635–5673 的 `Box_4044`）。

本实验在 shknee140 基础上叠加 **站立时跳过每 shelf 末行碰撞**，验证 FP 收益与 FN 代价。

## 2. 末行识别规则

实现：`event_engine/shelf_grid.py`（与 torso160 末行实验相同）

| 项 | 规则 |
|----|------|
| 网格 | 每 shelf 读取 `grid_shape: [rows, cols]`（默认 4×4） |
| 末行 | `layer == max(该 shelf 已标注 layer)` |
| 双货架 | `shelves[]` 内每个 shelf 独立取底行 `box_id` |
| 兜底 | 无 `layer` 时由 `box_id` 线性编号推断 |

示例（2-5-1 双货架 `clip_0009`）：

| shelf_code | 有效底行 layer | 末行 box_id |
|------------|----------------|-------------|
| 91（仅 9 格） | 3 | 4043, 4044, 4045 |
| 92（满 16 格） | 4 | 3077–3080 |

## 3. 门控逻辑（完整）

```
block_speed = (ankle_max_speed > 80)
    AND NOT triple90(arm_torso≥90 AND elbow≥150 AND wrist_elev≥60)
    AND (shoulder_hip_knee_angle_min >= 140)   // 仅站立时可 block 速度门控

collision_boxes = 全部货位
    若 站立(shknee_min≥140)：排除 is_bottom_row 货位
    否则：保留全部货位（蹲取仍检测末行）
```

参数对齐：`pose_frame_interval=2`，`alarm_min=3`，`cooldown=0`。

## 4. 系统级准确率（28-clip）

| 指标 | shknee140（无末行排除） | **本实验（shknee140+末行排除）** | Δ |
|------|-------------------------|----------------------------------|---|
| TP | 146 | **143** | −3 |
| FN | 10 | **13** | +3 |
| FP | 310 | **217** | **−93** |
| 召回率 | 93.59% | **91.67%** | −1.92pp |
| 精确率代理¹ | 32.02% | **39.72%** | +7.70pp |

¹ TP / (TP + FP)

### 相对 shknee140（无末行排除）

| 变化 | 数值 |
|------|------|
| FP | 310 → **217（−93）** |
| FN | 10 → **13（+3）** |
| 新增误报 | **0** |
| 消除误报 | **93**（含 `clip_0009` `Box_4044` 连续 28 帧等末行 token） |

**解读**：末行排除显著压低站立投影类误报，零新增误报；代价为 3 段新增漏报（末行或近末行真取货 + 站立判定）。

### 横向对比：同策略下 shknee140 vs torso160（均含末行排除）

| 指标 | torso160+末行排除 | shknee140+末行排除 |
|------|-------------------|---------------------|
| TP | 138 | **143** |
| FN | 18 | **13** |
| FP | 218 | **217** |
| 召回 | 88.46% | **91.67%** |

在末行排除场景下，**shknee140 比 torso160 多检出 5 段、少漏 5 段，FP 基本持平**。

## 5. 新增漏报（3 段）

| clip | 机位 | 帧区间 | 标真货框 | 说明 |
|------|------|--------|----------|------|
| clip_0009_start_00-17-17 | 2-2-2 | 1–19 | Box_4018 | 末行取货 + 站立 |
| clip_0009_start_00-17-17 | 2-2-2 | 269–291 | Box_4016 | 同上 |
| clip_0023_start_00-49-39 | 2-3-1 | 376–444 | Box_4026 | 末行取货 + 站立 |

> torso160+末行排除 FN=18（含 `Box_2015/2016/3096` 等）；shknee140 站立判定更宽，部分末行真取货仍能通过，故 FN 代价更小（+3 vs +8）。

## 6. 复现命令

```bash
# 导出
python scripts/data/export_prefilter_triple_angle_upload.py \
  --speed-feature ankle_max_speed --speed-threshold 80 \
  --stance-feature shoulder_hip_knee_angle_min --stance-threshold 140 \
  --exclude-bottom-row-standing \
  --output-dir localdata/export/rule-speed-prefilter-ankle-max80-triple90-shknee140-excl-bottomrow-prod-test

# 评估
python scripts/data/evaluate_inference_upload.py \
  --dir localdata/export/rule-speed-prefilter-ankle-max80-triple90-shknee140-excl-bottomrow-prod-test --in-place

# 对比无末行排除的 shknee140 包
python scripts/data/compare_export_false_alarms.py \
  --baseline localdata/export/rule-speed-prefilter-ankle-max80-triple90-shknee140-prod-test \
  --experiment localdata/export/rule-speed-prefilter-ankle-max80-triple90-shknee140-excl-bottomrow-prod-test
```

## 7. 相关文件

| 文件 | 说明 |
|------|------|
| `event_engine/shelf_grid.py` | 末行识别 |
| `scripts/data/export_prefilter_triple_angle_upload.py` | `--exclude-bottom-row-standing` |
| `scripts/data/validate_prefilter_joint_angle28.py` | 站立时跳过 `is_bottom_row` 碰撞 |
| `docs/prefilter-ankle80-triple90-shknee-stance-experiment.md` | shknee140 基线实验 |

## 8. 结论与后续

| 结论 | 说明 |
|------|------|
| FP 收益 | −93，零新增误报；`clip_0009` `Box_4044` 误报簇已消除 |
| FN 代价 | +3 段，低于 torso160+末行排除的 +8 |
| 推荐组合 | 若需末行排除，**shknee140 优于 torso160**（召回更高、FP 相当） |
| 风险 | 末行真取货 + 站立姿态仍会被硬排除，不宜无条件下线 |
| 后续 | 末行+速度联合条件、或末行降权替代硬排除 |

对比 MD：

- 摘要：`docs/compare-ankle80-triple90-shknee140-excl-bottomrow-vs-shknee140.md`
- 明细：`localdata/export/compare_rule-speed-prefilter-ankle-max80-triple90-shknee140-excl-bottomrow-prod-test_vs_rule-speed-prefilter-ankle-max80-triple90-shknee140-prod-test.md`
