# 脚本工具索引

按用途分类；均在项目根目录下执行。

## setup/ — 环境与模型

| 脚本 | 用途 |
|------|------|
| `setup/setup_linux.sh` | Linux 一键部署：conda 环境、GPU 依赖、ONNX 模型、GPU 验证 |
| `setup/setup_windows.ps1` | Windows 一键部署（同上） |
| `setup/install_requirements.sh` | 仅安装 Linux GPU 依赖（无 conda / 模型） |
| `setup/install_requirements.ps1` | 仅安装 Windows GPU 依赖 |
| `setup/download_onnx_models.py` | 下载 RTMDet + RTMPose ONNX 权重 |
| `setup/verify_gpu.py` | 验证 ONNX Runtime CUDA 是否可用 |

```bash
bash scripts/setup/setup_linux.sh
python scripts/setup/verify_gpu.py
python scripts/setup/download_onnx_models.py --det t,m --pose t
```

## collect/ — 批量采集

| 脚本 | 用途 |
|------|------|
| `collect/batch_skeleton_collect.py` | 单机位 / 多机位文件夹递归批处理（骨架 + 可选碰撞） |
| `collect/batch_video_workspace.py` | 工作区下多个批次根目录依次调用批处理 |
| `collect/batch_staging_parallel.py` | **并行** staging 采集（默认 2 路 + 新终端窗口） |
| `data/merge_staging_batches.py` | 将 `localdata_staging/{批次}/` 合并入主 `localdata/` |

```bash
python scripts/collect/batch_skeleton_collect.py /path/videos --group-by-subfolder --with-collision
python scripts/collect/batch_video_workspace.py /path/workspace --variant t --skip-existing

# 并行 staging（默认工作区 /home/hqit/zyrao/skeleton-video/）
python scripts/collect/batch_staging_parallel.py --plan-only
python scripts/collect/batch_staging_parallel.py --terminal --with-collision --skip-existing
python scripts/collect/batch_staging_parallel.py --terminal --with-collision --merge-after --consolidate-after
python scripts/data/merge_staging_batches.py --consolidate-after
```

## data/ — 数据迁移与维护

推荐顺序：**模型层迁移 → slug 归并 → 跨机合并**。

| 脚本 | 用途 |
|------|------|
| `data/migrate_pose_model_tiers.py` | 扁平 `localdata/json|video/{机位}` → `rtmpose-t/{机位}` |
| `data/consolidate_camera_slugs.py` | 同机位 `-(2)/(3)` slug 归并到 canonical 机位（同名记录加后缀，不覆盖） |
| `data/merge_pose_tier_data.py` | 合并另一台机器/导出目录的采集数据（含复核 `event_review` 并集） |
| `data/repair_batch_records.py` | 为已有记录补 `annotation.json` 回放副本 |
| `data/restore_source_videos.py` | 将 `localdata/video` 配套视频复制回批处理源目录 |
| `data/backfill_no_collision_review.py` | 批量为无碰撞记录写入 `event_review`（无碰撞） |
| `data/migrate_event_review_to_review_dir.py` | 将包内 `event_review` 迁到 `localdata/review/`（见 [docs/migrate-event-review.md](../docs/migrate-event-review.md)） |
| `data/extract_wrist_features.py` | **手腕速度 + 碰撞段位移**特征提取（无需重跑模型） |
| `data/analyze_wrist_feature_discrimination.py` | 按标签/机位批量提取并生成特征区分度报告（`docs/`） |

```bash
# 本机 slug 归并（先 --dry-run）
python scripts/data/consolidate_camera_slugs.py --tier rtmpose-t --dry-run
python scripts/data/consolidate_camera_slugs.py --tier rtmpose-t

# 跨机合并
python scripts/data/merge_pose_tier_data.py --source /path/to/export --tier rtmpose-t --dry-run

# 手腕特征（见下方说明）
python scripts/data/extract_wrist_features.py --tier rtmpose-m --dry-run
```

