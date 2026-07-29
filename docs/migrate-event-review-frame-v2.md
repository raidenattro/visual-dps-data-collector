# event_review schema v2 迁移指南

将 `verified_true` 从 legacy（逐货框 / 混合签名）一次性迁移为 **schema v2：逐帧 + bindings**，避免运行时读时聚合带来的误并问题。

## 为什么用方案二 + 脚本

| 问题 | 读时聚合（当前分支） | schema v2 + 迁移脚本 |
|------|----------------------|-------------------------|
| legacy 多条同帧 | `box_tokens` 误当 confirmed | 迁移时显式规则，可审计 |
| 两人两 box | 无法表达 | `bindings` 数组，每人一条 |
| 运行时复杂度 | `enrich_events_with_review` 聚合 | 只读 v2，无 fallback |
| 对新分支改动 | 已改写入/聚合逻辑 | **脚本离线迁移 + 简化读路径** |

## schema v2 条目格式

```json
{
  "schema": 2,
  "verified_true": [
    {
      "frame_idx": 1272,
      "source_frame_idx": 1272,
      "event_type": "collision",
      "box_tokens": ["Box_2014", "Box_2015"],
      "bindings": [
        {
          "person_id": 0,
          "confirmed_box_tokens": ["Box_2014"]
        },
        {
          "person_id": 1,
          "confirmed_box_tokens": ["Box_2015"]
        }
      ]
    }
  ]
}
```

| 字段 | 含义 |
|------|------|
| `box_tokens` | 该帧 timeline 检测参考（告警/碰撞并集） |
| `bindings[]` | 人工标真：每条 = 可选 `person_id` + 必填 `confirmed_box_tokens` |
| `event_type` | `alarm` / `collision` / `frame`（无检测但人工标真） |

## legacy → v2 迁移规则

1. 按 `frame_idx` 分组 legacy 条目。
2. 每条 legacy 转为 binding：
   - **优先** `confirmed_box_tokens`
   - 若无 confirmed 且 `box_tokens` **仅 1 个**：视为旧版逐货框标真，confirmed = 该 box
   - 若无 confirmed 且 `box_tokens` 多个：**跳过并标记 ambiguous**（不猜）
3. 同帧 bindings 去重：`(person_id, confirmed 集合)` 相同则合并。
4. 有 record locator 时读取 timeline 填充 `box_tokens` / `event_type`。
5. 写入 `schema: 2`，备份原文件为 `event_review.json.bak.{timestamp}`。

## 操作步骤

在项目根目录：

```bash
# 1. 预览（默认 dry-run）
python scripts/data/migrate_event_review_to_frame_v2.py --dry-run

# 2. 仅处理某机位
python scripts/data/migrate_event_review_to_frame_v2.py 1-1-1-_2 --dry-run

# 3. 校验是否已为 v2
python scripts/data/migrate_event_review_to_frame_v2.py --verify-only

# 4. 确认后写入
python scripts/data/migrate_event_review_to_frame_v2.py --write

# 5. 单元测试
python -m unittest tests.test_event_review_frame_v2_migration -v
```

## 与新分支的配合（推荐落地顺序）

1. **保留** Bug A（并发写锁 + 原子 JSON）与帧级导航 UX（下一帧）。
2. **运行本脚本** 迁移全部 `localdata/review/`（生产前在副本上试跑）。
3. **简化运行时**（后续 PR，改动集中且可测）：
   - `load_event_review` / `enrich_events_with_review`：只解析 v2；`schema < 2` 时提示跑脚本
   - 删除 `enrich` 中 legacy 聚合 fallback（`box_tokens` 当 confirmed）
   - 删除 `drop_review_frame` 清光同帧旧条；改为按帧更新单条 v2
   - PATCH toggle：读写 `bindings`，不再依赖 `event_signature(box_tokens)`
4. **普通帧标真**：若产品需要漏报补标，保留 `event_type: frame` + bindings；否则可限制仅 `box_tokens` 非空帧可标真。

## 迁移后仍需人工抽查的场景

- 同帧 ambiguous 帧（脚本输出 `待人工帧 N`）
- 旧 bug 产生的重复/矛盾 legacy（799→786 类数据）：迁移不会猜用户意图，只会 dedupe 完全相同的 binding
- `person_id` 缺失的多 binding 帧

## 相关文件

| 文件 | 说明 |
|------|------|
| `event_review_frame_v2.py` | 迁移核心逻辑（可与运行时共用） |
| `scripts/data/migrate_event_review_to_frame_v2.py` | CLI |
| `tests/test_event_review_frame_v2_migration.py` | 回归测试 |
