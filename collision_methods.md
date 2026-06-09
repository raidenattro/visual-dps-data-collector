# 碰撞方法逻辑说明

当前碰撞检测通过 `collision_method` 切换方法：

```text
wrist_point  旧逻辑：手腕点连续帧
hand_state   新逻辑：同手同箱状态机
```

## 1. wrist_point

`wrist_point` 只判断手腕点是否进入货箱多边形。

### 帧级碰撞

每帧读取左右手腕：

```text
left_wrist  = keypoints[9]
right_wrist = keypoints[10]
```

若满足：

```text
wrist_score > 0.3
and point_in_polygon(wrist, box_polygon)
```

则该货箱进入当前帧：

```text
collisions
```

### 告警

对每个货箱维护连续命中计数：

```text
if box in collisions:
    consecutive_hits[box] += 1
else:
    consecutive_hits[box] = 0
```

触发告警条件：

```text
consecutive_hits[box] >= alarm_min_consecutive_frames
and frame_idx - last_alarm_frame[box] >= alarm_cooldown_frames
```

满足后输出：

```text
alarm_collisions = [box]
```

### 参数

| 参数 | 含义 |
| --- | --- |
| `alarm_min_consecutive_frames` | 同一货箱连续命中多少帧后触发告警 |
| `alarm_cooldown_frames` | 同一货箱两次告警之间的最小帧间隔 |

## 2. hand_state

`hand_state` 判断同一人、同一只手、同一货箱是否完成：

```text
稳定命中 -> 稳定离开
```

状态机维度：

```text
person_track_id + hand
```

其中：

```text
left  = elbow keypoints[7], wrist keypoints[9]
right = elbow keypoints[8], wrist keypoints[10]
```

### 状态

```text
IDLE
ENTER_PENDING(box)
INSIDE(box)
EXIT_PENDING(box)
COOLDOWN(box)
```

含义：

| 状态 | 含义 |
| --- | --- |
| `IDLE` | 当前手没有候选事件 |
| `ENTER_PENDING(box)` | 当前手疑似命中某货箱，等待确认 |
| `INSIDE(box)` | 当前手已稳定命中该货箱 |
| `EXIT_PENDING(box)` | 当前手疑似离开锁定货箱，等待确认 |
| `COOLDOWN(box)` | 已上报，进入冷却 |

### 每帧观测

每帧对每只手生成一个观测：

```text
HIT(box)     明确命中某货箱
NO_HIT       没有可信命中
UNKNOWN      本帧不可靠，不推动状态
```

`UNKNOWN` 用于过滤关键点低置信度、手腕跳变、手臂结构异常、相邻货箱分数接近等情况。

### 关键点质量

人体尺度：

```text
person_scale = max(x_max - x_min, y_max - y_min, 20)
```

只统计 score > 0.2 的人体关键点。

低质量：

```text
low_quality =
    wrist_score < wrist_score_min
    or elbow_score < elbow_score_min
```

手腕跳变：

```text
jump_norm = distance(wrist_t, wrist_prev_valid) / person_scale
jump_bad = jump_norm > jump_max
```

前臂长度突变：

```text
forearm_len = distance(elbow, wrist)
forearm_len_ratio = forearm_len / recent_forearm_len

limb_unstable =
    forearm_len_ratio < forearm_min_ratio
    or forearm_len_ratio > forearm_max_ratio
```

近期前臂长度更新：

```text
recent_forearm_len = recent_forearm_len * 0.75 + forearm_len * 0.25
```

若出现：

```text
low_quality or jump_bad or limb_unstable
```

则当前帧观测为：

```text
UNKNOWN
```

### 命中分数

对每个货箱计算三个几何量：

```text
wrist_in_box = point_in_polygon(wrist, box_polygon)

forearm_intersects_box =
    segment_intersects_polygon(segment(elbow, wrist), box_polygon)

wrist_near_box_edge =
    distance_point_to_polygon_edge(wrist, box_polygon)
    <= sqrt(box_polygon_area) * near_edge_ratio
```

命中分数：

