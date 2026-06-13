/** 回放舞台 DOM 与尺寸监听 */
// --- 回放 ---
const videoEl = $("#playback-video-el");
const canvas = $("#playback-canvas");
const ctx = canvas.getContext("2d");
const seekBar = $("#seek-bar");
const timeLabel = $("#time-label");
const eventMarkersEl = $("#seek-event-markers");
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

/** 舞台尺寸变化时重算 canvas 并强制重绘（避免退出全屏/窗口缩放后骨架卡住） */
function bindStageLayoutWatch() {
  if (!stageWrap || stageWrap.dataset.layoutWatch) return;
  stageWrap.dataset.layoutWatch = "1";

  let layoutTimer = null;
  const onLayoutChange = () => {
    if (layoutTimer) clearTimeout(layoutTimer);
    layoutTimer = setTimeout(() => {
      layoutTimer = null;
      syncCanvasSize();
      redrawCurrentFrame();
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
