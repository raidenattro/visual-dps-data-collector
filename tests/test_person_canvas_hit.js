const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "..", "web", "app", "10-render-collision.js"),
  "utf8"
);
const start = source.indexOf("function hitTestPersonDetailAtClient");
const end = source.indexOf("/** 骨骼特征", start);
assert.ok(start >= 0 && end > start);

// 命中测试靠 measureLabelWidth 算标签宽度，用真实实现而不是替身，
// 才能保证命中框和画出来的标签量的是同一个宽度。
const labelWidthHelper = source.match(
  /const PERSON_LABEL_FONT =[\s\S]*?\nfunction measureLabelWidth\([\s\S]*?\n}/
);
assert.ok(labelWidthHelper, "未找到 measureLabelWidth 实现");

const persons = [
  {
    person_id: 0,
    bbox: [100, 100, 220, 340],
    keypoints: [[150, 150, 0.9]],
  },
  {
    person_id: 1,
    bbox: [400, 100, 520, 340],
    keypoints: [[450, 150, 0.9]],
  },
];
const context = vm.createContext({
  console,
  frameCache: new Map([
    [
      1,
      {
        frame_idx: 1,
        infer_width: 640,
        infer_height: 480,
        persons,
      },
    ],
  ]),
  getPinnedPlaybackEvent: () => ({ frame_idx: 1 }),
  canvas: { getBoundingClientRect: () => ({ left: 0, top: 0 }) },
  poseData: { infer_width: 640, infer_height: 480 },
  pausedPlaybackLayout: null,
  frozenPlaybackLayout: null,
  getDisplayLayout: () => ({}),
  mapInferToDisplay: (x, y) => [Number(x), Number(y)],
  getStablePersonDisplayInfo: (_frame, _person, idx) => ({
    stableLabel: idx === 0 ? "A" : "B",
  }),
  personDetBbox: (person) => person.bbox,
  resolvePersonLabelAnchor: (person) => ({
    ax: (person.bbox[0] + person.bbox[2]) / 2,
    ay: person.bbox[1],
  }),
  ctx: {
    save() {},
    restore() {},
    measureText(text) {
      return { width: String(text).length * 14 };
    },
    font: "",
  },
});
vm.runInContext(`${labelWidthHelper[0]}\n${source.slice(start, end)}`, context);

// 点击完整标签区域（不只是旧版中心 22px）可以选人。
assert.equal(context.hitTestPersonAtClient(188, 74), 0);
assert.equal(context.hitTestPersonDetailAtClient(188, 74).kind, "label");
// 点击骨架关键点可以选人。
assert.equal(context.hitTestPersonAtClient(150, 150), 0);
assert.equal(context.hitTestPersonDetailAtClient(150, 150).kind, "keypoint");
// 点击人体框内部、但不靠近关键点，也可以选人。
assert.equal(context.hitTestPersonAtClient(470, 280), 1);
assert.equal(context.hitTestPersonDetailAtClient(470, 280).kind, "bbox");
// 画面空白不应误选人员。
assert.equal(context.hitTestPersonAtClient(300, 420), null);

// 人体框/手部骨架与货框重叠时，货框必须能收到点击。
const bboxOverlapHit = context.resolveEventReviewCanvasHit(
  context.hitTestPersonDetailAtClient(470, 280),
  "Box_2016"
);
assert.equal(bboxOverlapHit.kind, "annotation");
assert.equal(bboxOverlapHit.value, "Box_2016");
const keypointOverlapHit = context.resolveEventReviewCanvasHit(
  context.hitTestPersonDetailAtClient(150, 150),
  "Box_2016"
);
assert.equal(keypointOverlapHit.kind, "annotation");
assert.equal(keypointOverlapHit.value, "Box_2016");
// 明确点击人物文字标签仍然用于选人。
const labelOverlapHit = context.resolveEventReviewCanvasHit(
  context.hitTestPersonDetailAtClient(188, 74),
  "Box_2016"
);
assert.equal(labelOverlapHit.kind, "person");
assert.equal(labelOverlapHit.value, 0);

console.log("person canvas hit tests passed");
