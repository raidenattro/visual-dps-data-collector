/**
 * 2026-07-07 的回放性能优化让播放时丢了两样东西：
 *   1. drawDetBboxes 只在 full 模式调用 → 人形框播放时整片消失
 *   2. collisionSetsForPlaybackFrame 直接返回空集 → 货框只剩淡绿，红/黄碰撞色不出来
 * 这里锁住修复，并验证喂给有状态追踪器的次数没被重复渲染放大。
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const renderPath = path.join(__dirname, "..", "web", "app", "10-render-collision.js");
const render = fs.readFileSync(renderPath, "utf8");

// --- 源码结构：两条绘制路径不能再各自维护一份颜色/图元决定 ---

const drawFrame = render.match(/function drawSkeletonFrame\([\s\S]*?\n}/);
assert.ok(drawFrame, "未找到 drawSkeletonFrame");
// 人形框必须在 lite/full 之外无条件调用，且拿到与骨架同一个 layout。
assert.match(drawFrame[0], /drawDetBboxes\(frame, inferW, inferH, layout\)/);
assert.equal(
  (drawFrame[0].match(/drawDetBboxes\(/g) || []).length,
  1,
  "drawDetBboxes 应只在模式分支外调用一次"
);
const liteBranch = drawFrame[0].match(/if \(mode === "lite"\) \{[\s\S]*?\n  \} else \{/);
assert.ok(liteBranch);
assert.doesNotMatch(liteBranch[0], /drawDetBboxes/);

// 播放专用的「碰撞恒为空」分支必须彻底消失，否则两条路径还会再次漂移。
assert.doesNotMatch(render, /collisionSetsForPlaybackFrame/);
assert.doesNotMatch(render, /播放热路径不实时算碰撞/);

// lite 与 full 必须共用同一份上色决定，不能各留一份。
assert.doesNotMatch(render, /drawAnnotationBoxesCollisionOnly/);
assert.match(liteBranch[0], /drawAnnotationBoxesAccentOnly\(frame, inferW, inferH, collisionSets\)/);
const fullBoxes = render.match(/function drawAnnotationBoxes\(frame[\s\S]*?\n}/);
assert.ok(fullBoxes);
assert.match(fullBoxes[0], /paintAnnotationBox\(displayPts, resolveAnnotationBoxState\(token, ctxSets\)\)/);
const accentOnly = render.match(/function drawAnnotationBoxesAccentOnly\([\s\S]*?\n}/);
assert.ok(accentOnly);
// 播放路径必须拿到人工确认色与漏报/误报色，而不只是碰撞集。
assert.match(accentOnly[0], /getReviewBoxHighlightContext\(frameIdx\)/);
assert.match(accentOnly[0], /getAccuracyOutlineForFrame\(frameIdx, alarmSet\)/);
assert.match(accentOnly[0], /paintAnnotationBox\(displayPts, state\)/);
// 淡绿底色已在烘焙静态层里，播放时不该重复描一遍。
assert.match(accentOnly[0], /annotationBoxStateIsPlain\(state\)\) return/);

// 复核高亮不能再全表扫描：播放每帧都要算。
const highlight = render.match(/function getReviewBoxHighlightContext\([\s\S]*?\n}\n/);
assert.ok(highlight);
assert.match(highlight[0], /getEventsOnFrame\(segmentFi\)/);

// 人物标签：播放时钉住的事件通常不在当前帧，必须回退到本帧标真事件取配对色。
const personLabels = render.match(/function drawPersonIdLabels\([\s\S]*?\n}\n/);
assert.ok(personLabels);
assert.match(personLabels[0], /if \(!reviewEv && labelFrameIdx > 0/);
assert.match(personLabels[0], /pairedPersonIds\.add\(Number\(binding\.person_id\)\)/);
// 已配对的单人帧在播放时也要出颜色。
assert.match(
  personLabels[0],
  /mode === "lite" && framePersons\.length < 2 && !pairedPersonIds\.size/
);

// drawDetBboxes 不能再自己去取 layout。
const detBboxes = render.match(/function drawDetBboxes\([\s\S]*?\n}/);
assert.ok(detBboxes);
assert.match(detBboxes[0], /layoutOverride \|\| getDisplayLayout\(\)/);

// 追踪器重置时记忆必须一起失效。
const resetTracker = render.match(/function resetPlaybackCollisionTracker\([\s\S]*?\n}/);
assert.ok(resetTracker);
assert.match(resetTracker[0], /invalidateTrackedCollisionMemo\(\)/);

// --- 行为：按帧记忆，避免同一帧被重复喂给有状态追踪器 ---

const pick = (names) =>
  names
    .map((name) => {
      const match = render.match(new RegExp(`\\nfunction ${name}\\(([\\s\\S]*?)\\n}`));
      assert.ok(match, `未找到函数 ${name}`);
      return match[0];
    })
    .join("\n");

const context = vm.createContext({ console, Map, Set, Number, Array, String });
vm.runInContext(
  `
  var updateCalls = [];
  var annotationBoxes = [{}];
  // 记录未持久化碰撞时才会走实时追踪器，这正是老数据的情形。
  function getEvalCollisionSetsForFrame() { return null; }
  function frameUsesStoredCollisions() { return false; }
  function getPlaybackCollisionTracker() {
    return {
      update(frame) {
        updateCalls.push(Number(frame.frame_idx));
        return { collisions: ["Box_1"], alarm_collisions: ["Box_1"] };
      },
    };
  }
  var lastTrackedCollisionFrameIdx = 0;
  var lastTrackedCollisionSets = null;
  ${pick(["invalidateTrackedCollisionMemo", "getFrameCollisionSets"])}
  `,
  context
);

const { getFrameCollisionSets, invalidateTrackedCollisionMemo } = context;

const first = getFrameCollisionSets({ frame_idx: 10 }, 640, 480);
assert.deepEqual([...first.collisionSet], ["Box_1"]);
assert.deepEqual([...first.alarmSet], ["Box_1"]);

// 同一帧重复渲染（rAF 快于帧推进、暂停后重绘）不能再喂一次：
// 追踪器按连续命中数判告警，多喂会让告警提前触发。
getFrameCollisionSets({ frame_idx: 10 }, 640, 480);
getFrameCollisionSets({ frame_idx: 10 }, 640, 480);
// VM 里的数组来自另一个 realm，原型不同，展开成本地数组再比。
const calls = () => [...context.updateCalls];
assert.deepEqual(calls(), [10], "同一帧只应喂追踪器一次");

// 帧推进后必须重算，否则颜色会卡在上一帧。
getFrameCollisionSets({ frame_idx: 11 }, 640, 480);
assert.deepEqual(calls(), [10, 11]);

// 重置后同一帧要重新计算。
invalidateTrackedCollisionMemo();
getFrameCollisionSets({ frame_idx: 11 }, 640, 480);
assert.deepEqual(calls(), [10, 11, 11]);

// 没有货框时不该惊动追踪器。
context.annotationBoxes = [];
invalidateTrackedCollisionMemo();
const empty = getFrameCollisionSets({ frame_idx: 12 }, 640, 480);
assert.equal(empty.collisionSet.size, 0);
assert.deepEqual(calls(), [10, 11, 11]);

console.log("playback overlay render tests passed");
