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

// 复核慢放：保留原速默认值，并提供 0.25× 最低档。
assert.match(html, /<option value="0\.25">0\.25×<\/option>/);
assert.match(html, /<option value="1" selected>1×<\/option>/);

// 第一批：侧栏标注/事件分区；Y 与 A/D/R 同时可用，Tab 切侧栏。
assert.match(html, /id="event-review-side-annotate-btn"/);
assert.match(html, /id="event-review-side-events-btn"/);
assert.match(html, /id="event-review-pane-annotate"/);
assert.match(html, /id="event-review-pane-events"/);
assert.match(html, /class="[^"]*event-review-range-card[^"]*"/);
assert.doesNotMatch(html, /id="event-review-mode-frame-btn"/);
assert.doesNotMatch(html, /review-mode-range-only/);
assert.match(workspace, /event\.key === "Tab"/);
assert.match(workspace, /toggleEventReviewSideTab\(\)/);
assert.match(workspace, /event\.key === "Escape"/);
assert.match(workspace, /hasRangeAnnotDraft\(\)/);
assert.match(controls, /e\.key === "y" \|\| e\.key === "Y"/);
assert.match(controls, /e\.key === "a" \|\| e\.key === "A"/);
assert.doesNotMatch(controls, /isEventReviewRangeMode\(\)/);

