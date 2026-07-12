# 朝向特征 + 速度门控组合实验

> 生成时间：2026-07-10T09:00:23.312897+00:00
> 脚本：`scripts/data/validate_prefilter_orientation_combo28.py`

## 1. 参数

| 参数 | 值 |
|------|-----|
| pose_frame_interval | 2 |
| alarm_min_consecutive_frames | 3 |
| alarm_cooldown_frames | 0 |

## 2. 实验逻辑

门控：下肢速度超阈 **且** 朝向未达豁免阈 → 跳过手腕进框。

豁免特征（取较大侧或均值）：
- `arm_torso_angle_max`：∠(髋心, 肩, 肘) 峰值
- `arm_torso_angle_mean`：肩-肘相对躯干均值
- 双条件：`arm_torso_angle_max` **且** `elbow_angle_mean` 同时达标

## 3. 帧级阈值筛选（速度已超阈子集）

在会被速度门控的帧上，比较标真 vs 误报的「豁免命中率」=`P(orient≥阈)`。
优选 **separation = 标真命中率 − 误报命中率** 最大的阈。

### knee@65

| 特征 | 豁免阈≥ | 标真豁免率 | 误报豁免率 | separation |
|------|---------|------------|------------|------------|
| `wrist_elevation_angle_max` | 55 | 0.4091 | 0.2716 | 0.1375 |
| `elbow_waist_angle_max` | 135 | 0.2131 | 0.0864 | 0.1267 |
| `wrist_elevation_angle_max` | 85 | 0.2149 | 0.0988 | 0.1161 |
| `wrist_elevation_angle_max` | 45 | 0.4711 | 0.358 | 0.1131 |
| `wrist_elevation_angle_max` | 65 | 0.3223 | 0.2099 | 0.1124 |
| `arm_torso_angle_max` | 85 | 0.3852 | 0.284 | 0.1012 |
| `arm_torso_angle_mean` | 45 | 0.6066 | 0.5062 | 0.1004 |
| `arm_torso_angle_max` | 60 | 0.5779 | 0.4815 | 0.0964 |

### lower@60

| 特征 | 豁免阈≥ | 标真豁免率 | 误报豁免率 | separation |
|------|---------|------------|------------|------------|
| `elbow_waist_angle_max` | 135 | 0.2266 | 0.0638 | 0.1628 |
| `arm_torso_angle_max` | 80 | 0.4317 | 0.2766 | 0.1551 |
| `arm_torso_angle_max` | 75 | 0.4568 | 0.3085 | 0.1483 |
| `arm_torso_angle_max` | 85 | 0.3885 | 0.2447 | 0.1438 |
| `wrist_elevation_angle_max` | 55 | 0.4239 | 0.2872 | 0.1367 |
| `arm_torso_angle_max` | 60 | 0.5719 | 0.4362 | 0.1357 |
| `elbow_waist_angle_max` | 145 | 0.1619 | 0.0319 | 0.13 |
| `arm_torso_angle_mean` | 60 | 0.3885 | 0.266 | 0.1225 |

## 4. 参照方案

| 方案 | TP | FP | FN | 召回率 |
|------|-----|-----|-----|--------|
| local baseline（无门控） | 147 | 429 | 9 | 0.9423 |
| lower@60 prefilter | 142 | 287 | 14 | 0.9103 |
| knee@65 prefilter | 144 | 298 | 12 | 0.9231 |
| knee@65 + 肘角豁免≥150（对照） | 147 | 361 | 9 | 0.9423 |

## 5. 组合门控全量评估（按 FP 约束优选）

优选准则：FP ≤ knee@65 + 12，最小化 FN，其次 FP。

| 方案 | TP | FP | FN | 召回率 | ΔFP vs knee@65 | ΔFN vs knee@65 |
|------|-----|-----|-----|--------|----------------|----------------|
| knee@65 + elbow_waist_angle_max≥145 | 144 | 303 | 12 | 0.9231 | +5 | +0 |
| knee@65 + elbow_waist_angle_max≥135 | 144 | 308 | 12 | 0.9231 | +10 | +0 |
| lower@60 + elbow_waist_angle_max≥145 | 143 | 295 | 13 | 0.9167 | -3 | +1 |
| lower@60 + elbow_waist_angle_max≥135 | 143 | 299 | 13 | 0.9167 | +1 | +1 |
| lower@60 + 躯干85° & 肘150° | 143 | 309 | 13 | 0.9167 | +11 | +1 |
| lower@60 + 躯干90° & 肘150° | 143 | 309 | 13 | 0.9167 | +11 | +1 |

### 5.1 召回优先（FN < knee@65，允许 FP 适度上升）

| 方案 | TP | FP | FN | 召回率 | ΔFP vs knee@65 | ΔFN vs knee@65 |
|------|-----|-----|-----|--------|----------------|----------------|
| knee@65 + 躯干70° & 肘150° | 146 | 324 | **10** | **0.9359** | +26 | **−2** |
| knee@65 + 躯干70° & 肘140° | 146 | 326 | **10** | **0.9359** | +28 | **−2** |
| knee@65 + arm_torso_angle_mean≥45 | 146 | 358 | **10** | **0.9359** | +60 | **−2** |
| knee@65 + 肘角豁免≥150（对照） | 147 | 361 | **9** | **0.9423** | +63 | **−3** |

**召回优先推荐**：`knee@65 + 躯干70° & 肘150°` — 在朝向双条件下，比单纯肘角豁免少 **37** 次 FP（361→324），FN 仅多 1 段（9→10）。

**FP 约束下推荐（§5 准则）**：`knee@65 + elbow_waist_angle_max≥145` — FN 与 knee@65 相同（12），FP +5（298→303）。

**优选组合**：knee@65 + elbow_waist_angle_max≥145

- TP=144 FP=303 FN=12 recall=0.9231
- 相对 knee@65：ΔFP=+5，ΔFN=+0

## 6. 结论

1. **帧级筛选最优阈**（separation 最大）：knee@65 下 `wrist_elevation≥55`、`elbow_waist≥135`、`arm_torso_max≥85`；lower@60 下 `elbow_waist≥135`、`arm_torso_max≥80`。
2. **FP 约束（≤ knee@65+12）**：`knee@65 + elbow_waist_angle_max≥145` → FN=12（不变），FP=303（+5），无法降漏报。
3. **召回优先**：`knee@65 + 躯干70° & 肘150°` 双条件豁免 → FN **10**（−2），FP **324**（+26），优于单纯肘角豁免（FN=9 FP=361）。
4. **朝向单特征**难以在「少增 FP」前提下显著降漏报；**双条件（躯干张开 + 肘伸直）** 更有望替代单纯肘角豁免。

若落地验证，建议优先导出：`knee@65 + arm_torso_angle_max≥70 AND elbow_angle_mean≥150`。
