/** 区间增强标真：首帧选 person_id + 货框，区间内批量标真 */

function clearRangeAnnotBounds() {
  rangeAnnotStartFrame = null;
  rangeAnnotEndFrame = null;
  rangeAnnotTemplateSnapshot = null;
  updateRangeAnnotUi();
}

/** 在首帧选取 person_id / 货框后立即缓存，避免切换事件时丢失暂选 */
function refreshRangeAnnotTemplateSnapshot() {
  if (rangeAnnotStartFrame == null || rangeAnnotStartFrame <= 0) {
    rangeAnnotTemplateSnapshot = null;
    return;
  }
  const fi = rangeAnnotStartFrame;
  const template = getRangeAnnotTemplate(fi);
  if (!template?.ev) {
    rangeAnnotTemplateSnapshot = null;
    return;
  }
  let confirmed = normalizeBoxTokenList(template.confirmed);
  if (!confirmed.length) {
    confirmed = normalizeBoxTokenList(getEventConfirmedBoxes(template.ev));
  }
  if (!confirmed.length) {
    confirmed = normalizeBoxTokenList(template.ev.box_tokens);
  }
  let personId = template.personId ?? getEventPersonId(template.ev);
  const personIds = getFramePersonIds(fi);
  if (personId == null && personIds.length === 1) {
    personId = personIds[0];
  }
  rangeAnnotTemplateSnapshot = {
    confirmed,
    personId: personId != null ? Number(personId) : null,
  };
}

function getRangeAnnotTemplateForApply(startFrame) {
  const fi = parseInt(startFrame, 10) || 0;
  if (rangeAnnotTemplateSnapshot && rangeAnnotStartFrame === fi) {
    const frameEvents = getEventsOnFrame(fi);
    return {
      ev: frameEvents[0] || null,
      confirmed: [...rangeAnnotTemplateSnapshot.confirmed],
      personId: rangeAnnotTemplateSnapshot.personId,
    };
  }
  return getRangeAnnotTemplate(fi);
}

function normalizeRangeAnnotBounds(start, end) {
  const a = parseInt(start, 10) || 0;
  const b = parseInt(end, 10) || 0;
  if (a <= 0 || b <= 0) return null;
  return { start: Math.min(a, b), end: Math.max(a, b) };
}

function getPersonTrackIdAtFrame(frameIdx, personId) {
  const fi = parseInt(frameIdx, 10) || 0;
  if (fi <= 0 || typeof frameCache === "undefined") return null;
  const frame = frameCache.get(fi);
  if (!frame?.persons?.length) return null;
  const target = Number(personId);
  for (let idx = 0; idx < frame.persons.length; idx++) {
    const p = frame.persons[idx];
    const pid = p.person_id != null ? Number(p.person_id) : idx;
    if (pid !== target) continue;
    const tid = p.person_track_id;
    if (tid != null && tid !== "" && Number.isFinite(Number(tid))) return Number(tid);
    return null;
  }
  return null;
}

/** 按 person_track_id 解析某帧 person_id；单人帧可自动回退 */
function resolvePersonIdForFrame(frameIdx, trackId) {
  const fi = parseInt(frameIdx, 10) || 0;
  if (fi <= 0 || typeof frameCache === "undefined") return null;
  const frame = frameCache.get(fi);
  if (!frame?.persons?.length) return null;
  if (trackId != null && Number.isFinite(Number(trackId))) {
    const tid = Number(trackId);
    for (let idx = 0; idx < frame.persons.length; idx++) {
      const p = frame.persons[idx];
      const pt =
        p.person_track_id != null && p.person_track_id !== "" ? Number(p.person_track_id) : null;
      if (pt === tid) {
        const pid = p.person_id != null ? Number(p.person_id) : idx;
        return Number.isFinite(pid) ? pid : idx;
      }
    }
  }
  const ids = getFramePersonIds(fi);
  return ids.length === 1 ? ids[0] : null;
}

function eventInRangeFrame(ev, start, end) {
  const fi = parseInt(ev?.frame_idx, 10) || 0;
  return fi >= start && fi <= end;
}

function eventMatchesRangeBoxFilter(ev, confirmedTokens) {
  const confirmed = normalizeBoxTokenList(confirmedTokens);
  if (!confirmed.length) return true;
  const evTokens = normalizeBoxTokenList(ev?.box_tokens);
  return evTokens.some((t) => confirmed.includes(t));
}

function collectRangeAnnotEvents(start, end, confirmedTokens) {
  return (playbackEvents || []).filter(
    (ev) => eventInRangeFrame(ev, start, end) && eventMatchesRangeBoxFilter(ev, confirmedTokens)
  );
}

