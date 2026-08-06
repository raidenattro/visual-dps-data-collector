/** 复核工作台：侧栏分区、撤销、重试、异常导航、专注模式与快捷键帮助。 */

const EVENT_REVIEW_SIDE_ANNOTATE = "annotate";
const EVENT_REVIEW_SIDE_EVENTS = "events";
const EVENT_REVIEW_WINDOW_SIZE = 240;
const EVENT_REVIEW_UNDO_LIMIT = 30;

let eventReviewSideTab = EVENT_REVIEW_SIDE_ANNOTATE;
let eventReviewFocusMode = false;
let eventReviewRetryAction = null;
let eventReviewWindowStart = 0;
const eventReviewUndoStack = [];

/**
 * 焦点是否落在会吃字符的控件上，用来给输入让位。
 * range 不算：进度条点过之后会一直握着焦点，若把它也当输入，空格与左右键
 * 就全被原生滑块吞了（原生步进是 1/1000，对逐帧复核没有意义）。
 */
function isReviewTypingTarget(target) {
  const tag = String(target?.tagName || "").toLowerCase();
  if (tag === "input") {
    return String(target?.type || "").toLowerCase() !== "range";
  }
  return tag === "textarea" || tag === "select" || !!target?.isContentEditable;
}

/** 兼容旧调用：已取消单帧/区间互斥模式，始终视为非区间模式。 */
function isEventReviewRangeMode() {
  return false;
}

function hasRangeAnnotDraft() {
  return (
    (typeof rangeAnnotStartFrame !== "undefined" && rangeAnnotStartFrame != null) ||
    (typeof rangeAnnotEndFrame !== "undefined" && rangeAnnotEndFrame != null)
  );
}

function currentReviewFrameIdx() {
  return (
    (typeof getResolvedPlaybackFrameIdx === "function" &&
      Number(getResolvedPlaybackFrameIdx())) ||
    (typeof getCurrentPlaybackFrameIdx === "function" &&
      Number(getCurrentPlaybackFrameIdx())) ||
    Number(lastRenderedFrameIdx) ||
    0
  );
}

function updateEventReviewPrimaryActionLabels() {
  const frameIdx = currentReviewFrameIdx();
  const markButton = $("#event-mark-true-next-btn");
  const unmarkButton = $("#event-unmark-btn");
  if (markButton) {
    markButton.textContent =
      frameIdx > 0 ? `✓ 标真当前帧 ${frameIdx} · 下一帧` : "✓ 标真当前帧 · 下一帧";
  }
  if (unmarkButton) {
    unmarkButton.textContent =
      frameIdx > 0 ? `取消帧 ${frameIdx} 标真 · 下一帧` : "取消当前帧标真 · 下一帧";
  }
}

function updateEventReviewSideTabUi() {
  const panel = $("#playback-events-panel");
  const isAnnotate = eventReviewSideTab === EVENT_REVIEW_SIDE_ANNOTATE;
  panel?.classList.toggle("is-side-annotate", isAnnotate);
  panel?.classList.toggle("is-side-events", !isAnnotate);

  document.querySelectorAll(".event-review-side-tab").forEach((button) => {
    const active = button.dataset.sideTab === eventReviewSideTab;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });

  const annotatePane = $("#event-review-pane-annotate");
  const eventsPane = $("#event-review-pane-events");
  if (annotatePane) {
    annotatePane.classList.toggle("is-active", isAnnotate);
    annotatePane.hidden = !isAnnotate;
  }
  if (eventsPane) {
    eventsPane.classList.toggle("is-active", !isAnnotate);
    eventsPane.hidden = isAnnotate;
  }
  updateEventReviewPrimaryActionLabels();
}

/** @deprecated 旧模式 API：统一切到标注侧栏，不再区分单帧/区间模式。 */
function setEventReviewMode(_mode, options = {}) {
  setEventReviewSideTab(EVENT_REVIEW_SIDE_ANNOTATE, options);
}

function setEventReviewSideTab(tab, options = {}) {
  const next =
    tab === EVENT_REVIEW_SIDE_EVENTS
      ? EVENT_REVIEW_SIDE_EVENTS
      : EVENT_REVIEW_SIDE_ANNOTATE;
  if (eventReviewSideTab === next) {
    updateEventReviewSideTabUi();
    return;
  }
  eventReviewSideTab = next;
  updateEventReviewSideTabUi();
  if (options.silent) return;
  setEventReviewSaveStatus(
    next === EVENT_REVIEW_SIDE_EVENTS
      ? "已切换到事件列表 · Tab 可回标注"
      : "已切换到标注 · Y 单帧 · A/D/R 区间",
    "mode"
  );
}

