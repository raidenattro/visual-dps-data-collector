# 对比报告：末行排除 vs ankle@80 + triple90 + torso160

> 生成时间：2026-07-14  
> **基准（baseline）**：`rule-speed-prefilter-ankle-max80-triple90-torso160-prod-test`  
> **实验（experiment）**：`rule-speed-prefilter-ankle-max80-triple90-torso160-excl-bottomrow-prod-test`  
> eval_id：基准 `20260712_123340_52cee1d0` · 实验 `20260714_094919_1c5d0962`

## 1. 规则差异

| 项 | 基准 torso160 | 实验 excl-bottomrow |
|----|---------------|---------------------|
| 速度门控 | ankle_max_speed > 80 | 同左 |
| 手部豁免 | triple90 AND | 同左 |
| 站立门控 | torso_leg_angle_mean ≥ 160 才可 block | 同左 |
| **末行碰撞** | 全部货位检测 | **站立时跳过每 shelf 末行**（`max(layer)` per shelf） |

参数对齐：`pose_frame_interval=2`，`alarm_min=3`，`cooldown=0`。

## 2. 系统级指标（28-clip）

| 指标 | 基准 | 实验 | Δ（实验−基准） |
|------|------|------|----------------|
| 标真段 | 156 | 156 | — |
| TP | 146 | 138 | −8 |
| FN | 10 | 18 | **+8** |
| FP | 311 | 218 | **−93** |
| 召回率 | 93.59% | 88.46% | −5.13pp |
| 精确率代理¹ | 31.95% | 38.76% | +6.81pp |

¹ TP / (TP + FP)

**小结**：末行排除在 **零新增误报** 前提下消除 **93 次 FP**；代价 **+8 段 FN**（多为末行真取货 + 站立姿态）。

## 3. 误报变化

| 项 | 数量 |
|----|------|
| 减少的误报（仅基准有） | **93** |
| 新增的误报（仅实验有） | **0** |

### 3.1 按 clip 消除误报（节选）

| clip | 机位 | 消除次数 | 典型末行 token |
|------|------|----------|----------------|
| `clip_0009_start_00-37-59` | 2-5-1 | **28** | Box_4044、Box_4045、Box_3079 |
| `clip_0023_start_00-49-39` | 2-3-1 | 21 | Box_4025、Box_4026 |
| `clip_0019_start_00-40-01` | 2-2-2 | 16 | Box_4017 等 |
| `clip_0008_start_00-30-49` | 2-5-1 | 2 | Box_3080 |
| `clip_0020_start_00-50-44` | 2-5-1 | 3 | Box_3080 |

`clip_0009` 帧 5635–5673 的 `Box_4044` 站立投影误报已全部消除。

## 4. 漏报变化

| 项 | 数量 |
|----|------|
| 减少的漏报 | 0 |
| **新增的漏报** | **8** |

| clip | 机位 | 帧区间 | 标真货框 | 说明 |
|------|------|--------|----------|------|
| `clip_0006_start_00-14-11` | 2-6-1 | 739–755 | Box_3096 | 末行取货 |
| `clip_0006_start_00-41-26` | 2-2-2 | 638–662 | Box_3029 | 末行取货 |
| `clip_0007_start_00-22-47` | 2-6-1 | 1–20 | Box_3096 | 末行取货 |
| `clip_0009_start_00-17-17` | 2-2-2 | 1–19 | Box_4018 | 末行 |
| `clip_0009_start_00-17-17` | 2-2-2 | 269–291 | Box_4016 | 末行 |
| `clip_0013_start_00-11-22` | 1-1-1 | 4207–4231 | **Box_2015** | 货架 82 末行 |
| `clip_0013_start_00-11-22` | 1-1-1 | 4343–4368 | **Box_2016** | 货架 82 末行 |
| `clip_0023_start_00-49-39` | 2-3-1 | 376–444 | Box_4026 | 末行 |

## 5. 结论

| 维度 | 评价 |
|------|------|
| FP | 显著改善（−93，约 −30%） |
| FN | 明显恶化（+8），末行站立取货被误杀 |
| 适用 | 适合作为「压误报」试验分支；上线需末行+蹲姿/取货动作联合保护 |

## 6. 复现与明细

```bash
python scripts/data/compare_export_false_alarms.py \
  --baseline localdata/export/rule-speed-prefilter-ankle-max80-triple90-torso160-prod-test \
  --experiment localdata/export/rule-speed-prefilter-ankle-max80-triple90-torso160-excl-bottomrow-prod-test
```

完整逐条误报/漏报列表：

- `localdata/export/compare_rule-speed-prefilter-ankle-max80-triple90-torso160-excl-bottomrow-prod-test_vs_rule-speed-prefilter-ankle-max80-triple90-torso160-prod-test.md`
- `localdata/export/rule-speed-prefilter-ankle-max80-triple90-torso160-excl-bottomrow-prod-test误报漏报情况.md`

实验说明：`docs/prefilter-ankle80-triple90-torso160-excl-bottomrow-experiment.md`
