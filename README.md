# visual-dps-datacollect

从**本地视频**采集人体骨架坐标，输出 JSON；配置命名与 [visual-dps](../visual-dps) 的 `app_config.json` 对齐。

## 安装

```bash
pip install -r requirements.txt
```

## Web 前端

```bash
python server.py
```

浏览器：**http://127.0.0.1:8765**

| 页签 | 功能 |
|------|------|
| **采集** | 上传视频 + **必填货框标注**（手动机位标识 / 上传 JSON / 标注页已存）→ 首帧预览对照填写机位 → 推理并**落盘碰撞事件** |
| **回放** | 导入 JSON；需叠加画面时再上传视频（临时文件，离开回放页/暂停/播完会删除） |

## 配置（`config.json`，对齐 visual-dps）

| 节点 | 字段 | 说明 |
|------|------|------|
| `paths` | `json_dir` | 骨架 JSON 默认目录（`localdata/json`） |
| `paths` | `video_dir` | 配套视频目录（`localdata/video`） |
| `paths` | `upload_dir` | 采集时临时视频目录（不长期保留） |
| `paths` | `models_onnx_dir` | ONNX 权重根目录（见下方目录结构） |
| `models` | `backend` | `rtmpose_t` / `rtmpose_s` / `rtmpose_m`（姿态档） |
| `models` | `det_variant` | 检测档 `t`（nano 320）/ `m`（640）；`s`/`l` 无官方 ONNX 时自动回退 |
| `models` | `rtmpose_onnx_device` | CPU 设备名 |
| `models` | `use_gpu` | 默认 `true`，使用 `rtmpose_onnx_device_gpu` |
| `models` | `rtmpose_onnx_device_gpu` | GPU 设备名（`cuda`） |
| `inference` | `height` | 默认推理高度 |
| `inference` | `frame_rate` | 采集推理节拍（帧/秒），**默认 0**=全速；如 `15` 则限速到约 15 次推理/秒 |
| `inference` | `pose_frame_interval` | 抽帧间隔 |
| `inference` | `max_pose_frames` | 最多采集帧数，0 不限制 |
| `inference` | `alarm_min_consecutive_frames` | 碰撞报警：连续命中帧数（默认 3，同 visual-dps） |
| `inference` | `alarm_cooldown_frames` | 碰撞报警：同货框冷却帧数（默认 6） |
| `source` | `video` | CLI 默认视频路径 |
| `server` | `host` / `port` | Web 服务 |

GPU：默认开启（`models.use_gpu: true` + `onnxruntime-gpu`）。启动日志应出现 `推理设备: cuda` 与 `ORT 实际 EP: CUDAExecutionProvider`。强制 CPU 可设 `"use_gpu": false` 或 `set INFERENCE_USE_GPU=0`。

## ONNX 模型目录

默认根目录：`localdata/models/onnx`（`config.json` → `paths.models_onnx_dir`）

```
localdata/models/onnx/
  detection/          # 人体检测（RTMDet）
    rtmdet_nano/end2end.onnx   # det_variant=t，320×320
    rtmdet_m/end2end.onnx      # det_variant=m，640×640
  pose/               # 姿态估计（RTMPose）
    rtmpose_t/end2end.onnx     # backend=rtmpose_t
    rtmpose_s/end2end.onnx
    rtmpose_m/end2end.onnx
```

与 visual-dps 旧布局 `localdata/models/rtmpose_onnx/{模型名}/` 仍兼容：若新路径不存在会自动回退读取。

**预下载（推荐）**：

```bash
# 可选：复制 .env.example 并设置 OPENMMLAB_MIRROR_BASE 加速
python scripts/download_onnx_models.py
python scripts/download_onnx_models.py --det t,m --pose t
```

复用 visual-dps 已有权重时：

```bash
python collect_pose.py --video test.mp4 --models-dir ../visual-dps/localdata/models/rtmpose_onnx
```

## 命令行

```bash
python collect_pose.py --video test.mp4 --backend rtmpose_t
```

未指定 `-o` 时写入 `paths.json_dir/{视频主名}_{backend}.json`（如 `test.mp4` → `test_rtmpose_t.json`）。

采集须带有效货框标注（上传 JSON 或 `localdata/json/annotations/{视频主名}.json`）：

```bash
python collect_pose.py --video test.mp4 --annotation path/to/boxes.json
# 或先在 Web「标注」页保存后：
python collect_pose.py --video test.mp4
```

Web 采集页：无本地标注时会提示去「标注」页或上传 JSON，服务端拒绝无标注采集。

## 碰撞检测（与 visual-dps event-worker 一致）

- **输入**：visual-dps / box_human_det 同款标注 JSON（`shelves[]` 或 legacy 顶层 `boxes[]`，含 `video_polygon` / `video_polygon_norm`、`annotation_size`）
- **逻辑**：COCO-17 手腕（索引 9/10）score > 0.3，点落在货框多边形内 → `collisions`；连续 N 帧 + 冷却 → `alarm_collisions`
- **输出**：每帧 `collisions` / `alarm_collisions`；根节点 `annotation.boxes` 为已缩放到推理分辨率的货框
- **回放**：绿框=无碰撞，黄框=瞬时碰撞，红框=报警（与 box_human_det 三色一致）

标注 JSON 会另存为 `{pose_stem}_annotation.json`，也可通过 `GET /api/records/{id}/annotation.json` 下载。

## JSON 格式

`frames[].persons[].keypoints`：`[[x, y, score], ...]` × 17（COCO-17）。

## 机位标识与 reflection（手动输入）

1. 将 `examples/reflection.example.json` 复制为仓库根目录 `reflection.json`，按现场维护 `camera`（画面机位，如 `2-1组-3`）↔ `annotation`（标注 JSON 编号）。
2. 标注 JSON 放在 `localdata/json/annotations/{编号}.json`。
3. 采集页：**选择视频** → 对照首帧预览右下角 **手动填写机位标识** → **开始采集**（也可上传标注 JSON，或按视频主名使用标注页已存文件）。

`config.json` → `reflection.path` 默认为 `reflection.json`。

## 问题记录

见 **[problem.md](./problem.md)**：仓库/采集过程中观察到的问题（现象、原因、建议方案）。按编号追加，便于与代码变更对照；**未标注已修复的条目默认未实现**。
