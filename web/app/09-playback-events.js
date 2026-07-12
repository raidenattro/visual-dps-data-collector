/** 回放事件加载、定位与清除 */

/** 按 frame_idx 精确导航时的权威帧（直到拖动进度条/播放才清除，避免 seeked 回调漂移） */
let playbackAuthorityFrameIdx = null;

/** 按 frame_idx 精确 seek 时的目标帧（与权威帧同步，供 seeked 回调读取） */
let explicitSeekFrameIdx = null;

function setPlaybackAuthorityFrameIdx(frameIdx) {
  const fi = parseInt(frameIdx, 10) || 0;
  playbackAuthorityFrameIdx = fi > 0 ? fi : null;
  explicitSeekFrameIdx = playbackAuthorityFrameIdx;
}

function getPlaybackAuthorityFrameIdx() {
  return playbackAuthorityFrameIdx;
}

function clearPlaybackAuthorityFrameIdx() {
  playbackAuthorityFrameIdx = null;
  explicitSeekFrameIdx = null;
}

function setExplicitSeekFrameIdx(frameIdx) {
  setPlaybackAuthorityFrameIdx(frameIdx);
}

function getExplicitSeekFrameIdx() {
  return playbackAuthorityFrameIdx ?? explicitSeekFrameIdx;
}

function clearExplicitSeekFrameIdx() {
  clearPlaybackAuthorityFrameIdx();
}

/** 将回放画面与事件（若有）对齐到指定帧 */
async function linkPlaybackToFrame(frameIdx, { pinEvent = true } = {}) {
  const fi = parseInt(frameIdx, 10) || 0;
  if (!fi || !frameByTime?.length) return;
  const row = frameEntryByIdx(fi);
  if (!row) return;

  let ev = null;
  if (typeof filteredPlaybackEvents === "function") {
    ev = filteredPlaybackEvents().find((e) => (parseInt(e.frame_idx, 10) || 0) === fi) || null;
  }
  if (!ev && playbackEvents?.length) {
    ev = playbackEvents.find((e) => (parseInt(e.frame_idx, 10) || 0) === fi) || null;
  }

  if (ev && pinEvent) {
    await seekToEvent(ev);
    return;
  }

  setPlaybackAuthorityFrameIdx(fi);
  if (!videoEl.paused) videoEl.pause();
  playbackEventLinkExact = false;
  await seekToTimestamp(row.t, fi, { skipEventSync: false });
}

/** 钉住事件期间，将画面拉回事件帧（不解除钉住） */
async function realignPlaybackToPinnedEvent() {
  if (!playbackEventLinkExact || !activeEventKey) return false;
  const ev =
    typeof getPinnedPlaybackEvent === "function"
      ? getPinnedPlaybackEvent()
      : typeof getActiveEvent === "function"
        ? getActiveEvent()
        : null;
  if (!ev) return false;
  const fi =
    typeof eventDisplayFrameIdx === "function"
      ? eventDisplayFrameIdx(ev)
      : parseInt(ev.frame_idx, 10) || 0;
  if (!fi) return false;
  const hit = frameEntryByIdx(fi);
  if (!hit) return false;
  setPlaybackAuthorityFrameIdx(fi);
  await renderExplicitPlaybackFrame(fi);
  if (videoEl.duration && Number.isFinite(videoEl.duration) && videoEl.duration > 0) {
    videoEl.currentTime = Math.min(hit.t, videoEl.duration);
    if (seekBar) seekBar.value = String((videoEl.currentTime / videoEl.duration) * 1000);
    if (timeLabel) timeLabel.textContent = formatTime(videoEl.currentTime);
  }
  return lastRenderedFrameIdx === fi;
}

/** 按显式目标帧渲染（供 seeked / pause 回调与逐帧步进复用） */
async function renderExplicitPlaybackFrame(frameIdx) {
  const fi = parseInt(frameIdx, 10) || 0;
  if (!fi || !frameByTime?.length) return false;
  const hit = frameEntryByIdx(fi);
  if (!hit) return false;
  lastRenderedFrameIdx = -1;
  tickPoseFrameIdx = -1;
  tickVideoFrameIdx = -1;
  lastEventSyncFrameIdx = -1;
  resetPlaybackCollisionTracker();
  await renderFrameEntry(hit);
  return true;
}

