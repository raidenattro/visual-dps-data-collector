const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "..", "web", "app", "08-event-review-range.js"),
  "utf8"
);
const context = vm.createContext({
  console,
  Map,
  Number,
  Math,
  String,
  Array,
  setTimeout,
  clearTimeout,
});
vm.runInContext(source, context);

function person(personId, trackId, bbox) {
  return { person_id: personId, person_track_id: trackId, bbox };
}

// Stable spatial continuity should pass through ordinary multi-person frames.
// A raw P0/P1 reorder is mapped automatically and must not interrupt the range.
context.frameCache = new Map([
  [1, { persons: [person(0, 7, [327, 292, 476, 443])] }],
  [
    2,
    {
      persons: [
        person(0, 7, [325, 296, 475, 443]),
        person(1, 12, [325, 296, 416, 437]),
      ],
    },
  ],
  [
    3,
    {
      persons: [
        person(0, 12, [327, 295, 476, 443]),
        person(1, 7, [327, 295, 416, 437]),
      ],
    },
  ],
  [
    4,
    {
      persons: [
        person(0, 7, [325, 294, 416, 437]),
        person(1, 12, [325, 295, 475, 443]),
      ],
    },
  ],
]);

const firstStop = context.resolveRangePersonAssignments(1, 4, 0, new Map());
assert.deepEqual(Array.from(firstStop.confirmationFrames), []);
assert.deepEqual(
  Array.from(firstStop.assignments, (item) => Number(item.personId)),
  [0, 0, 0, 1]
);

const resolved = context.resolveRangePersonAssignments(
  1,
  4,
  0,
  new Map([[4, 1]])
);
assert.deepEqual(Array.from(resolved.confirmationFrames), []);
assert.deepEqual(
  Array.from(resolved.assignments, (item) => Number(item.personId)),
  [0, 0, 0, 1]
);
assert.deepEqual(
  Array.from(resolved.assignments, (item) => String(item.trackId)),
  ["7", "7", "12", "12"]
);
const resolvedSummary = context.summarizeRangePersonAssignments(
  resolved.assignments
);
assert.match(resolvedSummary, /P0/);
assert.match(resolvedSummary, /P1/);

// Two people can remain visible for the whole interval without creating
// review work when the selected physical person's trajectory is stable.
context.frameCache = new Map([
  [
    40,
    {
      persons: [
        person(0, 10, [100, 100, 200, 300]),
        person(1, 11, [400, 100, 500, 300]),
      ],
    },
  ],
  [
    41,
    {
      persons: [
        person(0, 10, [103, 100, 203, 300]),
        person(1, 11, [397, 100, 497, 300]),
      ],
    },
  ],
  [
    42,
    {
      persons: [
        person(0, 10, [106, 100, 206, 300]),
        person(1, 11, [394, 100, 494, 300]),
      ],
    },
  ],
]);
const stableMulti = context.resolveRangePersonAssignments(
  40,
  42,
  0,
  new Map()
);
assert.deepEqual(Array.from(stableMulti.confirmationFrames), []);
assert.deepEqual(
  Array.from(stableMulti.assignments, (item) => Number(item.personId)),
  [0, 0, 0]
);

// A physical person can change raw person_id even when only one candidate is
// visible. The transition is automatic while each frame's raw ID is preserved.
context.frameCache = new Map([
  [30, { persons: [person(0, 20, [100, 100, 200, 300])] }],
  [31, { persons: [person(1, 20, [101, 100, 201, 300])] }],
]);
const singleCandidateIdSwap = context.resolveRangePersonAssignments(
  30,
  31,
  0,
  new Map()
);
assert.deepEqual(Array.from(singleCandidateIdSwap.confirmationFrames), []);
assert.deepEqual(
  Array.from(singleCandidateIdSwap.assignments, (item) => Number(item.personId)),
  [0, 1]
);

