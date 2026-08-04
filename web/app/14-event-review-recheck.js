/**
 * 复核模式（recheck mode）：只读优先的复核镜头、画面级冲突提示与收尾自检清单。
 *
 * 它是叠在单帧/区间之上的一层「镜头」，不是第四种标注模式：
 * 只改变「看到什么、什么时候能改」，写盘逻辑与 event_review 数据格式一字不动。
 */

let eventReviewRecheckMode = false;
let eventReviewRecheckEditing = false;
/** 打开编辑态时所在的画面帧；翻到别的帧就自动收回只读态。null 表示等下一次绘制来认领。 */
let eventReviewRecheckEditFrame = null;
/** 当前帧冲突结果，画布描边与侧栏冲突条共用一份，避免重复计算。 */
let eventReviewFrameConflicts = null;
/** 复核默认慢放倍速：漏标只能靠完整看过去发现，原速看不清。 */
const EVENT_REVIEW_RECHECK_SPEED = 0.25;
/** 进入复核模式前的倍速，退出时还原；null 表示当前不在复核模式。 */
let eventReviewRecheckPrevSpeed = null;

function isEventReviewRecheckMode() {
  return eventReviewRecheckMode;
}

/** 只读复核中：标注控件收起，点画面或按 E 才展开。 */
function isEventReviewRecheckReadonly() {
  return eventReviewRecheckMode && !eventReviewRecheckEditing;
}

function emptyReviewFrameConflicts() {
  return {
    missingTokens: new Set(),
    mismatchTokens: new Set(),
    missing: 0,
    mismatch: 0,
    orphanPersons: [],
    total: 0,
  };
}

/**
 * 当前帧的三类复核冲突，全部由已有数据推导，不新增任何存盘字段：
 * - missing  这一帧已经标真过，但算法告警的货框没被人工确认 → 漏标嫌疑
 * - mismatch 人工确认的货框在这一帧既不告警也不碰撞 → 标错嫌疑
 * - orphanPersons 已标真事件的 person_id 在这一帧骨架里找不到 → 身份悬空
 *
 * 未标真的帧不算 missing：那与「未标真」队列完全重合，会把整段染成冲突。
 * 这一帧骨架还没拉到本地时（frameCache 未命中）不判身份悬空，避免误报。
 */
function computeReviewFrameConflicts(frameIdx, collisionSet, alarmSet, reviewCtx) {
  const result = emptyReviewFrameConflicts();
  const fi = parseInt(frameIdx, 10) || 0;
  if (!fi || typeof getVerifiedEventsOnFrame !== "function") return result;

  const verifiedOnFrame = getVerifiedEventsOnFrame(fi);
  const cache = typeof getAnnotationDisplayCache === "function" ? getAnnotationDisplayCache() : [];
  cache.forEach(({ token }) => {
    const confirmed = !!tokenValueInTokenMap(token, reviewCtx?.confirmedByToken);
    const isAlarm = tokenInCollisionSet(token, alarmSet);
    const isHit = tokenInCollisionSet(token, collisionSet);
    if (confirmed) {
      if (!isAlarm && !isHit) {
        addTokenKeysToSet(token, result.mismatchTokens);
        result.mismatch += 1;
      }
      return;
    }
    if (isAlarm && verifiedOnFrame.length) {
      addTokenKeysToSet(token, result.missingTokens);
      result.missing += 1;
    }
  });

  const framePersons = typeof getFramePersonIds === "function" ? getFramePersonIds(fi) : [];
  if (framePersons.length && verifiedOnFrame.length) {
    const known = new Set(framePersons.map(Number));
    const orphan = new Set();
    verifiedOnFrame.forEach((ev) => {
      const bindings =
        typeof getEventEffectiveBindings === "function" ? getEventEffectiveBindings(ev) : [];
      const ids = bindings.length
        ? bindings.map((binding) => binding.person_id)
        : [typeof getEventPersonId === "function" ? getEventPersonId(ev) : null];
      ids.forEach((raw) => {
        if (raw == null || raw === "") return;
        const pid = Number(raw);
        if (Number.isFinite(pid) && !known.has(pid)) orphan.add(pid);
      });
    });
    result.orphanPersons = [...orphan].sort((a, b) => a - b);
  }

  result.total = result.missing + result.mismatch + result.orphanPersons.length;
  return result;
}

const REVIEW_CONFLICT_OUTLINE_STYLES = {
  missing: { stroke: "rgba(255, 159, 28, 0.98)", dash: [11, 6], width: 4.5 },
  mismatch: { stroke: "rgba(236, 72, 153, 0.98)", dash: [4, 4], width: 4.5 },
};

