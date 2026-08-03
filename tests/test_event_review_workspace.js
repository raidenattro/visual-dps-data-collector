const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.join(__dirname, "..");
const read = (relativePath) =>
  fs.readFileSync(path.join(root, relativePath), "utf8");

const html = read("web/index.html");
const workspace = read("web/app/12-event-review-workspace.js");
const review = read("web/app/08-event-review.js");
const controls = read("web/app/11-playback-controls.js");
const events = read("web/app/09-playback-events.js");
const collision = read("web/app/10-render-collision.js");

// 第一批：模式隔离、明确影响范围与键盘切换。
assert.match(html, /id="event-review-mode-frame-btn"/);
assert.match(html, /id="event-review-mode-range-btn"/);
assert.match(html, /class="[^"]*review-mode-range-only[^"]*"/);
assert.match(html, /class="[^"]*review-mode-frame-only[^"]*"/);
assert.match(workspace, /event\.key === "Tab"/);
assert.match(workspace, /event\.key === "Escape"/);
assert.match(controls, /isEventReviewRangeMode\(\)/);

// 第二批：保存保护、撤销/重试、异常导航、专注与长列表分批。
assert.match(html, /id="event-save-retry-btn"/);
assert.match(html, /id="event-review-undo-btn"/);
assert.match(html, /id="event-review-anomaly-jump-btn"/);
assert.match(html, /id="event-review-focus-btn"/);
assert.match(html, /id="event-review-window-next-btn"/);
assert.match(workspace, /beforeunload/);
assert.match(workspace, /EVENT_REVIEW_WINDOW_SIZE = 240/);
assert.match(workspace, /persistEventReviewVerifiedList\(snapshot\.verifiedTrue/);
assert.match(review, /hasUnsavedEventReviewDrafts\(\)/);

// 切换事件只切当前显示，不应清空尚未落盘的人物/货框草稿。
const clearOnEventChange = review.match(
  /function clearEventReviewPickStatusOnEventChange\([\s\S]*?\n}/
);
assert.ok(clearOnEventChange);
assert.doesNotMatch(clearOnEventChange[0], /pendingConfirmedBoxesByKey\.delete/);
assert.doesNotMatch(clearOnEventChange[0], /pendingReviewBindingsByKey\.delete/);

// 单轨时间轴：算法候选与人工复核合并到同一条轨道的上下两个通道。
assert.match(html, /id="seek-event-markers"/);
assert.match(html, /id="seek-review-markers"/);
assert.match(html, /class="review-timeline-track"/);
assert.doesNotMatch(html, /review-timeline-row/);
assert.match(review, /reviewMarkersEl\.appendChild/);
assert.match(events, /reviewMarkersEl\.innerHTML = ""/);

// 上千事件时按像素桶聚合并用事件委托，避免逐点建 DOM 导致进度条卡顿。
assert.match(review, /REVIEW_TIMELINE_BUCKET_PX/);
assert.match(review, /function bindReviewTimelineDelegation/);
assert.match(events, /reviewTimelineBucketByKey/);

// 专注模式必须进入浏览器全屏，才能覆盖标签栏。
assert.match(workspace, /requestFullscreen/);
assert.match(workspace, /"fullscreenchange"/);
assert.match(workspace, /relocateShortcutsDialog/);

// 身份状态条与配对区合并为一条主路径。
assert.match(html, /id="event-review-identity-title"/);
assert.match(html, /class="event-review-person-steps"/);

// 第三批：算法框不顶替人工确认、单人自动选人、Y 乐观跳帧、页面内二次确认。
const range = read("web/app/08-event-review-range.js");
const confirmTrue = review.match(/async function confirmTrueAndNextFrame\([\s\S]*?\n}/);
assert.ok(confirmTrue);
// 任何事件类型都要先点货框，不再只拦 event_type === "frame"。
assert.doesNotMatch(confirmTrue[0], /event_type === "frame"/);
assert.match(confirmTrue[0], /getEventConfirmedBoxes\(ev\)\.length > 0 \|\| getEventEffectiveBindings\(ev\)\.length > 0/);
// 先跳帧再等保存结果，避免每帧都卡在 PATCH 往返上。
assert.match(confirmTrue[0], /const savePromise = persistEventReviewToggle\(ev, true\)/);
assert.ok(
  confirmTrue[0].indexOf("await navigatePlaybackFrame(1)") <
    confirmTrue[0].indexOf("await savePromise")
);
assert.match(review, /const autoSinglePid =/);
assert.match(html, /id="review-confirm-dialog"/);
assert.match(workspace, /function openReviewConfirm/);
assert.match(range, /openReviewConfirm\({/);
// 区间落盘成功后自动清空首尾帧，连续标注不用先按 Shift+R。
const applyRange = range.match(/async function applyRangeAnnotVerified\([\s\S]*?\n}/);
assert.ok(applyRange);
const applyRangeOkBranch = applyRange[0].slice(applyRange[0].lastIndexOf("if (ok) {"));
assert.match(applyRangeOkBranch, /clearRangeAnnotBounds\(\);/);
assert.doesNotMatch(applyRangeOkBranch, /updateRangeAnnotUi\(\);/);
// 两个键盘监听都要在模态弹窗打开时让位，否则 F / Tab / R 会穿透到底层页面。
assert.match(controls, /dialog\[open\]/);
assert.match(workspace, /dialog\[open\]/);
// 侧栏降噪：模式横幅与底部快捷键长条已移除。
assert.doesNotMatch(html, /event-review-mode-banner/);
assert.doesNotMatch(html, /event-review-kbd-hint/);

// 记录列表：机位内分页滚到底自动续拉，「加载更多」按钮仅作降级入口。
const records = read("web/app/06-records.js");
assert.match(records, /function observeRecordsAutoLoad/);
assert.match(records, /new IntersectionObserver/);
assert.match(records, /playback-records-sentinel/);
assert.match(records, /observeRecordsAutoLoad\(list\);/);

// 时间轴中间准确率层：默认不画，显示时按像素桶聚合，避免白点叠团。
assert.match(collision, /function shouldShowAccuracySeekMarkers/);
assert.match(collision, /externalPlaybackAccuracyOverlay/);
assert.match(collision, /filter === "miss" \|\| filter === "false_alarm"/);
const renderAccuracy = collision.match(
  /function renderAccuracySeekMarkers\([\s\S]*?\n}/
);
assert.ok(renderAccuracy);
assert.match(renderAccuracy[0], /shouldShowAccuracySeekMarkers\(\)/);
assert.match(renderAccuracy[0], /bucketCount/);

const personUiStart = review.indexOf("function renderEventReviewPersonUi(");
const personUiEnd = review.indexOf("function finishUpdateReviewDock(", personUiStart);
const eventTableStart = review.indexOf("function renderEventReviewTable(");
const eventTableEnd = review.indexOf("async function markActiveEventVerified(", eventTableStart);
assert.ok(personUiStart >= 0 && personUiEnd > personUiStart);
assert.ok(eventTableStart >= 0 && eventTableEnd > eventTableStart);
assert.match(review.slice(personUiStart, personUiEnd), /event-review-binding-chip/);
assert.doesNotMatch(review.slice(eventTableStart, eventTableEnd), /optionsEl/);

console.log("event review workspace first/second batch tests passed");
