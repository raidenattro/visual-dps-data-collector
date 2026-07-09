# 全骨骼速度特征过滤（rule-baseline-prod-test 28 条验证）

> 生成时间：2026-07-09
> 验证脚本：`scripts/data/validate_baseline28_subsampled_velocity.py`
> 数据 JSON：`localdata/export/rule-baseline-prod-test/skeleton_velocity_speed_only.json`
> 实现模块：`event_engine/skeleton_features.py`

---

## 1. 背景与目标

**问题**：人走过货位时手腕短暂进框，baseline 生产规则（`is_picking`）产生大量误报。

**假设**：停下拣货时躯干/下肢近静止，只有手腕快速伸入；走过时全身持续移动。

**目标**：基于全骨骼速度特征做段级二次过滤，在 TP 损失可控前提下降低 FP。

**策略**：仅使用速度特征上限过滤，**不使用** Combo1、位移、帧数、持续时间等规则。

---

## 2. 特征提取方式

实现见 `event_engine/skeleton_features.py`，分三层：**单点速度 → 帧级聚合 → 段级统计**。

### 2.1 单点速度（COCO-17 关键点）

| 步骤 | 说明 |
|------|------|
| 置信度过滤 | 关键点 `score ≥ 0.3`（`WRIST_KPT_SCORE_MIN`）才参与 |
| 位置平滑 | 最近 **3 帧**坐标做**中值滤波**（median filter），抑制姿态抖动 |
| 速度计算 | `speed = hypot(Δx, Δy) / Δt`，单位 **px/s**（像素/秒） |
| 时间间隔 | 优先用 `timestamp_sec` 差分；无效时用 `(frame_idx 差) / 25` |
| 断链 | 相邻帧间隔 `> 2` 时重置历史，避免跳帧产生虚假高速 |
| 归一化 | `speed_norm = speed / hypot(infer_width, infer_height)`（分析未用） |

### 2.2 帧级聚合特征

| 特征 | 定义 |
|------|------|
| `torso_speed` | 躯干锚点速度：优先**双肩中心**，不可用则**双髋中心** |
| `lower_mean_speed` | 下肢 6 点（髋/膝/踝，索引 11–16）有效速度的算术平均 |
| `upper_mean_speed` | 上肢 6 点（肩/肘/腕，索引 5–10）有效速度的算术平均 |
| `body_mean_speed` | 全部 17 点有效速度的算术平均 |
| `body_max_speed` | 17 点速度最大值 |
| `wrist_max_speed` | 左右腕速度最大值 |
| `wrist_torso_ratio` | `wrist_max_speed / (torso_speed + 1e-3)` |

### 2.3 段级统计（碰撞段 `[frame_enter, frame_exit]`）

对段内每一帧（按 `person_track_id` 匹配）收集帧级速度后统计：

| 段级特征 | 定义 |
|----------|------|
| `torso_speed_p50` | 段内 `torso_speed` 的 P50（中位数） |
| `torso_speed_max` | 段内 `torso_speed` 的最大值 |
| `lower_mean_speed_p50` | 段内 `lower_mean_speed` 的 P50 |
| `body_mean_speed_p50` | 段内 `body_mean_speed` 的 P50 |
| `wrist_torso_ratio_p50` | 段内 `wrist_torso_ratio` 的 P50 |
| `motion_valid_ratio` | 有有效躯干速度的帧数 / 段总帧数 |

### 2.4 抽帧约定（贴合现场）

现场导出 `pose_frame_interval=2`，速度**仅在导出抽帧序列**上差分（`extract_subsampled_velocity_from_frames`），不在全帧序列上计算。相邻两帧实际间隔约 2 个 source frame，速度数值低于全帧差分。

### 2.5 过滤语义

```
保留告警 if 碰撞段 feature ≤ threshold
```

- **低速度** → 躯干/下肢近静止 → 更像停下拣货 → **保留**
- **高速度** → 全身在移动 → 更像走过误报 → **丢弃**
- 段级特征为 `None`（无运动数据）→ **保留**（不误杀）

---

## 3. 验证方法