/** 重建 frame_idx → events[] 索引，加速播放时同帧查找 */
function rebuildPlaybackEventsFrameIndex() {
  playbackEventsFrameIndex = new Map();
  (playbackEvents || []).forEach((ev) => {
    const fi = parseInt(ev.frame_idx, 10) || 0;
    if (!fi) return;
    if (!playbackEventsFrameIndex.has(fi)) playbackEventsFrameIndex.set(fi, []);
    playbackEventsFrameIndex.get(fi).push(ev);
  });
}

function eventsAtFrameIndexed(frameIdx, pool = null) {
  const fi = parseInt(frameIdx, 10) || 0;
  if (!fi) return [];
  const atFrame = playbackEventsFrameIndex.get(fi) || [];
  if (!pool) return atFrame;
  const poolSet = new Set(pool);
  return atFrame.filter((e) => poolSet.has(e));
}

/** 采集时是否已启用碰撞并落盘（有则信任存储字段，含空数组） */
function collisionPersistedAtCollect() {
  return !!(poseData?.collision?.enabled);
}

function frameUsesStoredCollisions(frame) {
  if (!collisionPersistedAtCollect() || !frame) return false;
  return (
    ("collisions" in frame || "alarm_collisions" in frame) &&
    (Array.isArray(frame.collisions) || Array.isArray(frame.alarm_collisions))
  );
}

async function collectAllFramesForPlayback(recordId) {
  if ((poseData?.schema || 1) < 2) {
    if (poseData?.frames?.length) {
      return poseData.frames.filter((f) => f && typeof f === "object");
    }
    return frameByTime.map((e) => e.frame).filter(Boolean);
  }
  const total = Number(poseData?.frame_count) || frameByTime.length;
  if (!recordId || total <= 0) return [];
  for (let from = 1; from <= total; from += FRAME_CHUNK_SIZE) {
    const to = Math.min(from + FRAME_CHUNK_SIZE - 1, total);
    await prefetchFrameChunk(from, to);
  }
  const frames = [];
  for (let i = 1; i <= total; i++) {
    const fr = frameCache.get(i);
    if (fr) frames.push(fr);
  }
  frames.sort((a, b) => (Number(a.frame_idx) || 0) - (Number(b.frame_idx) || 0));
  return frames;
}

/** 无采集碰撞落盘但有标注时，按帧扫描生成事件（方案一：仅回放侧） */
async function buildPlaybackEventsFromRealtime(recordId) {
  if (!annotationBoxes.length || collisionPersistedAtCollect()) return [];
  resetPlaybackCollisionTracker();
  const tracker = getPlaybackCollisionTracker();
  const frames = await collectAllFramesForPlayback(recordId);
  const events = [];
  for (const fr of frames) {
    const inferW = Number(fr.infer_width) || Number(poseData?.infer_width) || 640;
    const inferH = Number(fr.infer_height) || Number(poseData?.infer_height) || 480;
    const computed = tracker.update(fr, inferW, inferH);
    const ts = Number(fr.timestamp_sec) || 0;
    const fi = Number(fr.frame_idx) || 0;
    const sfi = Number(fr.source_frame_idx) || fi;
    const alarms = canonicalizeBoxTokenList(computed.alarm_collisions || []);
    const collisions = canonicalizeBoxTokenList(computed.collisions || []);
    if (alarms.length) {
      events.push({
        event_type: "alarm",
        frame_idx: fi,
        source_frame_idx: sfi,
        timestamp_sec: ts,
        box_tokens: alarms,
      });
    }
    const alarmSet = new Set(alarms);
    const collOnly = collisions.filter((t) => !alarmSet.has(t));
    if (collOnly.length) {
      events.push({
        event_type: "collision",
        frame_idx: fi,
        source_frame_idx: sfi,
        timestamp_sec: ts,
        box_tokens: collOnly,
      });
    }
  }
  events.sort((a, b) => a.timestamp_sec - b.timestamp_sec || a.frame_idx - b.frame_idx);
  return events;
}

