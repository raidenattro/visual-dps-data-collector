# 人工复核（event_review）迁移指南

将 `event_review.json` 从 pose 模型目录（`localdata/json/rtmpose-*`）迁出，统一存放到 `localdata/review/`，与骨架、碰撞数据解耦。

## 背景

| 数据类型 | 迁移前 | 迁移后 |
|----------|--------|--------|
| 骨架、timeline（碰撞） | `localdata/json/{tier}/{机位}/{记录包}/` | 不变 |
| 人工复核 | 记录包内 `event_review.json` | `localdata/review/{机位}/{clip_key}/event_review.json` |

同一源视频、同一片段时间窗下，不同 `rtmpose-t/s/m` 采集记录**共用一份**复核。

## review_key 规则

逻辑键格式：

```
{camera_slug}/{source_video_stem}__{start}__{end}
```

**时间窗（segment）解析优先级：**

1. `meta.clip_start` / `meta.clip_end`（或 `segment_*`、`*_sec` 秒数字段）
2. `manifest` / `collect_config` 中同名字段
3. 记录名正则：`_start_00-11-22`、`_end_00-12-30`
4. 默认整段视频：`0` / `full`

**示例：**

| 场景 | review_key |
|------|------------|
| 整段 `batch_001.mp4` | `1-1-1/batch_001__0__full` |
| 记录名含 `_start_00-11-22` | `2-1-3/clip__00-11-22__full` |
| meta 显式片段时间 | `1-1-1/v__00-11-22__00-12-30` |

**落盘路径：**

```
localdata/review/{camera_slug}/{source_stem}__{start}__{end}/event_review.json
```

配置项见 `config.json` → `paths.review_dir`（默认 `localdata/review`）。

## 运行时行为（迁移后）

- **读**：先 `review_dir`，再记录包内旧文件（兼容期双读）
- **写**：仅写 `review_dir`
- **删除单条采集记录**：不删除 `review_dir` 中的共享复核

## 迁移步骤

均在**项目根目录**执行。

### 1. 更新代码并重启服务

确保已包含 `review_store.py`、`pose_store` 双读单写逻辑及 `config.json` 中的 `review_dir`。

重启后端 API 服务，使新读写路径生效。

### 2. 预览（dry-run）

```bash
python scripts/data/migrate_event_review_to_review_dir.py --dry-run
```

可选：仅处理某一模型层：

```bash
python scripts/data/migrate_event_review_to_review_dir.py --dry-run --tier rtmpose-t
```

检查输出中的「扫描 / 迁移 / 合并 / 错误」数量是否符合预期。

### 3. 正式迁移

```bash
python scripts/data/migrate_event_review_to_review_dir.py
```

脚本会：

- 读取记录包内旧 `event_review.json`（及 `review_dir` 已有文件）
- 按 `review_key` 合并冲突后写入 `localdata/review/`
- **默认保留**记录包内旧文件（便于回滚）

### 4. 验证

抽样检查：

1. `localdata/review/{机位}/` 下是否生成对应 `event_review.json`
2. Web 回放页：复核状态、紫色货框、标真（Y）落盘是否正常
3. 记录列表：复核状态标签是否正确

可选刷新 SQLite 索引（将磁盘复核状态同步到 `data.db`）：

```bash
# 通过 API（服务需已启动）
curl "http://127.0.0.1:8765/api/records/import-event-reviews"
```

或带 tier 过滤：`?pose_tier=rtmpose-t`

### 5. 清理包内旧文件（可选）

确认无误后再执行：

```bash
python scripts/data/migrate_event_review_to_review_dir.py --remove-legacy
```

仅删除已成功写入 `review_dir` 的记录包内旧 `event_review.json`，**不删除** `review_dir` 中的文件。

### 6. 跨机合并（如有）

`merge_pose_tier_data.py` 已适配：导入侧复核写入 `review_dir`，不再复制到记录包。

```bash
python scripts/data/merge_pose_tier_data.py --source /path/to/export --tier rtmpose-t --dry-run
python scripts/data/merge_pose_tier_data.py --source /path/to/export --tier rtmpose-t
```

## 相关脚本

| 脚本 | 用途 |
|------|------|
| `scripts/data/migrate_event_review_to_review_dir.py` | 本迁移主脚本 |
| `scripts/data/backfill_no_collision_review.py` | 无碰撞记录写回复核（已走 `review_dir`） |
| `scripts/data/demote_incomplete_box_review.py` | 缺货框确认的已复核记录降级 |
| `scripts/data/merge_pose_tier_data.py` | 跨机数据合并（复核按 `review_key`） |

## 回滚

若迁移后出现问题、且未执行 `--remove-legacy`：

- 记录包内旧 `event_review.json` 仍在，双读逻辑会优先读 `review_dir`，可临时删除有问题的 `localdata/review/...` 条目，或还原代码后重启服务。

若已 `--remove-legacy`，需从备份恢复记录包内文件或 `localdata/review/` 目录。

## 注意事项

- 同一 `review_key` 下多 tier 复核冲突时，迁移脚本会**并集合并** `verified_true`，状态取更完整一方。
- 若 re-collect 改变了片段时间窗或源视频名，会生成新的 `review_key`，原复核不会自动关联。
- 碰撞重算会重置共享复核为「复核中」（清空标真），不会直接删除 `review_dir` 文件。