| 项 | 约定 |
|----|------|
| 数据集 | `localdata/export/rule-baseline-prod-test/_manifest.json` 28 条记录 |
| 告警来源 | `clip_*.json` 的 `is_picking` + `rule_alarm_collisions`（baseline 生产规则） |
| 标真来源 | `event_review.verified_true` |
| 误报段定义 | 碰撞段与 baseline 误报帧重叠，且未被标真覆盖 |
| baseline 参数 | `alarm_min=3`, `cooldown=0` |
| 评估指标 | 相对 baseline 原始告警的 TP / FP / FP 下降率 / TP 损失率 |

---

## 4. 验证结论

### 4.1 方案可行

全骨骼速度特征能有效区分「停下拣货」与「走过误报」：

- 段级 F1 约 **0.85**，显著优于单用手腕帧级速度（约 0.17）
- 误报段躯干/下肢速度明显高于标真段，验证假设成立

### 4.2 baseline 现状

| 指标 | 数值 |
|------|------|
| TP | 147 |
| FP | 410 |
| 精确率代理 | 26.4% |

### 4.3 段级区分度（标真 vs 误报）

| 特征 | 标真 P50 | 误报 P50 | 最佳阈值 | F1 |
|------|----------|----------|----------|-----|
| **lower_mean_speed_p50** | 18.58 | 29.67 | ≤60 | **0.8474** |
| torso_speed_p50 | 18.23 | 48.64 | ≤60 | 0.8361 |
| torso_speed_max | 65.81 | 80.07 | ≤150 | 0.8132 |
| wrist_torso_ratio_p50 | 1.89 | 2.13 | ≥1.0 | 0.7397 |

**段级区分度最优：`lower_mean_speed_p50`**

### 4.4 告警级纯速度过滤（相对 baseline）

| 规则 | TP | FP | FP 下降 | TP 损失 |
|------|-----|-----|---------|---------|
| baseline（无过滤） | 147 | 410 | — | — |
| lower_mean_speed_p50 ≤ 60 | 144 | 333 | 18.8% | **2.0%** |
| torso_speed_p50 ≤ 60 | 141 | 310 | 24.4% | 4.1% |
| body_mean_speed_p50 ≤ 60 | 141 | 292 | **28.8%** | 4.1% |

在 TP 损失 ≤5% 约束下，告警级 FP 下降最多的是 `body_mean_speed_p50`；段级区分度最优仍是 `lower_mean_speed_p50`。

### 4.5 推荐规则

**生产建议（平衡版，TP 损失更低）：**

```
保留告警 if 碰撞段 lower_mean_speed_p50 ≤ 60
```

- TP=144，FP=333，FP 降 18.8%，TP 损 2.0%

**若更看重压误报（激进版）：**

```
保留告警 if 碰撞段 body_mean_speed_p50 ≤ 60
```

- TP=141，FP=292，FP 降 28.8%，TP 损 4.1%

### 4.6 明确不采用

- Combo1（位移/帧数/持续时间）：纯速度已能独立起作用，本次验证不叠加
- 单手腕速度：对「走过误报」区分度不足
- 过严阈值（如 ≤20）：FP 可大幅下降，但 TP 损失 40%+，不可接受

### 4.7 局限与风险

- 样本仅 28 条，阈值 60 需在更大数据集复核
- 速度过滤后 FP 仍有 292~333，是**辅助手段**，不能单独解决全部误报
- 部分标真碰撞有 2%~4% TP 损失，上线前建议抽查损失 case

---

## 5. 阈值网格（告警级）

baseline 原始：TP=147 FP=410

### torso_speed_p50 上限

| 阈值 ≤ | TP | FP | FP下降 | TP损失 | 精确率代理 |
|--------|-----|-----|--------|--------|------------|
| 10 | 49 | 135 | 0.6707 | 0.6667 | 0.2663 |
| 20 | 88 | 185 | 0.5488 | 0.4014 | 0.3223 |
| 30 | 115 | 216 | 0.4732 | 0.2177 | 0.3474 |
| 40 | 125 | 259 | 0.3683 | 0.1497 | 0.3255 |
| 50 | 135 | 285 | 0.3049 | 0.0816 | 0.3214 |
| 60 | 141 | 310 | 0.2439 | 0.0408 | 0.3126 |
| 80 | 145 | 348 | 0.1512 | 0.0136 | 0.2941 |
| 100 | 147 | 376 | 0.0829 | 0.0 | 0.2811 |
| 120 | 147 | 380 | 0.0732 | 0.0 | 0.2789 |
| 150 | 147 | 393 | 0.0415 | 0.0 | 0.2722 |

