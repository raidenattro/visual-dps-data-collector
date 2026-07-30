const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "..", "web", "app", "08-event-review-range.js"),
  "utf8"
);
const context = vm.createContext({ console, Map, Number, Math, String, Array });
vm.runInContext(source, context);

function person(personId, trackId, bbox) {
  return { person_id: personId, person_track_id: trackId, bbox };
}

// Real failure shape from frame 2217–2220: duplicate detections overlap and
// track IDs swap between P0/P1. The large physical-person bbox must win.
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

const resolved = context.resolveRangePersonAssignments(1, 4, 0);
assert.deepEqual(Array.from(resolved.ambiguousFrames), []);
assert.deepEqual(
  Array.from(resolved.assignments, (item) => Number(item.personId)),
  [0, 0, 0, 1]
);
assert.deepEqual(
  Array.from(resolved.assignments, (item) => String(item.trackId)),
  ["7", "7", "12", "12"]
);

// Two indistinguishable overlapping candidates are unsafe: stop the entire
// range instead of silently selecting P0 or P1.
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
const ambiguous = context.resolveRangePersonAssignments(10, 11, 0);
assert.deepEqual(Array.from(ambiguous.ambiguousFrames), [11]);

console.log("event review range identity tests passed");
