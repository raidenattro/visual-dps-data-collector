# 脚部踝点速度前置门控阈值选取

> 生成时间：2026-07-10T10:21:13.336880+00:00
> 脚本：`scripts/data/validate_prefilter_foot_ankle28.py`

## 1. 实验目的

基于 **左右踝骨骼点**（COCO17 索引 15/16）计算帧间速度，
选取合理前置门控阈值；**低置信度点（score < WRIST_KPT_SCORE_MIN）不参与速度计算**。

## 2. 参数（对齐 _manifest.json）

| 参数 | 值 |
|------|-----|
| pose_frame_interval | 2 |
| alarm_min_consecutive_frames | 3 |
| alarm_cooldown_frames | 0 |
| kpt_score_min | 0.3 |
| baseline 对照 | rule-baseline-local-prod-test |
| 记录数 | 28 |

## 3. 特征定义

| 特征 | 关键点 | 聚合 | 置信度 |
|------|--------|------|--------|
| `ankle_mean_speed` | 左踝(15)+右踝(16) | 有效点速度算术平均 | score ≥ 0.3 |
| `ankle_max_speed` | 左踝(15)+右踝(16) | 有效点速度取 max | score ≥ 0.3 |

单点速度：`speed = hypot(Δx, Δy) / Δt`，先 3 帧中值滤波再差分。
若单帧两踝均低置信度，则该帧 `ankle_*_speed` 为 `null`，门控 fail-open 不阻断。

## 4. 帧级区分度（标真重叠帧 vs baseline 误报帧）

| 特征 | 标真 P50 | 误报 P50 | 有效样本数（标真/误报） |
|------|----------|----------|-------------------------|
| `ankle_mean_speed` | 5.479 | 14.755 | 4561 / 434 |
| `ankle_max_speed` | 8.171 | 22.0 | 4561 / 434 |
| `knee_ankle_mean_speed` | 8.748 | 20.332 | 4598 / 438 |
| `lower_mean_speed` | 10.13 | 23.162 | 4611 / 439 |

踝点均值标真/误报 P50 差距约 9.3，帧级可区分；但去掉膝点后段级 FP 压制弱于膝踝联合。

## 5. 踝点特征阈值网格（相对 local baseline）

### ankle_mean_speed（左右踝算术平均）

| 阈值 ≤ | TP | FP | FN段 | 召回率 |
|--------|-----|-----|------|--------|
| 25 | 132 | 197 | 24 | 0.8462 |
| 30 | 136 | 216 | 20 | 0.8718 |
| 35 | 138 | 233 | 18 | 0.8846 |
| 40 | 140 | 250 | 16 | 0.8974 |
| 45 | 142 | 264 | 14 | 0.9103 |
| 50 | 143 | 274 | 13 | 0.9167 |
| 55 | 143 | 283 | 13 | 0.9167 |
| 60 | 143 | 295 | 13 | 0.9167 |
| 65 | 143 | 304 | 13 | 0.9167 |
| 70 | 145 | 314 | 11 | 0.9295 |
| 80 | 145 | 325 | 11 | 0.9295 |
| 100 | 145 | 337 | 11 | 0.9295 |
| 120 | 146 | 353 | 10 | 0.9359 |

**推荐**：召回需 ≥ knee@65（0.9231）时，无阈 FP 低于 298；若接受 lower@60 同级召回，**@45**（FP=264）略优。

### ankle_max_speed（左右踝取 max）

| 阈值 ≤ | TP | FP | FN段 | 召回率 |
|--------|-----|-----|------|--------|
| 25 | 125 | 149 | 31 | 0.8013 |
| 30 | 129 | 169 | 27 | 0.8269 |
| 35 | 129 | 187 | 27 | 0.8269 |
| 40 | 131 | 194 | 25 | 0.8397 |
| 45 | 134 | 205 | 22 | 0.859 |
| 50 | 136 | 212 | 20 | 0.8718 |
| 55 | 136 | 219 | 20 | 0.8718 |
| 60 | 140 | 233 | 16 | 0.8974 |
| 65 | 142 | 252 | 14 | 0.9103 |
| 70 | 143 | 259 | 13 | 0.9167 |
| 80 | 144 | 275 | 12 | 0.9231 **←推荐** |
| 100 | 144 | 295 | 12 | 0.9231 |
| 120 | 144 | 309 | 12 | 0.9231 |