### lower_mean_speed_p50 上限

| 阈值 ≤ | TP | FP | FP下降 | TP损失 | 精确率代理 |
|--------|-----|-----|--------|--------|------------|
| 10 | 58 | 114 | 0.722 | 0.6054 | 0.3372 |
| 20 | 89 | 176 | 0.5707 | 0.3946 | 0.3358 |
| 30 | 115 | 265 | 0.3537 | 0.2177 | 0.3026 |
| 40 | 130 | 288 | 0.2976 | 0.1156 | 0.311 |
| 50 | 137 | 308 | 0.2488 | 0.068 | 0.3079 |
| 60 | 144 | 333 | 0.1878 | 0.0204 | 0.3019 |
| 80 | 146 | 356 | 0.1317 | 0.0068 | 0.2908 |
| 100 | 147 | 376 | 0.0829 | 0.0 | 0.2811 |
| 120 | 147 | 396 | 0.0341 | 0.0 | 0.2707 |
| 150 | 147 | 401 | 0.022 | 0.0 | 0.2682 |

### torso_speed_max 上限

| 阈值 ≤ | TP | FP | FP下降 | TP损失 | 精确率代理 |
|--------|-----|-----|--------|--------|------------|
| 40 | 41 | 92 | 0.7756 | 0.7211 | 0.3083 |
| 60 | 62 | 153 | 0.6268 | 0.5782 | 0.2884 |
| 80 | 89 | 228 | 0.4439 | 0.3946 | 0.2808 |
| 100 | 120 | 291 | 0.2902 | 0.1837 | 0.292 |
| 120 | 131 | 330 | 0.1951 | 0.1088 | 0.2842 |
| 150 | 142 | 359 | 0.1244 | 0.034 | 0.2834 |
| 200 | 146 | 397 | 0.0317 | 0.0068 | 0.2689 |

### body_mean_speed_p50 上限

| 阈值 ≤ | TP | FP | FP下降 | TP损失 | 精确率代理 |
|--------|-----|-----|--------|--------|------------|
| 20 | 72 | 136 | 0.6683 | 0.5102 | 0.3462 |
| 40 | 122 | 242 | 0.4098 | 0.1701 | 0.3352 |
| 60 | 141 | 292 | 0.2878 | 0.0408 | 0.3256 |
| 80 | 145 | 342 | 0.1659 | 0.0136 | 0.2977 |
| 100 | 146 | 369 | 0.1 | 0.0068 | 0.2835 |
| 120 | 147 | 379 | 0.0756 | 0.0 | 0.2795 |
| 150 | 147 | 392 | 0.0439 | 0.0 | 0.2727 |

---

## 6. 分片摘要

