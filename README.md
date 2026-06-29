# visual-dps-datacollect

从**本地视频**采集人体骨架坐标，输出 JSON；配置命名与 [visual-dps](../visual-dps) 的 `app_config.json` 对齐。

## 安装

**前置**：Python 3.10+、NVIDIA 驱动（GPU 推理）。推荐 conda 独立环境。

脚本工具按用途分类见 [`scripts/README.md`](scripts/README.md)（`setup/` · `collect/` · `data/`）。

### 一键部署（推荐）

**Linux**（创建 conda 环境 `visual-dps`、装依赖、下模型、验 GPU）：

```bash
bash scripts/setup/setup_linux.sh
conda activate visual-dps
```

**Windows**（PowerShell）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup/setup_windows.ps1
```

常用参数：

| 平台 | 仅装依赖（已有 Python 环境） | CPU 推理 | 跳过模型下载 |
|------|------------------------------|----------|--------------|
| Linux | `bash scripts/setup/setup_linux.sh --skip-conda` | `--cpu` | `--skip-models` |
| Windows | 直接用 `scripts/setup/install_requirements.ps1` | `-Cpu` | `-SkipModels` |

### 依赖文件

| 文件 | 用途 |
|------|------|
| `requirements-base.txt` | 公共依赖（OpenCV、FastAPI 等） |
| `requirements-linux-gpu.txt` | Linux GPU（onnxruntime-gpu + nvidia cuDNN/CUDA pip 包） |
| `requirements-windows-gpu.txt` | Windows GPU（onnxruntime-gpu + nvidia-cudnn-cu12） |
| `requirements-cpu.txt` | CPU 推理（onnxruntime） |
| `requirements-dev.txt` | 开发/脚本测试（httpx） |
| `requirements.txt` | 索引说明（请用上面平台专用文件） |

**手动安装（GPU）**：

```bash
# Linux
pip install -r requirements-linux-gpu.txt
pip install "rtmlib>=0.0.13" --no-deps

