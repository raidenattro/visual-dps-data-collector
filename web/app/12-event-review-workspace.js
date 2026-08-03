/** 复核工作台：模式、撤销、重试、异常导航、专注模式与快捷键帮助。 */

const EVENT_REVIEW_MODE_FRAME = "frame";
const EVENT_REVIEW_MODE_RANGE = "range";
const EVENT_REVIEW_WINDOW_SIZE = 240;
const EVENT_REVIEW_UNDO_LIMIT = 30;

let eventReviewMode = EVENT_REVIEW_MODE_FRAME;
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

function isEventReviewRangeMode() {
  return eventReviewMode === EVENT_REVIEW_MODE_RANGE;
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

function updateEventReviewModeUi() {
  const panel = $("#playback-events-panel");
  const isRange = isEventReviewRangeMode();
  panel?.classList.toggle("is-range-mode", isRange);
  panel?.classList.toggle("is-frame-mode", !isRange);

  document.querySelectorAll(".event-review-mode-btn").forEach((button) => {
    const active = button.dataset.reviewMode === eventReviewMode;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });

  // 模式横幅已移除：模式按钮自带高亮，帧号由标真按钮和区间面板各自展示，不再重复占位。
  const frameIdx = currentReviewFrameIdx();
  const markButton = $("#event-mark-true-next-btn");
  const unmarkButton = $("#event-unmark-btn");
  if (markButton) {
    markButton.textContent = frameIdx > 0 ? `✓ 标真当前帧 ${frameIdx} · 下一帧` : "✓ 标真当前帧 · 下一帧";
  }
  if (unmarkButton) {
    unmarkButton.textContent = frameIdx > 0 ? `取消帧 ${frameIdx} 标真 · 下一帧` : "取消当前帧标真 · 下一帧";
  }
}

function setEventReviewMode(mode, options = {}) {
  const next = mode === EVENT_REVIEW_MODE_RANGE ? EVENT_REVIEW_MODE_RANGE : EVENT_REVIEW_MODE_FRAME;
  if (eventReviewMode === next) {
    updateEventReviewModeUi();
    return;
  }
  eventReviewMode = next;
  if (next === EVENT_REVIEW_MODE_RANGE) {
    const details = $(".event-review-range-details");
    if (details) details.open = true;
    setEventReviewSaveStatus("已进入区间标注：A 首帧 · D 尾帧 · R 执行", "mode");
  } else if (!options.silent) {
    setEventReviewSaveStatus(
      rangeAnnotStartFrame != null || rangeAnnotEndFrame != null
        ? "已返回单帧标注 · 区间草稿仍保留"
        : "已进入单帧标注",
      "mode"
    );
  }
  updateEventReviewModeUi();
}

function toggleEventReviewMode() {
  setEventReviewMode(
    isEventReviewRangeMode() ? EVENT_REVIEW_MODE_FRAME : EVENT_REVIEW_MODE_RANGE
  );
}

function exitRangeReviewMode({ clear = true } = {}) {
  if (clear && typeof clearRangeAnnotBounds === "function") clearRangeAnnotBounds();
  setEventReviewMode(EVENT_REVIEW_MODE_FRAME, { silent: true });
  setEventReviewSaveStatus(clear ? "已清除区间并返回单帧标注" : "已返回单帧标注", "mode");
}

function hasUnsavedEventReviewDrafts() {
  return (
    pendingConfirmedBoxesByKey.size > 0 ||
    pendingReviewBindingsByKey.size > 0 ||
    pendingPersonIdByKey.size > 0
  );
}

function updateReviewDraftStatus() {
  const status = $("#event-save-status");
  if (!status || status.classList.contains("is-error") || status.classList.contains("is-pending")) {
    return;
  }
  if (hasUnsavedEventReviewDrafts()) {
    status.textContent = "有未保存修改 · 按 Y 写入";
    status.className = "event-save-status hint is-dirty";
  }
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

/** 专注模式全屏的容器：回放三栏布局本身，全屏后可覆盖浏览器标签栏。 */
function reviewFocusContainer() {
  return document.querySelector("#panel-playback .playback-layout");
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
  setTimeout(() => {
    if (typeof syncCanvasSize === "function") syncCanvasSize({ force: true });
    if (typeof redrawCurrentFrame === "function") redrawCurrentFrame();
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
      if (container?.requestFullscreen && !document.fullscreenElement) {
        await container.requestFullscreen({ navigationUI: "hide" });
        relocateShortcutsDialog(container);
      }
    } else if (document.fullscreenElement) {
      await document.exitFullscreen();
    }
  } catch {
    if (eventReviewFocusMode) {
      setEventReviewSaveStatus("浏览器未允许全屏，已启用页面内专注布局", "");
    }
  }
}

function toggleEventReviewFocusMode() {
  void setEventReviewFocusMode(!eventReviewFocusMode);
}

/** 用户按 Esc / F11 退出系统全屏时，把专注状态同步回来。 */
function handleReviewFullscreenChange() {
  const container = reviewFocusContainer();
  const active = !!document.fullscreenElement && document.fullscreenElement === container;
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
  eventReviewMode = EVENT_REVIEW_MODE_FRAME;
  updateEventReviewUndoUi();
  updateEventReviewModeUi();
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
  updateEventReviewModeUi();
  updateReviewDraftStatus();
  updateEventReviewUndoUi();
  updateEventReviewAnomalyUi();
  updateReviewTimelineCursor();
}

function initEventReviewWorkspace() {
  $("#event-review-mode-frame-btn")?.addEventListener("click", () =>
    setEventReviewMode(EVENT_REVIEW_MODE_FRAME)
  );
  $("#event-review-mode-range-btn")?.addEventListener("click", () =>
    setEventReviewMode(EVENT_REVIEW_MODE_RANGE)
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

  document.addEventListener("keydown", (event) => {
    if (!panels.playback?.classList.contains("active") || isReviewTypingTarget(event.target)) {
      return;
    }
    // 模态弹窗（快捷键帮助 / 二次确认）打开时让位，避免 Tab、F 穿透到底层页面。
    if (document.querySelector("dialog[open]")) return;
    if (event.key === "Tab") {
      event.preventDefault();
      toggleEventReviewMode();
      return;
    }
    if (event.key === "Escape" && isEventReviewRangeMode()) {
      event.preventDefault();
      exitRangeReviewMode({ clear: true });
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
