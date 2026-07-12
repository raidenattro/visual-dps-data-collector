# 蹲/站姿态代理特征筛查

- 生成时间: 2026-07-12T12:22:57.870516+00:00
- 门控底座: ankle_max_speed@80.0 + triple90
- 记录数: 28

## 1. 帧级统计（triple90 门控命中）

| 集合 | 帧数 |
|------|------|
| 标真全帧 | 4869 |
| 误报全帧 | 446 |
| 标真被 triple90 block | 387 |
| 误报被 triple90 block | 81 |

## 2. 特征 P50 对比

| 特征 | 标真 blocked | 误报 blocked |
|------|-------------|-------------|
| torso_leg_angle_mean | 170.6 | 170.04 |
| torso_leg_angle_min | 164.08 | 163.83 |
| torso_leg_angle_max | 176.75 | 176.37 |
| center_torso_leg_angle | 173.97 | 175.2 |
| left_torso_leg_angle | 170.01 | 166.665 |
| right_torso_leg_angle | 168.83 | 167.78 |
| knee_angle_mean | 173.92 | 173.145 |
| knee_angle_min | 171.09 | 169.28 |
| knee_angle_max | 177.08 | 177.625 |
| leg_span_ratio | 1.1457 | 1.0001 |
| hip_knee_ankle_vertical_ratio | 1.1201 | 1.3946 |
| elbow_waist_angle_max | 119.88 | 119.81 |
| arm_torso_angle_max | 64.55 | 46.99 |
| wrist_elevation_angle_max | 38.745 | 28.69 |
| ankle_max_speed | 126.938 | 170.113 |
| torso_speed | 44.5495 | 74.949 |

## 3. 重点漏报段

### clip_0013_start_00-42-48_rtmpose_m.json [159, 169]
- triple90 blocked 行数: 0/6
- knee_angle_mean P50: 151.535
- leg_span_ratio P50: 2.5351

### clip_0020_start_00-48-44_rtmpose_m.json [79, 82]
- triple90 blocked 行数: 0/2
- knee_angle_mean P50: 148.455
- leg_span_ratio P50: 0.8159

### clip_0013_start_00-29-53_rtmpose_m.json [2244, 2254]
- triple90 blocked 行数: 4/5
- knee_angle_mean P50: 165.83
- leg_span_ratio P50: 1.0744

## 4. 结论

上下半身夹角 torso_leg_mean：标真 blocked P50=170.6° vs 误报 170.0°。 torso_leg_min：标真 blocked P50=164.1° vs 误报 163.8°（蹲取可看 min）。 膝角区分度有限（标真 blocked P50=173.9° vs 误报 blocked P50=173.1°），需结合 leg_span_ratio 网格验证。
