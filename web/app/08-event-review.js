/** 事件复核（标真 / 取消 / 保存） */

/** 复核 PATCH 串行队列，避免连按 Y 导致并发 toggle 互相覆盖 */
let eventReviewSaveChain = Promise.resolve();

function drainEventReviewSaveQueue() {
  return eventReviewSaveChain;
}

function runSerializedEventReviewSave(task) {
  const run = eventReviewSaveChain.then(() => task());
  eventReviewSaveChain = run.catch(() => {});
  return run;
}

/** 切换/关闭记录前：等待队列中 PATCH 落盘，并作废已切换记录后的过期 UI 响应 */
async function prepareEventReviewRecordSwitch() {
  if (eventReviewSaveTimer) {
    clearTimeout(eventReviewSaveTimer);
    eventReviewSaveTimer = null;
  }
  await drainEventReviewSaveQueue();
  eventReviewSaveSeq++;
}

/** 与后端 event_signature 一致 */
function eventRowKey(ev) {
  const tokens = canonicalizeBoxTokenList(ev?.box_tokens);
  const frameIdx = parseInt(ev.frame_idx, 10) || 0;
  const eventType = String(ev.event_type || "").trim();
  return `${eventType}:${frameIdx}:${tokens.join(",")}`;
}

function getEventsOnFrame(frameIdx) {
  const fi = parseInt(frameIdx, 10) || 0;
  return playbackEvents.filter((e) => (parseInt(e.frame_idx, 10) || 0) === fi);
}

function getVerifiedEventsOnFrame(frameIdx) {
  return getEventsOnFrame(frameIdx).filter((e) => isEventVerified(e));
}

function countFrameConfirmedBoxes(frameIdx) {
  return getEventsOnFrame(frameIdx).filter((e) => getEventConfirmedBoxes(e).length > 0).length;
}

function countVerifiedFrameConfirmedBoxes(frameIdx) {
  return getVerifiedEventsOnFrame(frameIdx).filter((e) => getEventConfirmedBoxes(e).length > 0).length;
}

function isAnnotationBoxToken(token) {
  const hit = canonicalBoxToken(token);
  if (!hit || !annotationBoxes.length) return false;
  return annotationBoxes.some((box) => boxCollisionToken(box) === hit);
}

function normalizeBoxTokenList(tokens) {
  return canonicalizeBoxTokenList(tokens);
}

function eventDisplayFrameIdx(ev) {
  return parseInt(ev?.frame_idx, 10) || 0;
}

function eventSourceFrameIdx(ev) {
  const sfi = parseInt(ev?.source_frame_idx, 10) || 0;
  const fi = eventDisplayFrameIdx(ev);
  return sfi > 0 ? sfi : fi;
}

/** 回放当前帧是否与事件对应（兼容 source_frame_idx） */
function eventMatchesPlaybackFrame(ev, playbackFrameIdx) {
  const fi = parseInt(playbackFrameIdx, 10) || 0;
  if (!fi || !ev) return false;
  return fi === eventDisplayFrameIdx(ev) || fi === eventSourceFrameIdx(ev);
}

function formatConfirmedBoxes(tokens) {
  const list = normalizeBoxTokenList(tokens);
  if (!list.length) return "";
  if (list.length <= 2) return list.join(", ");
  return `${list.slice(0, 2).join(", ")} +${list.length - 2}`;
}

function getEventConfirmedBoxes(ev) {
  if (!ev) return [];
  const key = eventRowKey(ev);
  if (pendingConfirmedBoxesByKey.has(key)) {
    return normalizeBoxTokenList(pendingConfirmedBoxesByKey.get(key) || []);
  }
  return getEventPersistedConfirmedBoxes(ev);
}

/** 已写入 event_review.json 的货框 */
function getEventPersistedConfirmedBoxes(ev) {
  if (!ev) return [];
  if (Array.isArray(ev.confirmed_box_tokens)) {
    return normalizeBoxTokenList(ev.confirmed_box_tokens);
  }
  const legacy = String(ev.confirmed_box_token || "").trim();
  return legacy ? [legacy] : [];
}

/** 当前事件是否有尚未按 Y 落盘的 box 点选 */
function hasPendingBoxAnnotation(ev) {
  if (!ev) return false;
  return pendingConfirmedBoxesByKey.has(eventRowKey(ev));
}

function getEventConfirmedBox(ev) {
  const boxes = getEventConfirmedBoxes(ev);
  return boxes[0] || "";
}

function setEventConfirmedBoxes(ev, tokens, { commitToEvent = false } = {}) {
  if (!ev) return;
  const list = normalizeBoxTokenList(tokens);
  const key = eventRowKey(ev);
  boxAnnotationTouchedKeys.add(key);
  if (!commitToEvent) {
    pendingConfirmedBoxesByKey.set(key, [...list]);
    return;
  }
  if (list.length) {
    ev.confirmed_box_tokens = [...list];
    pendingConfirmedBoxesByKey.delete(key);
  } else {
    delete ev.confirmed_box_tokens;
    pendingConfirmedBoxesByKey.delete(key);
  }
  delete ev.confirmed_box_token;
}

/** 标真落盘用的货框列表：优先人工点选，否则为空（标真时由 applyAuto 填充默认） */
function resolveConfirmedBoxesForSave(ev) {
  return getEventConfirmedBoxes(ev);
}

function applyAutoConfirmedBoxOnVerify(ev) {
  if (!ev) return;
  const key = eventRowKey(ev);
  if (boxAnnotationTouchedKeys.has(key)) return;
  if (getEventConfirmedBoxes(ev).length) return;
  const defaults = normalizeBoxTokenList(ev.box_tokens);
  if (!defaults.length) return;
  setEventConfirmedBoxes(ev, defaults, { commitToEvent: true });
  boxAnnotationTouchedKeys.delete(key);
}

/** 当前帧画面中的 person_id 列表（与骨架绘制一致） */
function getFramePersonIds(frameIdx) {
  const fi = parseInt(frameIdx, 10) || 0;
  if (fi <= 0 || typeof frameCache === "undefined") return [];
  const frame = frameCache.get(fi);
  const persons = frame?.persons || [];
  if (!persons.length) return [];
  return persons.map((person, idx) => {
    const pid = person?.person_id != null ? Number(person.person_id) : idx;
    return Number.isFinite(pid) ? pid : idx;
  });
}

function getEventPersistedPersonId(ev) {
  if (!ev || ev.person_id == null || ev.person_id === "") return null;
  const n = Number(ev.person_id);
  return Number.isFinite(n) ? n : null;
}

