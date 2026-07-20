# 踝点归一化速度阈值标定（ankle_max_speed_norm）

> 生成时间：2026-07-20T05:13:37.266414+00:00
> 数据集：28-clip prod-test（manifest28）
> 目标方案：`ankle_norm + triple90 + shknee140` 对齐 `ankle@80 + triple90 + shknee140`

## 1. 归一化定义

```
speed_norm = speed_px_per_sec / hypot(infer_width, infer_height)
```

与现有 `torso_speed_norm`、`kpt_*_speed_norm` 口径一致；`ankle_max_speed_norm` 为左右踝归一化速度的 max。

## 2. manifest28 infer 尺寸

| infer 尺寸 | diag | 80 px/s 理论换算 norm |
|------------|------|----------------------|
| 852x480 | 977.908 | **0.081807** |
| 853x480 | 978.779 | **0.081734** |

manifest28 仅上述两种尺寸，换算差异 < 0.1%。

## 3. 帧级分布校验

| 项 | 值 |
|----|-----|
| 有效帧对数 | 34905 |
| px/norm P50（应≈diag） | 977.908 |
| px/norm 范围 | 973.684 – 1000.0 |
| ankle_max_speed P50 | 8.574 |
| ankle_max_speed_norm P50 | 0.008768 |

## 4. 段级指标对比

| 方案 | 速度特征 | 阈值 | TP | FN | FP | 召回 |
|------|----------|------|-----|-----|-----|------|
| 参照（历史 export） | `ankle_max_speed` | 80 px/s | 146 | 10 | 310 | 93.59% |
| 本次重算（像素） | `ankle_max_speed` | 80 | 146 | 10 | 311 | 93.59% |
| **推荐（归一化）** | **`ankle_max_speed_norm`** | **0.081770** | **146** | **10** | **311** | **93.59%** |

> 历史 export FP=310 与本次重算 FP=311 差 1 次，属重算口径微差；归一化与像素重算指标一致。

## 5. 归一化阈值扫描（节选）

优选准则：与像素参照 FN/FP 完全一致；并列时取阈值更接近理论换算者。

| norm 阈值 | TP | FN | FP | 召回 | 与参照 Δ |
|-----------|-----|-----|-----|------|----------|
|0.080770| 146 | 10 | 309 | 93.59% | FN+0 FP-2 |
| 0.081734 | 146 | 10 | 311 | 93.59% | ✓ 一致 |
| **0.081770** | 146 | 10 | 311 | 93.59% | ✓ 一致（推荐） |
| 0.081807 | 146 | 10 | 311 | 93.59% | ✓ 一致 |
|0.082770| 146 | 10 | 311 | 93.59% | ✓ 一致 |
|0.083770| 146 | 10 | 312 | 93.59% | FN+0 FP+1 |

## 6. 推荐上线参数

```
block = ankle_max_speed_norm > 0.081770
    AND NOT triple90
    AND shoulder_hip_knee_angle_min >= 140
```

等价像素阈值（按 infer diag 反算）：`threshold_px ≈ 0.081770 × hypot(infer_w, infer_h)`

示例：

- 853×480：80.03 px/s
- 852×480：79.96 px/s

## 7. 代码落点

| 文件 | 说明 |
|------|------|
| `event_engine/skeleton_features.py` | 新增 `ankle_max_speed_norm` 聚合列 |
| `event_engine/speed_gated_collision.py` | `SpeedGateConfig.feature` 支持 `ankle_max_speed_norm` |
| `api/playback_features_service.py` | 回放侧栏展示 |

## 8. 结论

推荐 **`ankle_max_speed_norm > 0.081770`**，段级指标 TP=146 FN=10 FP=311，与 `ankle_max@80 + triple90 + shknee140` 完全一致。理论换算均值 0.081770，最优阈值偏差 0.000001。
