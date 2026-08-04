/**
 * 区间标真与按 Y 全量写入都不走 setEventVerified()，草稿曾因此永远消化不掉：
 * 数据已落盘，「有未保存修改」却一直挂着，也指不出是哪一帧。
 * 这里直接跑 pruneSettledEventReviewDrafts 的真实行为。
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const appDir = path.join(__dirname, "..", "web", "app");
const read = (name) => fs.readFileSync(path.join(appDir, name), "utf8");

const context = vm.createContext({
  console,
  Map,
  Set,
  Number,
  Math,
  String,
  Array,
  JSON,
  parseInt,
  parseFloat,
  isNaN,
  setTimeout,
  clearTimeout,
});

// 只取被测函数依赖的那几段，避免把整份 08 连同 DOM 依赖一起拉进来。
const core = read("00-core.js");
const review = read("08-event-review.js");
const pick = (source, names) =>
  names
    .map((name) => {
      const match = source.match(
        new RegExp(`\\nfunction ${name}\\(([\\s\\S]*?)\\n}`)
      );
      assert.ok(match, `未找到函数 ${name}`);
      return match[0];
    })
    .join("\n");

vm.runInContext(
  `
  // 必须用 var：VM 脚本里 const/let 只留在脚本词法作用域，挂不到 context 上。
  var pendingConfirmedBoxesByKey = new Map();
  var pendingReviewBindingsByKey = new Map();
  var pendingPersonIdByKey = new Map();
  var playbackEvents = [];
  var frameCache = new Map();
  ${pick(core, ["parseBoxIdFromToken", "canonicalBoxToken", "canonicalizeBoxTokenList"])}
  ${pick(review, [
    "normalizeBoxTokenList",
    "eventRowKey",
    "normalizeReviewBindings",
    "getPersonTrackIdAtFrame",
    "addFrameTrackIdToBindings",
    "eventPersistedBindings",
    "getEventPersistedPersonId",
    "reviewBoxListSignature",
    "reviewBindingsSignature",
    "pruneSettledEventReviewDrafts",
  ])}
  `,
  context
);

const {
  pendingConfirmedBoxesByKey,
  pendingReviewBindingsByKey,
  pendingPersonIdByKey,
  eventRowKey,
  pruneSettledEventReviewDrafts,
} = context;

const makeEvent = (frameIdx, extra = {}) => ({
  event_type: "collision",
  frame_idx: frameIdx,
  box_tokens: ["Box_1"],
  ...extra,
});

// 区间标真后的现场：磁盘已带上 bindings，草稿是首帧选人时留下的同一个值。
const settled = makeEvent(100, {
  bindings: [{ confirmed_box_tokens: ["Box_1"], person_id: 0 }],
});
// 真正还没落盘的草稿：选了人，磁盘上什么都没有。
const dirty = makeEvent(200);
context.playbackEvents = [settled, dirty];

pendingPersonIdByKey.set(eventRowKey(settled), 0);
pendingPersonIdByKey.set(eventRowKey(dirty), 1);
pendingReviewBindingsByKey.set(settled ? eventRowKey(settled) : "", [
  { confirmed_box_tokens: ["Box_1"], person_id: 0 },
]);
// 事件已不在 playbackEvents 里的孤儿键：既显示不出来也提交不掉，必须清掉，
// 否则「有未保存修改」永久卡死。
pendingConfirmedBoxesByKey.set("collision:999:Box_9", ["Box_9"]);

const dropped = pruneSettledEventReviewDrafts();

assert.equal(dropped, 3, "已落盘的人员/配对草稿与孤儿键都应清掉");
assert.equal(pendingPersonIdByKey.has(eventRowKey(settled)), false);
assert.equal(pendingReviewBindingsByKey.size, 0);
assert.equal(pendingConfirmedBoxesByKey.size, 0);
// 真正未落盘的草稿必须留着，否则会丢掉「先选人再点货框」中途的选择。
assert.equal(pendingPersonIdByKey.get(eventRowKey(dirty)), 1);

// 顺序不同但内容相同的货框，应判为已落盘。
pendingPersonIdByKey.clear();
const reordered = makeEvent(300, {
  bindings: [{ confirmed_box_tokens: ["Box_2", "Box_5"], person_id: 1 }],
});
context.playbackEvents = [reordered];
pendingReviewBindingsByKey.set(eventRowKey(reordered), [
  { confirmed_box_tokens: ["Box_5", "Box_2"], person_id: 1 },
]);
assert.equal(pruneSettledEventReviewDrafts(), 1);
assert.equal(pendingReviewBindingsByKey.size, 0);

// 货框有实质差异时不能清。
const changed = makeEvent(400, {
  bindings: [{ confirmed_box_tokens: ["Box_2"], person_id: 1 }],
});
context.playbackEvents = [changed];
pendingReviewBindingsByKey.set(eventRowKey(changed), [
  { confirmed_box_tokens: ["Box_7"], person_id: 1 },
]);
assert.equal(pruneSettledEventReviewDrafts(), 0);
assert.equal(pendingReviewBindingsByKey.size, 1);

console.log("event review draft prune tests passed");