function getEventPersonId(ev) {
  if (!ev) return null;
  const key = eventRowKey(ev);
  if (pendingPersonIdByKey.has(key)) {
    const pending = pendingPersonIdByKey.get(key);
    return pending == null ? null : pending;
  }
  return getEventPersistedPersonId(ev);
}

function hasPendingPersonIdAnnotation(ev) {
  if (!ev) return false;
  return pendingPersonIdByKey.has(eventRowKey(ev));
}

function setEventPersonId(ev, personId, { commitToEvent = false } = {}) {
  if (!ev) return;
  const key = eventRowKey(ev);
  const normalized =
    personId == null || personId === "" ? null : Number.isFinite(Number(personId)) ? Number(personId) : null;
  personIdTouchedKeys.add(key);
  if (!commitToEvent) {
    pendingPersonIdByKey.set(key, normalized);
    return;
  }
  if (normalized != null) {
    ev.person_id = normalized;
    pendingPersonIdByKey.delete(key);
  } else {
    delete ev.person_id;
    pendingPersonIdByKey.delete(key);
  }
}

function resolvePersonIdForSave(ev) {
  return getEventPersonId(ev);
}

function applyAutoPersonIdOnVerify(ev) {
  if (!ev) return;
  const key = eventRowKey(ev);
  if (personIdTouchedKeys.has(key)) return;
  if (getEventPersonId(ev) != null) return;
  const ids = getFramePersonIds(ev.frame_idx);
  if (ids.length === 1) {
    setEventPersonId(ev, ids[0], { commitToEvent: true });
    personIdTouchedKeys.delete(key);
  }
}

function validatePersonIdBeforeVerify(ev) {
  if (!ev) return { ok: false, message: "无事件" };
  const ids = getFramePersonIds(ev.frame_idx);
  if (!ids.length) return { ok: true };
  applyAutoPersonIdOnVerify(ev);
  const selected = getEventPersonId(ev);
  if (ids.length >= 2 && selected == null) {
    return { ok: false, message: "本帧有多人，请先选择 person_id（侧栏或点击骨架）" };
  }
  if (selected != null && !ids.includes(selected)) {
    return { ok: false, message: `person_id ${selected} 不在当前帧画面中，请重新选择` };
  }
  return { ok: true };
}

/** 复核画面：已确认 box 与检测参考 box（有事件即展示 box_tokens，无需标真） */
function getEventReviewBoxLayers(ev) {
  const detection = normalizeBoxTokenList(ev?.box_tokens);
  const confirmed = getEventConfirmedBoxes(ev);
  let detectionRef = [];
  if (detection.length) {
    detectionRef = confirmed.length
      ? detection.filter((t) => !confirmed.includes(t))
      : [...detection];
  }
  return { confirmed, detectionRef };
}

/** 回放画面上展示的货框 token（含检测参考） */
function getEventDisplayBoxTokens(ev) {
  if (!ev) return [];
  const { confirmed, detectionRef } = getEventReviewBoxLayers(ev);
  if (confirmed.length || detectionRef.length) {
    return [...confirmed, ...detectionRef];
  }
  return getEventConfirmedBoxes(ev);
}

function eventToReviewPayload(ev) {
  const tokens = [...(ev.box_tokens || [])]
    .map((t) => String(t).trim())
    .filter((t) => t.length > 0);
  const frameIdx = parseInt(ev.frame_idx, 10) || 0;
  const payload = {
    event_type: String(ev.event_type || "").trim(),
    frame_idx: frameIdx,
    source_frame_idx: parseInt(ev.source_frame_idx ?? ev.frame_idx, 10) || frameIdx,
    box_tokens: tokens,
  };
  const confirmed = resolveConfirmedBoxesForSave(ev);
  const key = eventRowKey(ev);
  if (
    confirmed.length > 0 ||
    pendingConfirmedBoxesByKey.has(key) ||
    boxAnnotationTouchedKeys.has(key)
  ) {
    payload.confirmed_box_tokens = confirmed;
  }
  const personId = resolvePersonIdForSave(ev);
  if (
    personId != null ||
    pendingPersonIdByKey.has(key) ||
    personIdTouchedKeys.has(key)
  ) {
    if (personId != null) payload.person_id = personId;
  }
  return payload;
}

function syncConfirmedBoxFromReview(reviewPayload, events = playbackEvents) {
  const list = reviewPayload?.verified_true;
  if (!Array.isArray(list)) return;
  const byKey = new Map();
  const personByKey = new Map();
  for (const item of list) {
    if (!item || typeof item !== "object") continue;
    const key = eventRowKey(item);
    const tokens = normalizeBoxTokenList(
      item.confirmed_box_tokens || (item.confirmed_box_token ? [item.confirmed_box_token] : [])
    );
    byKey.set(key, tokens);
    if (item.person_id != null && item.person_id !== "") {
      const pid = Number(item.person_id);
      if (Number.isFinite(pid)) personByKey.set(key, pid);
    }
  }
  (events || []).forEach((ev) => {
    const key = eventRowKey(ev);
    let tokens = byKey.has(key) ? byKey.get(key) : undefined;
    let personId = personByKey.has(key) ? personByKey.get(key) : undefined;
    if (tokens === undefined || personId === undefined) {
      for (const item of list) {
        if (!eventMatchesReviewEntry(ev, item)) continue;
        if (tokens === undefined) {
          tokens = normalizeBoxTokenList(
            item.confirmed_box_tokens ||
              (item.confirmed_box_token ? [item.confirmed_box_token] : [])
          );
        }
        if (personId === undefined && item.person_id != null && item.person_id !== "") {
          const pid = Number(item.person_id);
          if (Number.isFinite(pid)) personId = pid;
        }
        if (tokens !== undefined && personId !== undefined) break;
      }
    }
    if (tokens !== undefined) {
      if (tokens.length) {
        ev.confirmed_box_tokens = [...tokens];
        delete ev.confirmed_box_token;
        pendingConfirmedBoxesByKey.delete(key);
      } else {
        delete ev.confirmed_box_tokens;
        delete ev.confirmed_box_token;
        pendingConfirmedBoxesByKey.delete(key);
      }
    }
    if (personId !== undefined) {
      ev.person_id = personId;
      pendingPersonIdByKey.delete(key);
    }
  });
}

function buildBoxPickStatusHint(ev, confirmed, detN) {
  const pendingNote = hasPendingBoxAnnotation(ev)
    ? " · 暂选未落盘，切换事件将丢弃"
    : "";
  const countNote =
    detN > 0 ? ` · 已选 ${confirmed.length}/${detN}（可不选满）` : ` · 已选 ${confirmed.length} 个`;
  return `${countNote}${pendingNote} · 按 Y 写入`;
}