// Stable display identities are independent from raw person_id and track order.
// The left physical person remains A and the right remains B after raw IDs swap.
context.frameCache = new Map([
  [
    1,
    {
      frame_idx: 1,
      persons: [
        person(0, 20, [100, 100, 200, 300]),
        person(1, 10, [400, 100, 500, 300]),
      ],
    },
  ],
  [
    2,
    {
      frame_idx: 2,
      persons: [
        person(0, 10, [398, 100, 498, 300]),
        person(1, 20, [102, 100, 202, 300]),
      ],
    },
  ],
]);
context.resetStablePersonIdentityCache();
const frame1Left = context.getStablePersonDisplayInfoByRawId(1, 0);
const frame1Right = context.getStablePersonDisplayInfoByRawId(1, 1);
const frame2Left = context.getStablePersonDisplayInfoByRawId(2, 1);
const frame2Right = context.getStablePersonDisplayInfoByRawId(2, 0);
assert.equal(frame1Left.stableLabel, "A");
assert.equal(frame2Left.stableLabel, "A");
assert.equal(frame1Right.stableLabel, "B");
assert.equal(frame2Right.stableLabel, "B");
assert.equal(context.getRawPersonIdForStablePerson(2, 0), 1);
assert.equal(context.getRawPersonIdForStablePerson(2, 1), 0);
const frame1Buttons = context.sortStablePersonDisplayOptions([
  { pid: 0, ...frame1Left },
  { pid: 1, ...frame1Right },
]);
const frame2Buttons = context.sortStablePersonDisplayOptions([
  { pid: 0, ...frame2Right },
  { pid: 1, ...frame2Left },
]);
assert.deepEqual(
  Array.from(frame1Buttons, (item) => `${item.stableLabel}:P${item.pid}`),
  ["A:P0", "B:P1"]
);
assert.deepEqual(
  Array.from(frame2Buttons, (item) => `${item.stableLabel}:P${item.pid}`),
  ["A:P1", "B:P0"]
);

// A short disappearance plus track renumbering keeps the prior physical
// identities through spatial continuity (A/B must remain A/B).
context.frameCache = new Map([
  [
    1,
    {
      frame_idx: 1,
      persons: [
        person(0, 1, [100, 100, 200, 300]),
        person(1, 2, [400, 100, 500, 300]),
      ],
    },
  ],
  [
    2,
    {
      frame_idx: 2,
      persons: [person(0, 9, [103, 100, 203, 300])],
    },
  ],
  [
    3,
    {
      frame_idx: 3,
      persons: [
        person(1, 9, [106, 100, 206, 300]),
        person(0, 10, [397, 100, 497, 300]),
      ],
    },
  ],
]);
context.resetStablePersonIdentityCache();
const stableLabelsAfterReappear = [0, 1]
  .map((rawPid) =>
    context.getStablePersonDisplayInfoByRawId(3, rawPid)?.stableLabel
  )
  .sort();
assert.deepEqual(stableLabelsAfterReappear, ["A", "B"]);

// Two people crossing paths must keep their physical identities. Current-position
// IoU alone would swap them at frame 3; motion prediction keeps A moving right
// and B moving left even though raw IDs and track IDs are reordered.
context.frameCache = new Map([
  [
    1,
    {
      frame_idx: 1,
      persons: [
        person(0, 10, [60, 100, 140, 300]),
        person(1, 20, [360, 100, 440, 300]),
      ],
    },
  ],
  [
    2,
    {
      frame_idx: 2,
      persons: [
        person(1, 20, [260, 100, 340, 300]),
        person(0, 10, [160, 100, 240, 300]),
      ],
    },
  ],
  [
    3,
    {
      frame_idx: 3,
      persons: [
        person(0, 10, [160, 100, 240, 300]),
        person(1, 20, [260, 100, 340, 300]),
      ],
    },
  ],
]);
context.resetStablePersonIdentityCache();
assert.equal(
  context.getStablePersonDisplayInfoByRawId(1, 0).stableLabel,
  "A"
);
assert.equal(
  context.getStablePersonDisplayInfoByRawId(2, 0).stableLabel,
  "A"
);
assert.equal(
  context.getStablePersonDisplayInfoByRawId(3, 1).stableLabel,
  "A"
);
assert.equal(
  context.getStablePersonDisplayInfoByRawId(3, 0).stableLabel,
  "B"
);

