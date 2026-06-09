/** 事件复核（标真 / 取消 / 保存） */

/** 与后端 event_signature 一致 */
function eventRowKey(ev) {
  const tokens = [...(ev.box_tokens || [])]
    .map((t) => String(t).trim())
    .filter((t) => t.length > 0)
    .sort();
  const frameIdx = parseInt(ev.frame_idx, 10) || 0;
  const eventType = String(ev.event_type || "").trim();
  return `${eventType}:${frameIdx}:${tokens.join(",")}`;
}

function eventToReviewPayload(ev) {
  const tokens = [...(ev.box_tokens || [])]
    .map((t) => String(t).trim())
    .filter((t) => t.length > 0);
  const frameIdx = parseInt(ev.frame_idx, 10) || 0;
  return {
    event_type: String(ev.event_type || "").trim(),
    frame_idx: frameIdx,
    source_frame_idx: parseInt(ev.source_frame_idx ?? ev.frame_idx, 10) || frameIdx,
    box_tokens: tokens,
  };
}

function buildEventsFromFrames(frames) {
  const events = [];
  (frames || []).forEach((fr) => {
    if (!fr || typeof fr !== "object") return;
    const ts = Number(fr.timestamp_sec) || 0;
    const fi = Number(fr.frame_idx) || 0;
    const sfi = Number(fr.source_frame_idx) || fi;
    const alarms = [...(fr.alarm_collisions || [])].map(String).filter(Boolean);
    const collisions = [...(fr.collisions || [])].map(String).filter(Boolean);
    if (alarms.length) {
      events.push({
        event_type: "alarm",
        frame_idx: fi,
        source_frame_idx: sfi,
        timestamp_sec: ts,
        box_tokens: alarms,
      });
    }
    const collOnly = collisions.filter((t) => !alarms.includes(t));
    if (collOnly.length) {
      events.push({
        event_type: "collision",
        frame_idx: fi,
        source_frame_idx: sfi,
        timestamp_sec: ts,
        box_tokens: collOnly,
      });
    }
  });
  events.sort((a, b) => a.timestamp_sec - b.timestamp_sec || a.frame_idx - b.frame_idx);
  return events;
}

function getPlaybackDurationSec() {
  if (videoEl.duration && Number.isFinite(videoEl.duration) && videoEl.duration > 0) {
    return videoEl.duration;
  }
  if (frameByTime.length) {
    const last = frameByTime[frameByTime.length - 1];
    const tail = last?.t || 0;
    const fps = poseData?.fps || 15;
    return Math.max(tail + 1 / fps, tail);
  }
  return 0;
}

function formatEventTokens(tokens) {
  const list = (tokens || []).filter(Boolean);
  if (!list.length) return "—";
  if (list.length <= 2) return list.join(", ");
  return `${list.slice(0, 2).join(", ")} +${list.length - 2}`;
}

function isEventVerified(ev) {
  if (!ev) return false;
  const key = eventRowKey(ev);
  if (verifiedTrueKeys.has(key)) return true;
  if (ev.verified_true) {
    verifiedTrueKeys.add(key);
    return true;
  }
  return false;
}

function countVerifiedEvents() {
  return playbackEvents.filter((e) => isEventVerified(e)).length;
}

/** 按 activeEventKey 在完整事件列表中定位（不受筛选影响） */
function getActiveEvent() {
  if (!activeEventKey || !playbackEvents.length) return null;
  return playbackEvents.find((e) => eventRowKey(e) === activeEventKey) ?? null;
}

function refreshEventCountLabel() {
  if (!eventCountLabel) return;
  if (!playbackEvents.length) return;
  const alarmN = playbackEvents.filter((e) => e.event_type === "alarm").length;
  const collN = playbackEvents.filter((e) => e.event_type === "collision").length;
  const verifiedN = countVerifiedEvents();
  const list = filteredPlaybackEvents();
  const rtHint = playbackEventsFromRealtime ? " · 回放实时计算" : "";
  const filterHint = list.length !== playbackEvents.length ? ` · 队列 ${list.length}` : "";
  eventCountLabel.textContent = `告警 ${alarmN} · 碰撞 ${collN} · 标真 ${verifiedN}${rtHint}${filterHint}`;
}