// 第二批：保存保护、撤销/重试、异常导航、专注与长列表分批。
assert.match(html, /id="event-save-retry-btn"/);
assert.match(html, /id="event-review-undo-btn"/);
assert.match(html, /id="event-review-anomaly-jump-btn"/);
assert.match(html, /id="event-review-focus-btn"/);
assert.match(html, /id="event-review-window-next-btn"/);
assert.match(workspace, /beforeunload/);
assert.match(workspace, /EVENT_REVIEW_WINDOW_SIZE = 240/);
assert.match(workspace, /persistEventReviewVerifiedList\(snapshot\.verifiedTrue/);
// 专注模式切换后必须重建播放冻结布局，否则长视频预览下画面仍按旧舞台对齐。
assert.match(collision, /function refreshPlaybackStageLayout\(/);
assert.match(workspace, /refreshPlaybackStageLayout\(/);
assert.match(read("web/app/07-playback-stage.js"), /refreshPlaybackStageLayout\(/);
// 真全屏需兼容无 navigationUI 参数与 webkit 前缀，才能盖住系统任务栏。
assert.match(workspace, /function requestReviewFullscreen\(/);
assert.match(workspace, /navigationUI: "hide"/);
assert.match(workspace, /webkitRequestFullscreen|webkitRequestFullScreen/);
assert.match(workspace, /webkitfullscreenchange/);
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

// 标真后走缓存分支时要补齐所有受影响的桶，否则区间标真与标真跳帧不会立刻变绿。
assert.match(review, /function markReviewTimelineBucketDirty/);
assert.match(review, /function flushDirtyTimelineBuckets/);
assert.match(review, /markReviewTimelineBucketDirty\(ev\);/);
assert.match(review, /playbackEvents = body\.events;\s*\n\s*invalidateReviewTimelineCache\(\);/);

// 保存回包按标真差集标脏，避免每次单帧标真都整表重算桶状态。
assert.match(review, /function markReviewTimelineBucketsDirtyByKeyDiff/);
assert.match(review, /const previousVerifiedKeys = new Set\(verifiedTrueKeys\);/);
assert.match(
  review,
  /markReviewTimelineBucketsDirtyByKeyDiff\(previousVerifiedKeys, verifiedTrueKeys\);/
);
assert.doesNotMatch(review, /markReviewTimelineAllBucketsDirty/);

// 单帧标真只补这一帧的自动确认；否则每次保存都要遍历全部已标真事件。
assert.match(review, /autoConfirmEvents: \[ev\]/);
assert.match(review, /Array\.isArray\(options\.autoConfirmEvents\) && !Array\.isArray\(body\.events\)/);
// 没有草稿时不为整表建索引。
assert.match(
  review,
  /!pendingConfirmedBoxesByKey\.size &&\s*\n\s*!pendingReviewBindingsByKey\.size &&\s*\n\s*!pendingPersonIdByKey\.size/
);
assert.match(review, /function flushDirtyTimelineBuckets\(\) \{\s*\n\s*if \(!reviewTimelineDirtyBuckets\.size\) return;/);

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
// 480p 预览提示只在真正派生预览时出现，不能用宽度 >720 误判短片。
assert.match(records, /usedDerivedPreview/);
assert.match(records, /≥10000 帧派生的 480p 预览/);
assert.doesNotMatch(records, /frameW > 720/);

// 时间轴中间准确率层：默认不画，显示时按像素桶聚合，避免白点叠团。
assert.match(collision, /function shouldShowAccuracySeekMarkers/);
assert.match(collision, /externalPlaybackAccuracyOverlay/);
assert.match(collision, /filter === "miss" \|\| filter === "false_alarm"/);
// 普通复核不画误报白描边；与时间轴准确率点同一开关。
const accuracyOutline = collision.match(
  /function getAccuracyOutlineForFrame\([\s\S]*?\n}/
);
assert.ok(accuracyOutline);
assert.match(accuracyOutline[0], /shouldShowAccuracySeekMarkers/);
assert.match(accuracyOutline[0], /!shouldShowAccuracySeekMarkers\(\)/);
const renderAccuracy = collision.match(
  /function renderAccuracySeekMarkers\([\s\S]*?\n}/
);
assert.ok(renderAccuracy);
assert.match(renderAccuracy[0], /shouldShowAccuracySeekMarkers\(\)/);
assert.match(renderAccuracy[0], /bucketCount/);

// 复核模式：只读优先的镜头 + 画面冲突提示 + 收尾自检清单。
const recheck = read("web/app/14-event-review-recheck.js");
assert.match(html, /app\/14-event-review-recheck\.js\?v=/);
assert.match(html, /id="event-review-recheck-btn"/);
assert.match(html, /id="event-review-recheck-edit-btn"/);
assert.match(html, /id="event-review-conflict-bar"/);
assert.match(html, /class="playback-overlay-toggles"/);
assert.match(html, /class="playback-overlay-title"[^>]*>画面叠加</);
assert.match(html, /class="playback-time-readout"/);
assert.match(html, /id="playback-show-algo-collision"/);
assert.match(html, /id="playback-show-event-review"/);
assert.match(html, /id="playback-show-review-risk"/);
assert.match(html, /事件标真/);
assert.match(html, /复核风险提示/);
assert.match(controls, /initPlaybackAlgoCollisionToggle/);
assert.match(controls, /initPlaybackEventReviewToggle/);
assert.match(controls, /initPlaybackReviewRiskToggle/);
assert.match(collision, /showAlgoCollisionColors/);
assert.match(collision, /showEventReviewHighlights/);
assert.match(recheck, /showReviewRiskHints/);
// 复核模式是叠在侧栏之上的镜头，不能新增第三种侧栏 Tab。
assert.match(recheck, /setEventReviewSideTab\(/);
assert.doesNotMatch(html, /data-side-tab="recheck"/);
assert.doesNotMatch(html, /data-review-mode="recheck"/);
// 只读态靠 CSS 收起标注控件，不动 DOM 结构。
const styleCss = read("web/style.css");
assert.match(styleCss, /is-recheck-mode:not\(\.is-recheck-editing\)/);
assert.match(styleCss, /#event-review-person-select/);
assert.match(styleCss, /\.event-review-range-card/);
assert.match(html, /<span>切换标注 \/ 事件侧栏<\/span><kbd>Tab<\/kbd>/);
// 出问题的两条召出编辑态的路径：点画面与按 E。
assert.match(recheck, /function interceptRecheckCanvasClick/);
assert.match(controls, /interceptRecheckCanvasClick\(\)/);
assert.match(recheck, /key === "e" && eventReviewRecheckMode/);
assert.match(recheck, /key === "v"/);
// 翻帧自动收回编辑态，避免一直挂着标注控件。
assert.match(recheck, /eventReviewRecheckEditFrame/);
assert.match(recheck, /setEventReviewRecheckEditing\(false, { auto: true }\)/);
// 三类冲突全部由现有数据推导，不新增存盘字段。
const conflicts = recheck.match(/function computeReviewFrameConflicts\([\s\S]*?\n}/);
assert.ok(conflicts);
assert.match(conflicts[0], /getVerifiedEventsOnFrame\(fi\)/);
assert.match(conflicts[0], /getFramePersonIds\(fi\)/);
assert.doesNotMatch(recheck, /verified_true:|confirmed_box_tokens:/);
// 未标真的帧不算漏标嫌疑，否则整条队列都会被染成冲突。
assert.match(conflicts[0], /isAlarm && verifiedOnFrame\.length/);
// 描边两条绘制路径都要画，播放时也不能少。
const drawBoxes = collision.match(/function drawAnnotationBoxes\(frame[\s\S]*?\n}/);
assert.ok(drawBoxes);
assert.match(drawBoxes[0], /drawReviewConflictOutlines\(frameIdx, collisionSet, alarmSet, reviewCtx\)/);
const drawLite = collision.match(/function drawAnnotationBoxesAccentOnly\([\s\S]*?\n}/);
assert.ok(drawLite);
assert.match(drawLite[0], /drawReviewConflictOutlines\(frameIdx, ctxSets\.collisionSet, alarmSet, ctxSets\.reviewCtx\)/);
// 描边本身不许碰 DOM，否则就没法进播放路径。
const conflictDraw = recheck.match(/function drawReviewConflictOutlines\([\s\S]*?\n}/);
assert.ok(conflictDraw);
assert.doesNotMatch(conflictDraw[0], /updateReviewConflictBar|classList|textContent/);
// 侧栏同步独立出来，且内容没变时不写 DOM。
const conflictSync = recheck.match(/function syncReviewConflictUiForFrame\([\s\S]*?\n}/);
assert.ok(conflictSync);
assert.match(conflictSync[0], /reviewConflictBarSignature\(\) === lastReviewConflictBarSignature\) return/);
assert.match(conflictSync[0], /setEventReviewRecheckEditing\(false, \{ auto: true \}\)/);
// 签名不能含帧号，否则播放时每帧都判为脏，等于没做节流。
const conflictSig = recheck.match(/function reviewConflictBarSignature\([\s\S]*?\n}/);
assert.ok(conflictSig);
assert.doesNotMatch(conflictSig[0], /frameIdx/);
// 绘制路径只调节流版；无条件写 DOM 的版本留给模式切换。
const skeletonFrame = collision.match(/function drawSkeletonFrame\([\s\S]*?\n}/);
assert.ok(skeletonFrame);
assert.match(skeletonFrame[0], /syncReviewConflictUiForFrame\(resolveOverlayFrameIdx\(frame\)\)/);
assert.match(recheck, /function updateReviewConflictBar\(\)/);
// 收尾清单复用页面内确认弹窗，只统计全量的事件级信息。
assert.match(recheck, /function buildEventReviewChecklist/);
assert.match(recheck, /openReviewConfirm\({/);
assert.match(controls, /confirmMarkEventReviewCompleted\(\)/);
assert.match(html, /<span>复核模式（只读看画面）<\/span><kbd>V<\/kbd>/);
// 进复核模式压到 0.25×，退出还原进来之前的倍速。
const recheckToggle = recheck.match(/function setEventReviewRecheckMode\([\s\S]*?\n}/);
assert.ok(recheckToggle);
assert.match(recheckToggle[0], /setPlaybackSpeed\(EVENT_REVIEW_RECHECK_SPEED\)/);
assert.match(recheckToggle[0], /setPlaybackSpeed\(eventReviewRecheckPrevSpeed\)/);
assert.match(recheck, /EVENT_REVIEW_RECHECK_SPEED = 0\.25/);
// 倍速必须写回 select：换记录与 loadedmetadata 都会重新从 select 读。
const stage = read("web/app/07-playback-stage.js");
const setSpeed = stage.match(/function setPlaybackSpeed\([\s\S]*?\n}/);
assert.ok(setSpeed);
assert.match(setSpeed[0], /playbackSpeedSelect\.value = String\(rate\)/);
assert.match(setSpeed[0], /return previous/);

// 保存落盘后必须对账草稿，否则区间标真留下的草稿会让「有未保存修改」永久挂着。
const applyResponse = review.match(/function applyEventReviewResponse\([\s\S]*?\n}/);
assert.ok(applyResponse);
assert.match(applyResponse[0], /pruneSettledEventReviewDrafts\(\)/);
// 提示要说清是哪几帧，只说「有未保存修改」等于让人在上万帧里猜。
assert.match(workspace, /function listUnsavedEventReviewDraftFrames/);
assert.match(workspace, /有未保存修改：帧 \$\{shown\}/);
// 未落盘的草稿不能顺手清掉，否则「先选人再点货框」中途的选择会丢。
const prune = review.match(/function pruneSettledEventReviewDrafts\([\s\S]*?\n}/);
assert.ok(prune);
assert.match(prune[0], /getEventPersistedPersonId\(ev\) === draft/);

// 点过进度条后快捷键仍要生效：range 不算输入控件，且松手后不留焦点。
const typingTarget = workspace.match(/function isReviewTypingTarget\([\s\S]*?\n}/);
assert.ok(typingTarget);
assert.match(typingTarget[0], /!== "range"/);
const seekPointerUp = controls.match(
  /seekBar\.addEventListener\("pointerup", \(\) => \{[\s\S]*?\n\}\);/
);
assert.ok(seekPointerUp);
assert.match(seekPointerUp[0], /seekBar\.blur\(\);/);
// 拖动期间只更新读数，松手才真正 seek：长记录上逐次 seek 会不停触发解码与分块请求。
assert.match(seekPointerUp[0], /void commitPlaybackSeek\(\);/);
assert.match(controls, /if \(playbackSeekScrubbing\) \{\s*\n\s*cancelScheduledPlaybackSeek\(\);\s*\n\s*return;/);
// 判定只留一份，避免两处让位规则将来走偏。
assert.match(controls, /if \(isReviewTypingTarget\(e\.target\)\) return;/);
assert.doesNotMatch(controls, /tag === "textarea" \|\| tag === "select"/);

const personUiStart = review.indexOf("function renderEventReviewPersonUi(");
const personUiEnd = review.indexOf("function finishUpdateReviewDock(", personUiStart);
const eventTableStart = review.indexOf("function renderEventReviewTable(");
const eventTableEnd = review.indexOf("async function markActiveEventVerified(", eventTableStart);
assert.ok(personUiStart >= 0 && personUiEnd > personUiStart);
assert.ok(eventTableStart >= 0 && eventTableEnd > eventTableStart);
assert.match(review.slice(personUiStart, personUiEnd), /event-review-binding-chip/);
assert.doesNotMatch(review.slice(eventTableStart, eventTableEnd), /optionsEl/);

console.log("event review workspace first/second batch tests passed");
