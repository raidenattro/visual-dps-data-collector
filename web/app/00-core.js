/** 常量与共享状态 */
/** COCO-17 骨架连线（与 visual-dps 一致） */
const COCO_LINES = [
  [15, 13], [13, 11], [16, 14], [14, 12], [11, 12], [5, 11], [6, 12], [5, 6],
  [5, 7], [6, 8], [7, 9], [8, 10], [1, 2], [0, 1], [0, 2], [1, 3], [2, 4], [3, 5], [4, 6],
];
const SCORE_MIN = 0.3;

const $ = (sel) => document.querySelector(sel);
const tabs = document.querySelectorAll(".tab");
const panels = { collect: $("#panel-collect"), annotate: $("#panel-annotate"), playback: $("#panel-playback") };

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
/** 播放循环中已绘制的骨架帧号，避免 RAF 在相邻帧边界来回切换 */
let tickPoseFrameIdx = -1;
let currentRecordId = null;
let playbackEvents = [];
/** 事件列表来自回放实时重算（非采集落盘） */
let playbackEventsFromRealtime = false;
let activeEventKey = null;
/** 当前关联事件是否与播放位置精确对齐（否则为「最近」关联） */
let playbackEventLinkExact = false;
/** 人工标为真的事件键（eventRowKey） */
const verifiedTrueKeys = new Set();
/** 未标真前在画面上点选的货框列表（标真时一并写入 event_review） */
const pendingConfirmedBoxesByKey = new Map();
/** 用户已手动点选/重置过 box 的事件（重置后不再自动回填检测框） */
const boxAnnotationTouchedKeys = new Set();
let eventReviewSaveTimer = null;
let eventReviewSaveSeq = 0;
/** 标真并下一条后，上一条优先回到此事件（已标真事件不在「未标真」队列中） */
let reviewBackKey = null;
let currentEventReviewStatus = "not_started";
const FRAME_CHUNK_SIZE = 120;
const COLLISION_CFG_STORAGE_KEY = "datacollect_collision_cfg";
let rafId = null;
let playbackId = null;
let playbackVideoObjectUrl = null;