async function loadPlaybackEvents(recordId = null) {
  playbackEvents = [];
  playbackEventsFromRealtime = false;
  activeEventKey = null;
  playbackEventLinkExact = false;
  verifiedTrueKeys.clear();
  pendingConfirmedBoxesByKey.clear();
  boxAnnotationTouchedKeys.clear();
  eventReviewStatusEventKey = null;
  reviewBackKey = null;
  currentEventReviewStatus = "not_started";
  setEventReviewSaveStatus("");

  if (recordId) {
    try {
      const res = await fetch(recordApiUrl(recordId, "/events"));
      if (res.ok) {
        const body = await res.json();
        playbackEvents = Array.isArray(body.events) ? body.events : [];
        syncVerifiedKeysFromEvents(playbackEvents, body.event_review);
        currentEventReviewStatus =
          body.event_review_status ||
          (body.event_review?.status
            ? body.event_review.status
            : body.count === 0
              ? "no_collision"
              : body.event_review?.verified_true?.length || body.event_review?.updated_at
                ? "in_progress"
                : "not_started");
        if (body.count === 0 && currentRecordId) {
          patchPlaybackRecordReviewStatus(
            currentRecordId,
            currentEventReviewStatus,
            body.event_review_label || reviewStatusLabel(currentEventReviewStatus)
          );
        }
      }
    } catch {
      /* 忽略 */
    }
  } else if (poseData?.frames?.length) {
    playbackEvents = buildEventsFromFrames(poseData.frames);
  }

  const needRealtime =
    !playbackEvents.length && annotationBoxes.length > 0 && !collisionPersistedAtCollect();
  if (needRealtime) {
    playbackEvents = await buildPlaybackEventsFromRealtime(recordId);
    playbackEventsFromRealtime = playbackEvents.length > 0;
    resetPlaybackCollisionTracker();
  }

  applyVerifiedFlagsToEvents();
  playbackEvents.forEach((ev) => {
    if (isEventVerified(ev)) applyAutoConfirmedBoxOnVerify(ev);
  });
  rebuildPlaybackEventsFrameIndex();
  renderEventReviewList();
  invalidatePlaybackAccuracyOverlay();
}

function getCurrentPlaybackTimeSec() {
  if (videoEl.duration && Number.isFinite(videoEl.duration) && videoEl.duration > 0) {
    return videoEl.currentTime;
  }
  if (!frameByTime.length) return 0;
  const idx = Math.floor((parseInt(seekBar.value, 10) / 1000) * frameByTime.length);
  const item = frameByTime[Math.min(Math.max(0, idx), frameByTime.length - 1)];
  return item?.t ?? 0;
}

function getCurrentPlaybackFrameIdx() {
  const hit = findFrameAt(getCurrentPlaybackTimeSec());
  return hit?.frameIdx ?? null;
}

function frameEntryByIdx(frameIdx) {
  const fi = parseInt(frameIdx, 10) || 0;
  if (!fi) return null;
  if (typeof ensureFrameIndexEntry === "function") return ensureFrameIndexEntry(fi);
  if (!frameByTime?.length) return null;
  return frameByTime.find((item) => item.frameIdx === fi) || null;
}

/** 复核显式跳转后钉住的事件帧（与视频 currentTime 可能差 1 帧） */
function pinnedEventFrameIdx() {
  if (!playbackEventLinkExact || !activeEventKey) return null;
  const ev =
    typeof getPinnedPlaybackEvent === "function"
      ? getPinnedPlaybackEvent()
      : typeof getActiveEvent === "function"
        ? getActiveEvent()
        : null;
  const fi = ev ? parseInt(ev.frame_idx, 10) || 0 : 0;
  return fi > 0 ? fi : null;
}

/** 漏报/标真叠加层应对齐的帧号：钉住事件时优先用事件帧 */
function playbackOverlayFrameIdx(fallback = null) {
  const pinned = pinnedEventFrameIdx();
  if (pinned != null && pinned > 0) return pinned;
  const fb = Number(fallback) || 0;
  return fb > 0 ? fb : null;
}

function getPlaybackFrameCount() {
  return Number(poseData?.frame_count) || frameByTime.length || 0;
}

