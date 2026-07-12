# 多角度 AND/OR 组合豁免实验

> 生成时间：2026-07-10T09:52:00.644654+00:00
> 脚本：`scripts/data/validate_prefilter_multi_angle_logic28.py`

## 1. 参数

| 参数 | 值 |
|------|-----|
| pose_frame_interval | 2 |
| alarm_min_consecutive_frames | 3 |
| alarm_cooldown_frames | 0 |

## 2. 决策逻辑

共用的速度门控：下肢速度 > 阈 → 候选 block。

- **AND 豁免**：`角度A≥阈A` **且** `角度B≥阈B` → 不 block（更像伸手取货）
- **OR 豁免**：`角度A≥阈A` **或** `角度B≥阈B` → 不 block（更宽松，易增 FP）
- **三重 AND**：三个角度同时达标才豁免

## 3. 参照方案

| 方案 | TP | FP | FN | 召回率 |
|------|-----|-----|-----|--------|
| local baseline（无门控） | 147 | 429 | 9 | 0.9423 |
| lower@60 prefilter | 142 | 287 | 14 | 0.9103 |
| knee@65 prefilter | 144 | 298 | 12 | 0.9231 |
| knee@65 + 躯干70° AND 肘150°（上期对照） | 146 | 324 | 10 | 0.9359 |
| knee@65 + 肘角≥150（单条件对照） | 147 | 361 | 9 | 0.9423 |

## 4. AND 逻辑 Top（FP ≤ knee@65+15）

| 方案 | TP | FP | FN | 召回 | ΔFP | ΔFN |
|------|-----|-----|-----|------|-----|-----|
| knee@65 + elbow_angle_mean≥160.0 AND wrist_elevation_angle_max≥60.0 | 145 | 310 | 11 | 0.9295 | +12 | -1 |
| knee@65 + arm_torso_angle_mean≥60.0 AND elbow_angle_mean≥160.0 | 145 | 311 | 11 | 0.9295 | +13 | -1 |
| knee@65 + arm_torso_angle_max≥80.0 AND elbow_angle_mean≥160.0 | 145 | 313 | 11 | 0.9295 | +15 | -1 |
| knee@65 + elbow_angle_mean≥160.0 AND wrist_elevation_angle_max≥50.0 | 145 | 313 | 11 | 0.9295 | +15 | -1 |
| knee@65 + arm_torso_angle_max≥80.0 AND elbow_waist_angle_max≥140.0 | 144 | 298 | 12 | 0.9231 | +0 | +0 |
| knee@65 + elbow_waist_angle_max≥150.0 AND wrist_elevation_angle_max≥50.0 | 144 | 298 | 12 | 0.9231 | +0 | +0 |
| knee@65 + arm_torso_angle_max≥80.0 AND elbow_waist_angle_max≥130.0 | 144 | 299 | 12 | 0.9231 | +1 | +0 |
| knee@65 + elbow_waist_angle_max≥140.0 AND wrist_elevation_angle_max≥50.0 | 144 | 299 | 12 | 0.9231 | +1 | +0 |
| knee@65 + arm_torso_angle_max≥65.0 AND elbow_waist_angle_max≥130.0 | 144 | 302 | 12 | 0.9231 | +4 | +0 |
| knee@65 + arm_torso_angle_max≥75.0 AND elbow_waist_angle_max≥130.0 | 144 | 302 | 12 | 0.9231 | +4 | +0 |
| knee@65 + elbow_waist_angle_max≥130.0 AND wrist_elevation_angle_max≥50.0 | 144 | 302 | 12 | 0.9231 | +4 | +0 |
| lower@60 + arm_torso_angle_max≥65.0 AND elbow_waist_angle_max≥120.0 | 144 | 303 | 12 | 0.9231 | +5 | +0 |

## 4.2 OR 逻辑 Top（FP ≤ knee@65+15）

_无组合通过 FP 约束_ — OR 豁免在帧级 separation 虽高，但段级评估 FP 均 > 313，不适合单独使用。

### 4.3 召回优先（FN ≤ 10）

| 方案 | TP | FP | FN | 召回 | ΔFP vs knee@65 | ΔFN |
|------|-----|-----|-----|------|----------------|-----|
| knee@65 + arm_torso_mean≥40 AND elbow≥150 | **147** | 341 | **9** | **0.9423** | +43 | **−3** |
| knee@65 + 躯干70° AND 肘150° | 146 | 324 | **10** | 0.9359 | +26 | **−2** |
| knee@65 + 躯干80° AND 肘150° | 146 | 326 | **10** | 0.9359 | +28 | **−2** |
| knee@65 + 肘≥150（单条件） | 147 | 361 | **9** | 0.9423 | +63 | **−3** |

**说明**：`arm_torso_mean≥40 AND elbow≥150` 与单肘角豁免同为 FN=9，但 FP 少 **20** 次（361→341），体现双角度 AND 对误报的抑制。

## 5. 三重 AND Top

