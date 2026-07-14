# 对比报告：末行排除 vs ankle@80 + triple90 + shknee140

> 生成时间：2026-07-14  
> **基准（baseline）**：`rule-speed-prefilter-ankle-max80-triple90-shknee140-prod-test`  
> **实验（experiment）**：`rule-speed-prefilter-ankle-max80-triple90-shknee140-excl-bottomrow-prod-test`  
> eval_id：基准 `20260714_102254_6a310e83` · 实验 `20260714_102631_5a2cb454`

## 1. 规则差异

| 项 | 基准 shknee140 | 实验 excl-bottomrow |
|----|----------------|---------------------|
| 速度门控 | ankle_max_speed > 80 | 同左 |
| 手部豁免 | triple90 AND | 同左 |
| 站立门控 | shoulder_hip_knee_angle_min ≥ 140 | 同左 |
| **末行碰撞** | 全部货位检测 | **站立时跳过每 shelf 末行**（`max(layer)` per shelf） |

参数对齐：`pose_frame_interval=2`，`alarm_min=3`，`cooldown=0`。

## 2. 系统级指标（28-clip）

| 指标 | 基准 | 实验 | Δ（实验−基准） |
|------|------|------|----------------|
| 标真段 | 156 | 156 | — |
| TP | 146 | 143 | −3 |
| FN | 10 | 13 | **+3** |
| FP | 310 | 217 | **−93** |
| 召回率 | 93.59% | 91.67% | −1.92pp |
| 精确率代理¹ | 32.02% | 39.72% | +7.70pp |

¹ TP / (TP + FP)

**小结**：末行排除在 **零新增误报** 前提下消除 **93 次 FP**；代价 **+3 段 FN**（末行真取货 + 站立姿态）。

## 3. 误报变化

| 项 | 数量 |
|----|------|
| 减少的误报（仅基准有） | **93** |
| 新增的误报（仅实验有） | **0** |

典型消除（末行 token）：

| clip | 机位 | 货框 | 说明 |
|------|------|------|------|
| clip_0009_start_00-37-59 | 2-5-1 | Box_4044 | 帧 5635–5673 连续误报簇（shelf 91 末行） |
| clip_0006_start_00-14-11 | 2-6-1 | Box_3093 | 末行站立路过 |
| clip_0023_start_00-49-39 | 2-3-1 | Box_4025/4026 | 末行投影误触 |

## 4. 漏报变化

| 项 | 数量 |
|----|------|
| 减少的漏报（仅基准有） | 0 |
| 新增的漏报（仅实验有） | **3** |

| clip | 机位 | 帧区间 | 标真货框 |
|------|------|--------|----------|
| clip_0009_start_00-17-17 | 2-2-2 | 1–19 | Box_4018 |
| clip_0009_start_00-17-17 | 2-2-2 | 269–291 | Box_4016 |
| clip_0023_start_00-49-39 | 2-3-1 | 376–444 | Box_4026 |

## 5. 与 torso160+末行排除横向对比

| 指标 | torso160+末行 | shknee140+末行 |
|------|---------------|----------------|
| FN | 18 | **13** |
| FP | 218 | **217** |
| 召回 | 88.46% | **91.67%** |

同末行策略下 shknee140 漏报更少、召回更高，FP 几乎相同。

## 6. 复现

```bash
python scripts/data/compare_export_false_alarms.py \
  --baseline localdata/export/rule-speed-prefilter-ankle-max80-triple90-shknee140-prod-test \
  --experiment localdata/export/rule-speed-prefilter-ankle-max80-triple90-shknee140-excl-bottomrow-prod-test
```

完整逐事件明细：`localdata/export/compare_rule-speed-prefilter-ankle-max80-triple90-shknee140-excl-bottomrow-prod-test_vs_rule-speed-prefilter-ankle-max80-triple90-shknee140-prod-test.md`

实验记录：`docs/prefilter-ankle80-triple90-shknee140-excl-bottomrow-experiment.md`