### 手腕特征提取（`extract_wrist_features.py`）

基于已采集的 `skeleton.parquet` + `timeline.parquet` + 货框标注，**不重跑 RTMPose**，输出：

| 文件 | 含义 |
|------|------|
| `wrist_velocity.parquet` | 每帧 × 每人 × 左/右手腕：`vx, vy, speed, speed_norm`（推理坐标 px/s） |
| `wrist_box_segments.parquet` | 每次**碰撞段**（手腕进入某 box → 离开）：端点坐标、`dx/dy/displacement`、`path_length`；`event_type=collision`，与告警无关；段内若触发告警则 `had_alarm=true` |

v2 记录写在包目录内并更新 `manifest.json` 的 `files.wrist_*`；v1 JSON 记录写 sidecar：`{stem}.wrist_velocity.parquet`。

**依赖**：记录需有骨架帧。货框优先按机位 **reflection 多编号合并**（与采集碰撞一致）；若包内 `annotation.json` 仅单货架且少于 manifest 内嵌框数，则回退 `manifest.annotation`。`person_track_id` 由脚本后处理分配。

```bash
# 全库
python scripts/data/extract_wrist_features.py

# 指定模型层 / 机位
python scripts/data/extract_wrist_features.py --tier rtmpose-m
python scripts/data/extract_wrist_features.py --tier rtmpose-m --camera 2-7-2

# 单条记录
python scripts/data/extract_wrist_features.py --record rtmpose-m/2-7-2/clip_0001_start_...

# 已提取则跳过；批处理汇总 + 合并碰撞段
python scripts/data/extract_wrist_features.py --tier rtmpose-m --skip-existing \
  --export-dir localdata/features/export

# 碰撞段边界抖动合并（默认允许 1 帧间隙）
python scripts/data/extract_wrist_features.py --tier rtmpose-m --max-gap-frames 2
```

### 手腕特征区分度分析（`analyze_wrist_feature_discrimination.py`）

按回放「已保存记录」同款筛选（默认标签 `单人,无遮挡`、**已复核**、**有标真**，机位见 `DEFAULT_CAMERAS`），**重新提取**手腕特征并输出 Markdown 报告。正/负样本以 `event_review.verified_true` 合并的 ground truth 段为准（范本货框优先 `confirmed_box_tokens`，否则 `box_tokens`）；另单独统计**误报碰撞段**（与未被标真覆盖的 `alarm_collisions` 重叠的手腕段，与准确率误报定义一致）。

```bash
# 提取 + 分析（31 条 rtmpose-m 优质样本）
python scripts/data/analyze_wrist_feature_discrimination.py

# 仅分析已有 parquet
python scripts/data/analyze_wrist_feature_discrimination.py --skip-extract

# 自定义输出（JSON 默认 docs/json/{报告名}.json）
python scripts/data/analyze_wrist_feature_discrimination.py \
  --out docs/wrist-features-discrimination-rtmpose-m.md
```

报告示例：[docs/wrist-features-discrimination-rtmpose-m.md](../docs/wrist-features-discrimination-rtmpose-m.md)

读取示例（Python）：

```python
import pyarrow.parquet as pq
from pathlib import Path

base = Path("localdata/json/rtmpose-m/2-7-2/某记录目录")
vel = pq.read_table(base / "wrist_velocity.parquet").to_pandas()
seg = pq.read_table(base / "wrist_box_segments.parquet").to_pandas()
```

## archive/ — 历史工具（勿日常运行）

| 脚本 | 说明 |
|------|------|
| `archive/split_app_js.py` | 将 `web/app.monolith.js` 拆分为 `web/app/` 模块（拆分已完成） |

已删除的失效脚本（重构一次性工具，勿恢复）：`build_http_routes.py`、`split_server_modules.py`、`clean_http_routes.py`、`test_manifest_api.py`。
