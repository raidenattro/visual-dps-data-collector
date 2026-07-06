# 已执行的一次性数据迁移脚本（勿日常运行）

| 脚本 | 说明 |
|------|------|
| `migrate_pose_model_tiers.py` | 扁平机位 → `rtmpose-t/{机位}` |
| `migrate_event_review_to_review_dir.py` | 包内 event_review → `localdata/review/` |

若新环境从未迁移，可将脚本移回 `scripts/data/` 后 `--dry-run` 再执行。
