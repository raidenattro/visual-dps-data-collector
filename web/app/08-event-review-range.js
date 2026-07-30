/** 区间增强标真：首帧选 person_id + 货框，首尾均包含并逐帧原子保存 */

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
    if (tid != null && String(tid).trim()) return String(tid).trim();
    return null;
  }
  return null;
}

function eventInRangeFrame(ev, start, end) {
  const fi = parseInt(ev?.frame_idx, 10) || 0;
  return fi >= start && fi <= end;
}

function collectRangeAnnotFrames(start, end) {
  const byFrame = new Map();
  (playbackEvents || []).forEach((ev) => {
    const fi = parseInt(ev?.frame_idx, 10) || 0;
    if (fi >= start && fi <= end && !byFrame.has(fi)) byFrame.set(fi, ev);
  });
  const events = [];
  const missingFrames = [];
  for (let fi = start; fi <= end; fi += 1) {
    const ev = byFrame.get(fi);
    if (ev) events.push(ev);
    else missingFrames.push(fi);
  }
  return { events, missingFrames };
}

function normalizeRangePersonBbox(person) {
  const raw = person?.bbox;
  if (!Array.isArray(raw) || raw.length < 4) return null;
  const values = raw.slice(0, 4).map((value) => Number(value));
  if (values.some((value) => !Number.isFinite(value))) return null;
  const [x1, y1, x2, y2] = values;
  if (x2 <= x1 || y2 <= y1) return null;
  return values;
}

function rangePersonCandidates(frameIdx) {
  const fi = parseInt(frameIdx, 10) || 0;
  const persons = frameCache?.get(fi)?.persons || [];
  return persons.map((person, idx) => {
    const rawPid = person?.person_id != null ? Number(person.person_id) : idx;
    const personId = Number.isFinite(rawPid) ? rawPid : idx;
    const rawTrack = person?.person_track_id;
    return {
      personId,
      trackId: rawTrack != null && String(rawTrack).trim() ? String(rawTrack).trim() : null,
      bbox: normalizeRangePersonBbox(person),
    };
  });
}

function rangeBboxIou(a, b) {
  if (!a || !b) return 0;
  const ix1 = Math.max(a[0], b[0]);
  const iy1 = Math.max(a[1], b[1]);
  const ix2 = Math.min(a[2], b[2]);
  const iy2 = Math.min(a[3], b[3]);
  const intersection = Math.max(0, ix2 - ix1) * Math.max(0, iy2 - iy1);
  const areaA = Math.max(0, a[2] - a[0]) * Math.max(0, a[3] - a[1]);
  const areaB = Math.max(0, b[2] - b[0]) * Math.max(0, b[3] - b[1]);
  const union = areaA + areaB - intersection;
  return union > 0 ? intersection / union : 0;
}

function rangeBboxContinuityScore(previous, candidate) {
  const iou = rangeBboxIou(previous?.bbox, candidate?.bbox);
  if (!previous?.bbox || !candidate?.bbox) {
    return {
      score: previous?.trackId && previous.trackId === candidate?.trackId ? 0.1 : 0,
      iou,
    };
  }
  const prevCx = (previous.bbox[0] + previous.bbox[2]) / 2;
  const prevCy = (previous.bbox[1] + previous.bbox[3]) / 2;
  const nextCx = (candidate.bbox[0] + candidate.bbox[2]) / 2;
  const nextCy = (candidate.bbox[1] + candidate.bbox[3]) / 2;
  const diagonal = Math.max(
    1,
    Math.hypot(previous.bbox[2] - previous.bbox[0], previous.bbox[3] - previous.bbox[1])
  );
  const distance = Math.hypot(nextCx - prevCx, nextCy - prevCy) / diagonal;
  const proximity = Math.max(0, 1 - distance);
  const areaA =
    (previous.bbox[2] - previous.bbox[0]) * (previous.bbox[3] - previous.bbox[1]);
  const areaB =
    (candidate.bbox[2] - candidate.bbox[0]) * (candidate.bbox[3] - candidate.bbox[1]);
  const areaSimilarity = Math.min(areaA, areaB) / Math.max(areaA, areaB, 1);
  const trackBonus =
    previous.trackId != null && previous.trackId === candidate.trackId ? 0.02 : 0;
  return {
    score: iou * 0.72 + proximity * 0.2 + areaSimilarity * 0.08 + trackBonus,
    iou,
  };
}

/**
 * Resolve the same physical person by adjacent-frame bbox continuity.
 * Raw person_id and person_track_id may both swap when duplicate detections
 * overlap, so track is only a weak tie-breaker.
 */