function buildEventsFromFrames(frames) {
  const events = [];
  (frames || []).forEach((fr) => {
    if (!fr || typeof fr !== "object") return;
    const ts = Number(fr.timestamp_sec) || 0;
    const fi = Number(fr.frame_idx) || 0;
    const sfi = Number(fr.source_frame_idx) || fi;
    const alarms = canonicalizeBoxTokenList(fr.alarm_collisions || []);
    const collisions = canonicalizeBoxTokenList(fr.collisions || []);
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
  let alarmN = playbackEvents.filter((e) => e.event_type === "alarm").length;
  let collN = playbackEvents.filter((e) => e.event_type === "collision").length;
  let verifiedN = countVerifiedEvents();
  const list = filteredPlaybackEvents();
  const rtHint = playbackEventsFromRealtime ? " · 回放实时计算" : "";
  const filterHint = list.length !== playbackEvents.length ? ` · 队列 ${list.length}` : "";
  let accuracyHint = "";
  let sourceHint = "";

  const evalCounts =
    typeof getPlaybackAccuracyEvalCounts === "function" ? getPlaybackAccuracyEvalCounts() : null;
  if (evalCounts) {
    alarmN = evalCounts.alarms ?? alarmN;
    collN = evalCounts.collisions ?? collN;
    if (evalCounts.verified > 0) verifiedN = evalCounts.verified;
    accuracyHint = ` · 漏报段 ${evalCounts.missed_segments ?? 0} · 误报 ${evalCounts.false_alarms ?? 0}`;
    if (evalCounts.sourceLabel) {
      sourceHint = ` · 来源：${evalCounts.sourceLabel}`;
    }
  } else if (typeof countPlaybackMissEvents === "function") {
    const missN = countPlaybackMissEvents();
    const falseN = countPlaybackFalseAlarmEvents();
    if (missN > 0 || falseN > 0) {
      accuracyHint = ` · 漏报段 ${missN} · 误报 ${falseN}`;
    }
  }

  eventCountLabel.textContent = `告警 ${alarmN} · 碰撞 ${collN} · 标真 ${verifiedN}${accuracyHint}${sourceHint}${rtHint}${filterHint}`;
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
      verifiedTrueKeys.add(eventRowKey(item));
      (events || []).forEach((ev) => {
        if (eventMatchesReviewEntry(ev, item)) {
          verifiedTrueKeys.add(eventRowKey(ev));
          ev.verified_true = true;
        }
      });
    }
  }
  syncConfirmedBoxFromReview(reviewPayload, events);
}

function applyVerifiedFlagsToEvents() {
  playbackEvents.forEach((ev) => {
    ev.verified_true = !!(ev.verified_true || isEventVerified(ev));
  });
}

function setEventVerified(ev, verified) {
  const key = eventRowKey(ev);
  if (verified) {
    verifiedTrueKeys.add(key);
    applyAutoConfirmedBoxOnVerify(ev);
    applyAutoPersonIdOnVerify(ev);
    if (pendingPersonIdByKey.has(key)) {
      setEventPersonId(ev, pendingPersonIdByKey.get(key), { commitToEvent: true });
    }
  } else {
    verifiedTrueKeys.delete(key);
    delete ev.confirmed_box_tokens;
    delete ev.confirmed_box_token;
    delete ev.person_id;
    pendingConfirmedBoxesByKey.delete(key);
    pendingPersonIdByKey.delete(key);
    boxAnnotationTouchedKeys.delete(key);
    personIdTouchedKeys.delete(key);
  }
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

function applyEventReviewResponse(body, seq, forRecordId = currentRecordId, options = {}) {
  if (seq !== eventReviewSaveSeq) return false;
  const savedFor = String(body?.record_id || forRecordId || "").trim();
  const applyUi = !!savedFor && savedFor === currentRecordId;

  if (applyUi && Array.isArray(body.events)) {
    const prevKey = activeEventKey;
    playbackEvents = body.events;
    syncVerifiedKeysFromEvents(playbackEvents, body.event_review);
    applyVerifiedFlagsToEvents();
    if (prevKey && playbackEvents.some((e) => eventRowKey(e) === prevKey)) {
      activeEventKey = prevKey;
    }
  } else if (applyUi && (body.light || body.event_review)) {
    syncVerifiedKeysFromEvents(playbackEvents, body.event_review);
    applyVerifiedFlagsToEvents();
  }

  if (applyUi && !options.skipAutoConfirmBoxes) {
    playbackEvents.forEach((ev) => {
      if (isEventVerified(ev)) {
        applyAutoConfirmedBoxOnVerify(ev);
        applyAutoPersonIdOnVerify(ev);
      }
    });
  }

  if (savedFor) {
    const st =
      body.event_review_status ||
      body.event_review?.status ||
      (body.event_review?.verified_true?.length || body.event_review?.updated_at
        ? "in_progress"
        : null);
    if (st) {
      applyEventReviewPatchFromBody(body, savedFor);
    }
  }

  if (!applyUi) {
    return true;
  }

  currentEventReviewStatus =
    body.event_review_status || body.event_review?.status || currentEventReviewStatus || "in_progress";
  const n =
    typeof body.verified_true_count === "number" ? body.verified_true_count : countVerifiedEvents();
  setEventReviewSaveStatus(options.statusMessage || `已保存 · 标真 ${n} 条`);
  refreshEventCountLabel();
  updateReviewDock();
  if (!options.skipTable && $("#event-review-list-details")?.open) {
    if (options.patchTableOnly) patchEventReviewTableVerifiedStates();
    else renderEventReviewTable();
  }
  if (!options.skipMarkers) {
    if (options.patchMarkersOnly) patchEventMarkersVerifiedStates();
    else renderEventMarkers();
  }
  if (applyUi && typeof redrawCurrentFrame === "function") redrawCurrentFrame();
  return true;
}

/** 单条标真/取消：服务端 toggle，避免全量 verified_true 覆盖误删 */
async function persistEventReviewConfirmedBoxes(ev, confirmedBoxTokens) {
  const recordId = currentRecordId;
  if (!recordId || !ev) return false;
  const tokens = normalizeBoxTokenList(confirmedBoxTokens);
  const eventPayload = eventToReviewPayload(ev);
  delete eventPayload.confirmed_box_tokens;
  const eventTotal = playbackEvents.length;
  const seq = ++eventReviewSaveSeq;
  return runSerializedEventReviewSave(async () => {
    const showUi = recordId === currentRecordId;
    if (showUi) setEventReviewSaveStatus("保存货框确认…", "pending");
    try {
      const res = await fetch(recordApiUrl(recordId, "/event-review"), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: "set_confirmed_box",
          event: eventPayload,
          confirmed_box_tokens: tokens,
          event_total: eventTotal,
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `保存失败 (${res.status})`);
      }
      const body = await res.json();
      const msg = tokens.length
        ? `已确认货框 ${formatConfirmedBoxes(tokens)}`
        : "已清空货框标注";
      return applyEventReviewResponse(body, seq, recordId, {
        statusMessage: msg,
        skipAutoConfirmBoxes: true,
      });
    } catch (err) {
      if (seq !== eventReviewSaveSeq) return false;
      if (recordId === currentRecordId) {
        setEventReviewSaveStatus(err.message || "保存失败", "error");
      }
      return false;
    }
  });
}