function toggleEventReviewSideTab() {
  // 只读复核时不盲切，避免看不见的侧栏状态变化。
  if (
    typeof eventReviewRecheckMode !== "undefined" &&
    eventReviewRecheckMode &&
    !eventReviewRecheckEditing
  ) {
    return;
  }
  setEventReviewSideTab(
    eventReviewSideTab === EVENT_REVIEW_SIDE_ANNOTATE
      ? EVENT_REVIEW_SIDE_EVENTS
      : EVENT_REVIEW_SIDE_ANNOTATE
  );
}

function clearRangeAnnotDraftOnly() {
  if (!hasRangeAnnotDraft()) return false;
  if (typeof clearRangeAnnotBounds === "function") clearRangeAnnotBounds();
  setEventReviewSaveStatus("已清除区间草稿", "mode");
  return true;
}

/** 兼容旧调用：只清区间草稿，不再切换模式。 */
function exitRangeReviewMode({ clear = true } = {}) {
  if (clear) clearRangeAnnotDraftOnly();
}

function hasUnsavedEventReviewDrafts() {
  return (
    pendingConfirmedBoxesByKey.size > 0 ||
    pendingReviewBindingsByKey.size > 0 ||
    pendingPersonIdByKey.size > 0
  );
}

/**
 * 草稿涉及的帧号，升序去重。只说「有未保存修改」等于让人去 13000 帧里猜，
 * 所以把帧号点出来。按 key 在 playbackEvents 里反查，不去解析 eventRowKey 的字符串格式。
 */
function listUnsavedEventReviewDraftFrames() {
  const keys = new Set([
    ...pendingConfirmedBoxesByKey.keys(),
    ...pendingReviewBindingsByKey.keys(),
    ...pendingPersonIdByKey.keys(),
  ]);
  if (!keys.size) return [];
  const frames = new Set();
  playbackEvents.forEach((ev) => {
    if (!keys.has(eventRowKey(ev))) return;
    const frameIdx = parseInt(ev.frame_idx, 10) || 0;
    if (frameIdx > 0) frames.add(frameIdx);
  });
  return [...frames].sort((a, b) => a - b);
}

function updateReviewDraftStatus() {
  const status = $("#event-save-status");
  if (!status || status.classList.contains("is-error") || status.classList.contains("is-pending")) {
    return;
  }
  if (!hasUnsavedEventReviewDrafts()) return;
  const frames = listUnsavedEventReviewDraftFrames();
  const shown = frames.slice(0, 6).join("、");
  // 草稿键在 playbackEvents 里找不到对应事件时列不出帧号，退回原文案而不是显示空列表。
  status.textContent = frames.length
    ? `有未保存修改：帧 ${shown}${frames.length > 6 ? ` 等 ${frames.length} 帧` : ""} · 按 Y 写入`
    : "有未保存修改 · 按 Y 写入";
  status.className = "event-save-status hint is-dirty";
}

function setEventReviewRetryAction(action, label = "重试保存") {
  eventReviewRetryAction = typeof action === "function" ? { action, label } : null;
  const button = $("#event-save-retry-btn");
  if (button) {
    button.classList.toggle("hidden", !eventReviewRetryAction);
    button.textContent = eventReviewRetryAction?.label || "重试";
  }
}

function clearEventReviewRetryAction() {
  setEventReviewRetryAction(null);
}

async function retryEventReviewSave() {
  if (!eventReviewRetryAction) return false;
  const retry = eventReviewRetryAction;
  setEventReviewSaveStatus("正在重试保存…", "pending");
  const ok = await retry.action();
  if (ok) clearEventReviewRetryAction();
  return !!ok;
}

function cloneReviewMap(map) {
  return Array.from(map.entries(), ([key, value]) => [
    key,
    JSON.parse(JSON.stringify(value)),
  ]);
}

function recordEventReviewUndo(label = "上一步操作") {
  if (!playbackEvents.length) return;
  const snapshot = {
    label,
    recordId: currentRecordId,
    activeEventKey,
    verifiedTrue: JSON.parse(JSON.stringify(buildVerifiedTruePayload())),
    pendingBoxes: cloneReviewMap(pendingConfirmedBoxesByKey),
    pendingBindings: cloneReviewMap(pendingReviewBindingsByKey),
    pendingPersons: cloneReviewMap(pendingPersonIdByKey),
  };
  eventReviewUndoStack.push(snapshot);
  if (eventReviewUndoStack.length > EVENT_REVIEW_UNDO_LIMIT) eventReviewUndoStack.shift();
  updateEventReviewUndoUi();
}

