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

const frameByTime = Array.from({ length: 502 }, (_, idx) => ({
  frameIdx: idx + 1,
  t: idx / 25,
}));
const context = vm.createContext({
  console,
  Number,
  Math,
  frameByTime,
  seekBar: { value: "0" },
  timeLabel: { textContent: "" },
  videoEl: { duration: 23, currentTime: 0 },
  getPlaybackAuthorityFrameIdx: () => null,
  playbackTimelineSecFromVideo: () => 0,
  formatTime: (value) => String(value),
});
vm.runInContext(source.slice(start, end), context);

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

console.log("playback progress tests passed");
