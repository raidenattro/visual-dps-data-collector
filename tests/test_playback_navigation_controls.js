const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const indexHtml = fs.readFileSync(
  path.join(__dirname, "..", "web", "index.html"),
  "utf8"
);

for (const id of [
  "playback-first-frame-btn",
  "playback-back-10-btn",
  "playback-back-1-btn",
  "play-btn",
  "playback-forward-1-btn",
  "playback-forward-10-btn",
  "playback-last-frame-btn",
  "playback-frame-input",
]) {
  assert.match(indexHtml, new RegExp(`id="${id}"`));
}

assert.doesNotMatch(indexHtml, /id="event-prev-frame-btn"/);
assert.doesNotMatch(indexHtml, /id="event-next-frame-btn"/);
assert.doesNotMatch(indexHtml, /id="event-review-frame-pos"/);

const controlsSource = fs.readFileSync(
  path.join(__dirname, "..", "web", "app", "11-playback-controls.js"),
  "utf8"
);
assert.match(controlsSource, /navigatePlaybackFrame\(direction \* 10\)/);
assert.match(controlsSource, /elapsedSec \* fps \* 3/);
assert.match(controlsSource, /e\.key === "Home"/);
assert.match(controlsSource, /e\.key === "End"/);
assert.match(controlsSource, /e\.key === "g" \|\| e\.key === "G"/);

const eventsSource = fs.readFileSync(
  path.join(__dirname, "..", "web", "app", "09-playback-events.js"),
  "utf8"
);
assert.match(eventsSource, /frameInput\.value = String\(targetFi\)/);

console.log("playback navigation control tests passed");
