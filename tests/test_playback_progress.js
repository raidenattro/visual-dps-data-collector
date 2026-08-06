const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "..", "web", "app", "10-render-collision.js"),
  "utf8"
);
const start = source.indexOf("function playbackFrameProgress");
const end = source.indexOf("function syncCanvasSize", start);
assert.ok(start >= 0 && end > start);

// 帧号→位置索引在 00-core.js 里，进度计算依赖它，一起放进沙箱执行。
const core = fs.readFileSync(path.join(__dirname, "..", "web", "app", "00-core.js"), "utf8");
const coreStart = core.indexOf("let frameByTimePositionCache");
const coreEnd = core.indexOf("let frameCache = new Map();", coreStart);
assert.ok(coreStart >= 0 && coreEnd > coreStart);

const frameByTime = Array.from({ length: 502 }, (_, idx) => ({
  frameIdx: idx + 1,
  t: idx / 25,
}));
const context = vm.createContext({
  console,
  Number,
  Math,
  Map,
  parseInt,
  frameByTime,
  seekBar: { value: "0" },
  timeLabel: { textContent: "" },
  videoEl: { duration: 23, currentTime: 0 },
  getPlaybackAuthorityFrameIdx: () => null,
  playbackTimelineSecFromVideo: () => 0,
  formatTime: (value) => String(value),
});
vm.runInContext(`${core.slice(coreStart, coreEnd)}\n${source.slice(start, end)}`, context);

// 视频 duration 是 23 秒，而最后一帧 timeline 只有约 20 秒；
// 进度仍必须由帧位置决定并到达 100%。
context.updatePlaybackSeekBarUi(frameByTime.at(-1).t, 502);
assert.equal(Number(context.seekBar.value), 1000);

context.updatePlaybackSeekBarUi(frameByTime[0].t, 1);
assert.equal(Number(context.seekBar.value), 0);

context.updatePlaybackSeekBarUi(frameByTime[250].t, 251);
assert.ok(Number(context.seekBar.value) > 498);
assert.ok(Number(context.seekBar.value) < 501);

assert.equal(context.playbackFrameEntryForSeekValue(0).frameIdx, 1);
assert.equal(context.playbackFrameEntryForSeekValue(500).frameIdx, 252);
assert.equal(context.playbackFrameEntryForSeekValue(1000).frameIdx, 502);

// 帧号→位置索引：两万帧记录上每次 seek 都要按帧号反查，不能再线性扫描。
assert.equal(context.frameByTimePositionOf(1), 0);
assert.equal(context.frameByTimePositionOf(251), 250);
assert.equal(context.frameByTimePositionOf(502), 501);
assert.equal(context.frameByTimePositionOf(9999), -1);
assert.equal(context.frameByTimeEntryOf(251).frameIdx, 251);

// frameByTime 原地变动后必须显式作废，否则位置会停在旧的排序结果上。
frameByTime.unshift({ frameIdx: 1000, t: -1 });
context.invalidateFrameByTimeIndex();
assert.equal(context.frameByTimePositionOf(1000), 0);
assert.equal(context.frameByTimePositionOf(1), 1);

console.log("playback progress tests passed");