/** 首帧模板：优先当前钉住事件，否则首帧上已有货框/person 选取的事件 */
function getRangeAnnotTemplate(startFrame) {
  const fi = parseInt(startFrame, 10) || 0;
  const frameEvents = getEventsOnFrame(fi);
  if (!frameEvents.length) return null;

  const pinned = typeof getPinnedPlaybackEvent === "function" ? getPinnedPlaybackEvent() : null;
  if (pinned && (parseInt(pinned.frame_idx, 10) || 0) === fi) {
    return {
      ev: pinned,
      confirmed: getEventConfirmedBoxes(pinned),
      personId: getEventPersonId(pinned),
    };
  }

  for (const ev of frameEvents) {
    const confirmed = getEventConfirmedBoxes(ev);
    const personId = getEventPersonId(ev);
    if (confirmed.length || personId != null) {
      return { ev, confirmed, personId };
    }
  }

  return { ev: frameEvents[0], confirmed: [], personId: null };
}

function validateRangeAnnotTemplate(template, startFrame) {
  if (!template?.ev) {
    return { ok: false, message: `首帧 ${startFrame} 无碰撞/告警事件，无法区间标真` };
  }

  let confirmed = normalizeBoxTokenList(template.confirmed);
  if (!confirmed.length) {
    confirmed = normalizeBoxTokenList(template.ev.box_tokens);
  }
  if (!confirmed.length) {
    return { ok: false, message: "请先在首帧点选货框（或确保检测含货框）" };
  }

  const personIds = getFramePersonIds(startFrame);
  let personId = template.personId;
  if (personId == null && personIds.length === 1) {
    personId = personIds[0];
  }
  if (personIds.length >= 2 && personId == null) {
    return { ok: false, message: "首帧有多人，请先选择 person_id（侧栏或点击骨架）" };
  }
  if (personId != null && personIds.length && !personIds.includes(Number(personId))) {
    return { ok: false, message: `首帧 person_id ${personId} 不在当前画面人员列表中` };
  }

  const trackId = personId != null ? getPersonTrackIdAtFrame(startFrame, personId) : null;
  return { ok: true, confirmed, personId, trackId };
}

async function ensureFrameRangeLoaded(start, end) {
  if (typeof ensureFrameChunkLoaded !== "function") return;
  const tasks = [];
  for (let fi = start; fi <= end; fi += 1) {
    tasks.push(ensureFrameChunkLoaded(fi));
  }
  await Promise.all(tasks);
}

function setRangeAnnotStartFromCurrent() {
  const fi =
    typeof getResolvedPlaybackFrameIdx === "function" ? getResolvedPlaybackFrameIdx() : null;
  if (!fi || fi <= 0) {
    setEventReviewSaveStatus("无法读取当前帧，请先暂停到目标画面", "error");
    return;
  }
  rangeAnnotStartFrame = fi;
  if (rangeAnnotEndFrame != null && rangeAnnotEndFrame < rangeAnnotStartFrame) {
    rangeAnnotEndFrame = null;
  }
  refreshRangeAnnotTemplateSnapshot();
  updateRangeAnnotUi();
  setEventReviewSaveStatus(`已设首帧 ${fi} · 请在本帧选择 person_id 与货框`, "");
}

function setRangeAnnotEndFromCurrent() {
  const fi =
    typeof getResolvedPlaybackFrameIdx === "function" ? getResolvedPlaybackFrameIdx() : null;
  if (!fi || fi <= 0) {
    setEventReviewSaveStatus("无法读取当前帧，请先暂停到目标画面", "error");
    return;
  }
  rangeAnnotEndFrame = fi;
  if (rangeAnnotStartFrame != null && rangeAnnotEndFrame < rangeAnnotStartFrame) {
    const tmp = rangeAnnotStartFrame;
    rangeAnnotStartFrame = rangeAnnotEndFrame;
    rangeAnnotEndFrame = tmp;
  }
  updateRangeAnnotUi();
  setEventReviewSaveStatus(`已设尾帧 ${fi}`, "");
}