// The manually selected first frame is an anchor even when two people exist.
context.frameCache = new Map([
  [
    20,
    {
      persons: [
        person(0, 3, [100, 100, 200, 300]),
        person(1, 4, [300, 100, 400, 300]),
      ],
    },
  ],
]);
const multiStart = context.resolveRangePersonAssignments(20, 20, 0, new Map());
assert.deepEqual(Array.from(multiStart.confirmationFrames), []);
assert.deepEqual(
  Array.from(multiStart.assignments, (item) => Number(item.personId)),
  [0]
);

// Indistinguishable overlapping candidates are a low-confidence stop.
context.frameCache = new Map([
  [10, { persons: [person(0, 1, [100, 100, 200, 300])] }],
  [
    11,
    {
      persons: [
        person(0, 1, [101, 100, 201, 300]),
        person(1, 2, [101, 100, 201, 300]),
      ],
    },
  ],
]);
const ambiguous = context.resolveRangePersonAssignments(10, 11, 0, new Map());
assert.deepEqual(Array.from(ambiguous.confirmationFrames), [11]);
assert.equal(ambiguous.confirmationSuggestions[0].personId, 0);
assert.equal(ambiguous.confirmationSuggestions[0].reason, "low_confidence");

// Reappearance after a frame with no person also requires one confirmation.
context.frameCache = new Map([
  [50, { persons: [person(0, 5, [100, 100, 200, 300])] }],
  [51, { persons: [] }],
  [52, { persons: [person(0, 5, [102, 100, 202, 300])] }],
]);
const reacquired = context.resolveRangePersonAssignments(50, 52, 0, new Map());
assert.deepEqual(Array.from(reacquired.confirmationFrames), [52]);
assert.equal(reacquired.confirmationSuggestions[0].reason, "reacquire");

// Confirmation is explicit: focusing alone does not persist an anchor.
context.getEventsOnFrame = () => [];
void context.focusRangePersonConfirmation(52, 0);
assert.equal(context.isRangePersonConfirmationRequired(52), true);
assert.equal(context.getRangePersonConfirmationSuggestion(52), 0);
assert.equal(
  context.confirmRangePersonSelection({ frame_idx: 52 }, 0),
  true
);
assert.equal(context.isRangePersonConfirmationRequired(52), false);

// Leaving the first frame must not replace its cached box/person template with
// empty pending state while the user annotates the tail frame.
context.rangeAnnotStartFrame = 100;
context.rangeAnnotEndFrame = 200;
context.rangeAnnotTemplateSnapshot = null;
let currentFrameIdx = 100;
let startTemplate = {
  ev: { frame_idx: 100 },
  confirmed: ["Box_2015"],
  personId: 0,
};
context.getResolvedPlaybackFrameIdx = () => currentFrameIdx;
context.getRangeAnnotTemplate = () => startTemplate;
context.normalizeBoxTokenList = (values) => Array.from(values || []);
context.getEventConfirmedBoxes = () => [];
context.getEventPersonId = () => null;
context.getFramePersonIds = () => [0];
context.refreshRangeAnnotTemplateSnapshot({ force: true });
assert.deepEqual(
  Array.from(context.rangeAnnotTemplateSnapshot.confirmed),
  ["Box_2015"]
);
currentFrameIdx = 200;
startTemplate = { ev: { frame_idx: 100 }, confirmed: [], personId: null };
context.refreshRangeAnnotTemplateSnapshot();
assert.deepEqual(
  Array.from(context.rangeAnnotTemplateSnapshot.confirmed),
  ["Box_2015"]
);
assert.equal(context.rangeAnnotTemplateSnapshot.personId, 0);
currentFrameIdx = 100;
context.refreshRangeAnnotTemplateSnapshot();
assert.deepEqual(
  Array.from(context.rangeAnnotTemplateSnapshot.confirmed),
  ["Box_2015"]
);
context.updateRangeAnnotTemplatePersonFromEvent({ frame_idx: 100 }, 1);
assert.deepEqual(
  Array.from(context.rangeAnnotTemplateSnapshot.confirmed),
  ["Box_2015"]
);
assert.equal(context.rangeAnnotTemplateSnapshot.personId, 1);

console.log("event review range identity tests passed");