async function setConfirmedBoxesForEvent(ev, tokens) {
  if (!ev) return;
  const list = normalizeBoxTokenList(tokens);
  setEventConfirmedBoxes(ev, list);
  updateReviewDock();
  if (typeof updateStageBoxPickMode === "function") updateStageBoxPickMode();
  redrawCurrentFrame();
  if (typeof refreshRangeAnnotTemplateSnapshot === "function") refreshRangeAnnotTemplateSnapshot();
  const detN = normalizeBoxTokenList(ev.box_tokens).length;
  setEventReviewSaveStatus(
    `货框 ${formatConfirmedBoxes(list) || "（无）"}${buildBoxPickStatusHint(ev, list, detN)}`,
    ""
  );
}

async function setPersonIdForEvent(ev, personId) {
  if (!ev) return;
  const ids = getFramePersonIds(ev.frame_idx);
  const normalized =
    personId == null || personId === "" ? null : Number.isFinite(Number(personId)) ? Number(personId) : null;
  if (normalized != null && ids.length && !ids.includes(normalized)) {
    setEventReviewSaveStatus(`person_id ${normalized} 不在当前帧画面中`, "error");
    return;
  }
  setEventPersonId(ev, normalized);
  updateReviewDock();
  redrawCurrentFrame();
  if (typeof refreshRangeAnnotTemplateSnapshot === "function") refreshRangeAnnotTemplateSnapshot();
  const pendingNote = hasPendingPersonIdAnnotation(ev) ? " · 暂选未落盘，按 Y 写入" : "";
  setEventReviewSaveStatus(
    normalized != null
      ? `已选 person_id ${normalized}${pendingNote}`
      : `已清空 person_id${pendingNote}`,
    ""
  );
}

async function resetActiveEventPersonAnnotation() {
  const ev = getPinnedPlaybackEvent();
  if (!ev) {
    setEventReviewSaveStatus("请先选择一条事件", "");
    return;
  }
  setEventPersonId(ev, null);
  updateReviewDock();
  redrawCurrentFrame();
  setEventReviewSaveStatus("已重置 person_id 暂选，按 Y 标为真后写入 event_review", "");
}

async function toggleConfirmedBoxForEvent(ev, token) {
  if (!ev || !token) return;
  const hit = String(token).trim();
  if (!isAnnotationBoxToken(hit)) {
    setEventReviewSaveStatus(`货框 ${hit} 不在标注列表中`, "error");
    return;
  }
  const current = getEventConfirmedBoxes(ev);
  const next = current.includes(hit) ? current.filter((t) => t !== hit) : [...current, hit];
  await setConfirmedBoxesForEvent(ev, next);
}

async function resetActiveEventBoxAnnotation() {
  const ev = getPinnedPlaybackEvent();
  if (!ev) {
    setEventReviewSaveStatus("请先选择一条事件", "");
    return;
  }
  setEventConfirmedBoxes(ev, []);
  updateReviewDock();
  if (typeof updateStageBoxPickMode === "function") updateStageBoxPickMode();
  redrawCurrentFrame();
  setEventReviewSaveStatus("已重置 box 暂选，按 Y 标为真后写入 event_review", "");
}

/** 切换事件时清除上一条的货框点选提示/错误（保存中状态保留） */
let eventReviewStatusEventKey = null;

function clearEventReviewPickStatusOnEventChange() {
  const key = activeEventKey || "";
  if (key === eventReviewStatusEventKey) return;
  const prevKey = eventReviewStatusEventKey;
  eventReviewStatusEventKey = key;
  if (prevKey) {
    pendingConfirmedBoxesByKey.delete(prevKey);
    pendingPersonIdByKey.delete(prevKey);
    if (typeof redrawCurrentFrame === "function") redrawCurrentFrame();
  }
  const el = $("#event-save-status");
  if (el?.classList.contains("is-pending")) return;
  setEventReviewSaveStatus("");
}

/** @deprecated 兼容旧调用 */
async function persistEventReviewConfirmedBox(ev, confirmedBoxToken) {
  const token = String(confirmedBoxToken || "").trim();
  if (!token) return persistEventReviewConfirmedBoxes(ev, []);
  const merged = normalizeBoxTokenList([...getEventConfirmedBoxes(ev), token]);
  return persistEventReviewConfirmedBoxes(ev, merged);
}

/** @deprecated 兼容旧调用 */
async function selectConfirmedBoxForEvent(ev, token) {
  await toggleConfirmedBoxForEvent(ev, token);
}

async function persistEventReviewToggle(ev, wantVerified) {
  const recordId = currentRecordId;
  if (!recordId || !ev) return false;
  const eventPayload = eventToReviewPayload(ev);
  const eventTotal = playbackEvents.length;
  const seq = ++eventReviewSaveSeq;
  return runSerializedEventReviewSave(async () => {
    const showUi = recordId === currentRecordId;
    if (showUi) {
      setEventReviewSaveStatus(`标真 ${countVerifiedEvents()} 条 · 保存中…`, "pending");
    }
    try {
      const res = await fetch(recordApiUrl(recordId, "/event-review"), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: "toggle",
          event: eventPayload,
          verified_true: !!wantVerified,
          event_total: eventTotal,
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `保存失败 (${res.status})`);
      }
      const body = await res.json();
      return applyEventReviewResponse(body, seq, recordId);
    } catch (err) {
      if (seq !== eventReviewSaveSeq) return false;
      if (recordId === currentRecordId) {
        setEventReviewSaveStatus(err.message || "保存失败", "error");
      }
      return false;
    }
  });
}