function getResolvedPlaybackFrameIdx() {
  const authority = getPlaybackAuthorityFrameIdx();
  if (authority != null && authority > 0) return authority;
  const explicit = getExplicitSeekFrameIdx();
  if (explicit != null && explicit > 0) return explicit;
  if (lastRenderedFrameIdx >= 1) return lastRenderedFrameIdx;
  if (typeof getCurrentPlaybackFrameIdx === "function") {
    const fi = getCurrentPlaybackFrameIdx();
    if (fi > 0) return fi;
  }
  return null;
}

/** 更新事件复核栏「帧 N / 总数」与逐帧按钮状态 */
function updateEventReviewFrameNavUi() {
  const posEl = $("#event-review-frame-pos");
  const prevBtn = $("#event-prev-frame-btn");
  const nextBtn = $("#event-next-frame-btn");
  if (!posEl && !prevBtn && !nextBtn) return;

  const total = getPlaybackFrameCount();
  const cur = getResolvedPlaybackFrameIdx();
  const hasFrames = total > 0 && frameByTime.length > 0;

  if (posEl) {
    posEl.textContent =
      cur != null && cur > 0 && total > 0 ? `帧 ${cur} / ${total}` : hasFrames ? `帧 — / ${total}` : "帧 —";
  }
  if (prevBtn) prevBtn.disabled = !hasFrames || cur == null || cur <= 1;
  if (nextBtn) nextBtn.disabled = !hasFrames || cur == null || cur >= total;
}

/** 按帧步进（±1），与事件跳转独立；步进后关联最近事件 */
async function navigatePlaybackFrame(delta) {
  const total = getPlaybackFrameCount();
  if (!total || !frameByTime.length) return;

  const step = Number(delta) || 0;
  if (!step) return;

  let cur = getResolvedPlaybackFrameIdx();
  if (cur == null || cur < 1) cur = 1;

  const nextFi = Math.max(1, Math.min(total, cur + step));
  if (nextFi === cur) return;

  const hit = frameEntryByIdx(nextFi);
  if (!hit) return;

  setPlaybackAuthorityFrameIdx(nextFi);
  if (!videoEl.paused) videoEl.pause();
  playbackEventLinkExact = false;
  reviewBackKey = null;
  await seekToTimestamp(hit.t, hit.frameIdx, { skipEventSync: false });
  if (typeof updateReviewDock === "function") {
    updateReviewDock({ skipRedraw: true });
  }
  updateEventMarkerActiveState();
  updateEventReviewFrameNavUi();
}

function findEventsAtFrame(frameIdx) {
  if (frameIdx == null || !playbackEvents.length) return [];
  return eventsAtFrameIndexed(frameIdx);
}

function isExactEventAtPosition(ev, timeSec, frameIdx) {
  if (!ev) return false;
  if (frameIdx != null && (parseInt(ev.frame_idx, 10) || 0) === frameIdx) return true;
  const fps = poseData?.fps || 15;
  const thresh = 0.5 / fps;
  return Math.abs((Number(ev.timestamp_sec) || 0) - timeSec) <= thresh;
}

function eventsForPlaybackLink() {
  const mode = eventFilterSelect?.value || "all";
  if (mode === "all") return playbackEvents;
  const filtered = filteredPlaybackEvents();
  return filtered.length ? filtered : playbackEvents;
}

/** 当前播放位置对应事件：同帧优先，否则取时间最近 */
function findEventForPlaybackPosition(timeSec, frameIdx = null) {
  const pool = eventsForPlaybackLink();
  if (!pool.length) return null;
  if (frameIdx != null) {
    const atFrame = eventsAtFrameIndexed(frameIdx, pool);
    if (atFrame.length === 1) return atFrame[0];
    if (atFrame.length > 1) {
      return atFrame.find((e) => e.event_type === "alarm") || atFrame[0];
    }
  }
  const t = Math.max(0, Number(timeSec) || 0);
  let best = pool[0];
  let bestDist = Math.abs((Number(best.timestamp_sec) || 0) - t);
  for (const ev of pool) {
    const d = Math.abs((Number(ev.timestamp_sec) || 0) - t);
    if (
      d < bestDist ||
      (d === bestDist && (Number(ev.timestamp_sec) || 0) < (Number(best.timestamp_sec) || 0))
    ) {
      best = ev;
      bestDist = d;
    }
  }
  return best;
}