function discardLastEventReviewUndo() {
  eventReviewUndoStack.pop();
  updateEventReviewUndoUi();
}

function restoreReviewMap(map, entries) {
  map.clear();
  (entries || []).forEach(([key, value]) => map.set(key, value));
}

function updateEventReviewUndoUi() {
  const button = $("#event-review-undo-btn");
  const snapshot = eventReviewUndoStack[eventReviewUndoStack.length - 1];
  if (!button) return;
  button.disabled = !snapshot;
  button.textContent = snapshot ? `撤销：${snapshot.label}` : "撤销";
}

async function undoLastEventReviewAction() {
  const snapshot = eventReviewUndoStack.pop();
  updateEventReviewUndoUi();
  if (!snapshot) {
    setEventReviewSaveStatus("暂无可撤销操作", "");
    return false;
  }
  if (snapshot.recordId !== currentRecordId) {
    setEventReviewSaveStatus("上一操作属于其他记录，无法撤销", "error");
    return false;
  }
  const ok = currentRecordId
    ? await persistEventReviewVerifiedList(snapshot.verifiedTrue, `正在撤销：${snapshot.label}…`)
    : true;
  if (!ok) {
    eventReviewUndoStack.push(snapshot);
    updateEventReviewUndoUi();
    return false;
  }
  restoreReviewMap(pendingConfirmedBoxesByKey, snapshot.pendingBoxes);
  restoreReviewMap(pendingReviewBindingsByKey, snapshot.pendingBindings);
  restoreReviewMap(pendingPersonIdByKey, snapshot.pendingPersons);
  if (snapshot.activeEventKey) activeEventKey = snapshot.activeEventKey;
  updateReviewDock();
  renderEventMarkers();
  if ($("#event-review-list-details")?.open) renderEventReviewTable();
  setEventReviewSaveStatus(`已撤销：${snapshot.label}`, "saved");
  return true;
}

/**
 * 只有区间标真跟踪到低置信度身份切换点、必须由人来裁决时才算「身份待确认」。
 * 「多人帧尚未选人」不算异常：那与「未标真」完全重合，会把整条队列染成待确认。
 */
function eventNeedsIdentityAttention(ev) {
  if (!ev) return false;
  return (
    typeof isRangePersonConfirmationRequired === "function" &&
    isRangePersonConfirmationRequired(ev.frame_idx)
  );
}

/** 区间跟踪逐个停下来问，同一时刻最多只有一个待确认点。 */
function getEventReviewPendingIdentity() {
  return playbackEvents.find(eventNeedsIdentityAttention) || null;
}

function updateEventReviewAnomalyUi() {
  const bar = $("#event-review-anomaly-toolbar");
  if (!bar) return;
  const target = getEventReviewPendingIdentity();
  // 没有待确认点时整条不占位，避免常驻一行无信息量的状态。
  bar.classList.toggle("hidden", !target);
  const title = $("#event-review-identity-title");
  if (title) {
    title.textContent = target ? `帧 ${target.frame_idx} 身份待确认` : "身份待确认";
  }
  const jump = $("#event-review-anomaly-jump-btn");
  if (jump) jump.disabled = !target;
}

async function focusEventReviewPendingIdentity() {
  const target = getEventReviewPendingIdentity();
  if (!target) {
    setEventReviewSaveStatus("当前没有待确认的身份点", "");
    return;
  }
  if (typeof seekToEvent === "function") await seekToEvent(target);
  else if (typeof selectReviewEventWithoutPlaybackNavigation === "function") {
    selectReviewEventWithoutPlaybackNavigation(target);
  }
  setEventReviewSaveStatus(`身份待确认 · 帧 ${target.frame_idx} · 请选择人物`, "mode");
}

/** 专注模式全屏的容器：回放三栏布局本身，全屏后可覆盖浏览器标签栏与系统任务栏。 */
function reviewFocusContainer() {
  return document.querySelector("#panel-playback .playback-layout");
}