async function persistEventReviewVerifiedList(verified_true, statusMessage = "保存中…") {
  const recordId = currentRecordId;
  if (!recordId) return false;
  const eventTotal = playbackEvents.length;
  const seq = ++eventReviewSaveSeq;
  return runSerializedEventReviewSave(async () => {
    if (recordId === currentRecordId) {
      setEventReviewSaveStatus(statusMessage, "pending");
    }
    try {
      const res = await fetch(recordApiUrl(recordId, "/event-review"), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          verified_true,
          event_total: eventTotal,
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `保存失败 (${res.status})`);
      }
      const body = await res.json();
      return applyEventReviewResponse(body, seq, recordId);
    } catch (err) {
      if (seq !== eventReviewSaveSeq) return false;
      if (recordId === currentRecordId) {
        setEventReviewSaveStatus(err.message || "保存失败", "error");
      }
      return false;
    }
  });
}

/** 全部标真/取消：轻量 PATCH，服务端构建 verified_true，响应不含 events */
async function persistEventReviewBulkAll(markAll, statusMessage) {
  const recordId = currentRecordId;
  if (!recordId) return false;
  const eventTotal = playbackEvents.length;
  const seq = ++eventReviewSaveSeq;
  const doneMessage = markAll ? `已全部标真 · 共 ${eventTotal} 条` : "已取消全部标真";
  return runSerializedEventReviewSave(async () => {
    if (recordId === currentRecordId) {
      setEventReviewSaveStatus(statusMessage, "pending");
    }
    try {
      const res = await fetch(recordApiUrl(recordId, "/event-review"), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: "set_all_verified",
          mark_all: !!markAll,
          event_total: eventTotal,
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `保存失败 (${res.status})`);
      }
      const body = await res.json();
      return applyEventReviewResponse(body, seq, recordId, {
        patchTableOnly: true,
        patchMarkersOnly: true,
        statusMessage: doneMessage,
      });
    } catch (err) {
      if (seq !== eventReviewSaveSeq) return false;
      if (recordId === currentRecordId) {
        setEventReviewSaveStatus(err.message || "保存失败", "error");
      }
      return false;
    }
  });
}

async function saveEventReviewNow() {
  const verified_true = buildVerifiedTruePayload();
  await persistEventReviewVerifiedList(verified_true);
}

function restoreVerifiedSnapshot(snapshot) {
  verifiedTrueKeys.clear();
  snapshot.forEach((key) => verifiedTrueKeys.add(key));
  applyVerifiedFlagsToEvents();
}

async function markAllEventsVerified(verified) {
  if (!currentRecordId) {
    setEventReviewSaveStatus("导入 JSON 无法保存，请从记录列表打开", "error");
    return;
  }
  const total = playbackEvents.length;
  if (!total) {
    setEventReviewSaveStatus("无事件可操作", "");
    return;
  }

  const verifiedN = countVerifiedEvents();
  if (verified) {
    if (verifiedN >= total) {
      setEventReviewSaveStatus("全部事件已标真", "");
      return;
    }
    if (
      !window.confirm(
        `确定将本记录全部 ${total} 条事件标为真？\n\n确认后将一次性写入服务端；之后仍可用「全部取消标真」或逐条调整。`
      )
    ) {
      return;
    }
  } else {
    if (verifiedN <= 0) {
      setEventReviewSaveStatus("暂无已标真事件", "");
      return;
    }
    if (
      !window.confirm(
        `确定取消本记录全部 ${verifiedN} 条已标真？\n\n确认后将清空标真状态，需重新标真。`
      )
    ) {
      return;
    }
  }

  const snapshot = new Set(verifiedTrueKeys);
  playbackEvents.forEach((ev) => setEventVerified(ev, verified));
  updateReviewDock();
  patchEventReviewVerifiedUi();

  const statusMessage = verified ? `全部标真 ${total} 条 · 保存中…` : "取消全部标真 · 保存中…";
  const ok = await persistEventReviewBulkAll(verified, statusMessage);
  if (!ok) {
    restoreVerifiedSnapshot(snapshot);
    updateReviewDock();
    patchEventReviewVerifiedUi();
  }
}

async function markEventReviewCompleted() {
  const recordId = currentRecordId;
  if (!recordId) {
    setEventReviewSaveStatus("请从记录列表打开回放后再完成复核", "error");
    return;
  }
  const verified_true = buildVerifiedTruePayload();
  const eventTotal = playbackEvents.length;
  const seq = ++eventReviewSaveSeq;
  return runSerializedEventReviewSave(async () => {
    if (recordId === currentRecordId) {
      setEventReviewSaveStatus("正在标记已复核…", "pending");
    }
    try {
      const res = await fetch(recordApiUrl(recordId, "/event-review"), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          status: "completed",
          event_total: eventTotal,
          verified_true,
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `操作失败 (${res.status})`);
      }
      const body = await res.json();
      applyEventReviewResponse(body, seq, recordId);
      if (recordId === currentRecordId) {
        currentEventReviewStatus = body.event_review_status || "completed";
        setEventReviewSaveStatus("已标记为复核完成");
        updateReviewDock();
      }
    } catch (err) {
      setEventReviewSaveStatus(err.message || "操作失败", "error");
    }
  });
}