推荐阈值：**80**（召回 0.9231 与 knee@65 持平，FP 275，较 knee@65 少 23）。

## 6. 对照方案（固定阈）

| 方案 | 特征 | 阈值 | TP | FP | FN段 | 召回率 |
|------|------|------|-----|-----|------|--------|
| knee@65 | `knee_ankle_mean_speed` | 65.0 | 144 | 298 | 12 | 0.9231 |
| lower@60 | `lower_mean_speed` | 60.0 | 142 | 287 | 14 | 0.9103 |
| **ankle_max@80** | `ankle_max_speed` | 80.0 | 144 | 275 | 12 | 0.9231 |
| ankle_mean@45 | `ankle_mean_speed` | 45.0 | 142 | 264 | 14 | 0.9103 |

local baseline（无过滤）：TP=147 FP=429 recall=0.9423

## 7. 漏报重点 clip 门控分析（ankle_max@80）

| clip | 漏报段 | 标真帧超阈 |
|------|--------|------------|
| `clip_0013_start_00-42-48` | 159-169 | — |
| `clip_0020_start_00-48-44` | 79-82 | 1帧(79) |
| `clip_0013_start_00-29-53` | 2244-2254 | 5帧超阈 |

ankle_max@80 与 knee@65 漏报段数相同（12），`clip_0020` 79–82 仍受门控影响。

## 8. 结论

1. **`ankle_max_speed@80`**：在保持 knee@65 同级召回时 FP 275（−23），为踝点单独门控的最优阈。
2. **`ankle_mean_speed`**：均值更稳但段级压制不足；召回≥knee@65 时无法优于 FP=298；@45 仅在与 lower@60 同级召回时 FP 略低。
3. **不建议单独替代 knee@65**：膝点提供蹲起/行走判别，踝点单独使用对 `clip_0020` 等漏报无改善。
4. **后续**：可将 `ankle_max_speed` 与 knee@65 组合或作二级门控再验证。

## 9. 导出包（prod-test，对齐 baseline manifest）

| 方案 | 目录 | 特征 | 阈值 | TP | FP | FN | 召回 |
|------|------|------|------|-----|-----|-----|------|
| baseline | `localdata/export/rule-baseline-local-prod-test` | — | — | 147 | 429 | 9 | 94.23% |
| **ankle_mean@45** | `localdata/export/rule-speed-prefilter-ankle-mean45-prod-test` | `ankle_mean_speed` | 45 | 142 | 264 | 14 | 91.03% |
| **ankle_max@80** | `localdata/export/rule-speed-prefilter-ankle-max80-prod-test` | `ankle_max_speed` | 80 | 144 | 275 | 12 | 92.31% |

参数：`pose_frame_interval=2`, `alarm_min_consecutive_frames=3`, `alarm_cooldown_frames=0`

对比报告（相对 baseline）：

- `localdata/export/compare_rule-speed-prefilter-ankle-mean45-prod-test_vs_rule-baseline-local-prod-test.md`
- `localdata/export/compare_rule-speed-prefilter-ankle-max80-prod-test_vs_rule-baseline-local-prod-test.md`

导出命令：

```bash
python scripts/data/export_prefilter_upload.py \
  --baseline-manifest localdata/export/rule-baseline-local-prod-test/_manifest.json \
  --output-dir localdata/export/rule-speed-prefilter-ankle-mean45-prod-test \
  --feature ankle_mean_speed --threshold 45

python scripts/data/export_prefilter_upload.py \
  --baseline-manifest localdata/export/rule-baseline-local-prod-test/_manifest.json \
  --output-dir localdata/export/rule-speed-prefilter-ankle-max80-prod-test \
  --feature ankle_max_speed --threshold 80

python scripts/data/evaluate_inference_upload.py \
  --dirs localdata/export/rule-speed-prefilter-ankle-mean45-prod-test \
           localdata/export/rule-speed-prefilter-ankle-max80-prod-test --in-place
```

