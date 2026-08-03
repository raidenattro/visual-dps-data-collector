# Web 回放 / 采集前端模块

原 `app.js`（约 2800 行）按职责拆分为多个脚本，在 `index.html` 中**按编号顺序**加载，共享同一全局作用域（与 `annotate.js` 相同模式，无需打包）。

| 文件 | 职责 |
|------|------|
| `00-core.js` | 常量（COCO 连线等）、全局状态、`$` / 页签面板 |
| `01-layout-frames.js` | 布局换算、Parquet 帧分块拉取与缓存 |
| `02-playback-selection.js` | 回放记录列表选中、Tab 离开挂起 |
| `03-tabs.js` | 页签切换、采集 FPS/碰撞参数联动 |
| `04-collision-config.js` | 碰撞参数读写（表单 + localStorage） |
| `05-collect.js` | 采集表单、单视频 / 批处理、机位 reflection |
| `06-records.js` | 回放记录列表、复核状态展示、打开记录 |
| `07-playback-stage.js` | 视频 / canvas DOM、`bindStageLayoutWatch` |
| `08-event-review.js` | 事件复核（标真、取消、保存、导航） |
| `08-event-review-range.js` | 区间标真、逐帧人物身份连续性与人工确认 |
| `09-playback-events.js` | 事件列表加载、seek、清除 |
| `10-render-collision.js` | 骨架绘制、货框叠加、回放碰撞追踪 |
| `11-playback-controls.js` | 播放控件事件绑定与页面 `init` |
| `12-event-review-workspace.js` | 单帧/区间模式、撤销重试、异常导航、专注模式与长列表窗口 |

完整单文件备份：`web/app.monolith.js`（拆分前快照）。

重新拆分（修改 monolith 后）：

```bash
python scripts/archive/split_app_js.py
```

注意：脚本会读取 `web/app.js`；若已拆分，请先将 `app.monolith.js` 复制为 `app.js` 再运行。