function filteredPlaybackEvents() {
  const mode = eventFilterSelect?.value || "all";
  if (mode === "all") return playbackEvents;
  if (mode === "verified") return playbackEvents.filter((e) => isEventVerified(e));
  if (mode === "unreviewed") return playbackEvents.filter((e) => !isEventVerified(e));
  if (mode === "needs_box") {
    return playbackEvents.filter(
      (e) => isEventVerified(e) && !getEventConfirmedBoxes(e).length
    );
  }
  if (mode === "alarm" || mode === "collision") {
    return playbackEvents.filter((e) => e.event_type === mode);
  }
  if (mode === "miss") {
    if (
      typeof externalPlaybackAccuracyOverlay !== "undefined" &&
      externalPlaybackAccuracyOverlay &&
      typeof buildMissSegmentQueueEvents === "function"
    ) {
      const fromOverlay = buildMissSegmentQueueEvents();
      if (fromOverlay.length) return fromOverlay;
    }
    const matched =
      typeof isPlaybackEventInMissSegment === "function"
        ? playbackEvents.filter((e) => isPlaybackEventInMissSegment(e))
        : typeof isPlaybackEventMiss === "function"
          ? playbackEvents.filter((e) => isPlaybackEventMiss(e))
          : [];
    if (matched.length) return matched;
    // 无时间线事件命中时，按漏报段起点生成可导航占位事件
    const missSegments =
      typeof getAccuracyGroundTruthSegments === "function"
        ? getAccuracyGroundTruthSegments().filter((seg) => !seg.detected)
        : [];
    if (missSegments.length) {
      return missSegments
        .map((seg) => {
          const fi = Number(seg.frame_start) || 0;
          const existing = playbackEvents.find((e) => (parseInt(e.frame_idx, 10) || 0) === fi);
          if (existing) return existing;
          const row = frameByTime?.find((r) => Number(r.frameIdx) === fi);
          return {
            event_type: "alarm",
            frame_idx: fi,
            timestamp_sec: row?.t ?? (fi - 1) / (Number(poseData?.fps) || 25),
            box_tokens: seg.tokens || [],
          };
        })
        .filter((e) => (parseInt(e.frame_idx, 10) || 0) > 0);
    }
    return matched;
  }
  if (mode === "false_alarm") {
    if (
      typeof externalPlaybackAccuracyOverlay !== "undefined" &&
      externalPlaybackAccuracyOverlay?.allAlarms?.length &&
      typeof buildFalseAlarmQueueEvents === "function"
    ) {
      const fromOverlay = buildFalseAlarmQueueEvents();
      if (fromOverlay.length) return fromOverlay;
    }
    return typeof isPlaybackEventFalseAlarm === "function"
      ? playbackEvents.filter((e) => isPlaybackEventFalseAlarm(e))
      : [];
  }
  return playbackEvents;
}

function getActiveFilteredEvent() {
  const list = filteredPlaybackEvents();
  if (!list.length) return null;
  if (!activeEventKey) return list[0];
  return list.find((e) => eventRowKey(e) === activeEventKey) ?? null;
}

/** 钉住/复核当前事件：完整列表优先，误报/漏报队列占位事件回落到筛选列表 */
function getPinnedPlaybackEvent() {
  return getActiveEvent() ?? getActiveFilteredEvent();
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

function globalIndexForEventKey(key) {
  if (!key || !playbackEvents.length) return -1;
  return playbackEvents.findIndex((e) => eventRowKey(e) === key);
}

/** 按时间线全局顺序切换事件（不受筛选队列影响） */
function navigateReviewEventGlobal(delta, baseGlobalIdx = null) {
  if (!playbackEvents.length) return;
  const cur = baseGlobalIdx != null ? baseGlobalIdx : getActiveGlobalIndex();
  const idx = Math.max(0, Math.min(playbackEvents.length - 1, cur + delta));
  reviewBackKey = null;
  void seekToEvent(playbackEvents[idx]);
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
    idx = getActiveFilteredIndex();
    if (idx < 0 && curKey) {
      idx = list.findIndex((e) => eventRowKey(e) === curKey);
    }
    if (idx < 0) {
      const globalIdx = globalIndexForEventKey(curKey);
      if (globalIdx < 0) {
        idx = delta > 0 ? 0 : list.length - 1;
      } else if (delta > 0) {
        const next = list.find((e) => globalIndexForEventKey(eventRowKey(e)) > globalIdx);
        reviewBackKey = null;
        if (next) void seekToEvent(next);
        return;
      } else {
        let prev = null;
        for (let i = list.length - 1; i >= 0; i -= 1) {
          if (globalIndexForEventKey(eventRowKey(list[i])) < globalIdx) {
            prev = list[i];
            break;
          }
        }
        reviewBackKey = null;
        if (prev) void seekToEvent(prev);
        return;
      }
    }
    const nextIdx = idx + delta;
    if (nextIdx < 0 || nextIdx >= list.length) return;
    reviewBackKey = null;
    void seekToEvent(list[nextIdx]);
    return;
  } else {
    idx = getActiveGlobalIndex();
    if (idx < 0) idx = 0;
    idx = Math.max(0, Math.min(playbackEvents.length - 1, idx + delta));
  }

  reviewBackKey = null;
  void seekToEvent(playbackEvents[idx]);
}

