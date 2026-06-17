/** 回放事件加载、定位与清除 */

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

function findEventsAtFrame(frameIdx) {
  if (frameIdx == null || !playbackEvents.length) return [];
  const fi = parseInt(frameIdx, 10) || 0;
  return playbackEvents.filter((e) => (parseInt(e.frame_idx, 10) || 0) === fi);
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
    const fi = parseInt(frameIdx, 10) || 0;
    const atFrame = pool.filter((e) => (parseInt(e.frame_idx, 10) || 0) === fi);
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

  // 复核显式跳转后，同帧多条事件时不要被「同帧优先/最近」覆盖当前选中
  if (!opts.force && playbackEventLinkExact && activeEventKey) {
    const pinned = getActiveEvent();
    if (pinned) {
      const pinnedFi = parseInt(pinned.frame_idx, 10) || 0;
      const fi = frameIdx != null ? parseInt(frameIdx, 10) || 0 : null;
      if (fi != null && fi === pinnedFi) return;
      if (isExactEventAtPosition(pinned, timeSec, frameIdx)) return;
    }
  }

  const ev = findEventForPlaybackPosition(timeSec, frameIdx);
  if (!ev) return;
  const key = eventRowKey(ev);
  const exact = isExactEventAtPosition(ev, timeSec, frameIdx);
  if (!opts.force && key === activeEventKey && playbackEventLinkExact === exact) return;
  activeEventKey = key;
  playbackEventLinkExact = exact;
  if (!opts.keepReviewBack && reviewBackKey && key !== reviewBackKey) {
    reviewBackKey = null;
  }
  updateReviewDock();
  if ($("#event-review-list-details")?.open) renderEventReviewTable();
  updateEventMarkerActiveState();
  if (typeof updateStageBoxPickMode === "function") updateStageBoxPickMode();
  if (typeof redrawCurrentFrame === "function") redrawCurrentFrame();
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
  resetPlaybackCollisionTracker();
  const t = Math.max(0, Number(timeSec) || 0);
  if (videoEl.duration && Number.isFinite(videoEl.duration) && videoEl.duration > 0) {
    videoEl.currentTime = Math.min(t, videoEl.duration);
    seekBar.value = String((videoEl.currentTime / videoEl.duration) * 1000);
    timeLabel.textContent = formatTime(videoEl.currentTime);
    await renderAtTime(videoEl.currentTime);
    if (!opts.skipEventSync) {
      syncActiveEventFromPlaybackPosition({ timeSec: videoEl.currentTime, frameIdx });
    }
    return;
  }

  let hit = null;
  if (frameIdx != null) {
    hit = frameByTime.find((item) => item.frameIdx === frameIdx) || null;
  }
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
  activeEventKey = eventRowKey(ev);
  playbackEventLinkExact = true;
  if (!keepReviewBack && reviewBackKey && activeEventKey === reviewBackKey) {
    reviewBackKey = null;
  }
  updateReviewDock();
  if ($("#event-review-list-details")?.open) renderEventReviewTable();
  updateEventMarkerActiveState();
  videoEl.pause();
  await seekToTimestamp(ev.timestamp_sec, ev.frame_idx, { skipEventSync: true });
}

function clearPlaybackEvents() {
  playbackEvents = [];
  playbackEventsFromRealtime = false;
  activeEventKey = null;
  playbackEventLinkExact = false;
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
  if (eventCountLabel) eventCountLabel.textContent = "—";
  setEventReviewSaveStatus("");
}

function setPlaybackInfo(text) {
  $("#playback-info").textContent = text;
}