function syncVerifiedKeysFromEvents(events, reviewPayload = null) {
  verifiedTrueKeys.clear();
  (events || []).forEach((ev) => {
    if (ev?.verified_true) verifiedTrueKeys.add(eventRowKey(ev));
  });
  const reviewList = reviewPayload?.verified_true;
  if (Array.isArray(reviewList)) {
    for (const item of reviewList) {
      if (!item || typeof item !== "object") continue;
      const key = eventRowKey(item);
      verifiedTrueKeys.add(key);
    }
  }
}

function applyVerifiedFlagsToEvents() {
  playbackEvents.forEach((ev) => {
    ev.verified_true = isEventVerified(ev);
  });
}

function setEventVerified(ev, verified) {
  const key = eventRowKey(ev);
  if (verified) verifiedTrueKeys.add(key);
  else verifiedTrueKeys.delete(key);
  ev.verified_true = !!verified;
  if (!verified && isReviewTerminalStatus(currentEventReviewStatus)) {
    currentEventReviewStatus = "in_progress";
    patchPlaybackRecordReviewStatus(currentRecordId, "in_progress", "复核中");
  }
  refreshEventCountLabel();
}

async function flushSaveEventReview() {
  if (eventReviewSaveTimer) {
    clearTimeout(eventReviewSaveTimer);
    eventReviewSaveTimer = null;
  }
  await saveEventReviewNow();
}

function setEventReviewSaveStatus(text, kind = "") {
  const el = $("#event-save-status");
  if (!el) return;
  el.textContent = text || "";
  el.className = `event-save-status hint${kind ? ` is-${kind}` : ""}`;
}

function scheduleSaveEventReview() {
  if (!currentRecordId) {
    setEventReviewSaveStatus("仅已保存记录可写入复核", "error");
    return;
  }
  setEventReviewSaveStatus("保存中…", "pending");
  if (eventReviewSaveTimer) clearTimeout(eventReviewSaveTimer);
  eventReviewSaveTimer = setTimeout(() => void saveEventReviewNow(), 450);
}

function buildVerifiedTruePayload() {
  return playbackEvents.filter((e) => isEventVerified(e)).map((e) => eventToReviewPayload(e));
}

function applyEventReviewResponse(body, seq) {
  if (seq !== eventReviewSaveSeq) return false;
  if (Array.isArray(body.events)) {
    const prevKey = activeEventKey;
    playbackEvents = body.events;
    syncVerifiedKeysFromEvents(playbackEvents, body.event_review);
    applyVerifiedFlagsToEvents();
    if (prevKey && playbackEvents.some((e) => eventRowKey(e) === prevKey)) {
      activeEventKey = prevKey;
    }
  }
  currentEventReviewStatus =
    body.event_review_status || body.event_review?.status || currentEventReviewStatus || "in_progress";
  applyEventReviewPatchFromBody(body);
  const n = countVerifiedEvents();
  setEventReviewSaveStatus(`已保存 · 标真 ${n} 条`);
  refreshEventCountLabel();
  updateReviewDock();
  if ($("#event-review-list-details")?.open) renderEventReviewTable();
  renderEventMarkers();
  return true;
}

/** 单条标真/取消：服务端 toggle，避免全量 verified_true 覆盖误删 */
async function persistEventReviewToggle(ev, wantVerified) {
  if (!currentRecordId || !ev) return false;
  const seq = ++eventReviewSaveSeq;
  setEventReviewSaveStatus(`标真 ${countVerifiedEvents()} 条 · 保存中…`, "pending");
  try {
    const res = await fetch(recordApiUrl(currentRecordId, "/event-review"), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: "toggle",
        event: eventToReviewPayload(ev),
        verified_true: !!wantVerified,
        event_total: playbackEvents.length,
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `保存失败 (${res.status})`);
    }
    const body = await res.json();
    return applyEventReviewResponse(body, seq);
  } catch (err) {
    if (seq !== eventReviewSaveSeq) return false;
    setEventReviewSaveStatus(err.message || "保存失败", "error");
    return false;
  }
}