/** 当前系统全屏元素（含 webkit 前缀，兼容 Chromium / Safari 旧行为）。 */
function getReviewFullscreenElement() {
  return (
    document.fullscreenElement ||
    document.webkitFullscreenElement ||
    document.msFullscreenElement ||
    null
  );
}

/**
 * 进入真全屏以盖住系统任务栏。
 * 部分浏览器不接受 navigationUI 参数会直接 reject，需降级为无参调用。
 */
async function requestReviewFullscreen(el) {
  if (!el) throw new Error("missing fullscreen target");
  const req =
    el.requestFullscreen ||
    el.webkitRequestFullscreen ||
    el.webkitRequestFullScreen ||
    el.msRequestFullscreen;
  if (typeof req !== "function") throw new Error("fullscreen unsupported");
  try {
    await req.call(el, { navigationUI: "hide" });
  } catch {
    await req.call(el);
  }
  if (!getReviewFullscreenElement()) {
    throw new Error("fullscreen not active");
  }
}

async function exitReviewFullscreen() {
  const exit =
    document.exitFullscreen ||
    document.webkitExitFullscreen ||
    document.webkitCancelFullScreen ||
    document.msExitFullscreen;
  if (typeof exit === "function" && getReviewFullscreenElement()) {
    await exit.call(document);
  }
}

/** 全屏元素之外的 <dialog> 不会渲染，复核用的弹窗需跟随全屏容器搬家。 */
const REVIEW_FULLSCREEN_DIALOG_IDS = ["#event-review-shortcuts-dialog", "#review-confirm-dialog"];

function relocateShortcutsDialog(container) {
  const target = container || document.body;
  REVIEW_FULLSCREEN_DIALOG_IDS.forEach((selector) => {
    const dialog = $(selector);
    if (!dialog || dialog.parentElement === target) return;
    const wasOpen = dialog.open;
    dialog.close();
    target.appendChild(dialog);
    if (wasOpen) dialog.showModal();
  });
}

/**
 * 页面内二次确认，替代 window.confirm：不打断全屏、不抢系统焦点，明细一条不少。
 * @param {{title?: string, lead?: string, rows?: Array<[string, string]>, notes?: string[], confirmText?: string}} spec
 * @returns {Promise<boolean>}
 */
function openReviewConfirm(spec = {}) {
  const dialog = $("#review-confirm-dialog");
  if (!dialog || typeof dialog.showModal !== "function") {
    return Promise.resolve(window.confirm(spec.title || "确定执行？"));
  }
  const titleEl = $("#review-confirm-title");
  const bodyEl = $("#review-confirm-body");
  if (titleEl) titleEl.textContent = spec.title || "确认操作";
  if (bodyEl) {
    const esc = typeof escapeReviewHtml === "function" ? escapeReviewHtml : (s) => String(s ?? "");
    const lead = spec.lead ? `<p class="review-confirm-lead">${esc(spec.lead)}</p>` : "";
    const rows = (spec.rows || []).length
      ? `<dl class="review-confirm-rows">${spec.rows
          .map(([term, value]) => `<dt>${esc(term)}</dt><dd>${esc(value)}</dd>`)
          .join("")}</dl>`
      : "";
    const notes = (spec.notes || []).filter(Boolean).length
      ? `<ul class="review-confirm-notes">${spec.notes
          .filter(Boolean)
          .map((note) => `<li>${esc(note)}</li>`)
          .join("")}</ul>`
      : "";
    bodyEl.innerHTML = `${lead}${rows}${notes}`;
  }
  const okBtn = dialog.querySelector(".review-confirm-ok");
  if (okBtn && spec.confirmText) okBtn.innerHTML = `${spec.confirmText} <kbd>Enter</kbd>`;
  return new Promise((resolve) => {
    dialog.addEventListener(
      "close",
      () => resolve(dialog.returnValue === "ok"),
      { once: true }
    );
    dialog.showModal();
  });
}

function syncEventReviewFocusUi() {
  document.body.classList.toggle("review-focus-mode", eventReviewFocusMode);
  reviewFocusContainer()?.classList.toggle("is-focus-mode", eventReviewFocusMode);
  const button = $("#event-review-focus-btn");
  if (button) {
    button.setAttribute("aria-pressed", eventReviewFocusMode ? "true" : "false");
    button.classList.toggle("is-active", eventReviewFocusMode);
    const label = $("#event-review-focus-label");
    if (label) label.textContent = eventReviewFocusMode ? "退出专注" : "专注模式";
    else button.textContent = eventReviewFocusMode ? "退出专注" : "专注模式";
  }
  // 等全屏/布局样式生效后再量尺寸；播放中必须重建冻结布局，否则仍按旧舞台对齐。
  setTimeout(() => {
    if (typeof refreshPlaybackStageLayout === "function") {
      refreshPlaybackStageLayout();
    } else {
      if (typeof syncCanvasSize === "function") syncCanvasSize({ force: true });
      if (typeof redrawCurrentFrame === "function") redrawCurrentFrame();
    }
    if (typeof renderEventMarkers === "function") renderEventMarkers();
    updateReviewTimelineCursor();
  }, 0);
}