```text
hit_score =
    0.55 * wrist_in_box
  + 0.30 * forearm_intersects_box
  + 0.15 * wrist_near_box_edge
  - 0.40 * jump_bad
  - 0.30 * low_quality
  - 0.30 * limb_unstable
```

布尔量按：

```text
true = 1
false = 0
```

选择分数最高的货箱：

```text
best_box = argmax(hit_score)
second_score = 第二高分
```

确认 `HIT(best_box)` 的条件：

```text
best_score >= hit_threshold
and best_score - second_score >= box_margin
```

若最高分达到阈值但与第二名差距不足：

```text
best_score >= hit_threshold
and best_score - second_score < box_margin
```

则观测为：

```text
UNKNOWN
```

### 状态转移

#### IDLE -> ENTER_PENDING(A)

当前帧：

```text
HIT(A)
```

进入：

```text
ENTER_PENDING(A)
```

#### ENTER_PENDING(A) -> INSIDE(A)

确认条件：

```text
最近 enter_window_frames 帧中
HIT(A) 次数 >= enter_min_hits
and 其他货箱 HIT 次数 <= 1
```

确认后进入：

```text
INSIDE(A)
```

若超过：

```text
enter_timeout_frames
```

仍未确认，则回到：

```text
IDLE
```

#### INSIDE(A) -> EXIT_PENDING(A)

已锁定 A 后，若当前帧满足：

```text
obs != UNKNOWN
and obs != HIT(A)
```

进入：

```text
EXIT_PENDING(A)
```

若 `INSIDE(A)` 持续超过：

```text
max_inside_frames
```

仍未确认取出，则丢弃并回到：

```text
IDLE
```

#### EXIT_PENDING(A) -> COOLDOWN(A)

定义释放帧：

```text
release(A) =
    obs != UNKNOWN
    and obs != HIT(A)
```

确认取出条件：

```text
最近 exit_window_frames 帧中
release(A) 次数 >= exit_min_releases
```

确认后输出：

```text
alarm_collisions = [A]
```

并进入：

```text
COOLDOWN(A)
```

若取出候选期间重新出现：

```text
HIT(A)
```

则回到：

```text
INSIDE(A)
```

若超过：

```text
exit_timeout_frames
```

仍未确认，则回到：

```text
IDLE
```

#### COOLDOWN(A) -> IDLE

冷却帧数达到：

```text
cooldown_frames
```

后回到：

```text
IDLE
```

### 参数

| 参数 | 含义 |
| --- | --- |
| `enter_window_frames` | 拿取确认窗口长度 |
| `enter_min_hits` | 拿取确认窗口内，同一货箱至少命中帧数 |
| `enter_timeout_frames` | 进入候选后，超过多少帧未确认则丢弃 |
| `exit_window_frames` | 取出确认窗口长度 |
| `exit_min_releases` | 取出确认窗口内，至少多少帧离开锁定货箱 |
| `exit_timeout_frames` | 取出候选后，超过多少帧仍未确认则丢弃 |
| `max_inside_frames` | 已确认命中后，最多等待多少帧取出 |
| `cooldown_frames` | 完成上报后，同手同箱冷却帧数 |
| `hit_threshold` | 单帧确认 `HIT(box)` 的最低分数 |
| `box_margin` | 最佳货箱分数与第二名分数的最小差值 |
| `wrist_score_min` | 手腕关键点最低置信度 |
| `elbow_score_min` | 手肘关键点最低置信度 |
| `jump_max` | 手腕跳变阈值，公式为 `手腕位移 / 人体尺度` |
| `forearm_min_ratio` | 当前前臂长度 / 近期前臂长度 的下限 |
| `forearm_max_ratio` | 当前前臂长度 / 近期前臂长度 的上限 |
| `near_edge_ratio` | 手腕靠近货箱边界阈值比例 |

### 输出

`collisions`：

```text
状态机当前锁定或候选的货箱
```

`alarm_collisions`：

```text
完成 INSIDE -> EXIT_PENDING -> COOLDOWN 的取出确认后输出
```