function updateRangeAnnotUi() {
  const startEl = $("#event-range-start-label");
  const endEl = $("#event-range-end-label");
  const hintEl = $("#event-range-hint");
  const applyBtn = $("#event-range-apply-btn");

  const bounds = normalizeRangeAnnotBounds(rangeAnnotStartFrame, rangeAnnotEndFrame);
  if (startEl) {
    startEl.textContent =
      rangeAnnotStartFrame != null ? `首帧：${rangeAnnotStartFrame}` : "首帧：—";
    startEl.classList.toggle("is-set", rangeAnnotStartFrame != null);
  }
  if (endEl) {
    endEl.textContent = rangeAnnotEndFrame != null ? `尾帧：${rangeAnnotEndFrame}` : "尾帧：—";
    endEl.classList.toggle("is-set", rangeAnnotEndFrame != null);
  }

  let hint = "在首帧选择 person_id 与货框，设置尾帧后一键标真区间内全部事件";
  let canApply = false;
  let previewN = 0;

  if (bounds) {
    refreshRangeAnnotTemplateSnapshot();
    const template = getRangeAnnotTemplateForApply(bounds.start);
    const check = validateRangeAnnotTemplate(template, bounds.start);
    const events = check.ok
      ? collectRangeAnnotEvents(bounds.start, bounds.end, check.confirmed)
      : [];
    previewN = events.length;
    canApply = check.ok && previewN > 0 && !!currentRecordId;

    if (!check.ok) {
      hint = check.message;
    } else if (!previewN) {
      hint = `帧 ${bounds.start}–${bounds.end} 内无匹配货框的事件`;
    } else {
      const personNote =
        check.personId != null
          ? ` · 人员 P${check.personId}${check.trackId != null ? `（track ${check.trackId}）` : ""}`
          : "";
      hint = `帧 ${bounds.start}–${bounds.end} · 将标真 ${previewN} 条 · 货框 ${formatConfirmedBoxes(check.confirmed)}${personNote}`;
    }
  } else if (rangeAnnotStartFrame != null || rangeAnnotEndFrame != null) {
    hint = "请同时设置首帧与尾帧";
  }

  if (hintEl) {
    hintEl.textContent = hint;
    hintEl.classList.toggle("is-ready", canApply);
    hintEl.classList.toggle("is-error", bounds && previewN === 0 && rangeAnnotStartFrame && rangeAnnotEndFrame);
  }
  if (applyBtn) {
    applyBtn.disabled = !canApply;
    applyBtn.textContent = previewN > 0 ? `区间标真（${previewN} 条）` : "区间标真";
  }
}

async function applyRangeAnnotVerified() {
  if (!currentRecordId) {
    setEventReviewSaveStatus("导入 JSON 无法保存，请从记录列表打开", "error");
    return;
  }
  const bounds = normalizeRangeAnnotBounds(rangeAnnotStartFrame, rangeAnnotEndFrame);
  if (!bounds) {
    setEventReviewSaveStatus("请先设置首帧与尾帧", "error");
    updateRangeAnnotUi();
    return;
  }

  await ensureFrameRangeLoaded(bounds.start, bounds.end);

  refreshRangeAnnotTemplateSnapshot();
  const template = getRangeAnnotTemplateForApply(bounds.start);
  const check = validateRangeAnnotTemplate(template, bounds.start);
  if (!check.ok) {
    setEventReviewSaveStatus(check.message, "error");
    updateRangeAnnotUi();
    return;
  }

  const events = collectRangeAnnotEvents(bounds.start, bounds.end, check.confirmed);
  if (!events.length) {
    setEventReviewSaveStatus(`帧 ${bounds.start}–${bounds.end} 内无匹配事件`, "error");
    updateRangeAnnotUi();
    return;
  }

  const missingPersonFrames = [];
  for (const ev of events) {
    const fi = parseInt(ev.frame_idx, 10) || 0;
    const pid = resolvePersonIdForFrame(fi, check.trackId);
    const ids = getFramePersonIds(fi);
    if (ids.length >= 2 && pid == null) {
      missingPersonFrames.push(fi);
    }
  }
  if (missingPersonFrames.length) {
    const sample = [...new Set(missingPersonFrames)].slice(0, 5).join(", ");
    setEventReviewSaveStatus(
      `以下帧无法通过 track 匹配 person_id：${sample}${missingPersonFrames.length > 5 ? " …" : ""}`,
      "error"
    );
    return;
  }

  const personNote = check.personId != null ? ` · P${check.personId}` : "";
  if (
    !window.confirm(
      `确定区间标真？\n\n帧范围：${bounds.start} – ${bounds.end}\n事件：${events.length} 条\n货框：${formatConfirmedBoxes(check.confirmed)}${personNote}\n\n区间内各帧 person_id 将按 track 自动对应。`
    )
  ) {
    return;
  }

  for (const ev of events) {
    const fi = parseInt(ev.frame_idx, 10) || 0;
    const pid = resolvePersonIdForFrame(fi, check.trackId);
    setEventConfirmedBoxes(ev, check.confirmed, { commitToEvent: true });
    if (pid != null) {
      setEventPersonId(ev, pid, { commitToEvent: true });
      personIdTouchedKeys.delete(eventRowKey(ev));
    }
    boxAnnotationTouchedKeys.delete(eventRowKey(ev));
    setEventVerified(ev, true);
  }

  updateReviewDock();
  updateRangeAnnotUi();
  if ($("#event-review-list-details")?.open) renderEventReviewTable();
  renderEventMarkers();

  const ok = await persistEventReviewVerifiedList(
    buildVerifiedTruePayload(),
    `区间标真 ${events.length} 条 · 保存中…`
  );
  if (ok) {
    setEventReviewSaveStatus(
      `区间标真完成 · 帧 ${bounds.start}–${bounds.end} · 共 ${events.length} 条`
    );
  }
}
