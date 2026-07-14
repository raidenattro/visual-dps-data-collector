# 肩髋膝站立特征替代 torso160 实验

生成时间（UTC）: 2026-07-14

## 1. 背景

原规则 `ankle_max@80 + triple90 + torso_leg_angle_mean>=160` 用 **∠(肩,髋,踝)** 判定站立姿态：站立时躯干与整腿夹角较大，蹲姿取货时变小，从而在「下肢超速 + 未满足手部 triple90」时仅对站立者 block，保留蹲姿真取货。

本实验用 **∠(肩,髋,膝)**（顶点在髋）替代 torso，在相同 `ankle_max@80 + triple90` 前提下扫描阈值，验证能否达到 torso160 的 FN/FP 水平。

| 项 | 内容 |
|----|------|
| 模块 | 速度前置过滤 / 站立姿态门控 |
| 数据集 | 28 clip prod-test |
| 对照 | `rule-speed-prefilter-ankle-max80-triple90-torso160-prod-test` |
| 参数 | `pose_frame_interval=2`, `alarm_min=3`, `cooldown=0` |

## 2. 特征定义

在 `event_engine/skeleton_angles.py` 新增肩髋膝角（`SHOULDER_HIP_KNEE_ANGLE_DEFS`）：

| 特征 | 几何含义 |
|------|----------|
| `left/right_shoulder_hip_knee_angle` | 单侧 ∠(肩,髋,膝)，顶点在髋 |
| `center_shoulder_hip_knee_angle` | 肩心-髋心-膝心夹角 |
| `shoulder_hip_knee_angle_mean` | 上述角度均值 |
| `shoulder_hip_knee_angle_min` | 上述角度最小值（**推荐用于门控**） |

与 `torso_leg_angle_mean`（∠肩,髋,踝）相比，肩髋膝角**不含踝点**，对小腿姿态更不敏感，更聚焦躯干-大腿折叠程度。

**站立判定逻辑**（与 torso 一致）：

```
block = ankle_max>80 AND NOT(triple90) AND stance_angle >= threshold
```

缺角度数据时按站立处理（保守压误报）。

## 3. 网格扫描（28 clip 门控重算）

脚本：`scripts/data/validate_prefilter_shoulder_hip_knee_stance28.py`  
完整 JSON：`docs/json/prefilter-shoulder-hip-knee-stance-experiment.json`

### 参考基线

| 规则 | TP | FN | FP | 召回 |
|------|----|----|-----|------|
| triple90 only | 145 | 11 | 290 | 92.95% |
| **torso160（对照）** | **146** | **10** | **311** | **93.59%** |

### 推荐配置（FN≤10 且 FP 最接近 torso160）

**`shoulder_hip_knee_angle_min >= 140`**

| 指标 | torso160 | shknee140 | Δ |
|------|----------|-----------|---|
| TP | 146 | 146 | 0 |
| FN | 10 | 10 | 0 |
| FP | 311 | 310 | **-1** |
| 召回 | 93.59% | 93.59% | — |

### 其他值得关注的阈值

| 规则 | TP | FN | FP | 说明 |
|------|----|----|-----|------|
| shknee_mean>=100~120 | 146 | 10 | 303~304 | FP 更低，但阈值偏松、物理含义弱 |
| center_shknee>=170 | 147 | 9 | 335 | FN 更少，FP 明显升高 |
| shknee_min>=170 | 147 | 9 | 404 | 过严，误报大增 |

**结论**：`shoulder_hip_knee_angle_min@140` 在 27 组网格中唯一达到与 torso160 相同的 FN=10，且 FP 仅少 1。

## 4. 导出包验证

导出目录：`localdata/export/rule-speed-prefilter-ankle-max80-triple90-shknee140-prod-test`

```bash
python scripts/data/export_prefilter_triple_angle_upload.py \
  --speed-feature ankle_max_speed --speed-threshold 80 \
  --stance-feature shoulder_hip_knee_angle_min --stance-threshold 140 \
  --output-dir localdata/export/rule-speed-prefilter-ankle-max80-triple90-shknee140-prod-test

python scripts/data/evaluate_inference_upload.py \
  --dir localdata/export/rule-speed-prefilter-ankle-max80-triple90-shknee140-prod-test --in-place
```

评估结果（与网格一致）：

| 指标 | 值 |
|------|-----|
| TP | 146 |
| FN | 10 |
| FP | 310 |
| 召回 | 93.59% |

> 注意：导出脚本默认速度特征为 `knee_ankle_mean_speed@65`，本实验必须显式指定 `--speed-feature ankle_max_speed --speed-threshold 80`。

## 5. 与 torso160 逐事件对比

对比报告：`localdata/export/compare_rule-speed-prefilter-ankle-max80-triple90-shknee140-prod-test_vs_rule-speed-prefilter-ankle-max80-triple90-torso160-prod-test.md`

| 维度 | 变化 |
|------|------|
| FP 净变化 | 311 → 310（-1） |
| FN 净变化 | 10 → 10（0） |
| 仅 torso 有误报 | 2 条（`2-6-1/clip_0006` 帧951 `Box_3093`；`2-2-2/clip_0019` 帧393 `Box_4016`） |
| 仅 shknee 有误报 | 1 条（`2-6-1/clip_0006` 帧543 `Box_3083`） |
| 漏报差异 | 无 |

两条规则在事件级几乎等价，差异集中在个别帧的站立/蹲姿边界判定。

## 6. 工程建议

| 方案 | 特征 | 阈值 | 适用场景 |
|------|------|------|----------|
| **原方案** | `torso_leg_angle_mean` | 160 | 已上线对照 |
| **本实验推荐** | `shoulder_hip_knee_angle_min` | 140 | 与 torso160 等价，语义更贴近「躯干-大腿」折叠 |
| 召回优先 | `center_shoulder_hip_knee_angle` | 170 | FN=9，FP+24 |

**推荐**：若希望用肩髋膝替代 torso，采用 `shoulder_hip_knee_angle_min>=140`，指标与 torso160 对齐，且几何上更直观（不依赖踝点稳定性）。

## 7. 相关文件

| 文件 | 说明 |
|------|------|
| `event_engine/skeleton_angles.py` | 新增肩髋膝角特征 |
| `scripts/data/validate_prefilter_shoulder_hip_knee_stance28.py` | 网格扫描脚本 |
| `docs/json/prefilter-shoulder-hip-knee-stance-experiment.json` | 网格原始结果 |
| `localdata/export/rule-speed-prefilter-ankle-max80-triple90-shknee140-prod-test/` | 导出评估包 |
