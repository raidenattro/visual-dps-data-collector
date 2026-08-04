/** 回放控件绑定与页面初始化 */

$("#playback-json").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  if (!(await prepareEventReviewRecordSwitch())) {
    e.target.value = "";
    return;
  }
  await cleanupPlaybackVideo();
  clearVideoElement();
  poseData = JSON.parse(await file.text());
  currentRecordId = null;
  await buildFrameIndex();
  await loadPlaybackEvents(null);
  $("#playback-annotation").value = "";
  const f0 = frameByTime[0];
  setPlaybackInfo(
    `已导入 ${file.name}，${poseData.frame_count ?? poseData.frames?.length ?? 0} 帧` +
      (f0 ? `（推理 ${f0.w}×${f0.h}）` : "") +
      "。请上传配套视频后播放。"
  );
  redrawCurrentFrame();
});

$("#playback-annotation").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  try {
    await loadAnnotationBoxesFromFile(file);
    await loadPlaybackEvents(currentRecordId);
    if (playbackEvents.length) await beginEventReview();
    const rtNote = playbackEventsFromRealtime ? "，已生成回放事件列表" : "";
    setPlaybackInfo(`已导入标注 ${file.name}，${annotationBoxes.length} 个货框${rtNote}`);
    redrawCurrentFrame();
  } catch (err) {
    setPlaybackInfo(`❌ 标注 JSON 无效: ${err.message}`);
  }
});

$("#playback-video").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  await cleanupPlaybackVideo();
  stopPlayback();

  playbackVideoObjectUrl = URL.createObjectURL(file);
  videoEl.src = playbackVideoObjectUrl;
  videoEl.style.display = "block";
  videoEl.load();

  const onMeta = () => {
    videoEl.removeEventListener("loadedmetadata", onMeta);
    const { frameW, frameH } = getVideoFrameSize();
    const f0 = frameByTime[0];
    let hint = `视频 ${frameW}×${frameH} 已加载。`;
    if (f0 && (f0.w !== frameW || f0.h !== frameH)) {
      hint += ` JSON 推理分辨率为 ${f0.w}×${f0.h}，将按视频画面自动对齐。`;
    }
    hint += " 点击播放。";
    setPlaybackInfo(hint);
    redrawCurrentFrame();
  };
  videoEl.addEventListener("loadedmetadata", onMeta);
});

function initPlaybackSpeedControl() {
  const sel = $("#playback-speed");
  if (!sel || sel.dataset.bound) return;
  sel.dataset.bound = "1";
  sel.addEventListener("change", () => {
    readPlaybackSpeedFromSelect();
    restartJsonOnlyPlaybackIfActive();
  });
  readPlaybackSpeedFromSelect();
}

