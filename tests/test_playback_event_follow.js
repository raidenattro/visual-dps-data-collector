const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "..", "web", "app", "09-playback-events.js"),
  "utf8"
);
const start = source.indexOf("function refreshPlaybackReviewUiDuringPlay");
const end = source.indexOf("function updateEventMarkerActiveState", start);
assert.ok(start >= 0 && end > start);

const events = [
  { frame_idx: 10, timestamp_sec: 1 },
  { frame_idx: 20, timestamp_sec: 2 },
];
let realignCalls = 0;
let dockCalls = 0;
const context = vm.createContext({
  console,
  playbackEvents: events,
  activeEventKey: "10",
  playbackEventLinkExact: true,
  reviewBackKey: null,
  videoEl: { src: "clip.mp4", paused: false, ended: false, currentTime: 2 },
  poseData: { fps: 25 },
  eventRowKey: (ev) => String(ev.frame_idx),
  findEventForPlaybackPosition: (_time, frameIdx) =>
    frameIdx >= 20 ? events[1] : events[0],
  isExactEventAtPosition: (ev, _time, frameIdx) => ev.frame_idx === frameIdx,
  getCurrentPlaybackTimeSec: () => 2,
  getCurrentPlaybackFrameIdx: () => 20,
  getPinnedPlaybackEvent: () => events[0],
  eventDisplayFrameIdx: (ev) => ev.frame_idx,
  videoSeekTimeForFrameIdx: (fi) => fi / 10,
  realignPlaybackToPinnedEvent: () => {
    realignCalls += 1;
  },
  updateReviewDock: () => {
    dockCalls += 1;
  },
  patchEventReviewTableActiveState() {},
  scrollActiveEventRowIntoView() {},
  updateEventMarkerActiveState() {},
  updateStageBoxPickMode() {},
  updateEventReviewFrameNavUi() {},
  updatePlaybackReviewFrameMeta() {},
  updatePlaybackReviewPositionUi() {},
  $: () => null,
  redrawCurrentFrame() {},
});
vm.runInContext(source.slice(start, end), context);

// 即使调用方没有传 duringPlayback，只要视频正在播放，事件只能跟随视频；
// 不得使用旧的“事件钉住”状态拉回画面。
context.syncActiveEventFromPlaybackPosition({ timeSec: 2, frameIdx: 20 });
assert.equal(context.activeEventKey, "20");
assert.equal(context.playbackEventLinkExact, false);
assert.equal(realignCalls, 0);
assert.equal(dockCalls, 1);

const indexHtml = fs.readFileSync(
  path.join(__dirname, "..", "web", "index.html"),
  "utf8"
);
assert.match(indexHtml, /id="play-btn"/);
assert.doesNotMatch(indexHtml, /id="pause-btn"/);

console.log("playback event follow tests passed");