## 10. ankle_max@80 + triple90 组合（替代 knee@65）

规则：`ankle_max_speed > 80` 且未同时满足 arm_torso≥90 AND elbow≥150 AND wrist_elev≥60 → 门控跳过。

| 方案 | TP | FP | FN | 召回 |
|------|-----|-----|-----|------|
| ankle_max@80 | 144 | 275 | 12 | 92.31% |
| **ankle_max@80 + triple90** | **145** | 290 | **11** | **92.95%** |
| knee@65 + triple90（对照） | 145 | 310 | 11 | 92.95% |

相对纯 ankle_max@80：FN −1（恢复 1 段），FP +15。

对比 MD：`localdata/export/compare_rule-speed-prefilter-ankle-max80-triple90-prod-test_vs_rule-speed-prefilter-ankle-max80-prod-test.md`

导出：

```bash
python scripts/data/export_prefilter_triple_angle_upload.py \
  --speed-feature ankle_max_speed --speed-threshold 80 \
  --output-dir localdata/export/rule-speed-prefilter-ankle-max80-triple90-prod-test
```

## 11. 站立姿态豁免（ankle_max@80 + triple90 + 肩髋踝）

**动机**：相对 baseline 仍多 2 段漏报，多为蹲取时踝速高但 triple90 未满足。采用 **∠(肩, 髋, 踝)** 上下半身整体夹角判定站立，仅站立时速度门控可 block。

**门控逻辑**：

```
block = ankle_max_speed > 80
    AND NOT triple90
    AND torso_leg_angle_mean >= 160   # 肩-髋-踝，蹲姿豁免
```

**特征字段**（`event_engine/skeleton_angles.py`）：
- `left/right_torso_leg_angle`、`torso_leg_angle_mean/min/max`
- `center_torso_leg_angle`（肩中-髋中-踝中）
- 辅助：`knee_angle_mean/min/max`、`leg_span_ratio`、`hip_knee_ankle_vertical_ratio`

**28-clip 实验结果**（`validate_prefilter_stance_exempt28.py`）：

| 方案 | TP | FP | FN | 召回 |
|------|-----|-----|-----|------|
| baseline | 147 | 429 | 9 | 94.23% |
| ankle_max@80 + triple90 | 145 | 290 | 11 | 92.95% |
| ankle_max@80 + triple90 + knee≥120 | 146 | 294 | 10 | 93.59% |
| **ankle_max@80 + triple90 + torso≥160** | **146** | **311** | **10** | **93.59%** |

相对 triple90：FN −1，FP +21；**未追平 baseline FN=9**。

**筛查结论**（`screen_prefilter_stance_proxy28.py`）：
- 俯视 2D 下 `torso_leg_angle_mean` P50 标真/误报均 ~170°，区分度弱
- `torso_leg_angle_min` 对蹲姿略敏感（P50 标真 164° vs 误报 164°）
- 膝角 `knee_angle_mean` P50 亦 ~174°，已弃用为主站立代理

**导出包（当前采用）**：

```
localdata/export/rule-speed-prefilter-ankle-max80-triple90-torso160-prod-test/
```

```bash
python scripts/data/export_prefilter_triple_angle_upload.py \
  --speed-feature ankle_max_speed --speed-threshold 80 \
  --stance-feature torso_leg_angle_mean --stance-threshold 160 \
  --output-dir localdata/export/rule-speed-prefilter-ankle-max80-triple90-torso160-prod-test
```

对比 MD：
- `localdata/export/compare_rule-speed-prefilter-ankle-max80-triple90-torso160-prod-test_vs_rule-baseline-local-prod-test.md`
- `localdata/export/compare_rule-speed-prefilter-ankle-max80-triple90-torso160-prod-test_vs_rule-speed-prefilter-ankle-max80-triple90-prod-test.md`

**待跟进**：俯视相机下肩髋踝区分仍有限；可试 `torso_leg_min`、腕抬升组合或 3D/侧面机位特征。