function strokeReviewConflictOutline(displayPts, style) {
  ctx.beginPath();
  displayPts.forEach(([dx, dy], i) => {
    if (i === 0) ctx.moveTo(dx, dy);
    else ctx.lineTo(dx, dy);
  });
  ctx.closePath();
  ctx.setLineDash(style.dash);
  ctx.lineWidth = style.width;
  ctx.strokeStyle = style.stroke;
  ctx.stroke();
}

/**
 * 画布冲突描边。**只算与画，不碰 DOM**，所以 lite/full 两条绘制路径都能调用。
 * 侧栏冲突条由 syncReviewConflictUiForFrame() 单独同步：两件事混在一起时，
 * 描边就没法进播放路径 —— 那会变成每帧写一遍 DOM。
 */
function drawReviewConflictOutlines(frameIdx, collisionSet, alarmSet, reviewCtx) {
  if (!eventReviewRecheckMode) {
    eventReviewFrameConflicts = null;
    return;
  }
  const conflicts = computeReviewFrameConflicts(frameIdx, collisionSet, alarmSet, reviewCtx);
  eventReviewFrameConflicts = conflicts;
  if (!(conflicts.missing || conflicts.mismatch)) return;

  const cache = typeof getAnnotationDisplayCache === "function" ? getAnnotationDisplayCache() : [];
  cache.forEach(({ token, displayPts }) => {
    if (tokenInTokenSet(token, conflicts.missingTokens)) {
      strokeReviewConflictOutline(displayPts, REVIEW_CONFLICT_OUTLINE_STYLES.missing);
    } else if (tokenInTokenSet(token, conflicts.mismatchTokens)) {
      strokeReviewConflictOutline(displayPts, REVIEW_CONFLICT_OUTLINE_STYLES.mismatch);
    }
  });
  ctx.setLineDash([]);
}

function setReviewConflictChip(selector, count, text) {
  const chip = $(selector);
  if (!chip) return;
  chip.classList.toggle("hidden", !count);
  if (count) chip.textContent = text;
}

/** 冲突条已渲染内容的签名。不含帧号：帧号不显示在条上，含进去会每帧都判为脏。 */
let lastReviewConflictBarSignature = null;

function reviewConflictBarSignature() {
  if (!eventReviewRecheckMode) return "off";
  const conflicts = eventReviewFrameConflicts;
  return [
    conflicts?.missing || 0,
    conflicts?.mismatch || 0,
    (conflicts?.orphanPersons || []).join(","),
  ].join("|");
}

/**
 * 绘制路径每帧调用。播放时这里会被调到 30 次/秒，所以只做两件便宜的事：
 * 翻帧收回编辑态（纯状态判断），以及内容真的变了才写 DOM。
 */
function syncReviewConflictUiForFrame(frameIdx = null) {
  const fi = parseInt(frameIdx, 10) || 0;
  if (eventReviewRecheckEditing && fi > 0) {
    if (eventReviewRecheckEditFrame == null) eventReviewRecheckEditFrame = fi;
    else if (fi !== eventReviewRecheckEditFrame) {
      // 收回编辑态自己会走 syncEventReviewRecheckUi → updateReviewConflictBar。
      setEventReviewRecheckEditing(false, { auto: true });
      return;
    }
  }
  if (reviewConflictBarSignature() === lastReviewConflictBarSignature) return;
  updateReviewConflictBar();
}

/** 侧栏冲突条的 DOM 同步。模式切换等场景直接调用，无条件重写。 */
function updateReviewConflictBar() {
  lastReviewConflictBarSignature = reviewConflictBarSignature();
  const bar = $("#event-review-conflict-bar");
  if (!bar) return;
  const conflicts = eventReviewFrameConflicts;
  bar.classList.toggle("hidden", !eventReviewRecheckMode);
  if (!eventReviewRecheckMode) return;

  const missing = conflicts?.missing || 0;
  const mismatch = conflicts?.mismatch || 0;
  const orphans = conflicts?.orphanPersons || [];
  bar.classList.toggle("is-clean", !(missing || mismatch || orphans.length));
  setReviewConflictChip("#event-review-conflict-missing", missing, `漏标嫌疑 ${missing}`);
  setReviewConflictChip("#event-review-conflict-mismatch", mismatch, `标错嫌疑 ${mismatch}`);
  setReviewConflictChip(
    "#event-review-conflict-orphan",
    orphans.length,
    `身份悬空 ${orphans.map((pid) => `#${pid}`).join(" ")}`
  );
  const clean = $("#event-review-conflict-clean");
  if (clean) clean.classList.toggle("hidden", !!(missing || mismatch || orphans.length));
}

