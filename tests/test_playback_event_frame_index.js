/**
 * 按帧取事件原先是全表 filter，一条记录上万条事件，所以复核高亮长期只在暂停时才算，
 * 播放时人工确认的紫色/配对色完全不出现。这里验证索引的等价性与失效时机。
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "..", "web", "app", "08-event-review.js"),
  "utf8"
);

const pick = (names) =>
  names
    .map((name) => {
      const match = source.match(new RegExp(`\\nfunction ${name}\\(([\\s\\S]*?)\\n}`));
      assert.ok(match, `未找到函数 ${name}`);
      return match[0];
    })
    .join("\n");

const context = vm.createContext({ console, Map, Set, Number, Array, String, parseInt });
vm.runInContext(
  `
  var playbackEvents = [];
  var playbackEventFrameIndex = null;
  var playbackEventFrameIndexSource = null;
  ${pick([
    "eventDisplayFrameIdx",
    "eventSourceFrameIdx",
    "eventMatchesPlaybackFrame",
    "getPlaybackEventFrameIndex",
    "getEventsOnFrame",
  ])}
  // 等价性基准：索引化之前的实现。
  function getEventsOnFrameByScan(frameIdx) {
    const fi = parseInt(frameIdx, 10) || 0;
    return playbackEvents.filter((e) => eventMatchesPlaybackFrame(e, fi));
  }
  `,
  context
);

const { getEventsOnFrame, getEventsOnFrameByScan } = context;

const ev = (frameIdx, sourceFrameIdx = null) => {
  const item = { event_type: "collision", frame_idx: frameIdx, box_tokens: ["Box_1"] };
  if (sourceFrameIdx != null) item.source_frame_idx = sourceFrameIdx;
  return item;
};

// source_frame_idx 与 frame_idx 不同的事件必须两个帧号都能查到，
// 这正是 eventMatchesPlaybackFrame 的规则。
const a = ev(100);
const b = ev(200, 205);
const c = ev(100);
context.playbackEvents = [a, b, c];

// VM 里造的数组来自另一个 realm，原型不同，展开成本地数组再比。
const on = (fi) => [...getEventsOnFrame(fi)];

assert.deepEqual(on(100), [a, c]);
assert.deepEqual(on(200), [b]);
assert.deepEqual(on(205), [b], "source_frame_idx 也要能命中");
assert.deepEqual(on(999), []);
assert.deepEqual(on(0), []);

// 与旧的全表扫描逐帧等价。
for (const fi of [0, 100, 200, 205, 300, 999]) {
  assert.deepEqual(
    on(fi),
    [...getEventsOnFrameByScan(fi)],
    `帧 ${fi} 与全表扫描结果不一致`
  );
}

// 返回值必须是副本：调用方增删不能改到索引内部。
getEventsOnFrame(100).push(ev(100));
assert.equal(on(100).length, 2, "返回内部数组会被调用方污染");

// playbackEvents 整体替换后索引必须重建，否则会一直画旧帧的高亮。
const d = ev(300);
context.playbackEvents = [d];
assert.deepEqual(on(300), [d]);
assert.deepEqual(on(100), [], "换记录后旧帧不该再命中");

// 索引只存事件引用，标真/配对状态每次实时读，所以就地改事件对象立刻可见。
d.verified_true = true;
assert.equal(getEventsOnFrame(300)[0].verified_true, true);

console.log("playback event frame index tests passed");
