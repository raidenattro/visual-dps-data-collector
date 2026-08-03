# event_review 逐帧 schema v2 迁移指南

## 目标

`event_review.json` 的磁盘格式统一为“一帧一条”。模型检测和人工标真严格分离：

```json
{
  "schema": 2,
  "verified_true": [
    {
      "frame_idx": 1272,
      "source_frame_idx": 1272,
      "detected_event_types": ["collision"],
      "detected_box_tokens": ["Box_2014"],
      "bindings": [
        {
          "person_id": 0,
          "person_track_id": "optional-track-id",
          "confirmed_box_tokens": ["Box_2015"]
        }
      ]
    }
  ]
}
```

- `detected_*`：模型检测参考，可以在重算后变化，不是人工真值。
- `bindings[]`：人工真值。每条绑定一个人员/轨迹和一个或多个确认货框。
- 同一帧只允许一条帧记录；同一帧可以有多个 binding。
- 保存索引为 `frame_idx`；binding 更新索引为 `person_track_id` 或 `person_id`。

## 为什么不能自动猜旧格式

历史上多种不兼容格式都写成了 `"schema": 1`：

1. 旧逐帧/事件格式：`box_tokens` 可能是整帧多个真值框。
2. 逐货框格式：同帧多行，每行 `box_tokens` 只有一个框。
3. 新扁平格式：`confirmed_box_tokens` 才是人工真值，`box_tokens` 是检测参考。
4. 混合文件：同一个 JSON 同时包含上述几种行。

因此默认迁移只信显式的 `confirmed_box_tokens`。缺少 confirmed 的行会进入
`unresolved_legacy`，不会把 `box_tokens` 猜成人工真值，也不会被静默删除。

## 迁移模式

```powershell
# 1. 只读审计（默认）
python scripts/data/migrate_event_review_to_frame_v2.py 1-1-1-_2 `
  --source-format explicit-confirmed `
  --report audit.json

# 2. 生成独立候选，不修改源文件
python scripts/data/migrate_event_review_to_frame_v2.py 1-1-1-_2 `
  --source-format explicit-confirmed `
  --candidate-dir localdata/review-v2-candidate `
  --report candidate-report.json

# 3. 人工确认旧文件确实是“逐货框”后才可选择
python scripts/data/migrate_event_review_to_frame_v2.py 1-1-1-_2 `
  --source-format legacy-per-box `
  --candidate-dir localdata/review-v2-candidate

# 4. 人工确认旧文件确实是“逐帧/事件”后才可选择
python scripts/data/migrate_event_review_to_frame_v2.py 1-1-1-_2 `
  --source-format legacy-frame-event `
  --candidate-dir localdata/review-v2-candidate
```

原地替换必须显式加 `--replace`。脚本会先生成
`event_review.json.bak.<timestamp>`。存在 unresolved 时默认阻止替换；即使显式
使用 `--allow-unresolved`，原行也会完整保存在 `unresolved_legacy`。

## 只迁一条记录

页面报 409 时通常只有一条记录被挡住，没必要动整个机位。`--record` 接受
record_id 全名，也接受唯一片段（比如视频名）；匹配到多条会报错并列出候选，
不会替你猜：

```powershell
# 1. 只读审计这一条，报告里的 unresolved_frames 就是被挡住的帧号
python scripts/data/migrate_event_review_to_frame_v2.py `
  --record 00000001088000200_seg01_24-00_to_25-45 `
  --report localdata/audit-one.json

# 2. 原地替换这一条，先自动生成 .bak.<timestamp>
python scripts/data/migrate_event_review_to_frame_v2.py `
  --record 00000001088000200_seg01_24-00_to_25-45 `
  --replace --allow-unresolved
```

`--allow-unresolved` 不会丢数据：歧义行原样搬进 `unresolved_legacy` 留在文件里，
只是不再当人工真值使用，对应帧的标真状态需要在页面上手工重标一次。

## 运行时保护

- schema v2 直接读写。
- schema 1 中带显式 confirmed 的行可以只读展示。
- schema 1 中存在歧义行时，页面写入返回 HTTP 409，防止一次点击覆盖旧文件。
- 新写入始终落为 schema v2，并保留既有 `unresolved_legacy`。
- 取消标真只删除当前帧；修改一个人员只 upsert 该 binding，不覆盖同帧其他人员。

## 验收清单

1. 审计报告中检查 `input_count`、`output_frame_count`、`binding_count`。
2. `unresolved_entries` 必须由人工选择旧格式或逐条复核，不能用猜测消零。
3. 候选文件执行 `verify_frame_v2_verified_true`，不得有重复帧或空 binding。
4. 页面验证：标真 → 下一帧 → 返回，确认框与人员保持不变。
5. 同帧 P0/P1 测试：修改 P0 后 P1 不变。
6. 取消当前帧后，相邻帧和其他 binding 不变。
7. Excel、准确率和推理评估只使用 confirmed/bindings，不使用检测框作为真值。
8. 本地验收通过后，才安排停写窗口、服务器备份、候选替换和代码部署。