async function setEventReviewFocusMode(enabled, options = {}) {
  eventReviewFocusMode = !!enabled;
  syncEventReviewFocusUi();
  if (options.skipFullscreen) return;

  const container = reviewFocusContainer();
  try {
    if (eventReviewFocusMode) {
      if (container && getReviewFullscreenElement() !== container) {
        await requestReviewFullscreen(container);
        relocateShortcutsDialog(container);
      }
    } else if (getReviewFullscreenElement()) {
      await exitReviewFullscreen();
    }
  } catch {
    if (eventReviewFocusMode) {
      setEventReviewSaveStatus(
        "浏览器未允许全屏（任务栏仍会显示），已启用页面内专注布局",
        ""
      );
    }
  }
  // 全屏切换完成后几何才稳定；再刷一次，覆盖 setTimeout(0) 抢跑的情况。
  if (typeof refreshPlaybackStageLayout === "function") {
    requestAnimationFrame(() => refreshPlaybackStageLayout());
  }
}

function toggleEventReviewFocusMode() {
  void setEventReviewFocusMode(!eventReviewFocusMode);
}

/** 用户按 Esc / F11 退出系统全屏时，把专注状态同步回来。 */
function handleReviewFullscreenChange() {
  const container = reviewFocusContainer();
  const active = getReviewFullscreenElement() === container;
  if (!active) relocateShortcutsDialog(null);
  if (eventReviewFocusMode === active) return;
  eventReviewFocusMode = active;
  syncEventReviewFocusUi();
}

function openEventReviewShortcuts() {
  const dialog = $("#event-review-shortcuts-dialog");
  if (dialog && !dialog.open) dialog.showModal();
}

/** range 滑块 thumb 宽度，用于让播放头竖线与 thumb 中心严格对齐。 */
const REVIEW_TIMELINE_THUMB_PX = 12;

function updateReviewTimelineCursor(progress = null) {
  const value = progress == null ? Number(seekBar?.value) / 1000 : Number(progress);
  const clamped = Math.max(0, Math.min(1, Number.isFinite(value) ? value : 0));
  const percent = (clamped * 100).toFixed(4);
  const offset = ((0.5 - clamped) * REVIEW_TIMELINE_THUMB_PX).toFixed(2);
  const indicator = $("#seek-current-frame-indicator");
  if (indicator) indicator.style.left = `calc(${percent}% + ${offset}px)`;
  const track = indicator?.parentElement;
  if (track) track.style.setProperty("--review-progress", `${percent}%`);
}

function resetEventReviewWindow() {
  eventReviewWindowStart = 0;
}

function resetEventReviewWorkspaceState() {
  eventReviewUndoStack.length = 0;
  clearEventReviewRetryAction();
  resetEventReviewWindow();
  eventReviewSideTab = EVENT_REVIEW_SIDE_ANNOTATE;
  updateEventReviewUndoUi();
  updateEventReviewSideTabUi();
  // 复核模式本身是用户偏好，跨记录保留；只把编辑态与冲突结果清空。
  if (typeof resetEventReviewRecheckState === "function") resetEventReviewRecheckState();
}