/** 进度条/播放位置变化时，同步事件复核栏的当前关联事件 */
function syncActiveEventFromPlaybackPosition(opts = {}) {
  if (!playbackEvents.length) return;
  const timeSec = opts.timeSec ?? getCurrentPlaybackTimeSec();
  const frameIdx = opts.frameIdx ?? getCurrentPlaybackFrameIdx();

  // 钉住事件时：禁止 sync 解除钉住或切换事件；画面漂移则拉回事件帧
  if (!opts.force && playbackEventLinkExact && activeEventKey) {
    const pinned =
      typeof getPinnedPlaybackEvent === "function"
        ? getPinnedPlaybackEvent()
        : typeof getActiveEvent === "function"
          ? getActiveEvent()
          : null;
    if (pinned) {
      const pinnedFi =
        typeof eventDisplayFrameIdx === "function"
          ? eventDisplayFrameIdx(pinned)
          : parseInt(pinned.frame_idx, 10) || 0;
      const fi = frameIdx != null ? parseInt(frameIdx, 10) || 0 : null;
      if (pinnedFi > 0 && fi != null && fi > 0 && fi !== pinnedFi) {
        void realignPlaybackToPinnedEvent();
      }
    }
    return;
  }

  const ev = findEventForPlaybackPosition(timeSec, frameIdx);
  if (!ev) return;
  const key = eventRowKey(ev);
  const exact = isExactEventAtPosition(ev, timeSec, frameIdx);
  // 同一事件因视频漂移不同步时：保持钉住并拉回，不改为「最近」
  if (!opts.force && key === activeEventKey) {
    if (playbackEventLinkExact && !exact) {
      void realignPlaybackToPinnedEvent();
      return;
    }
    if (playbackEventLinkExact === exact) return;
  }
  activeEventKey = key;
  playbackEventLinkExact = exact;
  if (!opts.keepReviewBack && reviewBackKey && key !== reviewBackKey) {
    reviewBackKey = null;
  }
  updateReviewDock({ skipRedraw: opts.skipRedraw });
  if ($("#event-review-list-details")?.open) renderEventReviewTable();
  updateEventMarkerActiveState();
  if (typeof updateStageBoxPickMode === "function") updateStageBoxPickMode();
  if (!opts.skipRedraw && typeof redrawCurrentFrame === "function") redrawCurrentFrame();
}

function updateEventMarkerActiveState() {
  if (!eventMarkersEl) return;
  eventMarkersEl.querySelectorAll(".event-marker").forEach((dot) => {
    dot.classList.toggle("active", dot.dataset.eventKey === activeEventKey);
  });
}

async function seekToTimestamp(timeSec, frameIdx = null, opts = {}) {
  lastRenderedFrameIdx = -1;
  tickPoseFrameIdx = -1;
  lastEventSyncFrameIdx = -1;
  resetPlaybackCollisionTracker();
  const targetFi = frameIdx != null ? parseInt(frameIdx, 10) || 0 : 0;
  const hitByIdx = targetFi > 0 ? frameEntryByIdx(targetFi) : null;
  const t = hitByIdx?.t ?? Math.max(0, Number(timeSec) || 0);
  if (hitByIdx) setPlaybackAuthorityFrameIdx(hitByIdx.frameIdx);
  if (videoEl.duration && Number.isFinite(videoEl.duration) && videoEl.duration > 0) {
    videoEl.currentTime = Math.min(t, videoEl.duration);
    seekBar.value = String((videoEl.currentTime / videoEl.duration) * 1000);
    timeLabel.textContent = formatTime(videoEl.currentTime);
    // 有目标帧时强制按 frame_idx 渲染，避免预览视频 seek 吸附关键帧导致差 1 帧
    if (hitByIdx) {
      await renderFrameEntry(hitByIdx);
    } else {
      await renderAtTime(videoEl.currentTime);
    }
    if (!opts.skipEventSync) {
      syncActiveEventFromPlaybackPosition({
        timeSec: videoEl.currentTime,
        frameIdx: hitByIdx?.frameIdx ?? frameIdx,
        skipRedraw: !!hitByIdx,
      });
    }
    return;
  }

  let hit = hitByIdx;
  if (!hit) hit = findFrameAt(t);
  if (hit) {
    await renderFrameEntry(hit);
    const idx = frameByTime.indexOf(hit);
    if (idx >= 0 && frameByTime.length) {
      seekBar.value = String((idx / frameByTime.length) * 1000);
      timeLabel.textContent = `${idx + 1}/${frameByTime.length}`;
    } else {
      timeLabel.textContent = formatTime(t);
    }
  }
  if (!opts.skipEventSync) {
    syncActiveEventFromPlaybackPosition({ timeSec: t, frameIdx: hit?.frameIdx ?? frameIdx });
  }
}