# Windows
pip install -r requirements-windows-gpu.txt
pip install "rtmlib>=0.0.13" --no-deps
```

> `rtmlib` 必须用 `--no-deps`，否则会拉入 `onnxruntime`（CPU）与 `opencv-python`，与 `onnxruntime-gpu` / `opencv-python-headless` 冲突。

### GPU 验证

```bash
python scripts/setup/verify_gpu.py
```

成功时应看到 `ORT 可用 EP` 含 `CUDAExecutionProvider`，以及 `ORT 实际 EP: CUDAExecutionProvider`。

### GPU 说明（Linux / Windows）

- 默认开启 GPU（`config.json` → `models.use_gpu: true`）。
- 启动 `server.py` 或 `collect_pose.py` 时，日志应出现 `推理设备: cuda`。
- **Windows**：`ort_cuda_setup.py` 在 import ORT 前把 `nvidia/*/bin` 加入 PATH。
- **Linux**：除 `LD_LIBRARY_PATH` 外，还须预加载 `libcudnn.so.9` 等（已在 `ort_cuda_setup.py` 实现）；`setup_linux.sh` 会写入 conda `activate.d` 供子进程使用。
- 强制 CPU：`"use_gpu": false` 或环境变量 `INFERENCE_USE_GPU=0`。

## Web 前端

```bash
python server.py
# 端口冲突时指定监听端口（覆盖 config.json 的 server.port）
python server.py --port 8770
```

浏览器：**http://127.0.0.1:8765**（默认端口，可用 `--port` 修改）

### 代码结构（Web）

| 模块 | 职责 |
|------|------|
| `server.py` | 入口：CUDA 初始化 → `api.app` |
| `api/app.py` | FastAPI 工厂、静态页挂载、`main()` |
| `api/routes/http.py` | HTTP 路由（采集/记录/标注/回放） |
| `api/collect_service.py` | 采集与批处理后台任务 |
| `api/record_service.py` | 记录定位、meta、配套视频路径 |
| `api/job_store.py` | 任务状态与批处理进度估算 |
| `api/reflection_service.py` | reflection.json 机位映射 |

业务逻辑在 `collect_core` / `pose_store` / `annotation_store`；Web 层只做编排。

前端主逻辑在 `web/app/`（由原 `app.js` 按采集 / 记录 / 事件复核 / 渲染等拆分，见 `web/app/README.md`）；`annotate.js` 为标注页。

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
| `models` | `det_variant` | 检测档 `nano`（320）/ `m`（640）；`s`/`l` 无官方 ONNX 时自动回退；旧配置 `t` 等同 `nano` |
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
| `server` | `host` / `port` | Web 服务（启动时 `--host` / `--port` 可覆盖） |

GPU 配置见上文「安装 → GPU 说明」。

## ONNX 模型目录

默认根目录：`localdata/models/onnx`（`config.json` → `paths.models_onnx_dir`）

```
localdata/models/onnx/
  detection/          # 人体检测（RTMDet）
    rtmdet_nano/end2end.onnx   # det_variant=nano，320×320
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
python scripts/setup/download_onnx_models.py
python scripts/setup/download_onnx_models.py --det nano,m --pose t
```

复用 visual-dps 已有权重时：

```bash
python collect_pose.py --video test.mp4 --models-dir ../visual-dps/localdata/models/rtmpose_onnx
```

## 命令行

```bash
python collect_pose.py --video test.mp4 --backend rtmpose_t --det-variant m
```

未指定 `-o` 时写入 `localdata/json/rtmpose-t/_ungrouped/{视频主名}_{backend}/`（未分组机位）；指定 `--camera-label` 时写入对应机位子目录。

采集须带有效货框标注（上传 JSON、`--camera-label` 机位 reflection，或 `localdata/json/annotations/{视频主名}.json`）；`--skeleton-only` 可跳过标注：

```bash
python collect_pose.py --video test.mp4 --annotation path/to/boxes.json
python collect_pose.py --video test.mp4 --backend rtmpose_t --det-variant m --save-video
python collect_pose.py --video /path/to/clip.mp4 --camera-label 1-2组-1 --save-video
python collect_pose.py --video /path/to/clip.mp4 --skeleton-only --variant t --save-video
```

Web 采集页：无本地标注时会提示去「标注」页或上传 JSON，服务端拒绝无标注采集。

### 文件夹递归批处理（CLI 一键，无需启动 Web）

对齐 Web 采集页「文件夹批处理」，递归处理根目录及全部子文件夹内的视频。输入文件夹须**互不相同**；同机位多批数据用后缀区分：

| 输入文件夹（须唯一） | 机位（reflection） | 输出目录 |
|---------------------|-------------------|----------|
| `1-2组-1` | `1-2组-1` | `localdata/json/1-2-1/` |
| `1-2组-1(2)` | `1-2组-1` | `localdata/json/1-2-1-(2)/` |
| `1-2组-1(3)` | `1-2组-1` | `localdata/json/1-2-1-(3)/` |

无 `(2)` 后缀且输出目录已占用时，也会自动分配 `1-2-1-(2)`…

```bash
# 仅骨架（不算碰撞）
python scripts/collect/batch_skeleton_collect.py D:/videos/1-2组-1 --camera-label 1-2组-1
python scripts/collect/batch_skeleton_collect.py D:/videos/1-2组-1 --camera-label 1-2组-1 --save-video

# 多机位：root 下第一级子目录名作机位（D:/videos/1-2组-1/*.mp4、D:/videos/1-2组-2/*.mp4）
python scripts/collect/batch_skeleton_collect.py D:/videos --group-by-subfolder

# 骨架 + 碰撞（需 reflection.json 与 localdata/json/annotations/{编号}.json）
python scripts/collect/batch_skeleton_collect.py D:/videos --group-by-subfolder --with-collision
python scripts/collect/batch_skeleton_collect.py D:/videos/1-2组-1 --camera-label 1-2组-1 --with-collision

# 预览待处理列表
python scripts/collect/batch_skeleton_collect.py D:/videos --group-by-subfolder --dry-run

# 已存在 manifest.json 的记录跳过
python scripts/collect/batch_skeleton_collect.py D:/videos/1-2组-1 --camera-label 1-2组-1 --skip-existing
```

推理参数与 `collect_pose.py` 相同（`--backend`、`--frame-rate`、`--no-save-video` 等），读取 `config.json`。

CLI 批处理（`--with-collision`）**复用** `reflection.json` 指向的 `annotations/{编号}.json`，不会为每个视频新建 `clip_*.json`。记录包内仅保留一份 `annotation.json` 副本供回放。若历史批处理产生大量 `annotations/clip_*.json`，可安全删除（须保留 `71.json` 等 reflection 编号源文件）；已采集记录的骨架与碰撞数据在 `skeleton.parquet` / `timeline.parquet` 中，不受影响。

开启 `save_video` 时，配套视频**仅复制**到 `localdata/video/{机位slug}/`，**不会移动或删除**批处理源目录（如 `D:/.../skeleton-video/video`）中的原始 MP4。

### 数据迁移与合并（CLI）

```bash
# 旧扁平目录 → rtmpose-t 模型层
python scripts/data/migrate_pose_model_tiers.py --dry-run

# 同机位 -(2)/(3) slug 归并（同名记录自动加后缀，不覆盖）
python scripts/data/consolidate_camera_slugs.py --tier rtmpose-t --dry-run

# 跨机器合并导出数据
python scripts/data/merge_pose_tier_data.py --source /path/to/export --tier rtmpose-t --dry-run
```

详见 [`scripts/README.md`](scripts/README.md)。

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
3. 采集页：**单视频** 或 **文件夹批处理**（同一机位多视频）。批处理仅对**第一个视频**展示首帧预览，填写机位标识后 **开始批处理**；结果写入 `localdata/json/{机位目录}/`（如 `1-6组-2` → `1-6-2`）。若该机位目录已存在且非空，自动使用 `1-6-2-(2)`、`1-6-2-(3)` … 避免覆盖。回放页按机位分组展示记录。
4. **批处理进度**：Web 采集页显示总进度条、当前视频/帧、已用时间与**预计剩余时间**（按已完成视频平均耗时估算）。
5. **采集配置快照**：每次保存会在 `{record_id}.meta.json` 与 `manifest.json` 中写入 `collect_config`（backend、推理尺寸、frame_rate、碰撞参数、机位等）；批处理另生成 `localdata/json/{机位目录}/_batch_{batch_id}.json` 汇总清单。

`config.json` → `reflection.path` 默认为 `reflection.json`。

## 问题记录

见 **[problem.md](./problem.md)**：仓库/采集过程中观察到的问题（现象、原因、建议方案）。按编号追加，便于与代码变更对照；**未标注已修复的条目默认未实现**。