function syncEventReviewRecheckUi() {
  const panel = $("#playback-events-panel");
  panel?.classList.toggle("is-recheck-mode", eventReviewRecheckMode);
  panel?.classList.toggle("is-recheck-editing", eventReviewRecheckMode && eventReviewRecheckEditing);

  const button = $("#event-review-recheck-btn");
  if (button) {
    button.setAttribute("aria-pressed", eventReviewRecheckMode ? "true" : "false");
    button.classList.toggle("is-active", eventReviewRecheckMode);
    const label = $("#event-review-recheck-label");
    if (label) label.textContent = eventReviewRecheckMode ? "退出复核" : "复核模式";
  }

  const bar = $("#event-review-recheck-bar");
  bar?.classList.toggle("hidden", !eventReviewRecheckMode);
  const state = $("#event-review-recheck-state");
  if (state) {
    state.textContent = eventReviewRecheckEditing
      ? "标注已展开 · 改完按 Y 写入，翻帧自动收起"
      : "只读复核中 · 点画面或按 E 调出标注";
  }
  const editBtn = $("#event-review-recheck-edit-btn");
  if (editBtn) {
    editBtn.innerHTML = eventReviewRecheckEditing
      ? '收起标注 <kbd>E</kbd>'
      : '调出标注 <kbd>E</kbd>';
  }

  updateReviewConflictBar();
  // 冲突描边只在复核模式下绘制，进出模式都要重画当前帧。
  if (typeof redrawCurrentFrame === "function") redrawCurrentFrame();
}

function setEventReviewRecheckEditing(enabled, options = {}) {
  const next = !!enabled && eventReviewRecheckMode;
  if (eventReviewRecheckEditing === next) return;
  eventReviewRecheckEditing = next;
  eventReviewRecheckEditFrame = null;
  syncEventReviewRecheckUi();
  if (options.silent) return;
  if (typeof setEventReviewSaveStatus === "function") {
    setEventReviewSaveStatus(
      next
        ? "已调出标注控件 · 改完按 Y 写入"
        : options.auto
          ? "已翻帧 · 标注控件自动收起"
          : "已收起标注控件 · 回到只读复核",
      "mode"
    );
  }
}

function toggleEventReviewRecheckEditing() {
  if (!eventReviewRecheckMode) return;
  setEventReviewRecheckEditing(!eventReviewRecheckEditing);
}

function setEventReviewRecheckMode(enabled, options = {}) {
  const next = !!enabled;
  if (eventReviewRecheckMode === next) {
    syncEventReviewRecheckUi();
    return;
  }
  eventReviewRecheckMode = next;
  eventReviewRecheckEditing = false;
  eventReviewRecheckEditFrame = null;
  // 复核模式默认只读，区间草稿留在原处不动，仅把模式切回单帧免得 A/D/R 误触。
  if (next && typeof isEventReviewRangeMode === "function" && isEventReviewRangeMode()) {
    setEventReviewMode(EVENT_REVIEW_MODE_FRAME, { silent: true });
  }
  // 进来压到 0.25×，退出还原进来之前的倍速；期间用户仍可自己改下拉框。
  if (typeof setPlaybackSpeed === "function") {
    if (next) {
      eventReviewRecheckPrevSpeed = setPlaybackSpeed(EVENT_REVIEW_RECHECK_SPEED);
    } else if (eventReviewRecheckPrevSpeed != null) {
      setPlaybackSpeed(eventReviewRecheckPrevSpeed);
      eventReviewRecheckPrevSpeed = null;
    }
  }
  syncEventReviewRecheckUi();
  if (options.silent) return;
  if (typeof setEventReviewSaveStatus === "function") {
    setEventReviewSaveStatus(
      next ? "已进入复核模式 · 0.25× 只读看画面，E 调出标注" : "已退出复核模式",
      "mode"
    );
  }
}

function toggleEventReviewRecheckMode() {
  setEventReviewRecheckMode(!eventReviewRecheckMode);
}

/**
 * 只读复核时第一次点画面：先把标注控件召出来，这一次点击不落到货框上。
 * @returns {boolean} true 表示这次点击已被吞掉
 */
function interceptRecheckCanvasClick() {
  if (!isEventReviewRecheckReadonly()) return false;
  setEventReviewRecheckEditing(true);
  return true;
}

/**
 * 收尾自检清单。只统计事件级信息（playbackEvents 是全量的），
 * 不做整片扫描：schema 2 的骨架帧按需拉取，跨帧统计会因缓存缺失而失真。
 */