async function seekToEvent(ev, { keepReviewBack = false } = {}) {
  if (!ev) return;
  const key = eventRowKey(ev);
  const targetFi = parseInt(ev.frame_idx, 10) || 0;
  // 先钉住事件与权威帧，再 pause；否则 pause 回调会按旧 currentTime 异步重绘并覆盖目标帧
  activeEventKey = key;
  playbackEventLinkExact = true;
  if (targetFi > 0) setPlaybackAuthorityFrameIdx(targetFi);
  if (!keepReviewBack && reviewBackKey && activeEventKey === reviewBackKey) {
    reviewBackKey = null;
  }
  if (!videoEl.paused) videoEl.pause();
  await seekToTimestamp(ev.timestamp_sec, ev.frame_idx, { skipEventSync: true });
  // seek 完成后再次锁定（防止 seeked/loadedmetadata 异步覆盖）
  activeEventKey = key;
  playbackEventLinkExact = true;
  if (targetFi > 0) setPlaybackAuthorityFrameIdx(targetFi);
  if (typeof updateReviewDock === "function") {
    updateReviewDock({ skipRedraw: true });
  }
  if ($("#event-review-list-details")?.open) renderEventReviewTable();
  updateEventMarkerActiveState();
  if (typeof updateStageBoxPickMode === "function") updateStageBoxPickMode();
  updateEventReviewFrameNavUi();
  // 异步 seeked 可能把画面漂移：终态与下一帧任务各校验一次
  const ensurePinnedAligned = async () => {
    if (activeEventKey !== key || !playbackEventLinkExact || !targetFi) return;
    if (lastRenderedFrameIdx !== targetFi || getPlaybackAuthorityFrameIdx() !== targetFi) {
      await realignPlaybackToPinnedEvent();
      activeEventKey = key;
      playbackEventLinkExact = true;
      setPlaybackAuthorityFrameIdx(targetFi);
      updateEventReviewFrameNavUi();
    }
  };
  if (targetFi > 0 && lastRenderedFrameIdx !== targetFi) {
    await ensurePinnedAligned();
  } else if (targetFi > 0) {
    queueMicrotask(() => void ensurePinnedAligned());
    window.setTimeout(() => void ensurePinnedAligned(), 80);
  }
}

function clearPlaybackEvents() {
  playbackEvents = [];
  playbackEventsFromRealtime = false;
  playbackEventsFrameIndex = new Map();
  activeEventKey = null;
  playbackEventLinkExact = false;
  clearPlaybackAuthorityFrameIdx();
  verifiedTrueKeys.clear();
  pendingConfirmedBoxesByKey.clear();
  boxAnnotationTouchedKeys.clear();
  eventReviewStatusEventKey = null;
  reviewBackKey = null;
  if (eventReviewSaveTimer) {
    clearTimeout(eventReviewSaveTimer);
    eventReviewSaveTimer = null;
  }
  if (eventMarkersEl) eventMarkersEl.innerHTML = "";
  if (accuracyMarkersEl) accuracyMarkersEl.innerHTML = "";
  if (eventJumpList) eventJumpList.innerHTML = "";
  if (eventsPanel) eventsPanel.classList.add("hidden");
  if (typeof invalidatePlaybackAccuracyOverlay === "function") invalidatePlaybackAccuracyOverlay();
  if (typeof clearPlaybackWristFeatures === "function") clearPlaybackWristFeatures();
  if (typeof clearPlaybackSkeletonFeatures === "function") clearPlaybackSkeletonFeatures();
  if (eventCountLabel) eventCountLabel.textContent = "—";
  setEventReviewSaveStatus("");
}

function setPlaybackInfo(text) {
  $("#playback-info").textContent = text;
}