async function saveEventReviewNow() {
  if (!currentRecordId) return;
  const seq = ++eventReviewSaveSeq;
  const verified_true = buildVerifiedTruePayload();
  try {
    const res = await fetch(recordApiUrl(currentRecordId, "/event-review"), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        verified_true,
        event_total: playbackEvents.length,
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `保存失败 (${res.status})`);
    }
    const body = await res.json();
    applyEventReviewResponse(body, seq);
  } catch (err) {
    if (seq !== eventReviewSaveSeq) return;
    setEventReviewSaveStatus(err.message || "保存失败", "error");
  }
}

async function markEventReviewCompleted() {
  if (!currentRecordId) {
    setEventReviewSaveStatus("请从记录列表打开回放后再完成复核", "error");
    return;
  }
  setEventReviewSaveStatus("正在标记已复核…", "pending");
  try {
    const res = await fetch(recordApiUrl(currentRecordId, "/event-review"), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        status: "completed",
        event_total: playbackEvents.length,
        verified_true: buildVerifiedTruePayload(),
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `操作失败 (${res.status})`);
    }
    const body = await res.json();
    currentEventReviewStatus = body.event_review_status || "completed";
    patchPlaybackRecordReviewStatus(currentRecordId, "completed", "已复核");
    setEventReviewSaveStatus("已标记为复核完成");
    updateReviewDock();
    await loadRecords({ quiet: true });
  } catch (err) {
    setEventReviewSaveStatus(err.message || "操作失败", "error");
  }
}

function filteredPlaybackEvents() {
  const mode = eventFilterSelect?.value || "all";
  if (mode === "all") return playbackEvents;
  if (mode === "verified") return playbackEvents.filter((e) => isEventVerified(e));
  if (mode === "unreviewed") return playbackEvents.filter((e) => !isEventVerified(e));
  if (mode === "alarm" || mode === "collision") {
    return playbackEvents.filter((e) => e.event_type === mode);
  }
  return playbackEvents;
}

function getActiveFilteredEvent() {
  const list = filteredPlaybackEvents();
  if (!list.length) return null;
  if (!activeEventKey) return list[0];
  return list.find((e) => eventRowKey(e) === activeEventKey) ?? null;
}

function getActiveFilteredIndex() {
  const list = filteredPlaybackEvents();
  if (!list.length) return -1;
  const ev = getActiveFilteredEvent();
  if (!ev) return -1;
  return list.findIndex((e) => eventRowKey(e) === eventRowKey(ev));
}

function getActiveGlobalIndex() {
  if (!playbackEvents.length) return -1;
  if (!activeEventKey) return 0;
  const idx = playbackEvents.findIndex((e) => eventRowKey(e) === activeEventKey);
  return idx >= 0 ? idx : 0;
}

function navigateReviewEvent(delta) {
  if (!playbackEvents.length) return;

  const cur = getActiveEvent();
  const curKey = cur ? eventRowKey(cur) : "";

  // 标真并下一条后，上一条优先回到刚标真的事件（该事件已不在「未标真」队列）
  if (delta < 0 && reviewBackKey && curKey && curKey !== reviewBackKey) {
    const backEv = playbackEvents.find((e) => eventRowKey(e) === reviewBackKey);
    if (backEv) {
      void seekToEvent(backEv);
      return;
    }
  }

  const filterMode = eventFilterSelect?.value || "all";
  const useFiltered = filterMode !== "all";
  const list = useFiltered ? filteredPlaybackEvents() : playbackEvents;
  if (!list.length) return;

  let idx;
  if (useFiltered) {
    idx = curKey ? list.findIndex((e) => eventRowKey(e) === curKey) : -1;
    if (idx < 0) idx = delta > 0 ? -1 : list.length;
    idx = Math.max(0, Math.min(list.length - 1, idx + delta));
  } else {
    idx = getActiveGlobalIndex();
    if (idx < 0) idx = 0;
    idx = Math.max(0, Math.min(playbackEvents.length - 1, idx + delta));
  }

  reviewBackKey = null;
  void seekToEvent(useFiltered ? list[idx] : playbackEvents[idx]);
}

