# 前置门控特征对比：膝踝平均速度 vs 下半躯体平均速度

> 生成时间：2026-07-10T05:16:08.072081+00:00
> 脚本：`scripts/data/validate_prefilter_knee_ankle28.py`

## 1. 实验目的

验证用 **膝+踝 4 点算术平均**（`knee_ankle_mean_speed`）替代 **髋+膝+踝 6 点算术平均**（`lower_mean_speed`）
作为前置速度门控特征，能否缓解下蹲/起身过快被误过滤的问题。

> 说明：原 `lower_mean_speed` 已是算术平均（非求和）；本次变化是 **去掉髋部 2 点**，降低蹲起时髋部位移对门控的影响。

## 2. 参数（与先前一致）

| 参数 | 值 |
|------|-----|
| pose_frame_interval | 2 |
| alarm_min_consecutive_frames | 3 |
| alarm_cooldown_frames | 0 |
| max_threshold | 60.0 |
| baseline 对照 | rule-baseline-local-prod-test（本仓库重算） |
| 记录数 | 28 |

## 3. 特征定义

| 特征 | 关键点 | 聚合 |
|------|--------|------|
| `lower_mean_speed` | 髋(11,12)+膝(13,14)+踝(15,16) | 有效点速度算术平均 |
| `knee_ankle_mean_speed` | 膝(13,14)+踝(15,16) | 有效点速度算术平均 |

单点速度：`speed = hypot(Δx, Δy) / Δt`，先 3 帧中值滤波再差分。

## 4. 帧级区分度（标真重叠帧 vs baseline 误报帧）

| 特征 | 标真 P50 | 误报 P50 |
|------|----------|----------|
| `lower_mean_speed` | 10.13 | 23.162 |
| `knee_ankle_mean_speed` | 8.748 | 20.332 |

## 5. 阈值网格（前置，相对 local baseline）

### lower_mean_speed（髋+膝+踝 6 点算术平均）

| 阈值 ≤ | TP | FP | 召回率 |
|--------|-----|-----|--------|
| 30 | 128 | 206 | 0.8205 |
| 40 | 136 | 229 | 0.8718 |
| 50 | 140 | 259 | 0.8974 |
| 60 | 142 | 287 | 0.9103 |
| 70 | 142 | 308 | 0.9103 |
| 80 | 142 | 322 | 0.9103 |
| 100 | 145 | 353 | 0.9295 |
| 120 | 145 | 369 | 0.9295 |

### knee_ankle_mean_speed（膝+踝 4 点算术平均（不含髋））

| 阈值 ≤ | TP | FP | 召回率 |
|--------|-----|-----|--------|
| 30 | 133 | 198 | 0.8526 |
| 40 | 140 | 229 | 0.8974 |
| 50 | 142 | 261 | 0.9103 |
| 60 | 143 | 286 | 0.9167 |
| 70 | 145 | 310 | 0.9295 |
| 80 | 145 | 319 | 0.9295 |
| 100 | 145 | 342 | 0.9295 |
| 120 | 146 | 363 | 0.9359 |

## 6. 阈值 60 对比汇总

| 特征 | TP | FP | FN段 | 召回率 |
|------|-----|-----|------|--------|
| `lower_mean_speed` | 142 | 287 | 14 | 0.9103 |
| `knee_ankle_mean_speed` | 143 | 286 | 13 | 0.9167 |

local baseline（无过滤）：TP=147 FP=429 recall=0.9423

## 7. 昨日漏报重点 clip 复查

| clip | 特征 | 漏报段 | 标真帧门控分析 |
|------|------|--------|----------------|
| `clip_0009_start_00-37-59_rtmpose_m.json` | lower_mean_speed | 91-111 (Box_3079) | lower_mean_speed: 1帧超阈(95); knee_ankle_mean_speed: 1帧超阈(95);  |
| `clip_0013_start_00-42-48_rtmpose_m.json` | lower_mean_speed | 109-131 (Box_3100); 159-169 (Box_3105) | lower_mean_speed: 5帧超阈(109,113,115,119,123); knee_ankle_mean_speed: 4帧超阈(113,115,119,123);  |
| `clip_0020_start_00-48-44_rtmpose_m.json` | lower_mean_speed | 79-82 (Box_3018) | lower_mean_speed: 1帧超阈(79); knee_ankle_mean_speed: 1帧超阈(79);  |
| `clip_0013_start_00-29-53_rtmpose_m.json` | lower_mean_speed | 2244-2254 (Box_4014) | lower_mean_speed: 4帧超阈(2245,2247,2249,2251); knee_ankle_mean_speed: 4帧超阈(2245,2247,2249,2251);  |
| `clip_0009_start_00-37-59_rtmpose_m.json` | knee_ankle_mean_speed | 91-111 (Box_3079) | lower_mean_speed: 1帧超阈(95); knee_ankle_mean_speed: 1帧超阈(95);  |
| `clip_0013_start_00-42-48_rtmpose_m.json` | knee_ankle_mean_speed | 159-169 (Box_3105) | — |
| `clip_0020_start_00-48-44_rtmpose_m.json` | knee_ankle_mean_speed | 79-82 (Box_3018) | lower_mean_speed: 1帧超阈(79); knee_ankle_mean_speed: 1帧超阈(79);  |
| `clip_0013_start_00-29-53_rtmpose_m.json` | knee_ankle_mean_speed | 2244-2254 (Box_4014) | lower_mean_speed: 4帧超阈(2245,2247,2249,2251); knee_ankle_mean_speed: 4帧超阈(2245,2247,2249,2251);  |

## 8. 结论

膝踝平均特征在阈值 60 下召回改善：漏报段 14→13（少 1 段），FP 287→286。建议前置门控改用 `knee_ankle_mean_speed`。