| 方案 | TP | FP | FN | 召回 | ΔFP | ΔFN |
|------|-----|-----|-----|------|-----|-----|
| knee@65 + arm_torso_angle_max≥90 AND elbow_angle_mean≥150 AND wrist_elevation_angle_max≥60 | 145 | 310 | 11 | 0.9295 | +12 | -1 |
| knee@65 + arm_torso_angle_max≥90 AND elbow_angle_mean≥150 AND wrist_elevation_angle_max≥50 | 145 | 311 | 11 | 0.9295 | +13 | -1 |
| knee@65 + arm_torso_angle_max≥70 AND elbow_angle_mean≥150 AND wrist_elevation_angle_max≥60 | 145 | 312 | 11 | 0.9295 | +14 | -1 |
| knee@65 + arm_torso_angle_max≥80 AND elbow_angle_mean≥150 AND wrist_elevation_angle_max≥60 | 145 | 312 | 11 | 0.9295 | +14 | -1 |
| knee@65 + arm_torso_angle_max≥90 AND elbow_angle_mean≥140 AND wrist_elevation_angle_max≥60 | 145 | 312 | 11 | 0.9295 | +14 | -1 |
| knee@65 + arm_torso_angle_max≥80 AND elbow_angle_mean≥150 AND wrist_elevation_angle_max≥50 | 145 | 313 | 11 | 0.9295 | +15 | -1 |
| knee@65 + arm_torso_angle_max≥90 AND elbow_angle_mean≥140 AND wrist_elevation_angle_max≥50 | 145 | 313 | 11 | 0.9295 | +15 | -1 |
| knee@65 + arm_torso_angle_max≥70 AND elbow_angle_mean≥140 AND wrist_elevation_angle_max≥60 | 145 | 314 | 11 | 0.9295 | +16 | -1 |
| knee@65 + arm_torso_angle_max≥80 AND elbow_angle_mean≥140 AND wrist_elevation_angle_max≥60 | 145 | 314 | 11 | 0.9295 | +16 | -1 |
| knee@65 + arm_torso_angle_max≥80 AND elbow_angle_mean≥140 AND wrist_elevation_angle_max≥50 | 145 | 315 | 11 | 0.9295 | +17 | -1 |

## 6. 帧级筛选（速度超阈子集，按 separation 排序）

| 速度 | 逻辑 | 条件 | 标真豁免率 | 误报豁免率 | separation |
|------|------|------|------------|------------|------------|
| lower@60 | OR | arm_torso_angle_max≥80 OR elbow_waist_angle_max≥130 | 0.5993 | 0.3936 | 0.2057 |
| lower@60 | OR | arm_torso_angle_max≥80 OR elbow_waist_angle_max≥140 | 0.5331 | 0.3298 | 0.2033 |
| lower@60 | OR | arm_torso_angle_max≥80 OR elbow_waist_angle_max≥150 | 0.5 | 0.2979 | 0.2021 |
| lower@60 | OR | arm_torso_angle_max≥75 OR elbow_waist_angle_max≥130 | 0.6159 | 0.4149 | 0.201 |
| lower@60 | OR | arm_torso_angle_max≥75 OR elbow_waist_angle_max≥150 | 0.5232 | 0.3298 | 0.1934 |
| lower@60 | OR | arm_torso_angle_max≥75 OR elbow_waist_angle_max≥140 | 0.553 | 0.3617 | 0.1913 |
| lower@60 | OR | elbow_waist_angle_max≥140 OR wrist_elevation_angle_max≥60 | 0.4801 | 0.2979 | 0.1822 |
| lower@60 | OR | elbow_waist_angle_max≥130 OR wrist_elevation_angle_max≥60 | 0.5331 | 0.3511 | 0.182 |
| lower@60 | OR | elbow_waist_angle_max≥150 OR wrist_elevation_angle_max≥60 | 0.447 | 0.266 | 0.181 |
| lower@60 | OR | elbow_waist_angle_max≥130 OR wrist_elevation_angle_max≥70 | 0.4669 | 0.2872 | 0.1797 |
| lower@60 | OR | elbow_waist_angle_max≥150 OR wrist_elevation_angle_max≥50 | 0.5066 | 0.3298 | 0.1768 |
| lower@60 | OR | elbow_waist_angle_max≥140 OR wrist_elevation_angle_max≥50 | 0.5364 | 0.3617 | 0.1747 |
| lower@60 | OR | elbow_waist_angle_max≥130 OR wrist_elevation_angle_max≥50 | 0.5861 | 0.4149 | 0.1712 |
| lower@60 | OR | elbow_waist_angle_max≥140 OR wrist_elevation_angle_max≥70 | 0.404 | 0.234 | 0.17 |
| knee@65 | OR | elbow_waist_angle_max≥150 OR wrist_elevation_angle_max≥50 | 0.4869 | 0.321 | 0.1659 |
| lower@60 | OR | elbow_waist_angle_max≥150 OR wrist_elevation_angle_max≥70 | 0.3675 | 0.2021 | 0.1654 |
| knee@65 | OR | elbow_waist_angle_max≥140 OR wrist_elevation_angle_max≥50 | 0.5206 | 0.358 | 0.1626 |
| knee@65 | OR | elbow_waist_angle_max≥130 OR wrist_elevation_angle_max≥50 | 0.5693 | 0.4074 | 0.1619 |
| lower@60 | OR | arm_torso_angle_max≥65 OR elbow_waist_angle_max≥150 | 0.5828 | 0.4362 | 0.1466 |
| lower@60 | OR | elbow_waist_angle_max≥150 OR wrist_elevation_angle_max≥40 | 0.5695 | 0.4255 | 0.144 |

## 7. 结论

1. **OR 逻辑**：帧级 separation 可达 0.20+，但段级 FP 全部超标，**不可用**。
2. **AND 逻辑（FP 约束）**：`knee@65 + elbow≥160 AND wrist_elevation≥60` → FN=11（−1），FP=310（+12）。
3. **AND 逻辑（召回优先）**：`knee@65 + arm_torso_mean≥40 AND elbow≥150` → FN=9，FP=341，比单肘角豁免少 20 FP。
4. **三重 AND**：与双角度 AND 接近，FN=11 FP=310，增益有限。

**落地建议**：
- 要降漏报：`knee@65 + arm_torso_max≥70 AND elbow≥150`（FN=10 FP=324）
- 要压 FP：`knee@65 + elbow≥160 AND wrist_elevation≥60`（FN=11 FP=310）
- 避免：任意 2 角度 **OR** 豁免
