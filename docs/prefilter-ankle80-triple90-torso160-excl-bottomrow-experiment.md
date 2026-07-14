# 前置过滤实验：ankle@80 + triple90 + torso160 + 站立时跳过末行货架

> 生成时间：2026-07-14  
> 分支：`try/speed-filter`  
> 样本：28 条（单人 · 无遮挡 · 已复核 · 有标真）  
> 导出目录：`localdata/export/rule-speed-prefilter-ankle-max80-triple90-torso160-excl-bottomrow-prod-test`  
> eval_id：`20260714_093807_173db2b3`

## 1. 背景

现场站立时因透视投影，手腕点易落入 **每个货架网格最底一行**（`layer == grid_rows`）货位多边形，产生大量误告警。双货架机位需分别识别两套末行（reflection 合并后 `shelves[]` 各一条底行）。

## 2. 末行识别规则

实现：`event_engine/shelf_grid.py`

| 项 | 规则 |
|----|------|
| 网格 | 每 shelf 读取 `grid_shape: [rows, cols]`（默认 4×4） |
| 末行 | `layer == max(该 shelf 已标注 layer)`（未标满 grid 时如 9 格→layer=3） |
| 双货架 | `shelves[]` 内 **每个 shelf 独立** 取底行 `box_id` |
| 兜底 | 无 `layer` 时由 `box_id` 线性编号推断：`linear % 1000 → (layer-1)//cols+1 |

示例（2-5-1 双货架 `clip_0009`）：

| shelf_code | grid | 有效底行 layer | 末行 box_id |
|------------|------|----------------|-------------|
| 91（仅 9 格） | 4×4 | **3** | 4043, 4044, 4045 |
| 92（满 16 格） | 4×4 | 4 | 3077–3080 |

> **修正（2026-07-14）**：初版误用 `layer==grid_rows(4)`，导致 shelf 91 末行为空、`Box_4044` 等 layer=3 误报无法排除；已改为 per-shelf `max(layer)`。

## 3. 门控逻辑（完整）

```
block_speed = (ankle_max_speed > 80)
    AND NOT triple90(arm_torso≥90 AND elbow≥150 AND wrist_elev≥60)
    AND (torso_leg_angle_mean >= 160)   // 仅站立时可 block 碰撞

collision_boxes = 全部货位
    若 站立(torso_leg≥160)：排除 is_bottom_row 货位
    否则：保留全部货位（蹲取仍检测末行）
```

参数对齐：`pose_frame_interval=2`，`alarm_min=3`，`cooldown=0`。

## 4. 系统级准确率（28-clip）

| 指标 | baseline-local | triple90+torso160（无末行排除） | **本实验（修正末行后）** |
|------|----------------|--------------------------------|-------------------------|
| TP | 147 | 146 | **138** |
| FN | 9 | 10 | **18** |
| FP | 429 | 311 | **218** |
| 召回率 | 94.23% | 93.59% | **88.46%** |
| 精确率代理¹ | 25.52% | 31.94% | **38.76%** |

¹ TP / (TP + FP)

### 相对 triple90+torso160（无末行排除）

| 变化 | 数值 |
|------|------|
| FP | 311 → **218（−93）** |
| FN | 10 → **18（+8）** |
| 新增误报 | **0** |
| 消除误报 | 22（多为末行 token，如 Box_3093、Box_1015 等） |

**解读**：末行排除有效压低站立投影误报，但 **5 段真取货发生在末行且姿态被判为站立**，被误杀（如 `Box_2015`、`Box_2016`、`Box_3096`）。不宜直接上线，需与蹲姿细分或末行+速度联合条件联调。

## 5. 新增漏报（5 段）

| clip | 机位 | 帧区间 | 标真货框 | 说明 |
|------|------|--------|----------|------|
| clip_0006_start_00-14-11 | 2-6-1 | 739–755 | Box_3096 | 末行真取货 + 站立判定 |
| clip_0006_start_00-41-26 | 2-2-2 | 638–662 | Box_3029 | 同上 |
| clip_0007_start_00-22-47 | 2-6-1 | 1–20 | Box_3096 | 同上 |
| clip_0013_start_00-11-22 | 1-1-1 | 4207–4231 | **Box_2015** | 货架 82 末行 |
| clip_0013_start_00-11-22 | 1-1-1 | 4343–4368 | **Box_2016** | 货架 82 末行 |

## 6. 复现命令

```bash
# 导出
python scripts/data/export_prefilter_triple_angle_upload.py \
  --speed-feature ankle_max_speed --speed-threshold 80 \
  --stance-feature torso_leg_angle_mean --stance-threshold 160 \
  --exclude-bottom-row-standing \
  --output-dir localdata/export/rule-speed-prefilter-ankle-max80-triple90-torso160-excl-bottomrow-prod-test

# 评估
python scripts/data/evaluate_inference_upload.py \
  --dir localdata/export/rule-speed-prefilter-ankle-max80-triple90-torso160-excl-bottomrow-prod-test --in-place

# 对比无末行排除的 torso160 包
python scripts/data/compare_export_false_alarms.py \
  --baseline localdata/export/rule-speed-prefilter-ankle-max80-triple90-torso160-prod-test \
  --experiment localdata/export/rule-speed-prefilter-ankle-max80-triple90-torso160-excl-bottomrow-prod-test
```

## 7. 改动文件

| 文件 | 说明 |
|------|------|
| `event_engine/shelf_grid.py` | 末行识别、双货架支持 |
| `scripts/data/validate_prefilter_joint_angle28.py` | 站立时跳过 `is_bottom_row` 碰撞 |
| `scripts/data/upload_export_common.py` | 导出前标注末行打标 |
| `api/wrist_features_service.py` | `load_annotation_config_for_export` |
| `scripts/data/export_prefilter_triple_angle_upload.py` | `--exclude-bottom-row-standing` |

## 8. 结论与后续

| 结论 | 说明 |
|------|------|
| 末行识别 | 已按 shelf 独立识别；双货架 manifest 见 `records[].bottom_row_shelves` |
| FP 收益 | −22，无新增误报 |
| FN 代价 | +5，主要为末行真取货 |
| 建议 | 改为「站立 **且** 高速门控已 block」时跳过末行，或末行仅降权不硬排除；蹲取/末行取货保留碰撞 |

对比 MD：`localdata/export/compare_rule-speed-prefilter-ankle-max80-triple90-torso160-excl-bottomrow-prod-test_vs_rule-speed-prefilter-ankle-max80-triple90-torso160-prod-test.md`