| record_id | 抽帧数 | baseline告警 | 误报段 | 标真段 |
|-----------|--------|-------------|--------|--------|
| `rtmpose-m/1-1-1/clip_0013_start_00-11-22_rtmpose_m` | 4120 | 445 | 11 | 50 |
| `rtmpose-m/1-1-1/clip_0025_start_00-43-15_rtmpose_m` | 99 | 25 | 1 | 2 |
| `rtmpose-m/1-2-1/clip_0002_start_00-30-28_rtmpose_m` | 3030 | 43 | 0 | 1 |
| `rtmpose-m/1-2-1/clip_0028_start_00-33-12_rtmpose_m` | 403 | 37 | 3 | 2 |
| `rtmpose-m/2-2-2/clip_0003_start_00-02-43_rtmpose_m` | 96 | 32 | 2 | 1 |
| `rtmpose-m/2-2-2/clip_0006_start_00-41-26_rtmpose_m` | 396 | 92 | 3 | 8 |
| `rtmpose-m/2-2-2/clip_0009_start_00-17-17_rtmpose_m` | 156 | 77 | 2 | 10 |
| `rtmpose-m/2-2-2/clip_0013_start_00-29-53_rtmpose_m` | 169 | 50 | 5 | 4 |
| `rtmpose-m/2-2-2/clip_0014_start_00-31-27_rtmpose_m` | 233 | 21 | 0 | 7 |
| `rtmpose-m/2-2-2/clip_0018_start_00-39-31_rtmpose_m` | 183 | 15 | 1 | 7 |
| `rtmpose-m/2-2-2/clip_0019_start_00-40-01_rtmpose_m` | 202 | 49 | 6 | 3 |
| `rtmpose-m/2-2-2/clip_0020_start_00-48-44_rtmpose_m` | 265 | 6 | 1 | 1 |
| `rtmpose-m/2-2-2/clip_0025_start_00-50-13_rtmpose_m` | 383 | 7 | 1 | 1 |
| `rtmpose-m/2-3-1/clip_0002_start_00-00-53_rtmpose_m` | 245 | 26 | 1 | 1 |
| `rtmpose-m/2-3-1/clip_0003_start_00-39-45_rtmpose_m` | 2445 | 20 | 3 | 0 |
| `rtmpose-m/2-3-1/clip_0010_start_00-18-36_rtmpose_m` | 358 | 36 | 0 | 6 |
| `rtmpose-m/2-3-1/clip_0011_start_00-20-06_rtmpose_m` | 4774 | 18 | 3 | 2 |
| `rtmpose-m/2-3-1/clip_0023_start_00-49-39_rtmpose_m` | 299 | 120 | 9 | 13 |
| `rtmpose-m/2-4-1/clip_0002_start_00-24-56_rtmpose_m` | 11583 | 216 | 11 | 21 |
| `rtmpose-m/2-5-1/clip_0008_start_00-30-49_rtmpose_m` | 3184 | 163 | 1 | 14 |
| `rtmpose-m/2-5-1/clip_0009_start_00-37-59_rtmpose_m` | 7734 | 100 | 9 | 8 |
| `rtmpose-m/2-5-1/clip_0019_start_00-47-26_rtmpose_m` | 2325 | 32 | 4 | 3 |
| `rtmpose-m/2-5-1/clip_0020_start_00-50-44_rtmpose_m` | 842 | 62 | 4 | 3 |
| `rtmpose-m/2-6-1/clip_0005_start_00-07-59_rtmpose_m` | 2621 | 92 | 1 | 12 |
| `rtmpose-m/2-6-1/clip_0006_start_00-14-11_rtmpose_m` | 3340 | 80 | 2 | 7 |
| `rtmpose-m/2-6-1/clip_0007_start_00-22-47_rtmpose_m` | 249 | 8 | 0 | 1 |
| `rtmpose-m/2-6-1/clip_0011_start_00-40-16_rtmpose_m` | 1119 | 36 | 1 | 2 |
| `rtmpose-m/2-7-2/clip_0013_start_00-42-48_rtmpose_m` | 69 | 16 | 5 | 4 |

---

## 7. 相关文件

| 文件 | 说明 |
|------|------|
| `event_engine/skeleton_features.py` | 速度特征提取与段级统计 |
| `event_engine/speed_filter.py` | 段级速度过滤与 clip JSON 回写 |
| `scripts/data/validate_baseline28_subsampled_velocity.py` | 28 条纯速度验证脚本 |
| `scripts/data/export_speed_filtered_upload.py` | **导出速度过滤后的上传目录** |
| `scripts/data/run_manifest28_skeleton_extract.py` | 批量特征提取 |
| `api/skeleton_features_service.py` | Parquet 写入与 API |

---

## 8. 导出速度过滤目录（保守规则）

将 baseline 上传目录过滤后导出为同格式 JSON 文件夹，可直接用于准确率页上传或 `evaluate_inference_upload.py` 对比。

### 命令

```bash
python scripts/data/export_speed_filtered_upload.py
python scripts/data/evaluate_inference_upload.py \
  --dirs localdata/export/rule-baseline-prod-test \
           localdata/export/rule-speed-lower60-prod-test --in-place
```

### 默认参数

- 输入：`localdata/export/rule-baseline-prod-test`
- 输出：`localdata/export/rule-speed-lower60-prod-test`
- 规则：`lower_mean_speed_p50 ≤ 60`

### 实测对比（2026-07-09）

| 目录 | TP（检出） | FP（误报） | 召回率 |
|------|-----------|-----------|--------|
| rule-baseline-prod-test | 147 | 410 | 94.23% |
| rule-speed-lower60-prod-test | 144 | 333 | 92.31% |

与离线验证结论一致（FP 降 18.8%，TP 损 2.0%）。