function resolveRangePersonAssignments(start, end, selectedPersonId) {
  const assignments = [];
  const ambiguousFrames = [];
  const missingPersonFrames = [];
  let previous = null;

  for (let fi = start; fi <= end; fi += 1) {
    const candidates = rangePersonCandidates(fi);
    if (!candidates.length) {
      assignments.push({ frameIdx: fi, personId: null, trackId: null });
      missingPersonFrames.push(fi);
      continue;
    }

    let chosen = null;
    if (fi === start) {
      chosen = candidates.find(
        (candidate) => Number(candidate.personId) === Number(selectedPersonId)
      );
      if (!chosen && candidates.length === 1) chosen = candidates[0];
    } else if (previous && candidates.length === 1) {
      chosen = candidates[0];
    } else if (previous) {
      const ranked = candidates
        .map((candidate) => ({
          candidate,
          ...rangeBboxContinuityScore(previous, candidate),
        }))
        .sort((a, b) => b.score - a.score);
      const best = ranked[0];
      const second = ranked[1];
      const margin = second ? best.score - second.score : best.score;
      const iouSeparation = second ? best.iou - second.iou : best.iou;
      if (
        best.iou >= 0.2 &&
        (!second || margin >= 0.12 || iouSeparation >= 0.15)
      ) {
        chosen = best.candidate;
      }
    } else if (candidates.length === 1) {
      chosen = candidates[0];
    }

    if (!chosen) {
      ambiguousFrames.push(fi);
      break;
    }
    assignments.push({
      frameIdx: fi,
      personId: chosen.personId,
      trackId: chosen.trackId,
    });
    previous = chosen;
  }
  return { assignments, ambiguousFrames, missingPersonFrames };
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
    return { ok: false, message: `首帧 ${startFrame} 缺少帧数据，无法区间标真` };
  }

  let confirmed = normalizeBoxTokenList(template.confirmed);
  if (!confirmed.length) {
    return { ok: false, message: "请先在首帧人工点选确认货框" };
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

  let hint = "在首帧选择 person_id 与货框，设置尾帧后一键标真区间内全部帧";
  let canApply = false;
  let previewN = 0;

  if (bounds) {
    refreshRangeAnnotTemplateSnapshot();
    const template = getRangeAnnotTemplateForApply(bounds.start);
    const check = validateRangeAnnotTemplate(template, bounds.start);
    const collected = check.ok
      ? collectRangeAnnotFrames(bounds.start, bounds.end)
      : { events: [], missingFrames: [] };
    previewN = collected.events.length;
    const expectedN = bounds.end - bounds.start + 1;
    canApply =
      check.ok &&
      previewN === expectedN &&
      collected.missingFrames.length === 0 &&
      !!currentRecordId;

    if (!check.ok) {
      hint = check.message;
    } else if (collected.missingFrames.length) {
      const sample = collected.missingFrames.slice(0, 5).join(", ");
      hint = `区间帧数据不完整，缺少 ${sample}${collected.missingFrames.length > 5 ? " …" : ""}`;
    } else {
      const personNote =
        check.personId != null
          ? ` · 人员 P${check.personId}${check.trackId != null ? `（track ${check.trackId}）` : ""}`
          : "";
      hint = `帧 ${bounds.start}–${bounds.end}（含首尾）· 将逐帧标真 ${previewN} 帧 · 货框 ${formatConfirmedBoxes(check.confirmed)}${personNote}`;
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
    applyBtn.textContent = previewN > 0 ? `区间标真（${previewN} 帧）` : "区间标真";
  }
}

function buildRangeAnnotEventPayload(ev, check, assignment) {
  const frameIdx = parseInt(ev?.frame_idx, 10) || 0;
  const payload = {
    event_type: String(ev?.event_type || "frame").trim() || "frame",
    frame_idx: frameIdx,
    source_frame_idx: parseInt(ev?.source_frame_idx ?? frameIdx, 10) || frameIdx,
    box_tokens: normalizeBoxTokenList(ev?.box_tokens),
    confirmed_box_tokens: [...check.confirmed],
  };
  if (assignment?.personId != null) payload.person_id = Number(assignment.personId);
  if (assignment?.trackId != null && String(assignment.trackId).trim()) {
    payload.person_track_id = String(assignment.trackId).trim();
  }
  return payload;
}

function savedRangePayloadMatches(review, payloads) {
  const byFrame = new Map();
  (review?.verified_true || []).forEach((item) => {
    const fi = parseInt(item?.frame_idx, 10) || 0;
    if (fi > 0) byFrame.set(fi, item);
  });
  const missingFrames = [];
  payloads.forEach((payload) => {
    const saved = byFrame.get(payload.frame_idx);
    const expectedBoxes = normalizeBoxTokenList(payload.confirmed_box_tokens);
    const bindings = normalizeReviewBindings(saved?.bindings);
    const matched = bindings.some((binding) => {
      if (
        payload.person_track_id != null &&
        String(binding.person_track_id ?? "") !== String(payload.person_track_id)
      ) {
        return false;
      }
      if (
        payload.person_track_id == null &&
        payload.person_id != null &&
        Number(binding.person_id) !== Number(payload.person_id)
      ) {
        return false;
      }
      const actualBoxes = normalizeBoxTokenList(binding.confirmed_box_tokens);
      return (
        actualBoxes.length === expectedBoxes.length &&
        expectedBoxes.every((token) => actualBoxes.includes(token))
      );
    });
    if (!matched) missingFrames.push(payload.frame_idx);
  });
  return missingFrames;
}

async function persistEventReviewRange(payloads, bounds, statusMessage) {
  const recordId = currentRecordId;
  if (!recordId || !payloads.length) return false;
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
          action: "set_range_verified",
          range_start: bounds.start,
          range_end: bounds.end,
          events: payloads,
          event_total: eventTotal,
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `区间保存失败 (${res.status})`);
      }
      const body = await res.json();
      const expectedN = bounds.end - bounds.start + 1;
      if (Number(body.range_applied_count) !== expectedN) {
        throw new Error(
          `区间保存数量异常：应保存 ${expectedN} 帧，服务端返回 ${body.range_applied_count ?? 0} 帧`
        );
      }
      const missingFrames = savedRangePayloadMatches(body.event_review, payloads);
      if (missingFrames.length) {
        throw new Error(
          `区间保存后校验失败，缺少帧 ${missingFrames.slice(0, 8).join(", ")}${
            missingFrames.length > 8 ? " …" : ""
          }`
        );
      }
      return applyEventReviewResponse(body, seq, recordId, {
        skipAutoConfirmBoxes: true,
        statusMessage: `区间标真完成 · 帧 ${bounds.start}–${bounds.end} · 共 ${expectedN} 帧`,
      });
    } catch (err) {
      if (seq !== eventReviewSaveSeq) return false;
      if (recordId === currentRecordId) {
        setEventReviewSaveStatus(err.message || "区间保存失败", "error");
      }
      return false;
    }
  });
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

  const collected = collectRangeAnnotFrames(bounds.start, bounds.end);
  if (collected.missingFrames.length) {
    const sample = collected.missingFrames.slice(0, 8).join(", ");
    setEventReviewSaveStatus(
      `区间帧数据不完整，缺少 ${sample}${collected.missingFrames.length > 8 ? " …" : ""}`,
      "error"
    );
    updateRangeAnnotUi();
    return;
  }
  const events = collected.events;
  const expectedN = bounds.end - bounds.start + 1;
  if (events.length !== expectedN) {
    setEventReviewSaveStatus(
      `区间帧数异常：应有 ${expectedN} 帧，实际找到 ${events.length} 帧`,
      "error"
    );
    return;
  }

  const personResolution = resolveRangePersonAssignments(
    bounds.start,
    bounds.end,
    check.personId
  );
  if (personResolution.ambiguousFrames.length) {
    const sample = personResolution.ambiguousFrames.slice(0, 5).join(", ");
    setEventReviewSaveStatus(
      `检测到人员身份歧义，已停止且未保存。请人工检查帧：${sample}`,
      "error"
    );
    return;
  }
  const assignmentByFrame = new Map(
    personResolution.assignments.map((assignment) => [assignment.frameIdx, assignment])
  );
  const payloads = events.map((ev) =>
    buildRangeAnnotEventPayload(
      ev,
      check,
      assignmentByFrame.get(parseInt(ev.frame_idx, 10) || 0)
    )
  );

  const personNote = check.personId != null ? ` · P${check.personId}` : "";
  const missingPersonWarning = personResolution.missingPersonFrames.length
    ? `\n人员提示：${personResolution.missingPersonFrames.length} 帧没有人体检测，将保留逐帧货框标真但不填写 person_id。`
    : "";
  if (
    !window.confirm(
      `确定区间逐帧标真？\n\n帧范围：${bounds.start} – ${bounds.end}（含首尾）\n帧数：${expectedN}\n货框：${formatConfirmedBoxes(check.confirmed)}${personNote}\n\n人员将按相邻帧人体框连续性对应；track 仅作辅助，不会盲目跟随换到另一个人。${missingPersonWarning}`
    )
  ) {
    return;
  }

  const ok = await persistEventReviewRange(
    payloads,
    bounds,
    `区间逐帧标真 ${expectedN} 帧 · 原子保存中…`
  );
  if (ok) {
    updateRangeAnnotUi();
  }
}