function eventReviewWindowForRows(rows) {
  const list = Array.isArray(rows) ? rows : [];
  const activeIndex = list.findIndex((ev) => eventRowKey(ev) === activeEventKey);
  if (
    activeIndex >= 0 &&
    (activeIndex < eventReviewWindowStart ||
      activeIndex >= eventReviewWindowStart + EVENT_REVIEW_WINDOW_SIZE)
  ) {
    eventReviewWindowStart = Math.max(
      0,
      Math.min(
        Math.max(0, list.length - EVENT_REVIEW_WINDOW_SIZE),
        activeIndex - Math.floor(EVENT_REVIEW_WINDOW_SIZE / 2)
      )
    );
  }
  eventReviewWindowStart = Math.max(
    0,
    Math.min(eventReviewWindowStart, Math.max(0, list.length - EVENT_REVIEW_WINDOW_SIZE))
  );
  const end = Math.min(list.length, eventReviewWindowStart + EVENT_REVIEW_WINDOW_SIZE);
  const status = $("#event-review-window-status");
  if (status) {
    status.textContent = list.length
      ? `显示 ${eventReviewWindowStart + 1}–${end} / ${list.length}`
      : "无事件";
  }
  const prev = $("#event-review-window-prev-btn");
  const next = $("#event-review-window-next-btn");
  if (prev) prev.disabled = eventReviewWindowStart <= 0;
  if (next) next.disabled = end >= list.length;
  return list.slice(eventReviewWindowStart, end);
}

function shiftEventReviewWindow(direction) {
  eventReviewWindowStart += Number(direction) * EVENT_REVIEW_WINDOW_SIZE;
  renderEventReviewTable();
}

function updateEventReviewWorkspaceUi() {
  updateEventReviewSideTabUi();
  updateReviewDraftStatus();
  updateEventReviewUndoUi();
  updateEventReviewAnomalyUi();
  updateReviewTimelineCursor();
}

function initEventReviewWorkspace() {
  $("#event-review-side-annotate-btn")?.addEventListener("click", () =>
    setEventReviewSideTab(EVENT_REVIEW_SIDE_ANNOTATE)
  );
  $("#event-review-side-events-btn")?.addEventListener("click", () =>
    setEventReviewSideTab(EVENT_REVIEW_SIDE_EVENTS)
  );
  $("#event-review-focus-btn")?.addEventListener("click", toggleEventReviewFocusMode);
  $("#event-review-shortcuts-btn")?.addEventListener("click", openEventReviewShortcuts);
  $("#event-review-undo-btn")?.addEventListener("click", () => void undoLastEventReviewAction());
  $("#event-save-retry-btn")?.addEventListener("click", () => void retryEventReviewSave());
  $("#event-review-anomaly-jump-btn")?.addEventListener("click", () =>
    void focusEventReviewPendingIdentity()
  );
  $("#event-review-window-prev-btn")?.addEventListener("click", () =>
    shiftEventReviewWindow(-1)
  );
  $("#event-review-window-next-btn")?.addEventListener("click", () =>
    shiftEventReviewWindow(1)
  );
  updateEventReviewSideTabUi();

  document.addEventListener("keydown", (event) => {
    if (!panels.playback?.classList.contains("active") || isReviewTypingTarget(event.target)) {
      return;
    }
    // 模态弹窗（快捷键帮助 / 二次确认）打开时让位，避免 Tab、F 穿透到底层页面。
    if (document.querySelector("dialog[open]")) return;
    if (event.key === "Tab") {
      event.preventDefault();
      toggleEventReviewSideTab();
      return;
    }
    if (event.key === "Escape" && hasRangeAnnotDraft()) {
      event.preventDefault();
      clearRangeAnnotDraftOnly();
      return;
    }
    if (event.key === "F1") {
      event.preventDefault();
      openEventReviewShortcuts();
      return;
    }
    if (
      (event.key === "f" || event.key === "F") &&
      !event.altKey &&
      !event.ctrlKey &&
      !event.metaKey
    ) {
      event.preventDefault();
      if (!event.repeat) toggleEventReviewFocusMode();
      return;
    }
    if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === "z") {
      event.preventDefault();
      void undoLastEventReviewAction();
    }
  });

  document.addEventListener("fullscreenchange", handleReviewFullscreenChange);
  document.addEventListener("webkitfullscreenchange", handleReviewFullscreenChange);

  // 时间轴宽度变化会改变像素桶数量，需要重建标记以免错位。
  let timelineResizeTimer = null;
  window.addEventListener("resize", () => {
    if (timelineResizeTimer) clearTimeout(timelineResizeTimer);
    timelineResizeTimer = setTimeout(() => {
      timelineResizeTimer = null;
      if (typeof renderEventMarkers === "function") renderEventMarkers();
      updateReviewTimelineCursor();
    }, 120);
  });

  window.addEventListener("beforeunload", (event) => {
    if (!hasUnsavedEventReviewDrafts()) return;
    event.preventDefault();
    event.returnValue = "";
  });

  void setEventReviewFocusMode(false, { skipFullscreen: true });
  updateEventReviewWorkspaceUi();
}

initEventReviewWorkspace();
