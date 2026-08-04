/**
 * 货框与人员高亮的每帧热路径上有三处重复计算，这里逐个用计数锁住缓存效果：
 *   1. boxTokenLookupKeys 要遍历全部货框比字符串，每框每帧被调八次
 *   2. getReviewPersonAccentStyle 每次调用都重建整个调色板
 *   3. 人物标签宽度每人每帧量一次 measureText
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const appDir = path.join(__dirname, "..", "web", "app");
const source = fs.readFileSync(path.join(appDir, "10-render-collision.js"), "utf8");
const reviewSource = fs.readFileSync(path.join(appDir, "08-event-review.js"), "utf8");

const pickFrom = (text, names) =>
  names
    .map((name) => {
      const match = text.match(new RegExp(`\\nfunction ${name}\\(([\\s\\S]*?)\\n}`));
      assert.ok(match, `未找到函数 ${name}`);
      return match[0];
    })
    .join("\n");
const pick = (names) => pickFrom(source, names);

// 缓存状态与常量声明也要一起搬进来。
const decls = source.match(
  /const EMPTY_BOX_TOKEN_KEYS[\s\S]*?let boxTokenLookupKeysCacheLength = -1;/
);
assert.ok(decls, "未找到缓存声明");

const context = vm.createContext({ console, Map, Set, String, Number, Object, Array });
vm.runInContext(
  `
  var annotationBoxes = [];
  var computeCalls = 0;
  ${/* VM 脚本里 const/let 只留在脚本词法作用域，挂不到 context 上，测试改不到 */ ""}
  ${decls[0].replace(/^(const|let) /gm, "var ")}
  ${pick([
    "boxCollisionToken",
    "computeBoxTokenLookupKeys",
    "boxTokenLookupKeys",
    "tokenInCollisionSet",
    "tokenInTokenSet",
    "tokenValueInTokenMap",
  ])}
  // 统计真正做全表扫描的次数。
  var rawCompute = computeBoxTokenLookupKeys;
  computeBoxTokenLookupKeys = function (t) {
    computeCalls += 1;
    return rawCompute(t);
  };
  `,
  context
);

const { boxTokenLookupKeys } = context;
const box = (id) => ({ box_id: id });
context.annotationBoxes = [box("2011"), box("2012"), box("2013")];

// --- 等价性 ---

assert.deepEqual([...boxTokenLookupKeys("Box_2011")], ["Box_2011"]);
// shelf:id 写法要同时给出 Box_ 形式，这是复核高亮能跨格式命中的关键。
assert.deepEqual([...boxTokenLookupKeys("A1:2012")], ["A1:2012", "Box_2012"]);
assert.deepEqual([...boxTokenLookupKeys("")], []);
assert.deepEqual([...boxTokenLookupKeys(null)], []);
// 首尾空格与原实现一样先 trim。
assert.deepEqual([...boxTokenLookupKeys("  Box_2011  ")], ["Box_2011"]);

// --- 缓存命中：同一 token 只算一次 ---

context.boxTokenLookupKeysCacheSource = null; // 强制重建，模拟刚换记录
context.computeCalls = 0;
for (let i = 0; i < 50; i++) boxTokenLookupKeys("Box_2011");
assert.equal(context.computeCalls, 1, "同一 token 反复查应只计算一次");

// 模拟一帧的绘制：3 个货框 × 5 次状态查询 + 3 次冲突判定。
const tokens = ["Box_2011", "Box_2012", "Box_2013"];
const alarmSet = new Set(["Box_2011"]);
const missTokens = new Set();
const confirmed = new Map([["Box_2012", { fill: "x" }]]);
const drawFrame = () => {
  tokens.forEach((token) => {
    context.tokenInCollisionSet(token, alarmSet);
    context.tokenInCollisionSet(token, alarmSet);
    context.tokenValueInTokenMap(token, confirmed);
    context.tokenInTokenSet(token, missTokens);
    context.tokenInTokenSet(token, missTokens);
    context.tokenInCollisionSet(token, alarmSet);
    context.tokenValueInTokenMap(token, confirmed);
    context.tokenInTokenSet(token, missTokens);
  });
};

context.computeCalls = 0;
context.boxTokenLookupKeysCacheSource = null; // 强制重建，模拟刚换记录
for (let frame = 0; frame < 30; frame++) drawFrame();
// 30 帧 × 3 框 × 8 次查询 = 720 次调用，但只有 3 个不同 token，
// 缓存后全表扫描应只发生 3 次。
assert.equal(context.computeCalls, 3, `30 帧只应扫描 3 次，实际 ${context.computeCalls}`);

// --- 失效：货框换了必须重算 ---

context.computeCalls = 0;
context.annotationBoxes = [box("2011")];
boxTokenLookupKeys("Box_2011");
assert.equal(context.computeCalls, 1, "annotationBoxes 换了要重算");

// 长度不变但数组换了（换记录后货框数恰好相同）也要重算。
context.computeCalls = 0;
context.annotationBoxes = [box("3011")];
assert.deepEqual([...boxTokenLookupKeys("A1:3011")], ["A1:3011", "Box_3011"]);
assert.equal(context.computeCalls, 1, "同长度不同数组也要重算");

// --- 返回值是共享数组，必须防止被就地改坏 ---

const shared = boxTokenLookupKeys("Box_3011");
assert.equal(boxTokenLookupKeys("Box_3011"), shared, "应复用同一个数组");
// 用消息匹配：VM realm 的 TypeError 不是宿主的同一个构造器。
assert.throws(() => shared.push("Box_9999"), /not extensible/, "共享数组必须冻结");

// --- 人物配对色：调色板不能每次调用重建 ---

const accentDecl = reviewSource.match(
  /const REVIEW_PERSON_ACCENT_PALETTE = Object\.freeze\(\[[\s\S]*?\n\]\);/
);
assert.ok(accentDecl, "调色板必须提到模块级，不能建在函数体里");

const accentCtx = vm.createContext({ console, Map, Set, String, Number, Object, Math });
vm.runInContext(
  `
  ${accentDecl[0]}
  var stablePersonIdentityTrackHints = new Map();
  function getStablePersonDisplayInfoByRawId(_frameIdx, pid) {
    return { stableId: Number(pid) };
  }
  function getPersonTrackIdAtFrame() { return ""; }
  ${pickFrom(reviewSource, ["reviewPersonAccentIndex", "getReviewPersonAccentStyle"])}
  `,
  accentCtx
);

const ev = { frame_idx: 7 };
const accentA = accentCtx.getReviewPersonAccentStyle(ev, 0, 0);
assert.equal(accentA.name, "purple");
assert.equal(
  accentCtx.getReviewPersonAccentStyle(ev, 0, 0),
  accentA,
  "同一配对色应复用同一个对象，而不是每帧新建调色板"
);
assert.equal(accentCtx.getReviewPersonAccentStyle(ev, 1, 0).name, "orange");
// 四色循环：第五个人回到紫色。
assert.equal(
  accentCtx.getReviewPersonAccentStyle(ev, 4, 0),
  accentA,
  "稳定身份每 4 个循环一次配色"
);
// 非严格模式下改冻结对象是静默失败，直接断言冻结本身。
assert.ok(Object.isFrozen(accentA), "共享的配对色对象必须冻结");
const originalFill = accentA.fill;
accentA.fill = "red";
assert.equal(accentA.fill, originalFill, "共享的配对色不该被就地改坏");

// --- 人物标签宽度：measureText 是真实 canvas 调用，不该每人每帧重量 ---

const labelHelper = source.match(
  /const PERSON_LABEL_FONT =[\s\S]*?\nfunction measureLabelWidth\([\s\S]*?\n}/
);
assert.ok(labelHelper, "未找到 measureLabelWidth");

const labelCtx = vm.createContext({ console, Map });
vm.runInContext(
  `
  var measureCalls = 0;
  var ctx = {
    font: "10px serif",
    measureText(text) {
      measureCalls += 1;
      return { width: String(text).length * 7 };
    },
  };
  ${labelHelper[0]}
  `,
  labelCtx
);

const { measureLabelWidth, PERSON_LABEL_FONT } = labelCtx;
assert.equal(measureLabelWidth("人物1", PERSON_LABEL_FONT), 21);
// 两个人物 × 30 帧，只有两种文本，量两次就够。
for (let frame = 0; frame < 30; frame++) {
  measureLabelWidth("人物1", PERSON_LABEL_FONT);
  measureLabelWidth("人物2", PERSON_LABEL_FONT);
}
assert.equal(labelCtx.measureCalls, 2, `30 帧只应量 2 次，实际 ${labelCtx.measureCalls}`);
// 字体变了要重量，否则换字号后标签宽度会错。
measureLabelWidth("人物1", "bold 20px system-ui");
assert.equal(labelCtx.measureCalls, 3);
// 量完必须把字体还原，不能给调用方留下时有时无的 ctx 状态。
assert.equal(labelCtx.ctx.font, "10px serif", "measureLabelWidth 不该泄漏 ctx.font");

console.log("render hot path cache tests passed");
