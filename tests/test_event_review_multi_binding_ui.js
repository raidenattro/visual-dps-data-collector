const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const reviewSource = fs.readFileSync(
  path.join(__dirname, "..", "web", "app", "08-event-review.js"),
  "utf8"
);
const start = reviewSource.indexOf("function normalizeReviewBindings");
const end = reviewSource.indexOf("function syncConfirmedBoxFromReview", start);
assert.ok(start >= 0 && end > start);

const pendingPersonIdByKey = new Map();
const context = vm.createContext({
  console,
  Map,
  Set,
  Number,
  String,
  Array,
  pendingConfirmedBoxesByKey: new Map(),
  pendingReviewBindingsByKey: new Map(),
  pendingPersonIdByKey,
  boxAnnotationTouchedKeys: new Set(),
  personIdTouchedKeys: new Set(),
  canonicalizeBoxTokenList(tokens) {
    return [...new Set((tokens || []).map(String).map((v) => v.trim()).filter(Boolean))];
  },
  normalizeBoxTokenList(tokens) {
    return [...new Set((tokens || []).map(String).map((v) => v.trim()).filter(Boolean))];
  },
  eventRowKey(ev) {
    return `${ev.event_type}:${ev.frame_idx}`;
  },
  getStablePersonDisplayInfoByRawId(_frameIdx, personId) {
    return {
      stableId: Number(personId),
      stableLabel: Number(personId) === 0 ? "A" : "B",
    };
  },
});
vm.runInContext(reviewSource.slice(start, end), context);

const event = {
  event_type: "collision",
  frame_idx: 644,
  source_frame_idx: 644,
  box_tokens: ["Box_1007", "Box_2011"],
};
const key = context.eventRowKey(event);
assert.equal(context.getReviewPersonAccentStyle(event, 0).name, "purple");
assert.equal(context.getReviewPersonAccentStyle(event, 1).name, "orange");
context.getFramePersonIds = () => [0, 1];
context.getPinnedPlaybackEvent = () => event;
context.getEventPersonId = () => pendingPersonIdByKey.get(key) ?? null;
context.setPersonIdForEvent = (_ev, personId) => {
  pendingPersonIdByKey.set(key, Number(personId));
};
pendingPersonIdByKey.set(key, 0);
assert.equal(context.cycleEventReviewPersonSelection(), true);
assert.equal(pendingPersonIdByKey.get(key), 1);
assert.equal(context.cycleEventReviewPersonSelection(), true);
assert.equal(pendingPersonIdByKey.get(key), 0);

pendingPersonIdByKey.set(key, 0);
context.setEventConfirmedBoxes(event, ["Box_2011"]);
assert.deepEqual(
  Array.from(
    context.bindingConfirmedBoxesForPerson(
      context.getEventEffectiveBindings(event),
      0
    )
  ),
  ["Box_2011"]
);

pendingPersonIdByKey.set(key, 1);
context.setEventConfirmedBoxes(event, ["Box_1007"]);
assert.deepEqual(
  Array.from(
    context.bindingConfirmedBoxesForPerson(
      context.getEventEffectiveBindings(event),
      1
    )
  ),
  ["Box_1007"]
);

const bindings = context.getEventEffectiveBindings(event);
assert.equal(bindings.length, 2);
assert.deepEqual(
  Array.from(bindings, (binding) => [
    Number(binding.person_id),
    Array.from(binding.confirmed_box_tokens),
  ]),
  [
    [0, ["Box_2011"]],
    [1, ["Box_1007"]],
  ]
);

// Editing A again must replace it in place. Reordering to [B, A] made the
// fallback palette occasionally recolor A's purple box as B's orange box.
pendingPersonIdByKey.set(key, 0);
context.setEventConfirmedBoxes(event, ["Box_2011", "Box_2012"]);
assert.deepEqual(
  Array.from(context.getEventEffectiveBindings(event), (binding) =>
    Number(binding.person_id)
  ),
  [0, 1]
);
pendingPersonIdByKey.set(key, 0);
context.setEventConfirmedBoxes(event, ["Box_2011"]);

context.resolveConfirmedBoxesForSave = (ev) =>
  context.unionReviewBindingBoxes(context.getEventEffectiveBindings(ev));
const payload = context.eventToReviewPayload(event);
assert.deepEqual(
  Array.from(payload.bindings, (binding) => [
    Number(binding.person_id),
    Array.from(binding.confirmed_box_tokens),
  ]),
  [
    [0, ["Box_2011"]],
    [1, ["Box_1007"]],
  ]
);

// After Y commits the draft onto the event, serializing again must not append
// an anonymous binding that merges both boxes and loses the person pairing.
event.bindings = bindings;
delete event.person_id;
context.pendingReviewBindingsByKey.delete(key);
context.pendingPersonIdByKey.delete(key);
const committedPayload = context.eventToReviewPayload(event);
assert.equal(committedPayload.bindings.length, 2);
assert.ok(
  committedPayload.bindings.every((binding) => binding.person_id != null)
);

const indexHtml = fs.readFileSync(
  path.join(__dirname, "..", "web", "index.html"),
  "utf8"
);
const rangePos = indexHtml.indexOf("event-review-range-details--prominent");
const scrollPos = indexHtml.indexOf('class="event-review-dock-scroll"');
assert.ok(rangePos >= 0 && rangePos < scrollPos);
assert.match(
  indexHtml,
  /event-review-range-details--prominent[^"\r\n]*" open/
);
assert.match(indexHtml, /人员 — 货框配对/);
assert.match(indexHtml, /id="event-review-person-progress"/);

const controlsSource = fs.readFileSync(
  path.join(__dirname, "..", "web", "app", "11-playback-controls.js"),
  "utf8"
);
assert.match(indexHtml, /event-review-person-cycle-btn/);
assert.match(controlsSource, /e\.key === "Alt"/);
assert.match(controlsSource, /e\.key === "a" \|\| e\.key === "A"/);
assert.match(controlsSource, /e\.key === "d" \|\| e\.key === "D"/);
assert.match(controlsSource, /void applyRangeAnnotVerified\(\)/);

const renderSource = fs.readFileSync(
  path.join(__dirname, "..", "web", "app", "10-render-collision.js"),
  "utf8"
);
assert.match(renderSource, /addEventBindingsToHighlight/);
assert.match(renderSource, /getReviewPersonAccentStyle/);
assert.match(renderSource, /tokenValueInTokenMap/);

console.log("event review multi-binding and prominent range UI tests passed");