function readShowDetBboxFromStorage() {
  try {
    return localStorage.getItem(DET_BBOX_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

function persistShowDetBbox() {
  try {
    localStorage.setItem(DET_BBOX_STORAGE_KEY, showDetBbox ? "1" : "0");
  } catch {
    /* ignore */
  }
}

function initPlaybackDetBboxToggle() {
  const cb = $("#playback-show-det-bbox");
  if (!cb || cb.dataset.bound) return;
  cb.dataset.bound = "1";
  showDetBbox = readShowDetBboxFromStorage();
  cb.checked = showDetBbox;
  cb.addEventListener("change", () => {
    showDetBbox = !!cb.checked;
    persistShowDetBbox();
    redrawCurrentFrame();
  });
}

function readShowSkeletonFromStorage() {
  try {
    return localStorage.getItem(SKELETON_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

function persistShowSkeleton() {
  try {
    localStorage.setItem(SKELETON_STORAGE_KEY, showSkeleton ? "1" : "0");
  } catch {
    /* ignore */
  }
}

function initPlaybackSkeletonToggle() {
  const cb = $("#playback-show-skeleton");
  if (!cb || cb.dataset.bound) return;
  cb.dataset.bound = "1";
  showSkeleton = readShowSkeletonFromStorage();
  cb.checked = showSkeleton;
  cb.addEventListener("change", () => {
    showSkeleton = !!cb.checked;
    persistShowSkeleton();
    redrawCurrentFrame();
  });
}

function readShowAlgoCollisionFromStorage() {
  try {
    const raw = localStorage.getItem(ALGO_COLLISION_STORAGE_KEY);
    // 未写过时默认开启，与改造前行为一致。
    return raw == null ? true : raw === "1";
  } catch {
    return true;
  }
}

function persistShowAlgoCollision() {
  try {
    localStorage.setItem(ALGO_COLLISION_STORAGE_KEY, showAlgoCollisionColors ? "1" : "0");
  } catch {
    /* ignore */
  }
}

function initPlaybackAlgoCollisionToggle() {
  const cb = $("#playback-show-algo-collision");
  if (!cb || cb.dataset.bound) return;
  cb.dataset.bound = "1";
  showAlgoCollisionColors = readShowAlgoCollisionFromStorage();
  cb.checked = showAlgoCollisionColors;
  cb.addEventListener("change", () => {
    showAlgoCollisionColors = !!cb.checked;
    persistShowAlgoCollision();
    redrawCurrentFrame();
  });
}

function readShowEventReviewFromStorage() {
  try {
    const raw = localStorage.getItem(EVENT_REVIEW_HIGHLIGHT_STORAGE_KEY);
    // 未写过时默认开启，与改造前行为一致。
    return raw == null ? true : raw === "1";
  } catch {
    return true;
  }
}

function persistShowEventReview() {
  try {
    localStorage.setItem(
      EVENT_REVIEW_HIGHLIGHT_STORAGE_KEY,
      showEventReviewHighlights ? "1" : "0"
    );
  } catch {
    /* ignore */
  }
}

function initPlaybackEventReviewToggle() {
  const cb = $("#playback-show-event-review");
  if (!cb || cb.dataset.bound) return;
  cb.dataset.bound = "1";
  showEventReviewHighlights = readShowEventReviewFromStorage();
  cb.checked = showEventReviewHighlights;
  cb.addEventListener("change", () => {
    showEventReviewHighlights = !!cb.checked;
    persistShowEventReview();
    redrawCurrentFrame();
  });
}

function readShowReviewRiskFromStorage() {
  try {
    const raw = localStorage.getItem(REVIEW_RISK_STORAGE_KEY);
    // 未写过时默认关闭，避免复核时干扰标注。
    return raw == null ? false : raw === "1";
  } catch {
    return false;
  }
}

function persistShowReviewRisk() {
  try {
    localStorage.setItem(REVIEW_RISK_STORAGE_KEY, showReviewRiskHints ? "1" : "0");
  } catch {
    /* ignore */
  }
}

function initPlaybackReviewRiskToggle() {
  const cb = $("#playback-show-review-risk");
  if (!cb || cb.dataset.bound) return;
  cb.dataset.bound = "1";
  showReviewRiskHints = readShowReviewRiskFromStorage();
  cb.checked = showReviewRiskHints;
  cb.addEventListener("change", () => {
    showReviewRiskHints = !!cb.checked;
    persistShowReviewRisk();
    if (typeof updateReviewConflictBar === "function") updateReviewConflictBar();
    redrawCurrentFrame();
  });
}

function isPlaybackActive() {
  if (jsonOnlyTimer) return true;
  return !!(videoEl.src && !videoEl.paused && !videoEl.ended);
}

function syncPlaybackToggleButton() {
  const btn = $("#play-btn");
  if (!btn) return;
  const playing = isPlaybackActive();
  btn.textContent = playing ? "Ⅱ" : "▶";
  btn.setAttribute("aria-pressed", playing ? "true" : "false");
  btn.setAttribute("aria-label", playing ? "暂停" : "播放");
  btn.title = playing ? "暂停（空格）" : "播放（空格）";
}

async function startPlaybackTransport() {
  if (videoEl.src) {
    videoEl.style.display = "block";
    readPlaybackSpeedFromSelect();
    playbackEventLinkExact = false;
    lastEventSyncFrameIdx = -1;
    if (typeof clearPlaybackAuthorityFrameIdx === "function") clearPlaybackAuthorityFrameIdx();
    try {
      await videoEl.play();
    } catch (err) {
      setPlaybackInfo(`播放失败: ${err.message}（可点击视频控件或检查格式）`);
      return;
    }
    if (typeof ensurePlaybackRenderLoop === "function") {
      ensurePlaybackRenderLoop();
    }
    return;
  }
  if (poseData) {
    startJsonOnlyPlayback(jsonOnlyFrameIdx || tickPoseFrameIdx || 0);
    return;
  }
  setPlaybackInfo("请先导入 JSON 或加载记录");
}

function togglePlaybackTransport() {
  if (isPlaybackActive()) {
    stopPlayback();
    return;
  }
  void startPlaybackTransport();
}

let heldFrameNavigation = null;

function stopHeldFrameNavigation({ finish = true } = {}) {
  const state = heldFrameNavigation;
  if (!state) return;
  heldFrameNavigation = null;
  clearTimeout(state.delayTimer);
  clearInterval(state.repeatTimer);
  if (!finish || !state.accelerated) return;
  const fps = Math.max(1, Number(poseData?.fps) || 25);
  const elapsedSec = Math.max(0, (performance.now() - state.startedAt) / 1000);
  const distance = Math.max(1, Math.round(elapsedSec * fps * 3));
  void navigatePlaybackToFrame(state.startFrame + state.direction * distance);
}

/** 短按走 1 帧；按住 400ms 后按素材 fps 的 3× 速度连续快进/快退。 */
function startHeldFrameNavigation(direction, key) {
  stopHeldFrameNavigation({ finish: false });
  const firstFi = Number(frameByTime?.[0]?.frameIdx) || 1;
  const startFrame = getResolvedPlaybackFrameIdx() || firstFi;
  const state = {
    direction: direction < 0 ? -1 : 1,
    key,
    startFrame,
    startedAt: performance.now(),
    accelerated: false,
    lastTarget: null,
    delayTimer: null,
    repeatTimer: null,
  };
  heldFrameNavigation = state;
  void navigatePlaybackFrame(state.direction);
  state.delayTimer = setTimeout(() => {
    if (heldFrameNavigation !== state) return;
    state.accelerated = true;
    const updateTarget = () => {
      if (heldFrameNavigation !== state) return;
      const fps = Math.max(1, Number(poseData?.fps) || 25);
      const elapsedSec = Math.max(0, (performance.now() - state.startedAt) / 1000);
      const distance = Math.max(1, Math.round(elapsedSec * fps * 3));
      const target = state.startFrame + state.direction * distance;
      if (target === state.lastTarget) return;
      state.lastTarget = target;
      void navigatePlaybackToFrame(target);
    };
    updateTarget();
    state.repeatTimer = setInterval(updateTarget, 100);
  }, 400);
}

function jumpToFrameFromInput() {
  const input = $("#playback-frame-input");
  if (!input) return;
  const target = Math.round(Number(input.value) || 0);
  if (target > 0) void navigatePlaybackToFrame(target);
}

function initPlaybackFrameNavigationControls() {
  $("#playback-first-frame-btn")?.addEventListener("click", () => {
    const fi = Number(frameByTime?.[0]?.frameIdx) || 1;
    void navigatePlaybackToFrame(fi);
  });
  $("#playback-back-10-btn")?.addEventListener("click", () =>
    void navigatePlaybackFrame(-10)
  );
  $("#playback-back-1-btn")?.addEventListener("click", () =>
    void navigatePlaybackFrame(-1)
  );
  $("#playback-forward-1-btn")?.addEventListener("click", () =>
    void navigatePlaybackFrame(1)
  );
  $("#playback-forward-10-btn")?.addEventListener("click", () =>
    void navigatePlaybackFrame(10)
  );
  $("#playback-last-frame-btn")?.addEventListener("click", () => {
    const fi = Number(frameByTime?.[frameByTime.length - 1]?.frameIdx) || 1;
    void navigatePlaybackToFrame(fi);
  });
  const input = $("#playback-frame-input");
  input?.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    jumpToFrameFromInput();
    input.blur();
  });
  input?.addEventListener("change", jumpToFrameFromInput);
  updateEventReviewFrameNavUi();
}

$("#play-btn").addEventListener("click", () => {
  togglePlaybackTransport();
});

videoEl.addEventListener("ended", () => {
  stopPlayback();
  syncPlaybackToggleButton();
  if (videoEl.src && videoEl.src.startsWith("blob:")) {
    cleanupPlaybackVideo();
    videoEl.removeAttribute("src");
    videoEl.load();
    setPlaybackInfo("播放结束。可重新选择视频。");
  } else {
    setPlaybackInfo("播放结束。可再次点击播放。");
    const lastEntry = frameByTime?.[frameByTime.length - 1] || null;
    if (lastEntry?.frameIdx) {
      if (typeof setPlaybackAuthorityFrameIdx === "function") {
        setPlaybackAuthorityFrameIdx(lastEntry.frameIdx);
      }
      if (typeof updatePlaybackSeekBarUi === "function") {
        updatePlaybackSeekBarUi(lastEntry.t, lastEntry.frameIdx);
      } else if (seekBar) {
        seekBar.value = "1000";
      }
      if (typeof renderExplicitPlaybackFrame === "function") {
        void renderExplicitPlaybackFrame(lastEntry.frameIdx).then(() => {
          if (typeof updateEventReviewFrameNavUi === "function") {
            updateEventReviewFrameNavUi();
          }
        });
      }
    }
  }
});

videoEl.addEventListener("loadedmetadata", () => {
  readPlaybackSpeedFromSelect();
  syncCanvasSize({ force: true });
  if (!playbackRenderLoopActive) redrawCurrentFrame();
  renderEventMarkers();
});

/** 视频开始播放时启动唯一一条骨架渲染循环（底部按钮与 play 事件共用） */
videoEl.addEventListener("play", () => {
  if (typeof clearPlaybackAuthorityFrameIdx === "function") clearPlaybackAuthorityFrameIdx();
  playbackEventLinkExact = false;
  lastEventSyncFrameIdx = -1;
  readPlaybackSpeedFromSelect();
  if (typeof ensurePlaybackRenderLoop === "function") ensurePlaybackRenderLoop();
  if (typeof onPlaybackVideoPlayStateChange === "function") onPlaybackVideoPlayStateChange();
  syncPlaybackToggleButton();
});

videoEl.addEventListener("pause", () => {
  syncPlaybackToggleButton();
  const explicitFi =
    typeof getExplicitSeekFrameIdx === "function" ? getExplicitSeekFrameIdx() : null;
  const authorityFi =
    typeof getPlaybackAuthorityFrameIdx === "function" ? getPlaybackAuthorityFrameIdx() : null;
  if (explicitFi || authorityFi || playbackEventLinkExact) {
    if (typeof cancelPlaybackRenderLoop === "function") {
      cancelPlaybackRenderLoop({ preserveLayout: true });
    }
    const resumeMediaTime = lastPlaybackMediaTimeSec;
    const pinnedFi =
      playbackEventLinkExact && typeof pinnedEventFrameIdx === "function"
        ? pinnedEventFrameIdx()
        : null;
    const resumeFrameIdx =
      authorityFi ??
      explicitFi ??
      pinnedFi ??
      (lastRenderedFrameIdx >= 1 ? lastRenderedFrameIdx : null);
    if (typeof renderPausedPlaybackFrame === "function") {
      renderPausedPlaybackFrame({ mediaTime: resumeMediaTime, frameIdx: resumeFrameIdx });
    }
    if (typeof updateEventReviewFrameNavUi === "function") updateEventReviewFrameNavUi();
    if (typeof updateReviewDock === "function") updateReviewDock({ skipRedraw: true });
    if (typeof patchEventReviewTableActiveState === "function") patchEventReviewTableActiveState();
    if (typeof scrollActiveEventRowIntoView === "function") scrollActiveEventRowIntoView();
    if (typeof updateEventMarkerActiveState === "function") updateEventMarkerActiveState();
    if (typeof updatePlaybackSeekBarUi === "function") updatePlaybackSeekBarUi();
    else if (videoEl.duration && Number.isFinite(videoEl.duration)) {
      seekBar.value = String((videoEl.currentTime / videoEl.duration) * 1000);
      timeLabel.textContent = formatTime(videoEl.currentTime);
    }
    return;
  }
  const resumeMediaTime = lastPlaybackMediaTimeSec;
  const resumeFrameIdx = lastRenderedFrameIdx >= 1 ? lastRenderedFrameIdx : null;
  if (typeof cancelPlaybackRenderLoop === "function") {
    cancelPlaybackRenderLoop({ preserveLayout: true });
  }
  tickVideoFrameIdx = -1;
  if (!playbackEventLinkExact && typeof syncActiveEventFromPlaybackPosition === "function") {
    syncActiveEventFromPlaybackPosition({
      timeSec: videoEl.currentTime,
      frameIdx:
        resumeFrameIdx ??
        (typeof getCurrentPlaybackFrameIdx === "function"
          ? getCurrentPlaybackFrameIdx()
          : typeof frameIdxAtVideoTime === "function"
            ? frameIdxAtVideoTime(
                typeof resolvePlaybackMediaTime === "function"
                  ? resolvePlaybackMediaTime(resumeMediaTime)
                  : videoEl.currentTime,
                { playback: true }
              )
            : null),
      skipRedraw: true,
    });
  }
  if (typeof renderPausedPlaybackFrame === "function") {
    renderPausedPlaybackFrame({ mediaTime: resumeMediaTime, frameIdx: resumeFrameIdx });
  }
  if (typeof onPlaybackVideoPlayStateChange === "function") onPlaybackVideoPlayStateChange();
  if (typeof updatePlaybackSeekBarUi === "function") updatePlaybackSeekBarUi();
  else if (videoEl.duration && Number.isFinite(videoEl.duration)) {
    seekBar.value = String((videoEl.currentTime / videoEl.duration) * 1000);
    timeLabel.textContent = formatTime(videoEl.currentTime);
  }
});

eventFilterSelect?.addEventListener("change", () => {
  const list = filteredPlaybackEvents();
  renderEventMarkers();
  if (typeof renderAccuracySeekMarkers === "function") renderAccuracySeekMarkers();
  if ($("#event-review-list-details")?.open) {
    renderEventReviewTable(list);
  }
  refreshEventCountLabel();
  const first = list[0];
  if (first) {
    if (typeof selectReviewEventWithoutPlaybackNavigation === "function") {
      selectReviewEventWithoutPlaybackNavigation(first, { scroll: false });
    }
  } else {
    activeEventKey = null;
    playbackEventLinkExact = false;
    updateReviewDock();
  }
  eventFilterSelect?.blur();
});

function initEventReviewControls() {
  bindEventReviewListScrollSync();
  scheduleEventReviewListScrollHeight();

  $("#event-prev-btn")?.addEventListener("click", () => navigateReviewEvent(-1));
  $("#event-skip-next-btn")?.addEventListener("click", () => void skipToNextEvent());
  $("#event-mark-true-next-btn")?.addEventListener("click", () => void confirmTrueAndNextFrame());
  $("#event-unmark-btn")?.addEventListener("click", () => void unmarkTrueAndNextFrame());
  $("#event-mark-all-true-btn")?.addEventListener("click", () => void markAllEventsVerified(true));
  $("#event-unmark-all-btn")?.addEventListener("click", () => void markAllEventsVerified(false));
  $("#event-review-complete-btn")?.addEventListener("click", () =>
    void (typeof confirmMarkEventReviewCompleted === "function"
      ? confirmMarkEventReviewCompleted()
      : markEventReviewCompleted())
  );
  $("#event-review-person-cycle-btn")?.addEventListener("click", () => {
    cycleEventReviewPersonSelection();
  });

  $("#event-range-set-start-btn")?.addEventListener("click", () => setRangeAnnotStartFromCurrent());
  $("#event-range-set-end-btn")?.addEventListener("click", () => setRangeAnnotEndFromCurrent());
  $("#event-range-clear-btn")?.addEventListener("click", () => {
    clearRangeAnnotBounds();
    setEventReviewSaveStatus("已清除区间标真设置", "");
  });
  $("#event-range-apply-btn")?.addEventListener("click", () => void applyRangeAnnotVerified());

  canvas?.addEventListener("click", (e) => {
    if (!eventsPanel || eventsPanel.classList.contains("hidden")) return;
    // 只读复核时，第一次点画面只负责把标注控件召出来，不改数据。
    if (typeof interceptRecheckCanvasClick === "function" && interceptRecheckCanvasClick()) {
      return;
    }
    let ev = getActiveEvent() ?? getActiveFilteredEvent();
    const personHit =
      typeof hitTestPersonDetailAtClient === "function"
        ? hitTestPersonDetailAtClient(e.clientX, e.clientY)
        : (() => {
            const personId = hitTestPersonAtClient(e.clientX, e.clientY);
            return personId == null
              ? null
              : { personId, kind: "bbox", score: 0 };
          })();
    const annotationHit = annotationBoxes.length
      ? hitTestAnnotationBoxAtClient(e.clientX, e.clientY)
      : null;
    const canvasHit =
      typeof resolveEventReviewCanvasHit === "function"
        ? resolveEventReviewCanvasHit(personHit, annotationHit)
        : annotationHit
          ? { kind: "annotation", value: annotationHit }
          : personHit
            ? { kind: "person", value: personHit.personId }
            : null;
    if (canvasHit?.kind === "person") {
      if (!ev) {
        setEventReviewSaveStatus("请先在右侧选择一条碰撞/告警事件", "");
        return;
      }
      void setPersonIdForEvent(ev, Number(canvasHit.value));
      return;
    }
    if (!annotationBoxes.length) {
      setEventReviewSaveStatus("请先加载标注 JSON", "error");
      return;
    }
    if (canvasHit?.kind !== "annotation") {
      setEventReviewSaveStatus("未点中货框或骨架，请点击货架货框或骨架标签", "");
      return;
    }
    const hit = canvasHit.value;
    ev =
      typeof resolveEventForBoxAnnotation === "function"
        ? resolveEventForBoxAnnotation(hit)
        : getActiveEvent() ?? getActiveFilteredEvent();
    if (!ev) {
      setEventReviewSaveStatus("请先在右侧选择一条碰撞/告警事件", "");
      return;
    }
    const key = eventRowKey(ev);
    if (key !== activeEventKey) {
      activeEventKey = key;
      playbackEventLinkExact =
        typeof eventMatchesPlaybackFrame === "function" &&
        typeof getResolvedPlaybackFrameIdx === "function" &&
        eventMatchesPlaybackFrame(ev, getResolvedPlaybackFrameIdx());
      if (typeof updateReviewDock === "function") updateReviewDock({ skipRedraw: true });
      if (typeof updateEventMarkerActiveState === "function") updateEventMarkerActiveState();
    }
    void toggleConfirmedBoxForEvent(ev, hit);
  });

  $("#event-review-list-details")?.addEventListener("toggle", (e) => {
    if (e.target.open) renderEventReviewTable();
    scheduleEventReviewListScrollHeight();
  });

  let altPersonCycleArmed = false;
  let altPersonCycleCancelled = false;
  document.addEventListener("keydown", (e) => {
    if (!panels.playback?.classList.contains("active")) return;
    if (isReviewTypingTarget(e.target)) return;
    // 模态弹窗（快捷键帮助 / 二次确认）打开时让位，避免 R、Y 等键穿透到底层页面。
    if (document.querySelector("dialog[open]")) return;
    if (altPersonCycleArmed && e.key !== "Alt") {
      altPersonCycleCancelled = true;
    }
    if (
      (e.key === "1" || e.key === "2") &&
      typeof isRangePersonConfirmationRequired === "function" &&
      typeof getResolvedPlaybackFrameIdx === "function" &&
      isRangePersonConfirmationRequired(getResolvedPlaybackFrameIdx())
    ) {
      const frameIdx = getResolvedPlaybackFrameIdx();
      const stableId = Number(e.key) - 1;
      const personId =
        typeof getRawPersonIdForStablePerson === "function"
          ? getRawPersonIdForStablePerson(frameIdx, stableId)
          : stableId;
      const ids =
        typeof getFramePersonIds === "function" ? getFramePersonIds(frameIdx) : [];
      if (personId != null && ids.includes(Number(personId))) {
        const ev =
          typeof getPinnedPlaybackEvent === "function"
            ? getPinnedPlaybackEvent()
            : null;
        if (ev && Number(ev.frame_idx) === Number(frameIdx)) {
          e.preventDefault();
          void setPersonIdForEvent(ev, personId);
          return;
        }
      }
    }
    if (e.key === "Alt") {
      e.preventDefault();
      if (!e.repeat) {
        altPersonCycleArmed = true;
        altPersonCycleCancelled = false;
      }
      return;
    }
    if (
      (e.key === "a" || e.key === "A") &&
      !e.altKey &&
      !e.ctrlKey &&
      !e.metaKey
    ) {
      e.preventDefault();
      if (!e.repeat) setRangeAnnotStartFromCurrent();
      return;
    }
    if (
      (e.key === "d" || e.key === "D") &&
      !e.altKey &&
      !e.ctrlKey &&
      !e.metaKey
    ) {
      e.preventDefault();
      if (!e.repeat) setRangeAnnotEndFromCurrent();
      return;
    }
    if (
      (e.key === "r" || e.key === "R") &&
      !e.altKey &&
      !e.ctrlKey &&
      !e.metaKey
    ) {
      e.preventDefault();
      if (e.repeat) return;
      if (e.shiftKey) {
        clearRangeAnnotBounds();
        setEventReviewSaveStatus("已清除区间标真设置", "");
      } else {
        void applyRangeAnnotVerified();
      }
      return;
    }
    if (e.key === " " || e.code === "Space") {
      e.preventDefault();
      togglePlaybackTransport();
      return;
    }
    if (!playbackEvents.length && !frameByTime.length) return;
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      e.preventDefault();
      if (e.repeat) return;
      const direction = e.key === "ArrowLeft" ? -1 : 1;
      if (e.ctrlKey || e.metaKey) {
        stopHeldFrameNavigation({ finish: false });
        void navigatePlaybackFrame(direction * 10);
      } else {
        startHeldFrameNavigation(direction, e.key);
      }
      return;
    }
    if (e.key === "Home") {
      e.preventDefault();
      const fi = Number(frameByTime?.[0]?.frameIdx) || 1;
      void navigatePlaybackToFrame(fi);
      return;
    }
    if (e.key === "End") {
      e.preventDefault();
      const fi = Number(frameByTime?.[frameByTime.length - 1]?.frameIdx) || 1;
      void navigatePlaybackToFrame(fi);
      return;
    }
    if (e.key === "g" || e.key === "G") {
      e.preventDefault();
      const input = $("#playback-frame-input");
      input?.focus();
      input?.select();
      return;
    }
    if (!playbackEvents.length) return;
    if (e.key === "y" || e.key === "Y") {
      e.preventDefault();
      void confirmTrueAndNextFrame();
    } else if (e.key === "n" || e.key === "N" || e.key === "j" || e.key === "J") {
      e.preventDefault();
      void skipToNextEvent();
    } else if (e.key === "u" || e.key === "U") {
      e.preventDefault();
      void unmarkTrueAndNextFrame();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      navigateReviewEvent(1);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      navigateReviewEvent(-1);
    }
  });

  document.addEventListener("keyup", (e) => {
    if (e.key === "Alt") {
      const shouldCycle = altPersonCycleArmed && !altPersonCycleCancelled;
      altPersonCycleArmed = false;
      altPersonCycleCancelled = false;
      if (shouldCycle && panels.playback?.classList.contains("active")) {
        e.preventDefault();
        cycleEventReviewPersonSelection();
      }
      return;
    }
    if (!heldFrameNavigation || e.key !== heldFrameNavigation.key) return;
    stopHeldFrameNavigation({ finish: true });
  });
  window.addEventListener("blur", () => {
    altPersonCycleArmed = false;
    altPersonCycleCancelled = false;
    stopHeldFrameNavigation({ finish: true });
  });
}

videoEl.addEventListener("seeked", () => {
  if (typeof isExplicitFrameSeekInFlight === "function" && isExplicitFrameSeekInFlight()) {
    return;
  }
  const authorityFi =
    typeof getPlaybackAuthorityFrameIdx === "function" ? getPlaybackAuthorityFrameIdx() : null;
  if (authorityFi != null && authorityFi > 0) {
    return;
  }
  if (playbackRenderLoopActive && !videoEl.paused) return;

  const pinnedEventNav = playbackEventLinkExact && activeEventKey;
  const syncAfterSeek = (fi) => {
    if (!fi) return;
    if (!pinnedEventNav && typeof syncActiveEventFromPlaybackPosition === "function") {
      syncActiveEventFromPlaybackPosition({
        timeSec: videoEl.currentTime,
        frameIdx: fi,
        skipRedraw: true,
      });
    }
    if (typeof updateEventReviewFrameNavUi === "function") updateEventReviewFrameNavUi();
  };

  if (typeof renderSkeletonSyncedToVideo === "function" && videoEl.readyState >= 2) {
    void renderSkeletonSyncedToVideo({ playback: true, setAuthority: true }).then(syncAfterSeek);
    return;
  }

  lastRenderedFrameIdx = -1;
  tickPoseFrameIdx = -1;
  tickVideoFrameIdx = -1;
  lastEventSyncFrameIdx = -1;
  resetPlaybackCollisionTracker();
  void renderAtTime(videoEl.currentTime).then(() => {
    if (pinnedEventNav) return;
    syncActiveEventFromPlaybackPosition({ timeSec: videoEl.currentTime });
  });
});

window.addEventListener("resize", () => {
  if (typeof clearPausedPlaybackLayout === "function") clearPausedPlaybackLayout();
  syncCanvasSize({ force: true });
  redrawCurrentFrame();
});

window.addEventListener("beforeunload", () => {
  cleanupPlaybackVideo();
});

// 拖完不把焦点留在进度条上：焦点环看着像「选中了这一块」，容易让人以为页面卡住。
// 键盘 Tab 过来仍能用，上面的 keydown 已经放行 range。
seekBar.addEventListener("pointerup", () => seekBar.blur());

let playbackSeekInputTimer = 0;
let playbackSeekInputSeq = 0;

seekBar.addEventListener("input", () => {
  if (typeof clearPlaybackAuthorityFrameIdx === "function") clearPlaybackAuthorityFrameIdx();
  else if (typeof clearExplicitSeekFrameIdx === "function") clearExplicitSeekFrameIdx();
  if (typeof clearPlaybackVideoPtsSeekClock === "function") clearPlaybackVideoPtsSeekClock();
  playbackEventLinkExact = false;
  lastRenderedFrameIdx = -1;
  tickPoseFrameIdx = -1;
  lastEventSyncFrameIdx = -1;
  resetPlaybackCollisionTracker();
  const frameEntry =
    typeof playbackFrameEntryForSeekValue === "function"
      ? playbackFrameEntryForSeekValue(seekBar.value)
      : frameByTime.length
        ? frameByTime[
            Math.min(
              Math.round(
                (Number(seekBar.value) / 1000) *
                  Math.max(0, frameByTime.length - 1)
              ),
              frameByTime.length - 1
            )
          ]
        : null;
  const seekValue = Number(seekBar.value) || 0;
  const duration = Number(videoEl.duration);
  if (duration > 0 && Number.isFinite(duration)) {
    timeLabel.textContent = formatTime((seekValue / 1000) * duration);
  }
  const seq = ++playbackSeekInputSeq;
  if (playbackSeekInputTimer) clearTimeout(playbackSeekInputTimer);
  playbackSeekInputTimer = setTimeout(() => {
    playbackSeekInputTimer = 0;
    void (async () => {
      if (seq !== playbackSeekInputSeq) return;
      if (frameEntry) {
        if (!videoEl.paused) videoEl.pause();
        await seekToTimestamp(frameEntry.t, frameEntry.frameIdx, {
          skipEventSync: false,
        });
        return;
      }
      if (!videoEl.duration || !Number.isFinite(videoEl.duration)) {
        const idx = Math.floor((seekValue / 1000) * frameByTime.length);
        const item = frameByTime[Math.min(idx, frameByTime.length - 1)];
        if (item) await renderFrameEntry(item);
        if (seq === playbackSeekInputSeq) {
          syncActiveEventFromPlaybackPosition({ timeSec: item?.t, frameIdx: item?.frameIdx });
        }
        return;
      }
      videoEl.currentTime = (seekValue / 1000) * videoEl.duration;
      await renderAtTime(videoEl.currentTime);
      if (seq === playbackSeekInputSeq) {
        syncActiveEventFromPlaybackPosition({ timeSec: videoEl.currentTime });
      }
    })();
  }, 60);
});

bindStageLayoutWatch();
initPlaybackSpeedControl();
initPlaybackDetBboxToggle();
initPlaybackSkeletonToggle();
initPlaybackAlgoCollisionToggle();
initPlaybackEventReviewToggle();
initPlaybackReviewRiskToggle();
initPlaybackFrameNavigationControls();
initEventReviewControls();
initPlaybackRecordFilter();
void loadInferenceConfigDefaults();
syncPlaybackToggleButton();
void loadReflectionCameras();
updatePlaybackLoadButton();

$("#playback-load-record")?.addEventListener("click", () => {
  startPlaybackFromSelectedRecord().catch((err) => setPlaybackInfo(`❌ ${err.message}`));
});
