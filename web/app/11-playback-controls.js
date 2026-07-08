/** 回放控件绑定与页面初始化 */

$("#playback-json").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  await prepareEventReviewRecordSwitch();
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

function isPlaybackActive() {
  if (jsonOnlyTimer) return true;
  return !!(videoEl.src && !videoEl.paused && !videoEl.ended);
}

async function startPlaybackTransport() {
  if (videoEl.src) {
    videoEl.style.display = "block";
    readPlaybackSpeedFromSelect();
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

$("#play-btn").addEventListener("click", () => {
  void startPlaybackTransport();
});

$("#pause-btn").addEventListener("click", () => {
  stopPlayback();
});

$("#end-playback-btn").addEventListener("click", () => {
  finishPlaybackSession();
});

videoEl.addEventListener("ended", () => {
  stopPlayback();
  if (videoEl.src && videoEl.src.startsWith("blob:")) {
    cleanupPlaybackVideo();
    videoEl.removeAttribute("src");
    videoEl.load();
    setPlaybackInfo("播放结束。可重新选择视频。");
  } else {
    setPlaybackInfo("播放结束。可再次点击播放。");
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
  readPlaybackSpeedFromSelect();
  if (typeof ensurePlaybackRenderLoop === "function") ensurePlaybackRenderLoop();
});

videoEl.addEventListener("pause", () => {
  if (typeof cancelPlaybackRenderLoop === "function") cancelPlaybackRenderLoop();
  // 逐帧/按 frame_idx 精确 seek 时，不在 pause 回调里按 currentTime 重绘
  const explicitFi =
    typeof getExplicitSeekFrameIdx === "function" ? getExplicitSeekFrameIdx() : null;
  const authorityFi =
    typeof getPlaybackAuthorityFrameIdx === "function" ? getPlaybackAuthorityFrameIdx() : null;
  if (explicitFi || authorityFi || playbackEventLinkExact) {
    if (videoEl.duration && Number.isFinite(videoEl.duration)) {
      seekBar.value = String((videoEl.currentTime / videoEl.duration) * 1000);
      timeLabel.textContent = formatTime(videoEl.currentTime);
    }
    return;
  }
  tickVideoFrameIdx = -1;
  lastRenderedFrameIdx = -1;
  // 显式事件跳转中（playbackEventLinkExact）不同步「最近事件」，避免覆盖 activeEventKey
  if (!playbackEventLinkExact && typeof syncActiveEventFromPlaybackPosition === "function") {
    syncActiveEventFromPlaybackPosition({
      timeSec: videoEl.currentTime,
      frameIdx:
        typeof getCurrentPlaybackFrameIdx === "function"
          ? getCurrentPlaybackFrameIdx()
          : typeof frameIdxAtVideoTime === "function"
            ? frameIdxAtVideoTime(videoEl.currentTime)
            : null,
    });
  }
  if (typeof redrawCurrentFrame === "function") redrawCurrentFrame();
  if (videoEl.duration && Number.isFinite(videoEl.duration)) {
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
    void seekToEvent(first);
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
  $("#event-prev-frame-btn")?.addEventListener("click", () => void navigatePlaybackFrame(-1));
  $("#event-next-frame-btn")?.addEventListener("click", () => void navigatePlaybackFrame(1));
  $("#event-skip-next-btn")?.addEventListener("click", () => void skipToNextEvent());
  $("#event-mark-true-next-btn")?.addEventListener("click", () => void confirmTrueAndNext());
  $("#event-unmark-btn")?.addEventListener("click", () => void unmarkTrueAndNext());
  $("#event-mark-all-true-btn")?.addEventListener("click", () => void markAllEventsVerified(true));
  $("#event-unmark-all-btn")?.addEventListener("click", () => void markAllEventsVerified(false));
  $("#event-review-complete-btn")?.addEventListener("click", () => void markEventReviewCompleted());

  canvas?.addEventListener("click", (e) => {
    if (!eventsPanel || eventsPanel.classList.contains("hidden")) return;
    if (!annotationBoxes.length) {
      setEventReviewSaveStatus("请先加载标注 JSON", "error");
      return;
    }
    const ev = getActiveEvent() ?? getActiveFilteredEvent();
    if (!ev) {
      setEventReviewSaveStatus("请先在右侧选择一条碰撞/告警事件", "");
      return;
    }
    const hit = hitTestAnnotationBoxAtClient(e.clientX, e.clientY);
    if (!hit) {
      setEventReviewSaveStatus("未点中货框，请点击画面中的货架货框", "");
      return;
    }
    void toggleConfirmedBoxForEvent(ev, hit);
  });

  $("#event-reset-box-btn")?.addEventListener("click", () => void resetActiveEventBoxAnnotation());

  $("#event-review-list-details")?.addEventListener("toggle", (e) => {
    if (e.target.open) renderEventReviewTable();
    scheduleEventReviewListScrollHeight();
  });

  document.addEventListener("keydown", (e) => {
    if (!panels.playback?.classList.contains("active")) return;
    const tag = (e.target?.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select" || e.target?.isContentEditable) {
      return;
    }
    if (e.key === " " || e.code === "Space") {
      e.preventDefault();
      togglePlaybackTransport();
      return;
    }
    if (!playbackEvents.length && !frameByTime.length) return;
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      void navigatePlaybackFrame(-1);
      return;
    }
    if (e.key === "ArrowRight") {
      e.preventDefault();
      void navigatePlaybackFrame(1);
      return;
    }
    if (!playbackEvents.length) return;
    if (e.key === "y" || e.key === "Y") {
      e.preventDefault();
      void confirmTrueAndNext();
    } else if (e.key === "n" || e.key === "N" || e.key === "j" || e.key === "J") {
      e.preventDefault();
      void skipToNextEvent();
    } else if (e.key === "u" || e.key === "U") {
      e.preventDefault();
      void unmarkTrueAndNext();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      navigateReviewEvent(1);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      navigateReviewEvent(-1);
    }
  });
}

videoEl.addEventListener("seeked", () => {
  if (playbackRenderLoopActive && !videoEl.paused) return;
  const pinnedEventNav = playbackEventLinkExact && activeEventKey;
  const pinnedFi =
    pinnedEventNav && typeof pinnedEventFrameIdx === "function" ? pinnedEventFrameIdx() : null;

  const explicitFi =
    typeof getExplicitSeekFrameIdx === "function" ? getExplicitSeekFrameIdx() : null;
  const authorityFi =
    typeof getPlaybackAuthorityFrameIdx === "function" ? getPlaybackAuthorityFrameIdx() : null;
  const targetFi = pinnedFi || authorityFi || explicitFi;
  if (targetFi && typeof renderExplicitPlaybackFrame === "function") {
    void renderExplicitPlaybackFrame(targetFi).then((ok) => {
      if (!ok) return;
      if (!pinnedEventNav && typeof syncActiveEventFromPlaybackPosition === "function") {
        syncActiveEventFromPlaybackPosition({
          timeSec: videoEl.currentTime,
          frameIdx: targetFi,
          skipRedraw: true,
        });
      } else if (pinnedEventNav && lastRenderedFrameIdx !== targetFi) {
        void realignPlaybackToPinnedEvent();
      }
      if (typeof updateEventReviewFrameNavUi === "function") updateEventReviewFrameNavUi();
    });
    return;
  }

  if (pinnedEventNav) {
    void realignPlaybackToPinnedEvent();
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
  syncCanvasSize();
  redrawCurrentFrame();
});

window.addEventListener("beforeunload", () => {
  cleanupPlaybackVideo();
});

seekBar.addEventListener("input", async () => {
  if (typeof clearPlaybackAuthorityFrameIdx === "function") clearPlaybackAuthorityFrameIdx();
  else if (typeof clearExplicitSeekFrameIdx === "function") clearExplicitSeekFrameIdx();
  playbackEventLinkExact = false;
  lastRenderedFrameIdx = -1;
  tickPoseFrameIdx = -1;
  lastEventSyncFrameIdx = -1;
  resetPlaybackCollisionTracker();
  if (!videoEl.duration || !Number.isFinite(videoEl.duration)) {
    const idx = Math.floor((seekBar.value / 1000) * frameByTime.length);
    const item = frameByTime[Math.min(idx, frameByTime.length - 1)];
    if (item) await renderFrameEntry(item);
    syncActiveEventFromPlaybackPosition({ timeSec: item?.t, frameIdx: item?.frameIdx });
    return;
  }
  videoEl.currentTime = (seekBar.value / 1000) * videoEl.duration;
  await renderAtTime(videoEl.currentTime);
  syncActiveEventFromPlaybackPosition({ timeSec: videoEl.currentTime });
});

bindStageLayoutWatch();
initPlaybackSpeedControl();
initPlaybackDetBboxToggle();
initEventReviewControls();
initPlaybackRecordFilter();
void loadInferenceConfigDefaults();
void loadReflectionCameras();
updatePlaybackLoadButton();

$("#playback-load-record")?.addEventListener("click", () => {
  startPlaybackFromSelectedRecord().catch((err) => setPlaybackInfo(`❌ ${err.message}`));
});
