/** 回放舞台 DOM 与尺寸监听 */
// --- 回放 ---
const videoEl = $("#playback-video-el");
const canvas = $("#playback-canvas");
const ctx = canvas.getContext("2d");
const seekBar = $("#seek-bar");
const timeLabel = $("#time-label");
const eventMarkersEl = $("#seek-event-markers");
const reviewMarkersEl = $("#seek-review-markers");
const accuracyMarkersEl = $("#seek-accuracy-markers");
const eventJumpList = $("#event-jump-list");
const eventFilterSelect = $("#event-filter");
const eventCountLabel = $("#event-count-label");
const eventsPanel = $("#playback-events-panel");
const playbackSpeedSelect = $("#playback-speed");
const stageWrap = document.querySelector(".playback-layout-main .stage-wrap");
/** 当前播放倍速（1 = 原速） */
let playbackSpeed = 1;

function applyPlaybackSpeed() {
  if (!videoEl) return;
  const rate = Number.isFinite(playbackSpeed) && playbackSpeed > 0 ? playbackSpeed : 1;
  videoEl.defaultPlaybackRate = rate;
  videoEl.playbackRate = rate;
}

function readPlaybackSpeedFromSelect() {
  const raw = parseFloat(playbackSpeedSelect?.value || "1");
  playbackSpeed = Number.isFinite(raw) && raw > 0 ? raw : 1;
  applyPlaybackSpeed();
}

/**
 * 代码里改倍速，效果等同用户自己选一次下拉框。
 * 必须写回 select：换记录、loadedmetadata、开始播放都会重新 readPlaybackSpeedFromSelect()，
 * 只改 playbackSpeed 会在下一次加载时被冲掉。
 * @returns {number} 改动前的倍速，便于调用方还原
 */
function setPlaybackSpeed(value) {
  const rate = Number(value);
  const previous = playbackSpeed;
  if (!Number.isFinite(rate) || rate <= 0) return previous;
  if (playbackSpeedSelect) {
    playbackSpeedSelect.value = String(rate);
    readPlaybackSpeedFromSelect();
  } else {
    playbackSpeed = rate;
    applyPlaybackSpeed();
  }
  // 无视频的 JSON 逐帧播放靠定时器控速，改完要重启才生效。
  if (typeof restartJsonOnlyPlaybackIfActive === "function") {
    restartJsonOnlyPlaybackIfActive();
  }
  return previous;
}

/** 舞台加载遮罩（别名，供 records 模块调用） */
function showStageLoading(text) {
  showPlaybackStageLoading(text);
}

function hideStageLoading() {
  hidePlaybackStageLoading();
}

function updateStageLoading(text) {
  updatePlaybackStageLoading(text);
}

/** 舞台加载遮罩 */
function showPlaybackStageLoading(text = "加载中…") {
  const el = document.getElementById("playback-stage-loading");
  if (!el) return;
  const textEl = el.querySelector(".playback-stage-loading-text");
  if (textEl) textEl.textContent = text;
  el.classList.remove("hidden");
}

function hidePlaybackStageLoading() {
  const el = document.getElementById("playback-stage-loading");
  if (el) el.classList.add("hidden");
}

function updatePlaybackStageLoading(text) {
  const el = document.getElementById("playback-stage-loading");
  if (!el) return;
  const textEl = el.querySelector(".playback-stage-loading-text");
  if (textEl) textEl.textContent = text;
  el.classList.remove("hidden");
}

/** 舞台尺寸变化时重算 canvas 并强制重绘（避免退出全屏/窗口缩放后骨架卡住） */
function bindStageLayoutWatch() {
  if (!stageWrap || stageWrap.dataset.layoutWatch) return;
  stageWrap.dataset.layoutWatch = "1";

  let layoutTimer = null;
  const onLayoutChange = () => {
    if (layoutTimer) clearTimeout(layoutTimer);
    layoutTimer = setTimeout(() => {
      layoutTimer = null;
      // 播放中也要重建冻结布局：专注模式全屏会改变舞台尺寸。
      if (typeof refreshPlaybackStageLayout === "function") {
        refreshPlaybackStageLayout();
      } else {
        if (typeof invalidateDisplayLayoutCache === "function") {
          invalidateDisplayLayoutCache();
        }
        syncCanvasSize({ force: true });
        redrawCurrentFrame();
      }
    }, 50);
  };

  if (typeof ResizeObserver !== "undefined") {
    const ro = new ResizeObserver(onLayoutChange);
    ro.observe(stageWrap);
  }
  window.addEventListener("resize", onLayoutChange);
  document.addEventListener("fullscreenchange", onLayoutChange);
  videoEl.addEventListener("webkitbeginfullscreen", onLayoutChange);
  videoEl.addEventListener("webkitendfullscreen", onLayoutChange);
}
