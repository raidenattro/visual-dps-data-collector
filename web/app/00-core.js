/** 常量与共享状态 */
/** COCO-17 骨架连线（与 visual-dps 一致） */
const COCO_LINES = [
  [15, 13], [13, 11], [16, 14], [14, 12], [11, 12], [5, 11], [6, 12], [5, 6],
  [5, 7], [6, 8], [7, 9], [8, 10], [1, 2], [0, 1], [0, 2], [1, 3], [2, 4], [3, 5], [4, 6],
];
const SCORE_MIN = 0.3;

const $ = (sel) => document.querySelector(sel);
const tabs = document.querySelectorAll(".tab");
const panels = {
  collect: $("#panel-collect"),
  annotate: $("#panel-annotate"),
  accuracy: $("#panel-accuracy"),
  sandbox: $("#panel-sandbox"),
  playback: $("#panel-playback"),
};

let poseData = null;
let annotationBoxes = [];
let annotationSize = null;
let frameByTime = [];
let frameCache = new Map();
/** 已拉取的 Parquet 分块 "from-to"，避免播放时重复请求 */
const loadedChunkKeys = new Set();
const prefetchPromises = new Map();
let renderGeneration = 0;
let lastRenderedFrameIdx = -1;
/** 播放循环中已绘制的骨架帧号 */
let tickPoseFrameIdx = -1;
/** 视频当前帧号（tick 内立即更新，避免缓存未命中时重复触发渲染） */
let tickVideoFrameIdx = -1;
let currentRecordId = null;
let playbackEvents = [];
/** 采集 /events 原始事件（变体切换时保留标真范本） */
let playbackEventsBaseline = [];
/** 当前 playbackEvents 来自碰撞变体 sidecar */
let playbackEventsFromVariant = false;
let playbackActiveVariantKey = null;
/** 事件列表来自回放实时重算（非采集落盘） */
let playbackEventsFromRealtime = false;
let activeEventKey = null;
/** 当前关联事件是否与播放位置精确对齐（否则为「最近」关联） */
let playbackEventLinkExact = false;
/** 人工标为真的事件键（eventRowKey） */
const verifiedTrueKeys = new Set();
/** 画面上点选的货框（按 Y 标真时一并写入 event_review.json） */
const pendingConfirmedBoxesByKey = new Map();
/** 用户已手动点选/重置过 box 的事件（重置后不再自动回填检测框） */
const boxAnnotationTouchedKeys = new Set();
let eventReviewSaveTimer = null;
let eventReviewSaveSeq = 0;
/** 标真并下一条后，上一条优先回到此事件（已标真事件不在「未标真」队列中） */
let reviewBackKey = null;
let currentEventReviewStatus = "not_started";
const FRAME_CHUNK_SIZE = 200;
/** 打开记录时并行预取的分块数（200×8 ≈ 64s@25fps） */
const FRAME_CHUNK_PREFETCH_INITIAL = 8;
/** 播放中提前预取的下一块数量 */
const FRAME_CHUNK_PREFETCH_AHEAD = 4;
/** 播放时按视频时间前瞻预取的秒数 */
const FRAME_CHUNK_PREFETCH_LOOKAHEAD_SEC = 4;
/** 块内进度超过该比例时触发下一块预取 */
const FRAME_CHUNK_PREFETCH_PROGRESS = 0.5;
const COLLISION_CFG_STORAGE_KEY = "datacollect_collision_cfg";
const DET_BBOX_STORAGE_KEY = "datacollect_playback_show_det_bbox";
/** 回放叠加 RTMDet 人体框（person.bbox） */
let showDetBbox = false;
/** getDisplayLayout 缓存（窗口/视频尺寸变化时失效） */
let cachedDisplayLayout = null;
let cachedDisplayLayoutKey = "";
/** frame_idx → events[]，加速播放时事件定位 */
let playbackEventsFrameIndex = new Map();
/** renderAtTime 合并：避免慢绘制时叠多个 in-flight 请求 */
let renderAtTimeInflight = false;
let renderAtTimePendingTime = null;
/** 上次已同步到事件栏的骨架帧号 */
let lastEventSyncFrameIdx = -1;
let rafId = null;
let playbackId = null;
let playbackVideoObjectUrl = null;
/** 全部分块已入 frameCache，播放时不再请求网络 */
let playbackSkeletonReady = false;
/** 播放期间冻结的布局（避免每帧 getBoundingClientRect） */
let frozenPlaybackLayout = null;
/** 播放期间冻结的 canvas CSS 尺寸 */
let frozenPlaybackCanvasCss = null;
/** 播放 UI 节流时间戳 */
let lastPlaybackUiSyncMs = 0;

/** 从碰撞 token 提取货位 box_id（Box_3098、MAP_19:3098 → 3098） */
function parseBoxIdFromToken(token) {
  const t = String(token || "").trim();
  if (!t) return "";
  if (t.startsWith("Box_")) return t.slice(4).trim();
  if (t.includes(":")) return t.split(":").pop().trim();
  return t;
}

/** 规范碰撞 token：统一为 Box_{box_id} */
function canonicalBoxToken(token) {
  const id = parseBoxIdFromToken(token);
  return id ? `Box_${id}` : "";
}

/** 去重排序后的规范 token 列表 */
function canonicalizeBoxTokenList(tokens) {
  const seen = new Set();
  const out = [];
  (tokens || []).forEach((raw) => {
    const canon = canonicalBoxToken(String(raw).trim());
    if (!canon || seen.has(canon)) return;
    seen.add(canon);
    out.push(canon);
  });
  out.sort();
  return out;
}

