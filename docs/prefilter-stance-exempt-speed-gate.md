# 站立姿态门控豁免实验（ankle_max@80 + triple90）

- 生成时间: 2026-07-12T12:21:57.585274+00:00
- 记录数: 28

## 1. 参照

| 方案 | TP | FP | FN | 召回 |
|------|-----|-----|-----|------|
| baseline（无门控） | 147 | 429 | 9 | 0.9423 |
| ankle_max@80 + triple90 | 145 | 290 | 11 | 0.9295 |

## 2. 门控逻辑

```
block = ankle_max_speed > 80
    AND NOT triple90(arm_torso≥90 AND elbow≥150 AND wrist_elev≥60)
    AND is_standing(stance_feature >= threshold)
```

非站立（蹲/蹲取）时即使超速也不 block。

## 3. 网格 Top（FN 优先）

| 方案 | TP | FP | FN | 召回 | ΔFN | ΔFP |
|------|-----|-----|-----|------|-----|-----|
| ankle_max@80 + triple90 + standing(knee_angle_mean>=120.0) | 146 | 294 | 10 | 0.9359 | -1 | 4 |
| ankle_max@80 + triple90 + standing(knee_angle_mean>=130.0) | 146 | 294 | 10 | 0.9359 | -1 | 4 |
| ankle_max@80 + triple90 + standing(knee_angle_mean>=140.0) | 146 | 294 | 10 | 0.9359 | -1 | 4 |
| ankle_max@80 + triple90 + standing(knee_angle_mean>=150.0) | 146 | 294 | 10 | 0.9359 | -1 | 4 |
| ankle_max@80 + triple90 + standing(knee_angle_min>=120.0) | 146 | 294 | 10 | 0.9359 | -1 | 4 |
| ankle_max@80 + triple90 + standing(knee_angle_min>=130.0) | 146 | 297 | 10 | 0.9359 | -1 | 7 |
| ankle_max@80 + triple90 + standing(knee_angle_min>=140.0) | 146 | 299 | 10 | 0.9359 | -1 | 9 |
| ankle_max@80 + triple90 + standing(torso_leg_angle_mean>=160.0) | 146 | 311 | 10 | 0.9359 | -1 | 21 |
| ankle_max@80 + triple90 + standing(center_torso_leg_angle>=160.0) | 146 | 311 | 10 | 0.9359 | -1 | 21 |
| ankle_max@80 + triple90 + standing(knee_angle_min>=150.0) | 146 | 311 | 10 | 0.9359 | -1 | 21 |
| ankle_max@80 + triple90 + standing(leg_span_ratio>=0.8) | 146 | 311 | 10 | 0.9359 | -1 | 21 |
| ankle_max@80 + triple90 + standing(center_torso_leg_angle>=170.0) | 146 | 330 | 10 | 0.9359 | -1 | 40 |
| ankle_max@80 + triple90 + standing(torso_leg_angle_min>=160.0) | 146 | 337 | 10 | 0.9359 | -1 | 47 |
| ankle_max@80 + triple90 + standing(leg_span_ratio>=1.0) | 146 | 357 | 10 | 0.9359 | -1 | 67 |
| ankle_max@80 + triple90 + standing(torso_leg_angle_mean>=170.0) | 146 | 358 | 10 | 0.9359 | -1 | 68 |
| ankle_max@80 + triple90 + standing(leg_span_ratio>=1.2) | 146 | 386 | 10 | 0.9359 | -1 | 96 |
| ankle_max@80 + triple90 + standing(torso_leg_angle_min>=170.0) | 146 | 399 | 10 | 0.9359 | -1 | 109 |
| ankle_max@80 + triple90 + standing(leg_span_ratio>=1.5) | 146 | 404 | 10 | 0.9359 | -1 | 114 |
| ankle_max@80 + triple90 + standing(torso_leg_angle_mean>=130.0) | 145 | 303 | 11 | 0.9295 | 0 | 13 |
| ankle_max@80 + triple90 + standing(torso_leg_angle_min>=130.0) | 145 | 303 | 11 | 0.9295 | 0 | 13 |

## 4. 推荐组合

- **ankle_max@80 + triple90 + standing(knee_angle_mean>=120.0)**
- TP=146 FP=294 FN=10 recall=0.9359

## 5. squat_watch 门控帧

| clip | triple90 blocked | stance exempt blocked |
|------|------------------|----------------------|
| clip_0009_start_00-37-59_rtmpose_m.json | 4 | 3 |
| clip_0013_start_00-42-48_rtmpose_m.json | 3 | 3 |
| clip_0020_start_00-48-44_rtmpose_m.json | 0 | 0 |
| clip_0013_start_00-29-53_rtmpose_m.json | 8 | 8 |

## 6. 结论

未追平 baseline FN=9；FP 约束下优选 `ankle_max@80 + triple90 + standing(knee_angle_mean>=120.0)` （FN=10 FP=294）。
