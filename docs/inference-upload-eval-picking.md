# 上传推测结果评估报告

> 生成时间：2026-07-06 08:19 UTC  
> 目录：`localdata\export\rule-baseline-prod-test`  
> 规则：is_picking=true 为碰撞告警；货框 rule_alarm_collisions → rule_collisions；box_id 兼容匹配

## 汇总

| 指标 | 数值 |
|------|------|
| 上传文件数 | 28 |
| 成功评估 | 28 |
| 跳过 | 0 |
| 失败 | 0 |
| 标真段数 | 161 |
| 检出（TP） | 147 |
| 漏报（FN） | 14 |
| 误报（FP） | 423 |
| 召回率 | 91.30% |
| 漏报率 | 8.70% |
| 精确率（代理） | 25.79% |

## 分片明细

| 文件 | record_id | 状态 | 标真段 | 检出 | 漏报 | 误报 | 召回 |
|------|-----------|------|--------|------|------|------|------|
| clip_0002_start_00-00-53_rtmpose_m.json | `rtmpose-m/2-3-1/clip_0002_start_00-00-53_rtmpose_m` | ok | 1 | 1 | 0 | 4 | 100.00% |
| clip_0002_start_00-24-56_rtmpose_m.json | `rtmpose-m/2-4-1/clip_0002_start_00-24-56_rtmpose_m` | ok | 19 | 19 | 0 | 61 | 100.00% |
| clip_0002_start_00-30-28_rtmpose_m.json | `rtmpose-m/1-2-1/clip_0002_start_00-30-28_rtmpose_m` | ok | 1 | 1 | 0 | 1 | 100.00% |
| clip_0003_start_00-02-43_rtmpose_m.json | `rtmpose-m/2-2-2/clip_0003_start_00-02-43_rtmpose_m` | ok | 1 | 1 | 0 | 18 | 100.00% |
| clip_0003_start_00-39-45_rtmpose_m.json | `rtmpose-m/2-3-1/clip_0003_start_00-39-45_rtmpose_m` | ok | 1 | 0 | 1 | 20 | 0.00% |
| clip_0005_start_00-07-59_rtmpose_m.json | `rtmpose-m/2-6-1/clip_0005_start_00-07-59_rtmpose_m` | ok | 10 | 9 | 1 | 6 | 90.00% |
| clip_0006_start_00-14-11_rtmpose_m.json | `rtmpose-m/2-6-1/clip_0006_start_00-14-11_rtmpose_m` | ok | 7 | 7 | 0 | 7 | 100.00% |
| clip_0006_start_00-41-26_rtmpose_m.json | `rtmpose-m/2-2-2/clip_0006_start_00-41-26_rtmpose_m` | ok | 8 | 7 | 1 | 11 | 87.50% |
| clip_0007_start_00-22-47_rtmpose_m.json | `rtmpose-m/2-6-1/clip_0007_start_00-22-47_rtmpose_m` | ok | 1 | 1 | 0 | 0 | 100.00% |
| clip_0008_start_00-30-49_rtmpose_m.json | `rtmpose-m/2-5-1/clip_0008_start_00-30-49_rtmpose_m` | ok | 12 | 12 | 0 | 24 | 100.00% |
| clip_0009_start_00-17-17_rtmpose_m.json | `rtmpose-m/2-2-2/clip_0009_start_00-17-17_rtmpose_m` | ok | 7 | 7 | 0 | 2 | 100.00% |
| clip_0009_start_00-37-59_rtmpose_m.json | `rtmpose-m/2-5-1/clip_0009_start_00-37-59_rtmpose_m` | ok | 5 | 4 | 1 | 71 | 80.00% |
| clip_0010_start_00-18-36_rtmpose_m.json | `rtmpose-m/2-3-1/clip_0010_start_00-18-36_rtmpose_m` | ok | 5 | 4 | 1 | 5 | 80.00% |
| clip_0011_start_00-20-06_rtmpose_m.json | `rtmpose-m/2-3-1/clip_0011_start_00-20-06_rtmpose_m` | ok | 1 | 1 | 0 | 6 | 100.00% |
| clip_0011_start_00-40-16_rtmpose_m.json | `rtmpose-m/2-6-1/clip_0011_start_00-40-16_rtmpose_m` | ok | 2 | 2 | 0 | 2 | 100.00% |
| clip_0013_start_00-11-22_rtmpose_m.json | `rtmpose-m/1-1-1/clip_0013_start_00-11-22_rtmpose_m` | ok | 54 | 47 | 7 | 26 | 87.04% |
| clip_0013_start_00-29-53_rtmpose_m.json | `rtmpose-m/2-2-2/clip_0013_start_00-29-53_rtmpose_m` | ok | 3 | 3 | 0 | 16 | 100.00% |
| clip_0013_start_00-42-48_rtmpose_m.json | `rtmpose-m/2-7-2/clip_0013_start_00-42-48_rtmpose_m` | ok | 2 | 2 | 0 | 14 | 100.00% |
| clip_0014_start_00-31-27_rtmpose_m.json | `rtmpose-m/2-2-2/clip_0014_start_00-31-27_rtmpose_m` | ok | 2 | 1 | 1 | 0 | 50.00% |
| clip_0018_start_00-39-31_rtmpose_m.json | `rtmpose-m/2-2-2/clip_0018_start_00-39-31_rtmpose_m` | ok | 1 | 1 | 0 | 10 | 100.00% |
| clip_0019_start_00-40-01_rtmpose_m.json | `rtmpose-m/2-2-2/clip_0019_start_00-40-01_rtmpose_m` | ok | 1 | 1 | 0 | 35 | 100.00% |
| clip_0019_start_00-47-26_rtmpose_m.json | `rtmpose-m/2-5-1/clip_0019_start_00-47-26_rtmpose_m` | ok | 1 | 1 | 0 | 12 | 100.00% |
| clip_0020_start_00-48-44_rtmpose_m.json | `rtmpose-m/2-2-2/clip_0020_start_00-48-44_rtmpose_m` | ok | 1 | 1 | 0 | 4 | 100.00% |
| clip_0020_start_00-50-44_rtmpose_m.json | `rtmpose-m/2-5-1/clip_0020_start_00-50-44_rtmpose_m` | ok | 3 | 3 | 0 | 19 | 100.00% |
| clip_0023_start_00-49-39_rtmpose_m.json | `rtmpose-m/2-3-1/clip_0023_start_00-49-39_rtmpose_m` | ok | 7 | 6 | 1 | 35 | 85.71% |
| clip_0025_start_00-43-15_rtmpose_m.json | `rtmpose-m/1-1-1/clip_0025_start_00-43-15_rtmpose_m` | ok | 2 | 2 | 0 | 2 | 100.00% |
| clip_0025_start_00-50-13_rtmpose_m.json | `rtmpose-m/2-2-2/clip_0025_start_00-50-13_rtmpose_m` | ok | 1 | 1 | 0 | 4 | 100.00% |
| clip_0028_start_00-33-12_rtmpose_m.json | `rtmpose-m/1-2-1/clip_0028_start_00-33-12_rtmpose_m` | ok | 2 | 2 | 0 | 8 | 100.00% |