function scrollActiveEventRowIntoView() {
  if (!eventJumpList || !activeEventKey) return;
  const row = eventJumpList.querySelector(`tr[data-event-key="${CSS.escape(activeEventKey)}"]`);
  row?.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

/** 按右栏面板剩余高度限制「全部事件列表」滚动区（details 无法可靠参与 flex 限高） */
let eventReviewListScrollSyncRaf = 0;

function syncEventReviewListScrollHeight() {
  const panel = eventsPanel;
  const details = $("#event-review-list-details");
  const wrap = $("#event-review-list-scroll");
  if (!wrap) return;

  if (!panel || panel.classList.contains("hidden") || !details?.open) {
    wrap.style.removeProperty("max-height");
    wrap.style.removeProperty("overflow-y");
    return;
  }

  const panelRect = panel.getBoundingClientRect();
  const wrapRect = wrap.getBoundingClientRect();
  const available = panelRect.bottom - wrapRect.top - 8;
  wrap.style.maxHeight = `${Math.max(120, Math.floor(available))}px`;
  wrap.style.overflowY = "auto";
}

function scheduleEventReviewListScrollHeight() {
  if (eventReviewListScrollSyncRaf) {
    cancelAnimationFrame(eventReviewListScrollSyncRaf);
  }
  eventReviewListScrollSyncRaf = requestAnimationFrame(() => {
    eventReviewListScrollSyncRaf = requestAnimationFrame(() => {
      eventReviewListScrollSyncRaf = 0;
      syncEventReviewListScrollHeight();
    });
  });
}

function bindEventReviewListScrollSync() {
  const panel = eventsPanel;
  const details = $("#event-review-list-details");
  if (!panel || !details || details.dataset.scrollSyncBound) return;
  details.dataset.scrollSyncBound = "1";

  details.addEventListener("toggle", () => scheduleEventReviewListScrollHeight());
  window.addEventListener("resize", () => scheduleEventReviewListScrollHeight());

  if (typeof ResizeObserver !== "undefined") {
    const ro = new ResizeObserver(() => scheduleEventReviewListScrollHeight());
    ro.observe(panel);
    const dock = $("#event-review-dock");
    if (dock) ro.observe(dock);
  }
}

function updateReviewDock(options = {}) {
  clearEventReviewPickStatusOnEventChange();
  const list = filteredPlaybackEvents();
  const ev = getPinnedPlaybackEvent();
  const evInFilter = ev ? list.some((item) => eventRowKey(item) === eventRowKey(ev)) : false;
  const posEl = $("#event-review-position");
  const badgeEl = $("#event-review-badge");
  const metaEl = $("#event-review-meta");
  const tokensEl = $("#event-review-tokens");
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

  const markAllBtn = $("#event-mark-all-true-btn");
  const unmarkAllBtn = $("#event-unmark-all-btn");
  const totalEvents = playbackEvents.length;
  const verifiedCount = countVerifiedEvents();
  const canBulkSave = !!currentRecordId && totalEvents > 0;
  if (markAllBtn) {
    markAllBtn.disabled = !canBulkSave || verifiedCount >= totalEvents;
  }
  if (unmarkAllBtn) {
    unmarkAllBtn.disabled = !canBulkSave || verifiedCount <= 0;
  }

  const completeBtn = $("#event-review-complete-btn");
  if (completeBtn) {
    const reviewDone = isReviewTerminalStatus(currentEventReviewStatus);
    completeBtn.disabled = reviewDone || !currentRecordId;
    completeBtn.classList.toggle("is-done", reviewDone);
    if (currentEventReviewStatus === "no_collision") {
      completeBtn.textContent = "无碰撞（已复核）";
    } else {
      completeBtn.textContent = reviewDone ? "✓ 已复核完成" : "标记复核完成";
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
    if (tokensEl) {
      tokensEl.textContent = "\u00a0";
      tokensEl.setAttribute("aria-hidden", "true");
    }
    verifiedTag?.classList.add("hidden");
    finishUpdateReviewDock();
    return;
  }

  if (!list.length) {
    if (posEl) posEl.textContent = "队列已清空";
    const mode = eventFilterSelect?.value || "all";
    const emptyHint =
      mode === "miss"
        ? "无漏报段（需有标真范本且段内无匹配告警）"
        : mode === "false_alarm"
          ? "无误报事件（告警均落在标真范本段内）"
          : "当前筛选下无待复核事件";
    if (metaEl) metaEl.textContent = emptyHint;
    if (tokensEl) {
      tokensEl.textContent = "\u00a0";
      tokensEl.setAttribute("aria-hidden", "true");
    }
    verifiedTag?.classList.add("hidden");
    finishUpdateReviewDock();
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

  if (!ev) {
    finishUpdateReviewDock();
    return;
  }
  const typeLabel = ev.event_type === "alarm" ? "告警" : "碰撞";
  if (badgeEl) {
    badgeEl.textContent = typeLabel;
    badgeEl.className = `event-badge ${ev.event_type}`;
  }
  if (metaEl) {
    let accuracyNote = "";
    if (typeof isPlaybackEventFalseAlarm === "function" && isPlaybackEventFalseAlarm(ev)) {
      accuracyNote = " · 误报";
    } else if (
      (typeof isPlaybackEventInMissSegment === "function" && isPlaybackEventInMissSegment(ev)) ||
      (typeof isPlaybackEventMiss === "function" && isPlaybackEventMiss(ev))
    ) {
      accuracyNote = " · 漏报段内";
    }
    const eventFi = parseInt(ev.frame_idx, 10) || 0;
    const playbackFi =
      typeof getResolvedPlaybackFrameIdx === "function" ? getResolvedPlaybackFrameIdx() : null;
    const frameNote =
      playbackFi && eventFi && playbackFi !== eventFi
        ? `画面 帧 ${playbackFi} · 事件 帧 ${eventFi}`
        : `帧 ${eventFi}`;
    metaEl.textContent = `${formatTime(ev.timestamp_sec)} · ${frameNote}${accuracyNote}`;
  }
  if (tokensEl) {
    const tokenText = formatEventTokens(ev.box_tokens);
    const confirmed = getEventConfirmedBoxes(ev);
    const persisted = getEventPersistedConfirmedBoxes(ev);
    let displayText = tokenText;
    if (persisted.length) {
      displayText = `${tokenText} → 已保存 ${formatConfirmedBoxes(persisted)}`;
    }
    if (hasPendingBoxAnnotation(ev)) {
      const pickNote = `暂选 ${formatConfirmedBoxes(confirmed) || "（无）"}`;
      displayText = displayText === tokenText ? pickNote : `${displayText} · ${pickNote}`;
    } else if (!persisted.length && confirmed.length) {
      displayText = `${tokenText} → 已确认 ${formatConfirmedBoxes(confirmed)}`;
    }
    tokensEl.textContent = displayText || "\u00a0";
    tokensEl.setAttribute("aria-hidden", displayText ? "false" : "true");
    if (displayText) tokensEl.title = displayText;
    else tokensEl.removeAttribute("title");
    tokensEl.classList.toggle("is-pending", hasPendingBoxAnnotation(ev));
    tokensEl.classList.toggle("is-confirmed", confirmed.length > 0 && !hasPendingBoxAnnotation(ev));
  }
  if (verifiedTag) {
    verifiedTag.classList.toggle("hidden", !isEventVerified(ev));
  }
  renderEventReviewPersonUi(ev);
  finishUpdateReviewDock(options);
}

function renderEventReviewPersonUi(ev) {
  const wrap = $("#event-review-person-select");
  const optionsEl = $("#event-review-person-options");
  const hintEl = $("#event-review-person-hint");
  if (!wrap || !optionsEl || !hintEl) return;

  if (!ev) {
    wrap.classList.add("hidden");
    optionsEl.innerHTML = "";
    hintEl.textContent = "";
    return;
  }

  const frameIds = getFramePersonIds(ev.frame_idx);
  const selected = getEventPersonId(ev);
  const persisted = getEventPersistedPersonId(ev);

  if (!frameIds.length) {
    wrap.classList.add("hidden");
    optionsEl.innerHTML = "";
    hintEl.textContent = "当前帧无骨架人员，标真时可不写 person_id";
    return;
  }

  wrap.classList.remove("hidden");
  const key = eventRowKey(ev);
  optionsEl.innerHTML = frameIds
    .map((pid) => {
      const checked = selected === pid ? " checked" : "";
      return `<label class="event-review-person-option">
        <input type="radio" name="event-person-${CSS.escape(key)}" value="${pid}"${checked} />
        <span>P${pid}</span>
      </label>`;
    })
    .join("");

  optionsEl.querySelectorAll('input[type="radio"]').forEach((input) => {
    input.addEventListener("change", () => {
      if (!input.checked) return;
      void setPersonIdForEvent(ev, Number(input.value));
    });
  });

  const pendingNote = hasPendingPersonIdAnnotation(ev) ? " · 暂选未落盘" : "";
  const savedNote =
    persisted != null && !hasPendingPersonIdAnnotation(ev) ? ` · 已保存 P${persisted}` : "";
  if (frameIds.length >= 2) {
    hintEl.textContent = selected != null
      ? `本帧 ${frameIds.length} 人 · 已选 P${selected}${savedNote}${pendingNote}`
      : `本帧 ${frameIds.length} 人 · 标真前请选择（或点击骨架标签）${pendingNote}`;
    hintEl.classList.toggle("is-required", selected == null);
  } else {
    hintEl.textContent = `本帧 1 人 · P${frameIds[0]}${selected === frameIds[0] ? "（已选）" : "（标真时自动选中）"}${savedNote}${pendingNote}`;
    hintEl.classList.remove("is-required");
  }
}

function finishUpdateReviewDock(options = {}) {
  if (typeof updateRangeAnnotUi === "function") updateRangeAnnotUi();
  if (typeof invalidatePlaybackAccuracyOverlay === "function") invalidatePlaybackAccuracyOverlay();
  if (typeof updateStageBoxPickMode === "function") updateStageBoxPickMode();
  if (typeof updateEventReviewFrameNavUi === "function") updateEventReviewFrameNavUi();
  if (!options.skipRedraw && typeof redrawCurrentFrame === "function") redrawCurrentFrame();
  const filterMode = eventFilterSelect?.value || "all";
  if (filterMode === "miss" || filterMode === "false_alarm") {
    refreshEventCountLabel();
    if ($("#event-review-list-details")?.open) renderEventReviewTable();
    renderEventMarkers();
  }
  scheduleEventReviewListScrollHeight();
}

function patchEventReviewTableVerifiedStates() {
  if (!eventJumpList) return;
  eventJumpList.querySelectorAll(".event-review-row").forEach((row) => {
    const key = row.dataset.eventKey;
    if (!key) return;
    const isVerified = verifiedTrueKeys.has(key);
    row.classList.toggle("verified-true", isVerified);
    const input = row.querySelector(".event-verify-check");
    if (input) input.checked = isVerified;
  });
}

function patchEventMarkersVerifiedStates() {
  if (!eventMarkersEl) return;
  eventMarkersEl.querySelectorAll(".event-marker").forEach((dot) => {
    const key = dot.dataset.eventKey;
    if (!key) return;
    const ev = playbackEvents.find((item) => eventRowKey(item) === key);
    if (!ev) return;
    const isVerified = isEventVerified(ev);
    dot.classList.toggle("verified", isVerified);
    const verifiedNote = isVerified ? " · 已标真" : "";
    dot.title = `${ev.event_type === "alarm" ? "告警" : "碰撞"} ${formatTime(ev.timestamp_sec)} · ${formatEventTokens(ev.box_tokens)}${verifiedNote}`;
  });
}

function patchEventReviewVerifiedUi() {
  if ($("#event-review-list-details")?.open) patchEventReviewTableVerifiedStates();
  patchEventMarkersVerifiedStates();
  if (typeof redrawCurrentFrame === "function") redrawCurrentFrame();
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
      let accuracyTag = "";
      if (typeof isPlaybackEventFalseAlarm === "function" && isPlaybackEventFalseAlarm(ev)) {
        accuracyTag = '<span class="event-accuracy-tag false-alarm" title="误报">误</span>';
      } else if (
        (typeof isPlaybackEventInMissSegment === "function" && isPlaybackEventInMissSegment(ev)) ||
        (typeof isPlaybackEventMiss === "function" && isPlaybackEventMiss(ev))
      ) {
        accuracyTag = '<span class="event-accuracy-tag miss" title="落在漏报标真段内">漏</span>';
      }
      return `<tr class="event-review-row${active}${verifiedCls}" data-event-key="${key}">
        <td class="col-verify"><input type="checkbox" class="event-verify-check" data-event-key="${key}"${checked}${disabled} aria-label="标为真实碰撞" /></td>
        <td class="col-type"><span class="event-badge ${ev.event_type}">${typeLabel}</span>${accuracyTag}</td>
        <td class="col-time">${formatTime(ev.timestamp_sec)}</td>
        <td class="col-frame">${ev.frame_idx}</td>
        <td class="col-tokens" title="${formatEventTokens(ev.box_tokens)}">${formatEventTokens(ev.box_tokens)}${getEventConfirmedBoxes(ev).length ? ` → ${formatConfirmedBoxes(getEventConfirmedBoxes(ev))}` : ""}</td>
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
        if (want) {
          const check = validatePersonIdBeforeVerify(item);
          if (!check.ok) {
            input.checked = false;
            setEventReviewSaveStatus(check.message, "error");
            updateReviewDock();
            return;
          }
        }
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
  scheduleEventReviewListScrollHeight();
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
  if (verified) {
    const check = validatePersonIdBeforeVerify(ev);
    if (!check.ok) {
      setEventReviewSaveStatus(check.message, "error");
      updateReviewDock();
      return;
    }
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
  const check = validatePersonIdBeforeVerify(ev);
  if (!check.ok) {
    setEventReviewSaveStatus(check.message, "error");
    updateReviewDock();
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

async function unmarkTrueAndNext() {
  const ev = getActiveEvent();
  if (!ev) {
    setEventReviewSaveStatus("请先在列表或进度条上选择一条事件", "");
    return;
  }
  const globalIdx = getActiveGlobalIndex();
  if (isEventVerified(ev)) {
    if (!currentRecordId) {
      setEventReviewSaveStatus("导入 JSON 无法保存，请从记录列表打开", "error");
      return;
    }
    setEventVerified(ev, false);
    updateReviewDock();
    if ($("#event-review-list-details")?.open) renderEventReviewTable();
    renderEventMarkers();
    const ok = await persistEventReviewToggle(ev, false);
    if (!ok) {
      setEventVerified(ev, true);
      updateReviewDock();
      if ($("#event-review-list-details")?.open) renderEventReviewTable();
      renderEventMarkers();
      return;
    }
    reviewBackKey = null;
  } else {
    setEventReviewSaveStatus("当前选中事件未标真", "");
  }
  // 「下一条」按时间线全局顺序，避免在「已标真」等筛选下取消标真后跳到队列首条
  navigateReviewEventGlobal(1, globalIdx);
}

async function skipToNextEvent() {
  navigateReviewEvent(1);
}

async function beginEventReview() {
  if (!playbackEvents.length) return;
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
  if (typeof renderAccuracySeekMarkers === "function") renderAccuracySeekMarkers();
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
  if (typeof updateStageBoxPickMode === "function") updateStageBoxPickMode();
  scheduleEventReviewListScrollHeight();
}

/** @deprecated 兼容旧调用 */
const renderEventJumpList = renderEventReviewList;
