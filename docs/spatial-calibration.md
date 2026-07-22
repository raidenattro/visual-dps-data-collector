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
| `wrist_face.parquet` | 左右手腕 Left/Right Map 轨迹 sidecar（Y×Z + 列/层） |



`floor_foot.parquet` 列：`frame_idx`、`foot_u/v_px`、`floor_x/y_m`、`raw_floor_x/y_m`、`person_id` 等。



旧记录若 floor 仍在 timeline 中，读取时会自动回退；离线补算会迁移到 sidecar 并清理 timeline。



## 标定步骤（CLI · 遗留）

旧版 CLI 仍支持 10 地面点 homography（`scripts/spatial/calibrate_from_video.py`）。**新流程请优先使用 Web 立体 8 角点标定**，floor_xy 由底面自动推导。

## 标定步骤（Web）

分步标定立体作业空间（schema v2，主流程 4 步）：

1. 打开 Web 页签 **地面标定**
2. 选择 **视频 tier** 与 **机位 slug**，**推理高** 与采集一致
3. **① 立体框 8 角点**：输入 W/D/H，依次点 BL→BR→FR→FL→TL→TR→FR_top→FL_top
4. **② 地面列区域**：左键**两点**画列界——第 1 点在 **BL–FL** 左纵深边，第 2 点在 **BR–FR** 右纵深边（端点经 floor homography 反算纵深 `boundaries_y_m`；每条线多一列）
5. **③ 左侧层线**：左键**两点**画一条水平层线（每条线多一层；不预生成均分线）
6. **④ 右侧层线**：层数可与左侧不同；同样两点确认每条层线
7. 点击 **预览全部** 叠加查看立体线框、底面网格、列线、层线
8. 勾选 **启用立体标定（采集 floor_xy + 回放 Map）** 后 **保存**

**floor_xy** 由立体底面 4 角点（BL/BR/FR/FL）自动计算 homography，写入 `computed.floor_source = "volume_bottom"`。无需单独标 10 地面点。

与货框 **annotation** 独立：列数、左右层数均在 spatial JSON 内配置，不读取货位标注。

### 回放 Left / Right Map

启用立体作业空间且对应侧面货架启用时，回放页显示：

| 面板 | 内容 |
|------|------|
| Ground Map | 足部 `floor_xy` 轨迹（中点） |
| Left Map | **左手腕**在左侧面 Y（深度）× Z（高度）上的位置与拖尾 |
| Right Map | **右手腕**在右侧面 Y×Z 上的位置与拖尾 |

左腕固定投射到左侧面 homography，右腕到右侧面；**不取左右腕中点**。机位只出现一侧货架时，取消勾选另一侧「启用货架」即可隐藏对应 Map。

### 8 角点顺序（世界坐标）

| 标签 | 世界坐标 |
|------|----------|
| BL / BR / FR / FL | 底面 z=0 |
| TL / TR / FR_top / FL_top | 顶面 z=H |

- **左侧面** = x=0；**右侧面** = x=W
- 地面 **列**：手标线段端点只能在 **BL–FL** 与 **BR–FR** 纵深棱边上；保存时反算 **`ground_columns.boundaries_y_m`**（沿通道纵深 Y 分列；`boundaries_x_m` 固定为整宽 `[0, width]`）
- 手腕 **层** 由左右侧面 homography + `layer_z_m` 判定

## CLI / Web 共用输出



- `localdata/spatial/1-1-1.json`

- CLI 另生成 `localdata/spatial/1-1-1.preview.png`（网格 + 红点 + RMSE）

- 建议底面 RMSE < 8 px；调整 W/D/H 或重标 8 角点



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

- **Ground Map**：floor 轨迹 + **列分割线**（纵深标定时为横向线，volume 标定后）
- 勾选 **地面网格** / **足部轨迹** / **立体空间**（线框 + 层线）
- 手腕 overlay 显示 `L3·C2`（左 3 层 · 第 2 列）；坐标栏同步列/层
- 列/层与手腕 Y×Z 写入 **`wrist_face.parquet`** sidecar；回放优先读 sidecar，无 sidecar 时回退 frameCache 实时计算



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
| GET | `/api/records/{id}/wrist-face` | 左右手腕 Y×Z 轨迹 sidecar |



## 模板



空配置见 [`examples/spatial.template.json`](../examples/spatial.template.json)。

