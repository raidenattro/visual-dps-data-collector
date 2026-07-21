# 地面标定（spatial / floor_xy）

## 概述

按 **camera_slug** 在 `localdata/spatial/{slug}.json` 保存地面单应标定；采集时用 RTMPose 脚踝点计算 `floor_xy_m`，写入独立 sidecar **`floor_foot.parquet`**（**不写入** `timeline.parquet`）。

不依赖 MediaPipe / 3D 骨架；**不替代** 2D 手腕碰撞检测。

## 存储结构（v2 记录包）

| 文件 | 内容 |
|------|------|
| `timeline.parquet` | 碰撞/告警等核心帧级数据（**不含** floor 列） |
| `skeleton.parquet` | 骨架关键点 |
| `floor_foot.parquet` | 足部轨迹 sidecar（可选，由 spatial 标定产生） |

`floor_foot.parquet` 列：`frame_idx`、`foot_u/v_px`、`floor_x/y_m`、`raw_floor_x/y_m`、`person_id` 等。

旧记录若 floor 仍在 timeline 中，读取时会自动回退；离线补算会迁移到 sidecar 并清理 timeline。

## 标定步骤（CLI）

1. 确认机位在 `localdata/video/{tier}/{camera_slug}/` 下有参考视频
2. 运行交互标定（按顺序点击 10 个地面控制点：远→近，每行左/右）：

```powershell
python scripts/spatial/calibrate_from_video.py --camera-slug 1-1-1 --pose-tier rtmpose-m
```

## 标定步骤（Web）

1. 打开 Web 页签 **地面标定**
2. 选择 **视频 tier** 与 **机位 slug**，**推理高** 保持与采集一致（默认 480 → 约 852×480）
3. 点击 **加载**（背景帧为推理分辨率，非原视频 2560×1440）
4. 在画布上按顺序左键点击 10 个地面控制点（右键撤销）
5. **physical 参数**：通道宽 2.0 m、行间距 **2.4 m**、控制行数 5（深度约 9.6 m）
6. 点击 **预览网格** 查看 RMSE 与绿色网格叠加
7. 勾选 **启用标定** 后点击 **保存标定**

保存结果与 CLI 相同：`localdata/spatial/{slug}.json`。

## CLI / Web 共用输出

- `localdata/spatial/1-1-1.json`
- CLI 另生成 `localdata/spatial/1-1-1.preview.png`（网格 + 红点 + RMSE）
- 建议 RMSE < 8 px；可在 JSON 或 Web 表单中调整 `physical.aisle_width_m`、`marker_spacing_m`

## 分辨率与宽高一致

- **标定分辨率** 必须与 **RTMPose 采集推理分辨率** 一致（默认 `inference.height=480` → 1-1-1 约 **852×480**）
- 若在原视频 **2560×1440** 上标点，再用于 852×480 骨架坐标系，网格会错位（表现为「宽高不一致」）
- Web 标定页 **推理高** 字段控制 preview-frame 缩放；保存 JSON 中 `calibration.resolution` 应为推理尺寸
- 行间距勿过小（如 0.5 m）：`grid_depth_m = spacing × (pairs-1)` 仅 2 m 时，绿色网格会像短方盒而非通道

## 采集

- `config.json` → `spatial.enabled: true`（默认）
- 采集时若存在对应 slug 的 enabled 标定，写入 `floor_foot.parquet` 并在 manifest `files.floor_foot` 登记
- manifest 增加 `spatial` 节点

## 离线补算历史记录

```powershell
python scripts/spatial/enrich_record_floor_xy.py rtmpose-m/1-1-1/clip_xxx_rtmpose_m
python scripts/spatial/enrich_manifest_floor_xy.py
```

补算会写入 `floor_foot.parquet`，并从 timeline 移除 legacy floor 列。

## 回放

- 加载记录后显示 **Ground Map** 面板与 `floor X/Y` 文字
- 勾选 **地面网格** / **足部轨迹** 叠加到视频 canvas

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/spatial/cameras` | 列出可标定机位 slug |
| POST | `/api/spatial/calibration/{camera_slug}/preview` | 预览网格与 RMSE（不落盘） |
| GET | `/api/spatial/calibration/{camera_slug}` | 读取标定 |
| PUT | `/api/spatial/calibration/{camera_slug}` | 保存并重算 H |
| GET | `/api/spatial/calibration/{camera_slug}/preview-frame` | 机位首帧 base64 |
| GET | `/api/records/{id}/spatial` | 记录 + 运行时标定 |
| GET | `/api/records/{id}/floor-foot` | 足部轨迹 sidecar |

## 模板

空配置见 [`examples/spatial.template.json`](../examples/spatial.template.json)。