function buildEventReviewChecklist() {
  const total = playbackEvents.length;
  const verified = typeof countVerifiedEvents === "function" ? countVerifiedEvents() : 0;
  const unverified = Math.max(0, total - verified);
  let missingBox = 0;
  let missingPerson = 0;
  playbackEvents.forEach((ev) => {
    if (typeof isEventVerified !== "function" || !isEventVerified(ev)) return;
    const bindings =
      typeof getEventEffectiveBindings === "function" ? getEventEffectiveBindings(ev) : [];
    const boxes = typeof getEventConfirmedBoxes === "function" ? getEventConfirmedBoxes(ev) : [];
    if (!bindings.length && !boxes.length) missingBox += 1;
    const hasPerson =
      bindings.some((binding) => binding.person_id != null) ||
      (typeof getEventPersonId === "function" && getEventPersonId(ev) != null);
    if (!hasPerson) missingPerson += 1;
  });
  const pendingIdentity =
    typeof getEventReviewPendingIdentity === "function" ? getEventReviewPendingIdentity() : null;
  const drafts =
    typeof hasUnsavedEventReviewDrafts === "function" && hasUnsavedEventReviewDrafts();
  const draftFrames =
    drafts && typeof listUnsavedEventReviewDraftFrames === "function"
      ? listUnsavedEventReviewDraftFrames()
      : [];

  const rows = [
    ["事件总数", `${total} 条`],
    ["已标真", `${verified} 条`],
    ["未标真", unverified ? `${unverified} 条` : "0 条"],
    ["已标真但没有货框", missingBox ? `${missingBox} 条` : "0 条"],
    ["已标真但没有 person_id", missingPerson ? `${missingPerson} 条` : "0 条"],
    ["身份待确认", pendingIdentity ? `帧 ${pendingIdentity.frame_idx}` : "无"],
    [
      "未保存草稿",
      draftFrames.length
        ? `帧 ${draftFrames.slice(0, 6).join("、")}${
            draftFrames.length > 6 ? ` 等 ${draftFrames.length} 帧` : ""
          }`
        : drafts
          ? "有"
          : "无",
    ],
  ];
  const notes = [
    unverified ? `还有 ${unverified} 条未标真，确认这些帧确实不该标真` : "",
    missingBox ? `${missingBox} 条已标真却没有确认货框，训练时取不到正样本框` : "",
    missingPerson ? `${missingPerson} 条已标真却没有 person_id，无法回溯到具体人` : "",
    pendingIdentity ? `帧 ${pendingIdentity.frame_idx} 的身份还没裁决` : "",
    drafts ? "有未保存草稿，建议先按 Y 写入再收尾" : "",
    "清单只查事件级信息；算法完全没检出的漏标只能靠完整播放发现",
  ].filter(Boolean);
  const clean = !unverified && !missingBox && !missingPerson && !pendingIdentity && !drafts;
  return { rows, notes, clean };
}

async function confirmMarkEventReviewCompleted() {
  if (typeof markEventReviewCompleted !== "function") return;
  if (!currentRecordId) {
    setEventReviewSaveStatus("请从记录列表打开回放后再完成复核", "error");
    return;
  }
  const checklist = buildEventReviewChecklist();
  const ok =
    typeof openReviewConfirm === "function"
      ? await openReviewConfirm({
          title: "标记复核完成",
          lead: checklist.clean
            ? "自检没有发现问题，可以收尾。"
            : "自检发现以下待确认项，确认后仍会标记完成。",
          rows: checklist.rows,
          notes: checklist.notes,
          confirmText: "标记复核完成",
        })
      : true;
  if (!ok) return;
  await markEventReviewCompleted();
}

function resetEventReviewRecheckState() {
  eventReviewRecheckEditing = false;
  eventReviewRecheckEditFrame = null;
  eventReviewFrameConflicts = null;
  syncEventReviewRecheckUi();
}

function initEventReviewRecheck() {
  $("#event-review-recheck-btn")?.addEventListener("click", toggleEventReviewRecheckMode);
  $("#event-review-recheck-edit-btn")?.addEventListener("click", toggleEventReviewRecheckEditing);

  document.addEventListener("keydown", (event) => {
    if (!panels.playback?.classList.contains("active") || isReviewTypingTarget(event.target)) {
      return;
    }
    if (document.querySelector("dialog[open]")) return;
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    const key = event.key.toLowerCase();
    if (key === "v") {
      event.preventDefault();
      if (!event.repeat) toggleEventReviewRecheckMode();
      return;
    }
    if (key === "e" && eventReviewRecheckMode) {
      event.preventDefault();
      if (!event.repeat) toggleEventReviewRecheckEditing();
    }
  });

  syncEventReviewRecheckUi();
}

initEventReviewRecheck();
