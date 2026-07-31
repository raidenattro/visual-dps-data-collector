const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const eventSource = fs.readFileSync(
  path.join(__dirname, "..", "web", "app", "09-playback-events.js"),
  "utf8"
);
const seekStart = eventSource.indexOf("async function seekToTimestamp");
const seekEnd = eventSource.indexOf("async function seekToEvent", seekStart);
assert.ok(seekStart >= 0 && seekEnd > seekStart);
const seekFunctionSource = eventSource.slice(seekStart, seekEnd);

let waitCall = 0;
const renderedFrames = [];
const seekContext = vm.createContext({
  console,
  explicitFrameSeekSequence: 0,
  explicitFrameSeekInFlight: false,
  lastRenderedFrameIdx: 0,
  tickPoseFrameIdx: 0,
  lastEventSyncFrameIdx: 0,
  playbackVideoClockUsesPtsSeek: false,
  videoEl: {
    duration: 100,
    currentTime: 0,
    paused: true,
    seeking: true,
  },
  seekBar: { value: "" },
  timeLabel: { textContent: "" },
  resetPlaybackCollisionTracker() {},
  frameEntryByIdx(fi) {
    return { frameIdx: fi, t: fi / 25 };
  },
  videoSeekTimeForFrameIdx(fi) {
    return fi / 25;
  },
  videoTimeForFrameIdx(fi) {
    return fi / 25;
  },
  clearPlaybackVideoPtsSeekClock() {},
  timelineUsesZeroBase() {
    return false;
  },
  containerPtsOffsetSec() {
    return 0;
  },
  setPlaybackAuthorityFrameIdx() {},
  formatTime(value) {
    return String(value);
  },
  waitVideoSeeked() {
    waitCall += 1;
    const delay = waitCall === 1 ? 30 : 0;
    return new Promise((resolve) => setTimeout(resolve, delay));
  },
  waitPresentedVideoFrame() {
    return Promise.resolve(0);
  },
  renderSkeletonSyncedToVideo(opts) {
    renderedFrames.push(opts.frameIdx);
    return Promise.resolve(opts.frameIdx);
  },
  renderExplicitPlaybackFrame(fi) {
    renderedFrames.push(fi);
    return Promise.resolve(fi);
  },
  syncActiveEventFromPlaybackPosition() {},
  findFrameAt() {
    return null;
  },
  renderFrameEntry() {
    return Promise.resolve();
  },
  frameByTime: [],
});
vm.runInContext(seekFunctionSource, seekContext);

async function testStaleSeekCannotOverwriteLatestFrame() {
  const first = seekContext.seekToTimestamp(156 / 25, 156);
  await new Promise((resolve) => setTimeout(resolve, 1));
  const second = seekContext.seekToTimestamp(157 / 25, 157);
  await Promise.all([first, second]);
  assert.deepEqual(renderedFrames, [157]);
}

const renderSource = fs.readFileSync(
  path.join(__dirname, "..", "web", "app", "10-render-collision.js"),
  "utf8"
);
const renderStart = renderSource.indexOf(
  "async function renderSkeletonSyncedToVideo"
);
const renderEnd = renderSource.indexOf(
  "function inferSizeForFrameIdx",
  renderStart
);
assert.ok(renderStart >= 0 && renderEnd > renderStart);
const renderFunctionSource = renderSource.slice(renderStart, renderEnd);

const exactCalls = [];
const renderContext = vm.createContext({
  console,
  videoEl: { src: "clip.mp4", readyState: 4, paused: true, currentTime: 6.4 },
  frameByTime: [{ frameIdx: 157 }],
  pausedPlaybackLayout: null,
  frozenPlaybackLayout: null,
  resolveExplicitPlaybackFrameIdx(opts) {
    return Number(opts.frameIdx) || 0;
  },
  ensureFrameIndexEntry(fi) {
    return { frameIdx: fi };
  },
  getPlaybackAuthorityFrameIdx() {
    return 157;
  },
  setPlaybackAuthorityFrameIdx() {},
  resetPlaybackCollisionTracker() {},
  ensureRenderPlaybackFrameByIdx(fi) {
    exactCalls.push(fi);
    return Promise.resolve(fi);
  },
  renderPlaybackFrameAtTime() {
    throw new Error("explicit frame render must not remap through media time");
  },
});
vm.runInContext(renderFunctionSource, renderContext);

async function testExplicitFrameUsesFrameIndexDirectly() {
  const result = await renderContext.renderSkeletonSyncedToVideo({
    frameIdx: 157,
    mediaTime: 160 / 25,
    waitPresented: false,
  });
  assert.equal(result, 157);
  assert.deepEqual(exactCalls, [157]);
}

Promise.resolve()
  .then(testStaleSeekCannotOverwriteLatestFrame)
  .then(testExplicitFrameUsesFrameIndexDirectly)
  .then(() => {
    console.log("playback exact frame seek tests passed");
  })
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