function scrollActiveEventRowIntoView() {
  if (!eventJumpList || !activeEventKey) return;
  const row = eventJumpList.querySelector(`tr[data-event-key="${CSS.escape(activeEventKey)}"]`);
  row?.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function updateReviewDock() {
  const list = filteredPlaybackEvents();
  const ev = getActiveEvent() ?? getActiveFilteredEvent();
  const evInFilter = ev ? list.some((item) => eventRowKey(item) === eventRowKey(ev)) : false;
  const posEl = $("#event-review-position");
  const badgeEl = $("#event-review-badge");
  const metaEl = $("#event-review-meta");
  const verifiedTag = $("#event-review-verified-tag");
  const summaryEl = $("#event-review-list-summary");
  const verifiedN = countVerifiedEvents();
  refreshEventCountLabel();

  if (summaryEl) {
    const reviewNote =
      currentEventReviewStatus === "completed"
        ? " · 记录已复核"
        : currentEventReviewStatus === "no_collision"
          ? " · 无碰撞"
          : currentEventReviewStatus === "in_progress"
            ? " · 复核中"
            : "";
    summaryEl.textContent = `全部事件列表（${playbackEvents.length} 条，已标真 ${verifiedN}${reviewNote}）`;
  }

  const completeBtn = $("#event-review-complete-btn");
  if (completeBtn) {
    const reviewDone = isReviewTerminalStatus(currentEventReviewStatus);
    completeBtn.disabled = reviewDone || !currentRecordId;
    if (currentEventReviewStatus === "no_collision") {
      completeBtn.textContent = "无碰撞（已复核）";
    } else {
      completeBtn.textContent = reviewDone ? "已复核完成" : "标记复核完成";
    }
  }

  if (!playbackEvents.length) {
    if (posEl) {
      posEl.textContent = isReviewTerminalStatus(currentEventReviewStatus) ? "无碰撞事件" : "无事件";
    }
    if (metaEl) {
      metaEl.textContent = isReviewTerminalStatus(currentEventReviewStatus)
        ? "无需人工复核"
        : "—";
    }
    verifiedTag?.classList.add("hidden");
    return;
  }

  if (!list.length) {
    if (posEl) posEl.textContent = "队列已清空";
    if (metaEl) metaEl.textContent = "当前筛选下无待复核事件";
    verifiedTag?.classList.add("hidden");
    return;
  }

  if (posEl) {
    const globalIdx = playbackEvents.findIndex((item) => eventRowKey(item) === eventRowKey(ev));
    const globalNote =
      globalIdx >= 0 ? ` · 总序 ${globalIdx + 1}/${playbackEvents.length}` : "";
    const linkNote = playbackEventLinkExact ? "" : " · 最近";
    if (evInFilter) {
      const idx = list.findIndex((item) => eventRowKey(item) === eventRowKey(ev));
      posEl.textContent = `第 ${idx + 1} / ${list.length} 条${linkNote}${list.length !== playbackEvents.length ? `（队列）${globalNote}` : globalNote}`;
    } else {
      posEl.textContent = `已标真 / 不在当前队列${linkNote}${globalNote}`;
    }
  }

  if (!ev) return;
  const typeLabel = ev.event_type === "alarm" ? "告警" : "碰撞";
  if (badgeEl) {
    badgeEl.textContent = typeLabel;
    badgeEl.className = `event-badge ${ev.event_type}`;
  }
  if (metaEl) {
    metaEl.textContent = `${formatTime(ev.timestamp_sec)} · 帧 ${ev.frame_idx} · ${formatEventTokens(ev.box_tokens)}`;
  }
  if (verifiedTag) {
    verifiedTag.classList.toggle("hidden", !isEventVerified(ev));
  }
}

function renderEventReviewTable(list = null) {
  if (!eventJumpList) return;
  const rows = list ?? filteredPlaybackEvents();
  const canSave = !!currentRecordId;

  eventJumpList.innerHTML = rows
    .map((ev) => {
      const key = eventRowKey(ev);
      const typeLabel = ev.event_type === "alarm" ? "告警" : "碰撞";
      const active = key === activeEventKey ? " active" : "";
      const verified = isEventVerified(ev);
      const verifiedCls = verified ? " verified-true" : "";
      const checked = verified ? " checked" : "";
      const disabled = canSave ? "" : " disabled";
      return `<tr class="event-review-row${active}${verifiedCls}" data-event-key="${key}">
        <td class="col-verify"><input type="checkbox" class="event-verify-check" data-event-key="${key}"${checked}${disabled} aria-label="标为真实碰撞" /></td>
        <td class="col-type"><span class="event-badge ${ev.event_type}">${typeLabel}</span></td>
        <td class="col-time">${formatTime(ev.timestamp_sec)}</td>
        <td class="col-frame">${ev.frame_idx}</td>
        <td class="col-tokens" title="${formatEventTokens(ev.box_tokens)}">${formatEventTokens(ev.box_tokens)}</td>
      </tr>`;
    })
    .join("");

  eventJumpList.querySelectorAll(".event-verify-check").forEach((input) => {
    input.addEventListener("click", (e) => e.stopPropagation());
    input.addEventListener("change", () => {
      void (async () => {
        const key = input.dataset.eventKey;
        const item = playbackEvents.find((row) => eventRowKey(row) === key);
        if (!item || !currentRecordId) return;
        const want = input.checked;
        setEventVerified(item, want);
        updateReviewDock();
        const ok = await persistEventReviewToggle(item, want);
        if (!ok) {
          setEventVerified(item, !want);
          input.checked = !want;
          updateReviewDock();
        }
        renderEventReviewTable();
        renderEventMarkers();
      })();
    });
  });

  eventJumpList.querySelectorAll(".event-review-row").forEach((row) => {
    row.addEventListener("click", () => {
      const key = row.dataset.eventKey;
      const item = playbackEvents.find((rowEv) => eventRowKey(rowEv) === key);
      if (item) void seekToEvent(item);
    });
  });

  scrollActiveEventRowIntoView();
}

async function markActiveEventVerified(verified) {
  const ev = getActiveEvent();
  if (!ev) {
    setEventReviewSaveStatus("请先在列表或进度条上选择一条事件", "");
    return;
  }
  if (!verified && !isEventVerified(ev)) {
    setEventReviewSaveStatus("当前选中事件未标真", "");
    return;
  }
  if (!currentRecordId) {
    setEventReviewSaveStatus("导入 JSON 无法保存，请从记录列表打开", "error");
    return;
  }
  setEventVerified(ev, verified);
  updateReviewDock();
  if ($("#event-review-list-details")?.open) renderEventReviewTable();
  renderEventMarkers();
  const ok = await persistEventReviewToggle(ev, verified);
  if (!ok) {
    setEventVerified(ev, !verified);
    updateReviewDock();
    if ($("#event-review-list-details")?.open) renderEventReviewTable();
    renderEventMarkers();
    return;
  }
  if (!verified && eventRowKey(ev) === reviewBackKey) {
    reviewBackKey = null;
  }
}

async function confirmTrueAndNext() {
  const list = filteredPlaybackEvents();
  const ev = getActiveFilteredEvent();
  if (!ev) return;
  const idx = getActiveFilteredIndex();
  if (!currentRecordId) {
    setEventReviewSaveStatus("导入 JSON 无法保存，请从记录列表打开", "error");
    return;
  }
  reviewBackKey = eventRowKey(ev);
  setEventVerified(ev, true);
  await persistEventReviewToggle(ev, true);

  const mode = eventFilterSelect?.value || "all";
  if (mode === "unreviewed") {
    const newList = filteredPlaybackEvents();
    if (!newList.length || idx >= newList.length) {
      updateReviewDock();
      renderEventReviewTable();
      renderEventMarkers();
      setEventReviewSaveStatus("未标真事件已全部复核", "");
      return;
    }
    await seekToEvent(newList[idx]);
    return;
  }

  const nextEv = list[idx + 1];
  if (nextEv) await seekToEvent(nextEv);
  else {
    updateReviewDock();
    if ($("#event-review-list-details")?.open) renderEventReviewTable();
    renderEventMarkers();
  }
}

async function skipToNextEvent() {
  navigateReviewEvent(1);
}

async function beginEventReview() {
  if (!playbackEvents.length) return;
  const hasUnreviewed = playbackEvents.some((e) => !isEventVerified(e));
  if (hasUnreviewed && eventFilterSelect) {
    eventFilterSelect.value = "unreviewed";
  }
  const first = filteredPlaybackEvents()[0];
  if (first) await seekToEvent(first);
  else updateReviewDock();
}

function renderEventMarkers() {
  if (!eventMarkersEl) return;
  eventMarkersEl.innerHTML = "";
  const dur = getPlaybackDurationSec();
  if (!dur || !playbackEvents.length) return;

  filteredPlaybackEvents().forEach((ev) => {
    const key = eventRowKey(ev);
    const pct = Math.min(100, Math.max(0, (ev.timestamp_sec / dur) * 100));
    const dot = document.createElement("button");
    dot.type = "button";
    const verifiedCls = isEventVerified(ev) ? " verified" : "";
    const activeCls = key === activeEventKey ? " active" : "";
    dot.className = `event-marker ${ev.event_type}${verifiedCls}${activeCls}`;
    dot.dataset.eventKey = key;
    dot.style.left = `${pct}%`;
    const verifiedNote = isEventVerified(ev) ? " · 已标真" : "";
    dot.title = `${ev.event_type === "alarm" ? "告警" : "碰撞"} ${formatTime(ev.timestamp_sec)} · ${formatEventTokens(ev.box_tokens)}${verifiedNote}`;
    dot.addEventListener("click", (e) => {
      e.stopPropagation();
      seekToEvent(ev);
    });
    eventMarkersEl.appendChild(dot);
  });
}

function renderEventReviewList() {
  if (!eventsPanel) return;
  const list = filteredPlaybackEvents();
  const verifiedN = countVerifiedEvents();

  if (!playbackEvents.length) {
    eventsPanel.classList.add("hidden");
    if (eventJumpList) eventJumpList.innerHTML = "";
    if (eventCountLabel) {
      const hint = annotationBoxes.length
        ? "无碰撞事件（已按标注实时扫描）"
        : "无事件（需采集时启用碰撞或加载标注）";
      eventCountLabel.textContent = hint;
    }
    setEventReviewSaveStatus("");
    updateReviewDock();
    return;
  }

  eventsPanel.classList.remove("hidden");
  refreshEventCountLabel();

  if (!currentRecordId) {
    setEventReviewSaveStatus("导入 JSON 无法保存，请从记录列表打开", "error");
  }

  updateReviewDock();
  if ($("#event-review-list-details")?.open) {
    renderEventReviewTable(list);
  } else if (eventJumpList) {
    eventJumpList.innerHTML = "";
  }
  renderEventMarkers();
}

/** @deprecated 兼容旧调用 */
const renderEventJumpList = renderEventReviewList;
