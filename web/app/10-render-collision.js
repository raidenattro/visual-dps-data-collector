/** 画布渲染、货框叠加与碰撞追踪 */

function clearVideoElement() {
  stopPlayback();
  clearPlaybackEvents();
  videoEl.pause();
  videoEl.removeAttribute("src");
  videoEl.load();
  videoEl.style.display = "block";
  ctx.clearRect(0, 0, canvas.width, canvas.height);
}

function boxCollisionToken(box) {
  const id = String(box.box_id ?? box.id ?? "").trim();
  if (!id) return "";
  return `Box_${id}`;
}

/** 同一货位的多种 token 写法（Box_id 与 shelf:id）用于复核高亮查找 */
function boxTokenLookupKeys(token) {
  const t = String(token || "").trim();
  if (!t) return [];
  const keys = new Set([t]);
  let boxId = "";
  if (t.startsWith("Box_")) {
    boxId = t.slice(4).trim();
  } else if (t.includes(":")) {
    boxId = t.split(":").pop().trim();
    if (boxId) keys.add(`Box_${boxId}`);
  }
  if (boxId) {
    for (const box of annotationBoxes) {
      const bid = String(box.box_id ?? box.id ?? "").trim();
      if (bid && bid === boxId) {
        const canon = boxCollisionToken(box);
        if (canon) keys.add(canon);
      }
    }
  }
  return [...keys];
}

/** 采集落盘碰撞 token 与当前货框 token 可能格式不同（Box_id vs shelf:id） */
function tokenInCollisionSet(token, set) {
  if (!token || !set?.size) return false;
  for (const key of boxTokenLookupKeys(token)) {
    if (set.has(key)) return true;
  }
  return false;
}

function tokenInTokenMap(token, map) {
  if (!token || !map?.size) return false;
  for (const key of boxTokenLookupKeys(token)) {
    if (map.get(key)) return true;
  }
  return false;
}

function boxIdsFromTokenList(tokens) {
  const ids = new Set();
  (tokens || []).forEach((raw) => {
    const id = parseBoxIdFromToken(raw);
    if (id) ids.add(id);
  });
  return ids;
}

function eventTypeFrameKey(ev) {
  return `${String(ev?.event_type || "").trim()}:${parseInt(ev?.frame_idx, 10) || 0}`;
}

function reviewEntryBoxIds(entry) {
  const ids = boxIdsFromTokenList(entry?.box_tokens);
  const confirmed =
    typeof normalizeBoxTokenList === "function"
      ? normalizeBoxTokenList(
          entry?.confirmed_box_tokens ||
            (entry?.confirmed_box_token ? [entry.confirmed_box_token] : [])
        )
      : [];
  confirmed.forEach((t) => {
    const id = parseBoxIdFromToken(t);
    if (id) ids.add(id);
  });
  return ids;
}

function eventMatchesReviewEntry(ev, entry) {
  if (!ev || !entry) return false;
  if (eventTypeFrameKey(ev) !== eventTypeFrameKey(entry)) return false;
  const evIds = boxIdsFromTokenList(ev?.box_tokens);
  const revIds = reviewEntryBoxIds(entry);
  for (const id of evIds) {
    if (revIds.has(id)) return true;
  }
  return false;
}

/** 与准确率 build_ground_truth_segments 一致：连续相同范本货框合并为段 */
function buildVerifiedGroundTruthSegments() {
  if (!playbackEvents?.length) return [];
  const entries = playbackEvents
    .filter((ev) => typeof isEventVerified === "function" && isEventVerified(ev))
    .map((ev) => {
      const confirmed =
        typeof getEventConfirmedBoxes === "function" ? getEventConfirmedBoxes(ev) : [];
      const tokens = canonicalizeBoxTokenList(
        confirmed.length > 0
          ? confirmed
          : typeof normalizeBoxTokenList === "function"
            ? normalizeBoxTokenList(ev?.box_tokens)
            : ev?.box_tokens
      );
      return {
        frame: parseInt(ev.frame_idx, 10) || 0,
        tokens: tokens.filter(Boolean),
      };
    })
    .filter((e) => e.tokens.length);
  entries.sort((a, b) => a.frame - b.frame);

  const tokenKey = (tokens) => canonicalizeBoxTokenList(tokens).join(",");

  const segments = [];
  let current = null;
  for (const entry of entries) {
    const key = tokenKey(entry.tokens);
    if (current && current.key === key) {
      current.frame_end = Math.max(current.frame_end, entry.frame);
      current.entry_count += 1;
    } else {
      if (current) segments.push(current);
      current = {
        key,
        tokens: entry.tokens,
        frame_start: entry.frame,
        frame_end: entry.frame,
        entry_count: 1,
      };
    }
  }
  if (current) segments.push(current);
  return segments;
}

function collisionTokensEquivalent(a, b) {
  const x = String(a || "").trim();
  const y = String(b || "").trim();
  if (!x || !y) return false;
  if (x === y) return true;
  const xi = parseBoxIdFromToken(x);
  const yi = parseBoxIdFromToken(y);
  return !!(xi && yi && xi === yi);
}

function tokenMatchesAnyList(token, candidates) {
  return (candidates || []).some((c) => collisionTokensEquivalent(token, c));
}

function tokenInTokenSet(token, set) {
  if (!token || !set?.size) return false;
  for (const key of boxTokenLookupKeys(token)) {
    if (set.has(key)) return true;
  }
  return false;
}

function addTokenKeysToSet(token, set) {
  for (const key of boxTokenLookupKeys(token)) {
    set.add(key);
  }
}

let playbackAccuracyOverlayCache = null;
let externalPlaybackAccuracyOverlay = null;
/** 漏报/误报 GT 段缓存（避免筛选/渲染时反复全量比对告警列表） */
let accuracyGroundTruthSegmentsCache = null;

const MAX_SEEK_ACCURACY_DOTS = 120;

function invalidateAccuracyGroundTruthSegmentsCache() {
  accuracyGroundTruthSegmentsCache = null;
}

/** 将评估 overlay 的 [[frame, token], ...] 建成按帧索引 */
function buildEvalOverlayFrameIndex(alarms, collisions) {
  const byFrame = new Map();
  const ensure = (fi) => {
    if (!byFrame.has(fi)) {
      byFrame.set(fi, { alarmSet: new Set(), collisionSet: new Set() });
    }
    return byFrame.get(fi);
  };
  const addToken = (set, token) => {
    const t = String(token || "").trim();
    if (!t) return;
    for (const key of boxTokenLookupKeys(t)) {
      set.add(key);
    }
  };
  (alarms || []).forEach((row) => {
    if (!Array.isArray(row) || row.length < 2) return;
    const fi = parseInt(row[0], 10) || 0;
    if (fi <= 0) return;
    addToken(ensure(fi).alarmSet, row[1]);
  });
  (collisions || []).forEach((row) => {
    if (!Array.isArray(row) || row.length < 2) return;
    const fi = parseInt(row[0], 10) || 0;
    if (fi <= 0) return;
    addToken(ensure(fi).collisionSet, row[1]);
  });
  return byFrame;
}

function normalizeExternalPlaybackOverlay(raw) {
  if (!raw || typeof raw !== "object") return null;
  const segments = (raw.segments || []).map((seg) => ({
    frame_start: Number(seg.frame_start) || 0,
    frame_end: Number(seg.frame_end) || 0,
    tokens: Array.isArray(seg.tokens)
      ? seg.tokens.map((t) => String(t).trim()).filter(Boolean)
      : canonicalizeBoxTokenList(seg.gt_tokens),
    detected: Boolean(seg.detected),
  }));
  const allAlarms = [];
  (raw.alarms || []).forEach((row) => {
    if (!Array.isArray(row) || row.length < 2) return;
    const fi = parseInt(row[0], 10) || 0;
    const token = String(row[1] || "").trim();
    if (fi > 0) allAlarms.push([fi, token]);
  });
  const allCollisions = [];
  (raw.collisions || []).forEach((row) => {
    if (!Array.isArray(row) || row.length < 2) return;
    const fi = parseInt(row[0], 10) || 0;
    const token = String(row[1] || "").trim();
    if (fi > 0 && token) allCollisions.push([fi, token]);
  });
  if (!segments.length && !allAlarms.length && !allCollisions.length) return null;

  const countsRaw = raw.counts && typeof raw.counts === "object" ? raw.counts : {};
  let falseAlarms = Number(countsRaw.false_alarms);
  if (!Number.isFinite(falseAlarms)) {
    falseAlarms = 0;
    allAlarms.forEach(([frame, token]) => {
      const fi = Number(frame) || 0;
      const covered = segments.some(
        (seg) =>
          fi >= seg.frame_start &&
          fi <= seg.frame_end &&
          tokenMatchesAnyList(token, seg.tokens)
      );
      if (!covered) falseAlarms += 1;
    });
  }
  const missedSegments = Number(countsRaw.missed_segments);
  const counts = {
    alarms: Number.isFinite(Number(countsRaw.alarms)) ? Number(countsRaw.alarms) : allAlarms.length,
    collisions: Number.isFinite(Number(countsRaw.collisions))
      ? Number(countsRaw.collisions)
      : allCollisions.length,
    verified: Number.isFinite(Number(countsRaw.verified)) ? Number(countsRaw.verified) : 0,
    missed_segments: Number.isFinite(missedSegments)
      ? missedSegments
      : segments.filter((seg) => !seg.detected).length,
    false_alarms: falseAlarms,
  };

  return {
    segments,
    allAlarms,
    allCollisions,
    evalFrameIndex: buildEvalOverlayFrameIndex(allAlarms, allCollisions),
    source_label: String(raw.source_label || "").trim(),
    counts,
    useEvalCollisions: true,
  };
}

function setExternalPlaybackAccuracyOverlay(overlay) {
  externalPlaybackAccuracyOverlay = overlay ? normalizeExternalPlaybackOverlay(overlay) : null;
  invalidateAccuracyGroundTruthSegmentsCache();
  playbackAccuracyOverlayCache = externalPlaybackAccuracyOverlay;
  if (typeof renderAccuracySeekMarkers === "function") renderAccuracySeekMarkers();
  if (typeof refreshEventCountLabel === "function") refreshEventCountLabel();
  if (typeof redrawCurrentFrame === "function") redrawCurrentFrame();
}

function clearExternalPlaybackAccuracyOverlay() {
  externalPlaybackAccuracyOverlay = null;
  invalidatePlaybackAccuracyOverlay();
  if (typeof refreshEventCountLabel === "function") refreshEventCountLabel();
}

window.setExternalPlaybackAccuracyOverlay = setExternalPlaybackAccuracyOverlay;
window.clearExternalPlaybackAccuracyOverlay = clearExternalPlaybackAccuracyOverlay;

/** 准确率评估跳转时 overlay 携带的来源统计（与评估结果一致） */
function getPlaybackAccuracyEvalCounts() {
  if (!externalPlaybackAccuracyOverlay?.counts) return null;
  return {
    sourceLabel: externalPlaybackAccuracyOverlay.source_label || "",
    alarms: externalPlaybackAccuracyOverlay.counts.alarms ?? 0,
    collisions: externalPlaybackAccuracyOverlay.counts.collisions ?? 0,
    verified: externalPlaybackAccuracyOverlay.counts.verified ?? 0,
    missed_segments: externalPlaybackAccuracyOverlay.counts.missed_segments ?? 0,
    false_alarms: externalPlaybackAccuracyOverlay.counts.false_alarms ?? 0,
  };
}

window.getPlaybackAccuracyEvalCounts = getPlaybackAccuracyEvalCounts;

function invalidatePlaybackAccuracyOverlay() {
  invalidateAccuracyGroundTruthSegmentsCache();
  playbackAccuracyOverlayCache = externalPlaybackAccuracyOverlay || null;
  if (!playbackAccuracyOverlayCache && typeof buildPlaybackAccuracyOverlayData === "function") {
    playbackAccuracyOverlayCache = null;
  }
  if (typeof renderAccuracySeekMarkers === "function") renderAccuracySeekMarkers();
}

function collectPlaybackAlarmIndex() {
  const allAlarms = [];
  (playbackEvents || []).forEach((ev) => {
    if (String(ev.event_type || "").trim() !== "alarm") return;
    const fi = parseInt(ev.frame_idx, 10) || 0;
    const tokens =
      typeof normalizeBoxTokenList === "function"
        ? normalizeBoxTokenList(ev.box_tokens)
        : (ev.box_tokens || []).map((t) => String(t).trim()).filter(Boolean);
    tokens.forEach((token) => allAlarms.push([fi, token]));
  });
  return allAlarms;
}

function segmentHasMatchingAlarm(seg, allAlarms) {
  return allAlarms.some(
    ([frame, token]) =>
      frame >= seg.frame_start && frame <= seg.frame_end && tokenMatchesAnyList(token, seg.tokens)
  );
}

function accuracySegmentKey(seg) {
  const tokens = canonicalizeBoxTokenList(seg.tokens || []).join(",");
  return `${Number(seg.frame_start) || 0}-${Number(seg.frame_end) || 0}\0${tokens}`;
}

/** 漏报/误报对比用 GT 段：优先 event_review 标真段，detected 状态来自评估 overlay */
function computeAccuracyGroundTruthSegments() {
  const reviewSegs = buildVerifiedGroundTruthSegments();
  if (!reviewSegs.length) {
    return externalPlaybackAccuracyOverlay?.segments?.length
      ? externalPlaybackAccuracyOverlay.segments
      : [];
  }
  if (!externalPlaybackAccuracyOverlay?.segments?.length) {
    const allAlarms = collectPlaybackAlarmIndex();
    return reviewSegs.map((seg) => ({
      ...seg,
      detected: segmentHasMatchingAlarm(seg, allAlarms),
    }));
  }
  const detectedByKey = new Map();
  externalPlaybackAccuracyOverlay.segments.forEach((seg) => {
    detectedByKey.set(accuracySegmentKey(seg), Boolean(seg.detected));
  });
  const evalAlarms = externalPlaybackAccuracyOverlay.allAlarms || [];
  return reviewSegs.map((seg) => ({
    ...seg,
    detected: detectedByKey.has(accuracySegmentKey(seg))
      ? detectedByKey.get(accuracySegmentKey(seg))
      : segmentHasMatchingAlarm(seg, evalAlarms),
  }));
}

function getAccuracyGroundTruthSegments() {
  if (!accuracyGroundTruthSegmentsCache) {
    accuracyGroundTruthSegmentsCache = computeAccuracyGroundTruthSegments();
  }
  return accuracyGroundTruthSegmentsCache;
}

/** 与准确率 evaluate_segments 一致：标真范本段 + 全记录告警索引 */
function buildPlaybackAccuracyOverlayData() {
  if (!playbackEvents?.length || !annotationBoxes.length) return null;

  const segments = buildVerifiedGroundTruthSegments();
  if (!segments.length) return null;

  const allAlarms = collectPlaybackAlarmIndex();
  return {
    segments: segments.map((seg) => ({
      ...seg,
      detected: segmentHasMatchingAlarm(seg, allAlarms),
    })),
    allAlarms,
  };
}

function buildPlaybackAccuracyOverlay() {
  return buildPlaybackAccuracyOverlayData();
}

function getPlaybackAccuracyOverlay() {
  if (externalPlaybackAccuracyOverlay) {
    const gtSegments = getAccuracyGroundTruthSegments();
    return {
      ...externalPlaybackAccuracyOverlay,
      segments: gtSegments.length ? gtSegments : externalPlaybackAccuracyOverlay.segments,
    };
  }
  if (!playbackAccuracyOverlayCache) {
    playbackAccuracyOverlayCache = buildPlaybackAccuracyOverlayData();
  }
  return playbackAccuracyOverlayCache;
}

function sampleSeekMarkerFrames(frames, maxDots = MAX_SEEK_ACCURACY_DOTS) {
  const sorted = [...new Set(frames)].filter((f) => f > 0).sort((a, b) => a - b);
  if (sorted.length <= maxDots) return sorted;
  const out = [];
  const step = sorted.length / maxDots;
  for (let i = 0; i < maxDots; i += 1) {
    out.push(sorted[Math.floor(i * step)]);
  }
  return out;
}

/** 漏报 / 误报帧号（与画面黑/白描边规则一致） */
function collectAccuracySeekMarkerFrames() {
  const overlay = getPlaybackAccuracyOverlay();
  if (!overlay) return { missFrames: [], falseAlarmFrames: [] };

  const missFrames = [];
  overlay.segments.forEach((seg) => {
    if (seg.detected) return;
    // 与准确率一致：每段漏报只打一个定位点（段起点）
    missFrames.push(seg.frame_start);
  });

  const falseAlarmFrames = [];
  overlay.allAlarms.forEach(([frame, token]) => {
    if (!isOverlayFalseAlarmEntry(frame, token, overlay)) return;
    falseAlarmFrames.push(Number(frame) || 0);
  });

  return {
    missFrames: sampleSeekMarkerFrames(missFrames),
    falseAlarmFrames: sampleSeekMarkerFrames(falseAlarmFrames),
  };
}

function frameIdxToSeekPct(frameIdx) {
  const dur =
    typeof getPlaybackDurationSec === "function" ? getPlaybackDurationSec() : 0;
  if (!dur || !frameByTime?.length) return null;
  const fi = Number(frameIdx) || 0;
  const row = frameByTime.find((r) => Number(r.frameIdx) === fi);
  if (row && Number.isFinite(row.t)) {
    return Math.min(100, Math.max(0, (row.t / dur) * 100));
  }
  const idx = frameByTime.findIndex((r) => Number(r.frameIdx) === fi);
  if (idx < 0) return null;
  return Math.min(100, Math.max(0, (idx / frameByTime.length) * 100));
}

function renderAccuracySeekMarkers() {
  if (!accuracyMarkersEl) return;
  accuracyMarkersEl.innerHTML = "";

  const { missFrames, falseAlarmFrames } = collectAccuracySeekMarkerFrames();
  if (!missFrames.length && !falseAlarmFrames.length) return;

  const appendDot = (frameIdx, kind, title) => {
    const pct = frameIdxToSeekPct(frameIdx);
    if (pct == null) return;
    const dot = document.createElement("button");
    dot.type = "button";
    dot.className = `accuracy-marker ${kind}`;
    dot.dataset.frameIdx = String(frameIdx);
    dot.style.left = `${pct}%`;
    dot.title = title;
    dot.addEventListener("click", (e) => {
      e.stopPropagation();
      const fi = parseInt(dot.dataset.frameIdx, 10) || 0;
      const row = frameByTime.find((r) => Number(r.frameIdx) === fi);
      if (row && typeof seekToTimestamp === "function") {
        void seekToTimestamp(row.t, fi, { skipEventSync: false });
      }
    });
    accuracyMarkersEl.appendChild(dot);
  };

  missFrames.forEach((fi) => {
    appendDot(fi, "miss", `漏报段 · 帧 ${fi}`);
  });
  falseAlarmFrames.forEach((fi) => {
    appendDot(fi, "false-alarm", `误报 · 帧 ${fi}`);
  });
}

/** 当前帧漏报（黑描边）/ 误报（白描边）货框 token 集合 */
function getAccuracyOutlineForFrame(frameIdx, alarmSet) {
  const empty = { missTokens: new Set(), falseAlarmTokens: new Set() };
  const overlay = getPlaybackAccuracyOverlay();
  if (!overlay) return empty;

  const fi = Number(frameIdx) || 0;
  if (fi <= 0) return empty;

  const missTokens = new Set();
  overlay.segments.forEach((seg) => {
    if (seg.detected || fi < seg.frame_start || fi > seg.frame_end) return;
    seg.tokens.forEach((token) => addTokenKeysToSet(token, missTokens));
  });

  const falseAlarmTokens = new Set();
  if (externalPlaybackAccuracyOverlay?.allAlarms?.length) {
    externalPlaybackAccuracyOverlay.allAlarms.forEach(([frame, token]) => {
      const afi = Number(frame) || 0;
      if (afi !== fi) return;
      const tok = String(token || "").trim();
      if (!tok) return;
      const covered = overlay.segments.some(
        (seg) =>
          fi >= seg.frame_start &&
          fi <= seg.frame_end &&
          tokenMatchesAnyList(tok, seg.tokens)
      );
      if (!covered) addTokenKeysToSet(tok, falseAlarmTokens);
    });
  } else if (alarmSet?.size) {
    alarmSet.forEach((token) => {
      const covered = overlay.segments.some(
        (seg) =>
          fi >= seg.frame_start &&
          fi <= seg.frame_end &&
          tokenMatchesAnyList(token, seg.tokens)
      );
      if (!covered) addTokenKeysToSet(token, falseAlarmTokens);
    });
  }

  return { missTokens, falseAlarmTokens };
}

/** 事件范本 token（与 buildVerifiedGroundTruthSegments 一致） */
function eventGroundTruthTokens(ev) {
  if (!ev) return [];
  const confirmed =
    typeof getEventConfirmedBoxes === "function" ? getEventConfirmedBoxes(ev) : [];
  if (confirmed.length) return canonicalizeBoxTokenList(confirmed);
  return typeof normalizeBoxTokenList === "function"
    ? normalizeBoxTokenList(ev.box_tokens)
    : canonicalizeBoxTokenList(ev.box_tokens);
}

/** 已标真且落在未检出范本段内（段内事件，不等同于多计漏报） */
function isPlaybackEventInMissSegment(ev) {
  if (!ev) return false;
  const overlay = getPlaybackAccuracyOverlay();
  if (!overlay?.segments?.length) return false;
  const fi = parseInt(ev.frame_idx, 10) || 0;
  if (fi <= 0) return false;
  const tokens = eventGroundTruthTokens(ev);

  const inMissSeg = overlay.segments.some((seg) => {
    if (seg.detected) return false;
    if (fi < seg.frame_start || fi > seg.frame_end) return false;
    if (ev._accuracy_miss_placeholder) {
      return (
        fi === seg.frame_start && tokens.some((t) => tokenMatchesAnyList(t, seg.tokens))
      );
    }
    return tokens.some((t) => tokenMatchesAnyList(t, seg.tokens));
  });
  if (!inMissSeg) return false;

  if (ev._accuracy_miss_placeholder) return true;

  // 上传推测评估跳转：漏报段内事件不要求已标真
  if (externalPlaybackAccuracyOverlay) return true;

  if (typeof isEventVerified !== "function" || !isEventVerified(ev)) return false;
  return true;
}

/** 评估 overlay 中该告警是否为误报（不在任标真范本段内） */
function isOverlayFalseAlarmEntry(frame, token, overlay = null) {
  const ov = overlay || getPlaybackAccuracyOverlay();
  if (!ov?.segments?.length) return false;
  const fi = Number(frame) || 0;
  const tok = String(token || "").trim();
  if (fi <= 0 || !tok) return false;
  const covered = ov.segments.some(
    (seg) =>
      fi >= seg.frame_start &&
      fi <= seg.frame_end &&
      tokenMatchesAnyList(tok, seg.tokens)
  );
  return !covered;
}

/** 从评估 overlay 构建误报导航队列（与误报计数/白描边规则一致） */
function buildFalseAlarmQueueEvents() {
  const overlay = externalPlaybackAccuracyOverlay;
  if (!overlay?.allAlarms?.length) return [];

  const items = [];
  const seen = new Set();

  overlay.allAlarms.forEach(([frame, token]) => {
    if (!isOverlayFalseAlarmEntry(frame, token, overlay)) return;

    const fi = Number(frame) || 0;
    const tok = String(token || "").trim();
    const dedupeKey = `${fi}\0${tok}`;
    if (seen.has(dedupeKey)) return;
    seen.add(dedupeKey);

    const existing = (playbackEvents || []).find((e) => {
      if (String(e.event_type || "").trim() !== "alarm") return false;
      if ((parseInt(e.frame_idx, 10) || 0) !== fi) return false;
      const tokens =
        typeof normalizeBoxTokenList === "function"
          ? normalizeBoxTokenList(e.box_tokens)
          : canonicalizeBoxTokenList(e.box_tokens);
      return tokenMatchesAnyList(tok, tokens);
    });
    if (existing) {
      items.push(existing);
      return;
    }

    const row = frameByTime?.find((r) => Number(r.frameIdx) === fi);
    items.push({
      event_type: "alarm",
      frame_idx: fi,
      timestamp_sec: row?.t ?? (fi - 1) / (Number(poseData?.fps) || 25),
      box_tokens: [tok],
    });
  });

  items.sort(
    (a, b) =>
      (Number(a.timestamp_sec) || 0) - (Number(b.timestamp_sec) || 0) ||
      (parseInt(a.frame_idx, 10) || 0) - (parseInt(b.frame_idx, 10) || 0)
  );
  return items;
}

/** 从评估漏报段构建导航队列（每段一条，与误报队列一致，避免扫全量时间线事件） */
function buildMissSegmentQueueEvents() {
  const segments = getAccuracyGroundTruthSegments().filter((seg) => !seg.detected);
  if (!segments.length) return [];

  const items = [];
  const seen = new Set();

  segments.forEach((seg) => {
    const fi = Number(seg.frame_start) || 0;
    if (fi <= 0) return;
    const tokenKey = canonicalizeBoxTokenList(seg.tokens || []).join(",");
    const dedupeKey = `${fi}\0${tokenKey}`;
    if (seen.has(dedupeKey)) return;
    seen.add(dedupeKey);

    const existing = (playbackEvents || []).find((e) => {
      if ((parseInt(e.frame_idx, 10) || 0) !== fi) return false;
      if (typeof isEventVerified === "function" && !isEventVerified(e)) return false;
      const tokens = eventGroundTruthTokens(e);
      return tokens.some((t) => tokenMatchesAnyList(t, seg.tokens));
    });
    if (existing) {
      items.push(existing);
      return;
    }

    const row = frameByTime?.find((r) => Number(r.frameIdx) === fi);
    items.push({
      event_type: "alarm",
      frame_idx: fi,
      timestamp_sec: row?.t ?? (fi - 1) / (Number(poseData?.fps) || 25),
      box_tokens: seg.tokens || [],
      _accuracy_miss_placeholder: true,
    });
  });

  items.sort(
    (a, b) =>
      (Number(a.timestamp_sec) || 0) - (Number(b.timestamp_sec) || 0) ||
      (parseInt(a.frame_idx, 10) || 0) - (parseInt(b.frame_idx, 10) || 0)
  );
  return items;
}

/** 告警不在任标真范本段内 → 误报 */
function isPlaybackEventFalseAlarm(ev) {
  if (!ev || String(ev.event_type || "").trim() !== "alarm") return false;
  const overlay = getPlaybackAccuracyOverlay();
  if (!overlay?.segments?.length) return false;
  const fi = parseInt(ev.frame_idx, 10) || 0;
  if (fi <= 0) return false;
  const tokens =
    typeof normalizeBoxTokenList === "function"
      ? normalizeBoxTokenList(ev.box_tokens)
      : canonicalizeBoxTokenList(ev.box_tokens);
  if (!tokens.length) return false;

  // 上传推测评估：误报须与 overlay 告警列表中未覆盖标真段的条目对应
  if (externalPlaybackAccuracyOverlay?.allAlarms?.length) {
    return tokens.some((token) => {
      const inOverlay = externalPlaybackAccuracyOverlay.allAlarms.some(
        ([af, at]) =>
          Number(af) === fi &&
          (tokenMatchesAnyList(token, [at]) || tokenMatchesAnyList(at, [token]))
      );
      if (!inOverlay) return false;
      const covered = overlay.segments.some(
        (seg) =>
          fi >= seg.frame_start &&
          fi <= seg.frame_end &&
          tokenMatchesAnyList(token, seg.tokens)
      );
      return !covered;
    });
  }

  return tokens.some((token) => {
    const covered = overlay.segments.some(
      (seg) =>
        fi >= seg.frame_start &&
        fi <= seg.frame_end &&
        tokenMatchesAnyList(token, seg.tokens)
    );
    return !covered;
  });
}

/** 与准确率 evaluate_segments 一致：未检出标真段数（每段最多记 1 次漏报） */
function countPlaybackMissSegments() {
  const overlay = getPlaybackAccuracyOverlay();
  if (!overlay?.segments?.length) return 0;
  return overlay.segments.filter((seg) => !seg.detected).length;
}

function countPlaybackMissEvents() {
  return countPlaybackMissSegments();
}

/** @deprecated 使用 isPlaybackEventInMissSegment */
function isPlaybackEventMiss(ev) {
  return isPlaybackEventInMissSegment(ev);
}

function countPlaybackFalseAlarmEvents() {
  if (externalPlaybackAccuracyOverlay?.counts) {
    return externalPlaybackAccuracyOverlay.counts.false_alarms ?? 0;
  }
  return (playbackEvents || []).filter((ev) => isPlaybackEventFalseAlarm(ev)).length;
}

/** 射线法：点是否在多边形内（推理坐标系） */
function pointInPolygon(point, polygon) {
  if (!Array.isArray(polygon) || polygon.length < 3) return false;
  const x = Number(point[0]);
  const y = Number(point[1]);
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
    const xi = Number(polygon[i][0]);
    const yi = Number(polygon[i][1]);
    const xj = Number(polygon[j][0]);
    const yj = Number(polygon[j][1]);
    const intersect = yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi || 1e-12) + xi;
    if (intersect) inside = !inside;
  }
  return inside;
}

/** 回放时实时碰撞（与 event_engine/collision 逻辑一致：手腕 9/10，score>0.3） */
class PlaybackCollisionTracker {
  constructor(minConsecutive = 3, cooldownFrames = 6) {
    this.minConsecutive = Math.max(1, minConsecutive);
    this.cooldownFrames = Math.max(1, cooldownFrames);
    this.consecutiveHits = new Map();
    this.lastAlarmFrame = new Map();
    this.boxCacheKey = "";
    this.boxCache = [];
  }

  reset() {
    this.consecutiveHits.clear();
    this.lastAlarmFrame.clear();
    this.boxCacheKey = "";
    this.boxCache = [];
  }

  getBoxesInInferSpace(inferW, inferH) {
    const key = `${inferW}x${inferH}:${annotationBoxes.length}:${annotationSize?.width || 0}x${annotationSize?.height || 0}`;
    if (key === this.boxCacheKey) return this.boxCache;
    const pl = window.previewLayout;
    if (!pl?.resolvePolygonFramePoints) {
      this.boxCache = [];
      this.boxCacheKey = key;
      return this.boxCache;
    }
    const { frameW, frameH } = getVideoFrameSize();
    const annSize = getEffectiveAnnotationSize();
    const f0 = frameByTime[0];
    const boxesAlreadyInfer =
      f0 &&
      annSize?.width === f0.w &&
      annSize?.height === f0.h &&
      annotationBoxes.every((b) => {
        let mx = 0;
        let my = 0;
        (b.video_polygon || []).forEach((pt) => {
          mx = Math.max(mx, Number(pt[0]) || 0);
          my = Math.max(my, Number(pt[1]) || 0);
        });
        return mx <= inferW * 1.05 && my <= inferH * 1.05;
      });

    this.boxCache = annotationBoxes
      .map((box) => {
        const poly = box.video_polygon;
        if (!Array.isArray(poly) || poly.length < 3) return null;
        let inferPts;
        if (boxesAlreadyInfer) {
          inferPts = poly.map((pt) => [Number(pt[0]), Number(pt[1])]);
        } else {
          const framePts = pl.resolvePolygonFramePoints(
            poly,
            box.video_polygon_norm,
            annSize,
            frameW,
            frameH
          );
          if (framePts.length < 3) return null;
          inferPts = framePts.map(([x, y]) => [
            (x * inferW) / Math.max(1, frameW),
            (y * inferH) / Math.max(1, frameH),
          ]);
        }
        const token = boxCollisionToken(box);
        return token ? { token, inferPts } : null;
      })
      .filter(Boolean);
    this.boxCacheKey = key;
    return this.boxCache;
  }

  update(frame, inferW, inferH) {
    const boxes = this.getBoxesInInferSpace(inferW, inferH);
    if (!boxes.length) return { collisions: [], alarm_collisions: [] };

    const frameIdx = Number(frame?.frame_idx ?? frame?.source_frame_idx ?? 0);
    const active = new Set();

    (frame?.persons || []).forEach((person) => {
      const kpts = person?.keypoints || [];
      for (const idx of [9, 10]) {
        const kp = kpts[idx];
        if (!kp || kp.length < 3 || Number(kp[2]) <= 0.3) continue;
        const wx = Number(kp[0]);
        const wy = Number(kp[1]);
        for (const { token, inferPts } of boxes) {
          if (pointInPolygon([wx, wy], inferPts)) {
            active.add(token);
            break;
          }
        }
      }
    });

    for (const token of this.consecutiveHits.keys()) {
      if (!active.has(token)) this.consecutiveHits.set(token, 0);
    }

    const alarms = [];
    active.forEach((token) => {
      const next = (this.consecutiveHits.get(token) || 0) + 1;
      this.consecutiveHits.set(token, next);
      const last = this.lastAlarmFrame.get(token) ?? -1e9;
      if (next >= this.minConsecutive && frameIdx - last >= this.cooldownFrames) {
        alarms.push(token);
        this.lastAlarmFrame.set(token, frameIdx);
      }
    });

    return {
      collisions: [...active],
      alarm_collisions: alarms,
    };
  }
}

let playbackCollisionTracker = null;

function resetPlaybackCollisionTracker() {
  playbackCollisionTracker = null;
  invalidatePlaybackAccuracyOverlay();
}

function getPlaybackCollisionTracker() {
  if (!playbackCollisionTracker) {
    const cfg = getEffectiveCollisionConfig();
    playbackCollisionTracker = new PlaybackCollisionTracker(
      cfg.alarm_min_consecutive_frames,
      cfg.alarm_cooldown_frames
    );
  }
  return playbackCollisionTracker;
}

/** 准确率跳转：按评估 overlay 取当前帧碰撞/告警（黄/红） */
function getEvalCollisionSetsForFrame(frameIdx) {
  if (!externalPlaybackAccuracyOverlay?.useEvalCollisions) return null;
  const fi = parseInt(frameIdx, 10) || 0;
  if (!fi) return null;
  const row = externalPlaybackAccuracyOverlay.evalFrameIndex?.get(fi);
  return {
    collisionSet: row ? new Set(row.collisionSet) : new Set(),
    alarmSet: row ? new Set(row.alarmSet) : new Set(),
  };
}

function getFrameCollisionSets(frame, inferW, inferH) {
  const fi = Number(frame?.frame_idx) || Number(frame?.source_frame_idx) || 0;
  const evalSets = getEvalCollisionSetsForFrame(fi);
  if (evalSets) return evalSets;

  if (frameUsesStoredCollisions(frame)) {
    return {
      collisionSet: new Set(frame.collisions || []),
      alarmSet: new Set(frame.alarm_collisions || []),
    };
  }
  if (!annotationBoxes.length) {
    return { collisionSet: new Set(), alarmSet: new Set() };
  }
  const computed = getPlaybackCollisionTracker().update(frame, inferW, inferH);
  return {
    collisionSet: new Set(computed.collisions),
    alarmSet: new Set(computed.alarm_collisions),
  };
}

function getEffectiveAnnotationSize() {
  const { frameW, frameH } = getVideoFrameSize();
  let size = annotationSize;
  if (!size?.width || !size?.height) {
    if (frameByTime.length) {
      size = { width: frameByTime[0].w, height: frameByTime[0].h };
    } else {
      size = { width: frameW, height: frameH };
    }
  }
  // 旧版 pose JSON：boxes 已缩放到推理分辨率，但 annotation_size 仍是原始标注尺寸
  if (frameByTime.length && annotationBoxes.length && size?.width && size?.height) {
    const f0 = frameByTime[0];
    let maxX = 0;
    let maxY = 0;
    annotationBoxes.forEach((box) => {
      (box.video_polygon || []).forEach((pt) => {
        maxX = Math.max(maxX, Number(pt[0]) || 0);
        maxY = Math.max(maxY, Number(pt[1]) || 0);
      });
    });
    if (
      maxX <= f0.w * 1.05 &&
      maxY <= f0.h * 1.05 &&
      (size.width > f0.w * 1.15 || size.height > f0.h * 1.15)
    ) {
      return { width: f0.w, height: f0.h };
    }
  }
  return size;
}

function syncAnnotationBoxesFromPose() {
  const ann = poseData?.annotation;
  annotationBoxes = Array.isArray(ann?.boxes) ? ann.boxes : [];
  annotationSize = ann?.annotation_size || null;
  invalidateAnnotationDisplayCache();
}

function loadAnnotationBoxesFromData(data) {
  if (Array.isArray(data?.annotation?.boxes)) {
    annotationBoxes = data.annotation.boxes;
    annotationSize = data.annotation.annotation_size || data.annotation_size || null;
    resetPlaybackCollisionTracker();
    invalidateAnnotationDisplayCache();
    return;
  }
  annotationSize = data?.annotation_size || null;
  // 同时存在顶层 boxes 与 shelves 时优先 shelves（visual-dps 规范），避免 legacy boxes 与模型层格式 token 不一致
  if (Array.isArray(data?.shelves) && data.shelves.length) {
    annotationBoxes = [];
    data.shelves.forEach((shelf) => {
      const code = String(shelf?.shelf_code || "").trim();
      (shelf?.boxes || []).forEach((b) => {
        annotationBoxes.push({ ...b, shelf_code: b.shelf_code || code });
      });
    });
    resetPlaybackCollisionTracker();
    invalidateAnnotationDisplayCache();
    return;
  }
  if (Array.isArray(data?.boxes)) {
    annotationBoxes = data.boxes;
    resetPlaybackCollisionTracker();
    invalidateAnnotationDisplayCache();
    return;
  }
  annotationBoxes = [];
  resetPlaybackCollisionTracker();
  invalidateAnnotationDisplayCache();
}

async function loadAnnotationBoxesFromFile(file) {
  const data = JSON.parse(await file.text());
  loadAnnotationBoxesFromData(data);
}

/** 将 timeline 行写入 frameByTime（v2 分包回放索引用） */
function applyTimelineRowsToFrameIndex(rows, inferW, inferH) {
  frameByTime = [];
  (rows || []).forEach((row) => {
    const fi = Number(row.frame_idx) || 0;
    if (!fi) return;
    frameByTime.push({
      t: Number(row.timestamp_sec) || 0,
      frameIdx: fi,
      w: Number(row.infer_width) || inferW,
      h: Number(row.infer_height) || inferH,
      floor_xy_m: Array.isArray(row.floor_xy_m) ? row.floor_xy_m : null,
      foot_uv_px: Array.isArray(row.foot_uv_px) ? row.foot_uv_px : null,
    });
  });
  frameByTime.sort((a, b) => a.t - b.t);
}

/** frame_count 已知时合成 1..N 密集时间轴（事件帧可能与稀疏 timeline 行不一致） */
function buildSyntheticFrameIndexFromManifest() {
  const total = Number(poseData?.frame_count) || 0;
  const fps = Number(poseData?.fps) || 15;
  if (!total) return false;

  const inferW = poseData.infer_width || 640;
  const inferH = poseData.infer_height || 480;
  frameByTime = [];
  for (let i = 1; i <= total; i += 1) {
    frameByTime.push({
      t: (i - 1) / fps,
      frameIdx: i,
      w: inferW,
      h: inferH,
    });
  }
  return frameByTime.length > 0;
}

/** 稀疏 timeline 加载后补齐 1..frame_count，避免事件 frame_idx 无对应行 */
function densifyFrameIndexFromManifest() {
  const total = Number(poseData?.frame_count) || 0;
  if (!total || !frameByTime.length) return;
  const fps = Number(poseData?.fps) || 15;
  const inferW = poseData.infer_width || frameByTime[0]?.w || poseData.infer_width || 640;
  const inferH = poseData.infer_height || frameByTime[0]?.h || poseData.infer_height || 480;
  const existing = new Set(frameByTime.map((e) => e.frameIdx));
  for (let i = 1; i <= total; i += 1) {
    if (existing.has(i)) continue;
    frameByTime.push({ t: (i - 1) / fps, frameIdx: i, w: inferW, h: inferH });
  }
  frameByTime.sort((a, b) => a.t - b.t);
}

/** 确保 frame_idx 在时间轴中有条目（必要时虚拟补齐） */
function ensureFrameIndexEntry(frameIdx) {
  const fi = parseInt(frameIdx, 10) || 0;
  if (!fi) return null;
  if (frameByTime?.length) {
    const hit = frameByTime.find((e) => e.frameIdx === fi) || null;
    if (hit) return hit;
  }
  const fps = Number(poseData?.fps) || 25;
  const inferW = poseData?.infer_width || frameByTime?.[0]?.w || 640;
  const inferH = poseData?.infer_height || frameByTime?.[0]?.h || 480;
  const hit = { t: (fi - 1) / fps, frameIdx: fi, w: inferW, h: inferH };
  if (!frameByTime) frameByTime = [];
  frameByTime.push(hit);
  frameByTime.sort((a, b) => a.t - b.t);
  return hit;
}

function buildFrameIndex(recordId = null) {
  frameByTime = [];
  resetFrameFetchState();
  resetPlaybackCollisionTracker();
  syncAnnotationBoxesFromPose();
  if (!poseData) return Promise.resolve();

  if ((poseData.schema || 1) >= 2 && recordId) {
    return fetch(`${recordApiUrl(recordId, "/timeline")}?light=1`)
      .then((res) => (res.ok ? res.json() : { timeline: [] }))
      .then((body) => {
        const tl = body.timeline || [];
        let inferW = Number(poseData.infer_width) || 0;
        let inferH = Number(poseData.infer_height) || 0;
        if ((!inferW || !inferH) && tl.length) {
          inferW = Number(tl[0].infer_width) || inferW;
          inferH = Number(tl[0].infer_height) || inferH;
        }
        if ((!inferW || !inferH) && playbackSpatialContext) {
          inferW = Number(playbackSpatialContext.infer_width) || inferW;
          inferH = Number(playbackSpatialContext.infer_height) || inferH;
        }
        if (!inferW || !inferH) {
          inferW = 852;
          inferH = 480;
        }
        poseData.infer_width = inferW;
        poseData.infer_height = inferH;

        if (buildSyntheticFrameIndexFromManifest()) {
          if (typeof renderAccuracySeekMarkers === "function") renderAccuracySeekMarkers();
          return;
        }
        applyTimelineRowsToFrameIndex(tl, inferW, inferH);
        densifyFrameIndexFromManifest();
        if (typeof renderAccuracySeekMarkers === "function") renderAccuracySeekMarkers();
      });
  }

  if (!poseData?.frames?.length) {
    if (typeof renderAccuracySeekMarkers === "function") renderAccuracySeekMarkers();
    return Promise.resolve();
  }
  poseData.frames.forEach((f) => {
    frameByTime.push({
      t: f.timestamp_sec ?? 0,
      frameIdx: f.frame_idx,
      frame: f,
      w: f.infer_width || 640,
      h: f.infer_height || 480,
    });
    if (f.frame_idx != null) frameCache.set(f.frame_idx, f);
  });
  frameByTime.sort((a, b) => a.t - b.t);
  if (typeof renderAccuracySeekMarkers === "function") renderAccuracySeekMarkers();
  playbackSkeletonReady =
    frameCache.size >= (Number(poseData?.frame_count) || frameByTime.length || 0);
  return Promise.resolve();
}

function findFrameAt(timeSec) {
  if (!frameByTime.length) return null;
  const t = Math.max(0, Number(timeSec) || 0);
  if (t <= frameByTime[0].t) return frameByTime[0];
  const last = frameByTime[frameByTime.length - 1];
  if (t >= last.t) return last;
  let lo = 0;
  let hi = frameByTime.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (frameByTime[mid].t <= t) lo = mid + 1;
    else hi = mid - 1;
  }
  return frameByTime[Math.max(0, hi)];
}

function syncCanvasSize(opts = {}) {
  const force = opts.force === true;
  if (!force && playbackRenderLoopActive && frozenPlaybackCanvasCss) {
    return frozenPlaybackCanvasCss;
  }
  const wrap = stageWrap || document.querySelector(".stage-wrap");
  if (!wrap) return { cw: 1, ch: 1 };
  if (!force && canvas._layoutCssW > 0 && canvas._layoutCssH > 0) {
    return { cw: canvas._layoutCssW, ch: canvas._layoutCssH };
  }
  const rect = wrap.getBoundingClientRect();
  const dpr = playbackRenderLoopActive ? 1 : window.devicePixelRatio || 1;
  const cssW = Math.max(1, Math.floor(rect.width));
  const cssH = Math.max(1, Math.floor(rect.height));
  canvas._layoutCssW = cssW;
  canvas._layoutCssH = cssH;
  canvas.width = Math.floor(cssW * dpr);
  canvas.height = Math.floor(cssH * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const out = { cw: cssW, ch: cssH };
  if (playbackRenderLoopActive) frozenPlaybackCanvasCss = out;
  return out;
}

/** 回放/复核时标真范本货框高亮（含连续标真片段覆盖的帧范围） */
function getReviewBoxHighlightContext(frameIdx = null) {
  if (!playbackEvents?.length || !annotationBoxes.length) return null;
  if (!eventsPanel || eventsPanel.classList.contains("hidden")) return null;

  const resolvedFi =
    typeof getResolvedPlaybackFrameIdx === "function" ? getResolvedPlaybackFrameIdx() : null;
  const rawFi =
    frameIdx != null && Number(frameIdx) > 0
      ? Number(frameIdx)
      : resolvedFi != null && resolvedFi > 0
        ? resolvedFi
        : lastRenderedFrameIdx >= 1
          ? lastRenderedFrameIdx
          : typeof getCurrentPlaybackFrameIdx === "function"
            ? getCurrentPlaybackFrameIdx()
            : null;
  const fi =
    typeof playbackOverlayFrameIdx === "function"
      ? playbackOverlayFrameIdx(rawFi) ?? rawFi
      : rawFi;
  // 未钉住事件时，标真段高亮跟随画面帧，不用事件帧覆盖
  const segmentFi =
    playbackEventLinkExact && fi != null && fi > 0
      ? fi
      : rawFi != null && rawFi > 0
        ? rawFi
        : fi;

  const confirmedByToken = new Map();

  if (segmentFi != null && segmentFi > 0) {
    for (const seg of buildVerifiedGroundTruthSegments()) {
      if (segmentFi < seg.frame_start || segmentFi > seg.frame_end) continue;
      seg.tokens.forEach((token) => {
        for (const key of boxTokenLookupKeys(token)) {
          confirmedByToken.set(key, true);
        }
      });
    }
  }

  const activeEv =
    (typeof getActiveEvent === "function" ? getActiveEvent() : null) ??
    (typeof getActiveFilteredEvent === "function" ? getActiveFilteredEvent() : null);
  if (activeEv) {
    const playbackFi =
      resolvedFi != null && resolvedFi > 0
        ? resolvedFi
        : lastRenderedFrameIdx >= 1
          ? lastRenderedFrameIdx
          : typeof getCurrentPlaybackFrameIdx === "function"
            ? getCurrentPlaybackFrameIdx()
            : fi;
    // 仅当画面帧与事件帧一致，或显式事件跳转钉住时，才叠加选中事件的范本紫色
    const includeActiveConfirmed =
      playbackEventLinkExact ||
      (typeof eventMatchesPlaybackFrame === "function" &&
        playbackFi != null &&
        eventMatchesPlaybackFrame(activeEv, playbackFi));
    if (includeActiveConfirmed) {
      const boxes =
        typeof getEventConfirmedBoxes === "function" ? getEventConfirmedBoxes(activeEv) : [];
      boxes.forEach((token) => {
        for (const key of boxTokenLookupKeys(token)) {
          confirmedByToken.set(key, true);
        }
      });
    }
  }

  if (!confirmedByToken.size) return null;

  return { confirmedByToken };
}

function collectAnnotationDisplayPolygons() {
  return getAnnotationDisplayCache();
}

/** 画面坐标点击命中货框（返回 box token，自上而下取最上层） */
function hitTestAnnotationBoxAtClient(clientX, clientY) {
  const rect = canvas.getBoundingClientRect();
  const x = clientX - rect.left;
  const y = clientY - rect.top;
  const polys = collectAnnotationDisplayPolygons();
  for (let i = polys.length - 1; i >= 0; i--) {
    if (pointInPolygon([x, y], polys[i].displayPts)) return polys[i].token;
  }
  return null;
}

function updateStageBoxPickMode() {
  const wrap = stageWrap || document.querySelector(".playback-layout-main .stage-wrap");
  if (!wrap) return;
  const canPick =
    annotationBoxes.length > 0 &&
    eventsPanel &&
    !eventsPanel.classList.contains("hidden") &&
    playbackEvents.length > 0;
  wrap.classList.toggle("stage-wrap--box-pick", canPick);
}

let annotationDisplayCacheKey = "";
/** @type {{ token: string, displayPts: number[][] }[]} */
let annotationDisplayCache = [];
/** 播放时预烘焙的静态货框层（避免每帧重绘 30+ 个框） */
let playbackStaticLayerCanvas = null;

function invalidateAnnotationDisplayCache() {
  annotationDisplayCacheKey = "";
  annotationDisplayCache = [];
  playbackStaticLayerCanvas = null;
}

/** 货框显示多边形（layout/标注不变时可复用，避免每帧重算坐标） */
function getAnnotationDisplayCache() {
  if (!annotationBoxes.length) return [];
  const pl = window.previewLayout;
  if (!pl?.resolvePolygonFramePoints || !pl?.mapPointToDisplay) return [];

  const { frameW, frameH } = getVideoFrameSize();
  const layout =
    frozenPlaybackLayout && playbackRenderLoopActive
      ? frozenPlaybackLayout
      : getDisplayLayout();
  const annSize = getEffectiveAnnotationSize();
  const layoutKey =
    typeof displayLayoutCacheKey === "function" ? displayLayoutCacheKey() : "";
  const key = `${layoutKey}:${annSize?.width || 0}x${annSize?.height || 0}:${annotationBoxes.length}`;
  if (key === annotationDisplayCacheKey && annotationDisplayCache.length) {
    return annotationDisplayCache;
  }

  const next = [];
  annotationBoxes.forEach((box) => {
    const poly = box.video_polygon;
    if (!Array.isArray(poly) || poly.length < 3) return;
    const framePts = pl.resolvePolygonFramePoints(
      poly,
      box.video_polygon_norm,
      annSize,
      frameW,
      frameH
    );
    if (framePts.length < 3) return;
    const token = boxCollisionToken(box);
    if (!token) return;
    const displayPts = framePts.map(([x, y]) => pl.mapPointToDisplay(x, y, layout));
    next.push({ token, displayPts });
  });
  annotationDisplayCacheKey = key;
  annotationDisplayCache = next;
  return annotationDisplayCache;
}

/** 解析 person.bbox（推理坐标 [x1,y1,x2,y2]） */
function personDetBbox(person) {
  const b = person?.bbox;
  if (!Array.isArray(b) || b.length < 4) return null;
  const nums = b.map((v) => Number(v));
  if (nums.some((v) => !Number.isFinite(v))) return null;
  return nums;
}

/** RTMDet 人体检测框（虚线矩形，与货框/骨架区分） */
function drawDetBboxes(frame, inferW, inferH) {
  if (!showDetBbox || !frame?.persons?.length) return;

  const layout = getDisplayLayout();
  ctx.save();
  ctx.lineWidth = 2;
  ctx.strokeStyle = "rgba(251, 146, 60, 0.92)";
  ctx.setLineDash([7, 5]);

  frame.persons.forEach((person, idx) => {
    const b = personDetBbox(person);
    if (!b) return;
    const [x1, y1] = mapInferToDisplay(b[0], b[1], inferW, inferH, layout);
    const [x2, y2] = mapInferToDisplay(b[2], b[3], inferW, inferH, layout);
    const left = Math.min(x1, x2);
    const top = Math.min(y1, y2);
    ctx.strokeRect(left, top, Math.abs(x2 - x1), Math.abs(y2 - y1));

    const label = person?.person_id != null ? `#${person.person_id}` : `#${idx}`;
    ctx.setLineDash([]);
    ctx.font = "12px system-ui, sans-serif";
    ctx.fillStyle = "rgba(251, 146, 60, 0.95)";
    ctx.fillText(label, left + 3, Math.max(14, top - 4));
    ctx.setLineDash([7, 5]);
  });

  ctx.restore();
}

function collisionSetsForPlaybackFrame(frame, inferW, inferH) {
  const fi = Number(frame?.frame_idx) || Number(frame?.source_frame_idx) || 0;
  const evalSets = getEvalCollisionSetsForFrame(fi);
  if (evalSets) return evalSets;

  if (frameUsesStoredCollisions(frame)) {
    return {
      collisionSet: new Set(frame.collisions || []),
      alarmSet: new Set(frame.alarm_collisions || []),
    };
  }
  // 播放热路径不实时算碰撞（避免每帧跑 CollisionProcessor）
  return { collisionSet: new Set(), alarmSet: new Set() };
}

/** 播放轻量模式：仅绘制当前帧碰撞/告警货框 */
function drawAnnotationBoxesCollisionOnly(frame, inferW, inferH, collisionSets) {
  if (!annotationBoxes.length || !collisionSets) return;
  const { collisionSet, alarmSet } = collisionSets;
  if (!collisionSet.size && !alarmSet.size) return;

  getAnnotationDisplayCache().forEach(({ token, displayPts }) => {
    const isAlarm = tokenInCollisionSet(token, alarmSet);
    const isHit = tokenInCollisionSet(token, collisionSet);
    if (!isAlarm && !isHit) return;

    ctx.beginPath();
    displayPts.forEach(([dx, dy], i) => {
      if (i === 0) ctx.moveTo(dx, dy);
      else ctx.lineTo(dx, dy);
    });
    ctx.closePath();
    ctx.setLineDash([]);
    ctx.lineWidth = isAlarm ? 2.5 : 2;
    ctx.strokeStyle = isAlarm ? "rgba(255, 71, 87, 0.95)" : "rgba(255, 209, 102, 0.95)";
    ctx.stroke();
  });
}

/** 播放时静态货框（淡绿描边，不算碰撞） */
function drawAnnotationBoxesStatic() {
  if (!annotationBoxes.length) return;
  const cache = getAnnotationDisplayCache();
  if (!cache.length) return;
  ctx.setLineDash([]);
  ctx.lineWidth = 1.5;
  ctx.strokeStyle = "rgba(0, 255, 0, 0.35)";
  cache.forEach(({ displayPts }) => {
    ctx.beginPath();
    displayPts.forEach(([dx, dy], i) => {
      if (i === 0) ctx.moveTo(dx, dy);
      else ctx.lineTo(dx, dy);
    });
    ctx.closePath();
    ctx.stroke();
  });
}

function drawAnnotationBoxes(frame, inferW, inferH, collisionSets = null, reviewCtx = null) {
  if (!annotationBoxes.length) return;

  const { collisionSet, alarmSet } =
    collisionSets || getFrameCollisionSets(frame, inferW, inferH);
  const rawFrameIdx =
    lastRenderedFrameIdx >= 1
      ? lastRenderedFrameIdx
      : Number(frame?.frame_idx) || Number(frame?.source_frame_idx) || 0;
  const frameIdx =
    typeof playbackOverlayFrameIdx === "function"
      ? playbackOverlayFrameIdx(rawFrameIdx) ?? rawFrameIdx
      : rawFrameIdx;
  reviewCtx = reviewCtx ?? getReviewBoxHighlightContext(frameIdx);
  const { missTokens, falseAlarmTokens } = getAccuracyOutlineForFrame(frameIdx, alarmSet);

  getAnnotationDisplayCache().forEach(({ token, displayPts }) => {
    const isAlarm = tokenInCollisionSet(token, alarmSet);
    const isHit = tokenInCollisionSet(token, collisionSet);
    const isManuallyConfirmed = tokenInTokenMap(token, reviewCtx?.confirmedByToken);
    const isMiss = tokenInTokenSet(token, missTokens);
    const isFalseAlarm = tokenInTokenSet(token, falseAlarmTokens);

    ctx.beginPath();
    displayPts.forEach(([dx, dy], i) => {
      if (i === 0) ctx.moveTo(dx, dy);
      else ctx.lineTo(dx, dy);
    });
    ctx.closePath();

    if (isManuallyConfirmed) {
      ctx.fillStyle = "rgba(168, 85, 247, 0.32)";
      ctx.fill();
    }

    ctx.setLineDash([]);
    ctx.shadowBlur = 0;
    ctx.shadowColor = "transparent";
    ctx.lineWidth = 3;

    if (isFalseAlarm) {
      ctx.strokeStyle = "rgba(0, 0, 0, 0.9)";
      ctx.lineWidth = 4.5;
      ctx.stroke();
      ctx.strokeStyle = "rgba(255, 255, 255, 0.98)";
      ctx.lineWidth = 2.5;
      ctx.stroke();
    } else if (isMiss) {
      ctx.strokeStyle = "rgba(0, 0, 0, 0.95)";
      ctx.lineWidth = 3;
      ctx.stroke();
    } else if (isAlarm) {
      ctx.strokeStyle = "rgba(255, 71, 87, 0.95)";
      ctx.lineWidth = 2.5;
      ctx.stroke();
    } else if (isHit) {
      ctx.strokeStyle = "rgba(255, 209, 102, 0.95)";
      ctx.lineWidth = 2.5;
      ctx.stroke();
    } else {
      ctx.strokeStyle = "rgba(0, 255, 0, 0.35)";
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }
  });
}

/** 由视频时间直接换算帧号（与 manifest fps 对齐，O(1)） */
function frameIdxAtVideoTime(timeSec) {
  const fps = Number(poseData?.fps) || 25;
  const total = Number(poseData?.frame_count) || frameByTime.length || 0;
  if (!total) return 0;
  const idx = Math.floor(Math.max(0, Number(timeSec) || 0) * fps) + 1;
  return Math.min(total, Math.max(1, idx));
}

function playbackInferSize() {
  const ctxInferW = Number(playbackSpatialContext?.infer_width) || 0;
  const ctxInferH = Number(playbackSpatialContext?.infer_height) || 0;
  const calInferW = Number(playbackSpatialContext?.calibration?.infer_width) || 0;
  const calInferH = Number(playbackSpatialContext?.calibration?.infer_height) || 0;
  return {
    w:
      poseData?.infer_width ||
      frameByTime[0]?.w ||
      ctxInferW ||
      calInferW ||
      852,
    h:
      poseData?.infer_height ||
      frameByTime[0]?.h ||
      ctxInferH ||
      calInferH ||
      480,
  };
}

/** 播放开始时预烘焙静态货框到离屏 canvas */
function bakePlaybackStaticLayer() {
  playbackStaticLayerCanvas = null;
  if (!annotationBoxes.length || !frozenPlaybackLayout) return null;
  const cache = getAnnotationDisplayCache();
  if (!cache.length) return null;
  const { cw, ch } = frozenPlaybackCanvasCss || syncCanvasSize({ force: true });
  const layer = document.createElement("canvas");
  layer.width = cw;
  layer.height = ch;
  const sctx = layer.getContext("2d");
  if (!sctx) return null;
  sctx.setLineDash([]);
  sctx.lineWidth = 1.5;
  sctx.strokeStyle = "rgba(0, 255, 0, 0.35)";
  cache.forEach(({ displayPts }) => {
    sctx.beginPath();
    displayPts.forEach(([dx, dy], i) => {
      if (i === 0) sctx.moveTo(dx, dy);
      else sctx.lineTo(dx, dy);
    });
    sctx.closePath();
    sctx.stroke();
  });
  playbackStaticLayerCanvas = layer;
  return layer;
}

/** 播放专用：静态货框 + 碰撞黄/告警红 + 地面网格 + 骨架连线 */
function drawSkeletonPlaybackOnly(frame, inferW, inferH) {
  const { cw, ch } = syncCanvasSize();
  ctx.clearRect(0, 0, cw, ch);
  if (playbackStaticLayerCanvas) {
    ctx.drawImage(playbackStaticLayerCanvas, 0, 0, cw, ch);
  } else {
    drawAnnotationBoxesStatic();
  }
  if (typeof drawSpatialGroundGridOverlay === "function") {
    drawSpatialGroundGridOverlay(inferW, inferH);
  }
  if (frame && annotationBoxes.length) {
    const collisionSets = collisionSetsForPlaybackFrame(frame, inferW, inferH);
    drawAnnotationBoxesCollisionOnly(frame, inferW, inferH, collisionSets);
  }
  const layout = frozenPlaybackLayout || getDisplayLayout();
  if (frame?.persons?.length) {
    ctx.lineWidth = 2;
    ctx.strokeStyle = "rgba(34, 211, 238, 0.9)";
    ctx.beginPath();
    frame.persons.forEach((person) => {
      const kpts = person.keypoints || [];
      COCO_LINES.forEach(([a, b]) => {
        const pa = kpts[a];
        const pb = kpts[b];
        if (!pa || !pb || pa[2] < SCORE_MIN || pb[2] < SCORE_MIN) return;
        const [x1, y1] = mapInferToDisplay(pa[0], pa[1], inferW, inferH, layout);
        const [x2, y2] = mapInferToDisplay(pb[0], pb[1], inferW, inferH, layout);
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
      });
    });
    ctx.stroke();
  }

  if (typeof drawPlaybackFootTrailOverlay === "function") {
    const fi =
      Number(frame?.frame_idx) ||
      Number(typeof lastRenderedFrameIdx !== "undefined" ? lastRenderedFrameIdx : 0) ||
      0;
    drawPlaybackFootTrailOverlay(frame, inferW, inferH, layout, fi);
  }
}

function clearFrozenPlaybackLayout() {
  frozenPlaybackLayout = null;
  frozenPlaybackCanvasCss = null;
  playbackStaticLayerCanvas = null;
}

function drawSkeleton(frame, inferW, inferH, collisionSets = null) {
  const { cw, ch } = syncCanvasSize();
  ctx.clearRect(0, 0, cw, ch);
  if (typeof drawSpatialGroundGridOverlay === "function") {
    drawSpatialGroundGridOverlay(inferW, inferH);
  }
  drawAnnotationBoxes(frame, inferW, inferH, collisionSets);
  drawDetBboxes(frame, inferW, inferH);
  const layout = getDisplayLayout();
  if (frame?.persons?.length) {
    ctx.lineWidth = 2;
    ctx.strokeStyle = "rgba(34, 211, 238, 0.9)";
    ctx.beginPath();
    frame.persons.forEach((person) => {
      const kpts = person.keypoints || [];
      COCO_LINES.forEach(([a, b]) => {
        const pa = kpts[a];
        const pb = kpts[b];
        if (!pa || !pb || pa[2] < SCORE_MIN || pb[2] < SCORE_MIN) return;
        const [x1, y1] = mapInferToDisplay(pa[0], pa[1], inferW, inferH, layout);
        const [x2, y2] = mapInferToDisplay(pb[0], pb[1], inferW, inferH, layout);
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
      });
    });
    ctx.stroke();

    frame.persons.forEach((person) => {
      const kpts = person.keypoints || [];
      kpts.forEach((kp, i) => {
        if (!kp || kp[2] < SCORE_MIN) return;
        const [x, y] = mapInferToDisplay(kp[0], kp[1], inferW, inferH, layout);
        ctx.fillStyle = ["#22d3ee", "#a78bfa", "#f472b6"][i % 3];
        ctx.beginPath();
        ctx.arc(x, y, 4, 0, Math.PI * 2);
        ctx.fill();
      });
    });
  }

  if (typeof drawPlaybackFootTrailOverlay === "function") {
    const fi = Number(frame?.frame_idx) || Number(typeof lastRenderedFrameIdx !== "undefined" ? lastRenderedFrameIdx : 0) || 0;
    drawPlaybackFootTrailOverlay(frame, inferW, inferH, layout, fi);
  }
}

function redrawCurrentFrame() {
  if (playbackRenderLoopActive && videoEl && !videoEl.paused) return;
  renderGeneration++;
  const gen = renderGeneration;
  const pinnedFi =
    typeof pinnedEventFrameIdx === "function" ? pinnedEventFrameIdx() : null;
  const authorityFi =
    typeof getPlaybackAuthorityFrameIdx === "function" ? getPlaybackAuthorityFrameIdx() : null;
  // 保留刚通过 frame_idx 精确渲染的帧，避免预览视频 seek 差帧把画面拉回
  const heldFi = lastRenderedFrameIdx >= 1 ? lastRenderedFrameIdx : null;
  lastRenderedFrameIdx = -1;
  tickPoseFrameIdx = -1;
  tickVideoFrameIdx = -1;
  lastEventSyncFrameIdx = -1;
  const targetFi = pinnedFi || authorityFi || heldFi;
  if (targetFi && frameByTime?.length) {
    const hit =
      typeof ensureFrameIndexEntry === "function"
        ? ensureFrameIndexEntry(targetFi)
        : typeof frameEntryByIdx === "function"
          ? frameEntryByIdx(targetFi)
          : frameByTime.find((item) => item.frameIdx === targetFi) || null;
    if (hit) {
      void renderFrameEntry(hit, gen);
      return;
    }
  }
  if (videoEl.src && videoEl.readyState >= 1) {
    void renderAtTime(videoEl.currentTime);
  } else if (frameByTime.length) {
    void renderFrameEntry(frameByTime[0], gen);
  }
}

async function renderFrameEntry(hit, renderGen) {
  if (!hit) return;
  const authorityFi =
    typeof getPlaybackAuthorityFrameIdx === "function" ? getPlaybackAuthorityFrameIdx() : null;
  // 丢弃/纠正落后于权威帧的异步渲染，避免 pause/seeked 竞态把画面拉回
  if (authorityFi != null && authorityFi > 0 && hit.frameIdx !== authorityFi) {
    const authHit =
      typeof ensureFrameIndexEntry === "function"
        ? ensureFrameIndexEntry(authorityFi)
        : typeof frameEntryByIdx === "function"
          ? frameEntryByIdx(authorityFi)
          : frameByTime.find((e) => e.frameIdx === authorityFi) || null;
    if (!authHit) return;
    hit = authHit;
  }
  const requestedFi = hit.frameIdx;
  let frame = hit.frame || (await ensureFrame(requestedFi));
  if (!frame && typeof findNearestCachedFrameEntry === "function") {
    const nearest = findNearestCachedFrameEntry(requestedFi);
    if (nearest) {
      frame = nearest.frame || (await ensureFrame(nearest.frameIdx));
    }
  }
  if (renderGen != null && renderGen !== renderGeneration) return;
  if (!frame) return;
  if (requestedFi === lastRenderedFrameIdx) return;
  lastRenderedFrameIdx = requestedFi;
  tickPoseFrameIdx = requestedFi;
  const collisionSets = getFrameCollisionSets(frame, hit.w, hit.h);
  drawSkeleton(frame, hit.w, hit.h, collisionSets);
  const { collisionSet, alarmSet } = collisionSets;
  if (collisionSet.size || alarmSet.size) {
    const c = [...collisionSet].join(", ") || "—";
    const a = [...alarmSet].join(", ") || "—";
    timeLabel.title = `碰撞: ${c} | 报警: ${a}`;
  } else {
    timeLabel.title = annotationBoxes.length ? "无碰撞" : "";
  }
  if (typeof updatePlaybackWristFeaturesUi === "function") {
    updatePlaybackWristFeaturesUi(hit.frameIdx);
  }
  if (typeof updatePlaybackFloorMapUi === "function") {
    updatePlaybackFloorMapUi(hit, frame);
  }
  if (typeof updateEventReviewFrameNavUi === "function") updateEventReviewFrameNavUi();
}

async function renderAtTimeCore(timeSec) {
  const hit = findFrameAt(timeSec);
  if (!hit) {
    lastRenderedFrameIdx = -1;
    const { cw, ch } = syncCanvasSize();
    ctx.clearRect(0, 0, cw, ch);
    return;
  }

  if ((poseData?.schema || 1) >= 2 && hit.frameIdx != null && !hit.frame) {
    maybePrefetchByChunkProgress(hit.frameIdx);
    prefetchLookaheadFromFrame(hit.frameIdx);

    if (!frameCache.has(hit.frameIdx)) {
      const nearest = findNearestCachedFrameEntry(hit.frameIdx);
      if (nearest && nearest.frameIdx !== lastRenderedFrameIdx) {
        await renderFrameEntry(nearest);
      }
      const { from, to } = chunkRangeForFrame(hit.frameIdx);
      await prefetchFrameChunk(from, to);
    }
  }

  if (hit.frame || frameCache.has(hit.frameIdx)) {
    await renderFrameEntry(hit);
  }
}

async function renderAtTime(timeSec) {
  const authorityFi =
    typeof getPlaybackAuthorityFrameIdx === "function" ? getPlaybackAuthorityFrameIdx() : null;
  if (authorityFi != null && authorityFi > 0) {
    const hit =
      typeof ensureFrameIndexEntry === "function"
        ? ensureFrameIndexEntry(authorityFi)
        : typeof frameEntryByIdx === "function"
          ? frameEntryByIdx(authorityFi)
          : frameByTime.find((e) => e.frameIdx === authorityFi) || null;
    if (hit) {
      await renderFrameEntry(hit);
      return;
    }
  }
  if (renderAtTimeInflight) {
    renderAtTimePendingTime = timeSec;
    return;
  }
  renderAtTimeInflight = true;
  renderAtTimePendingTime = null;
  try {
    let nextTime = timeSec;
    do {
      renderAtTimePendingTime = null;
      await renderAtTimeCore(nextTime);
      nextTime = renderAtTimePendingTime;
    } while (renderAtTimePendingTime != null);
  } finally {
    renderAtTimeInflight = false;
  }
}

/** 播放热路径：全量缓存就绪后仅同步绘制 */
function syncRenderPlaybackFrame(timeSec) {
  const targetIdx = frameIdxAtVideoTime(timeSec);
  if (!targetIdx) return;

  const frame = frameCache.get(targetIdx);
  if (!frame) {
    if (!playbackSkeletonReady) {
      const nearest = findNearestCachedFrameEntry(targetIdx);
      if (!nearest) return;
      const nearFrame = frameCache.get(nearest.frameIdx);
      if (!nearFrame || nearest.frameIdx === lastRenderedFrameIdx) return;
      lastRenderedFrameIdx = nearest.frameIdx;
      tickPoseFrameIdx = nearest.frameIdx;
      const { w, h } = playbackInferSize();
      drawSkeletonPlaybackOnly(nearFrame, w, h);
    }
    return;
  }

  if (targetIdx === lastRenderedFrameIdx) return;

  lastRenderedFrameIdx = targetIdx;
  tickPoseFrameIdx = targetIdx;
  const { w, h } = playbackInferSize();
  drawSkeletonPlaybackOnly(frame, w, h);
  if (typeof updatePlaybackFloorMapUi === "function") {
    const hit = frameByTime?.find((r) => r.frameIdx === targetIdx) || { frameIdx: targetIdx };
    updatePlaybackFloorMapUi(hit, frame);
  }
}

let videoFrameCallbackHandle = null;
let playbackRenderLoopActive = false;

function cancelPlaybackRenderLoop() {
  playbackRenderLoopActive = false;
  if (videoFrameCallbackHandle != null && typeof videoEl?.cancelVideoFrameCallback === "function") {
    videoEl.cancelVideoFrameCallback(videoFrameCallbackHandle);
    videoFrameCallbackHandle = null;
  }
  if (rafId) {
    cancelAnimationFrame(rafId);
    rafId = null;
  }
  clearFrozenPlaybackLayout();
}

/** 唯一入口：避免 play 事件与底部按钮重复启动多条渲染循环 */
function ensurePlaybackRenderLoop() {
  if (!videoEl?.src || videoEl.paused || videoEl.ended) return;
  cancelPlaybackRenderLoop();
  frozenPlaybackLayout = getDisplayLayout();
  frozenPlaybackCanvasCss = syncCanvasSize({ force: true });
  getAnnotationDisplayCache();
  bakePlaybackStaticLayer();
  playbackRenderLoopActive = true;
  tickVideoFrameIdx = -1;
  lastEventSyncFrameIdx = -1;
  lastPlaybackUiSyncMs = 0;
  resetPlaybackCollisionTracker();
  playbackRenderLoop();
}

function playbackRenderLoop() {
  if (!playbackRenderLoopActive || videoEl.paused || videoEl.ended) {
    playbackRenderLoopActive = false;
    clearFrozenPlaybackLayout();
    return;
  }

  if (videoEl.readyState >= 2) {
    const timeSec = videoEl.currentTime;
    const nextIdx = frameIdxAtVideoTime(timeSec);
    if (nextIdx > 0 && nextIdx !== tickVideoFrameIdx) {
      tickVideoFrameIdx = nextIdx;
      syncRenderPlaybackFrame(timeSec);
    }
  }

  const now = performance.now();
  if (now - lastPlaybackUiSyncMs >= 120) {
    lastPlaybackUiSyncMs = now;
    if (videoEl.duration && Number.isFinite(videoEl.duration)) {
      seekBar.value = String((videoEl.currentTime / videoEl.duration) * 1000);
      timeLabel.textContent = formatTime(videoEl.currentTime);
    }
  }

  if (!videoEl.paused && videoEl.readyState >= 2) {
    if (typeof videoEl.requestVideoFrameCallback === "function") {
      videoFrameCallbackHandle = videoEl.requestVideoFrameCallback(() => {
        playbackRenderLoop();
      });
    } else {
      rafId = requestAnimationFrame(playbackRenderLoop);
    }
  } else {
    playbackRenderLoopActive = false;
    clearFrozenPlaybackLayout();
  }
}

function tick() {
  ensurePlaybackRenderLoop();
}

function formatTime(sec) {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

let jsonOnlyTimer = null;
let jsonOnlyFrameIdx = 0;

function restartJsonOnlyPlaybackIfActive() {
  if (!jsonOnlyTimer) return;
  startJsonOnlyPlayback(jsonOnlyFrameIdx);
}

function stopPlayback() {
  videoEl.pause();
  clearInterval(jsonOnlyTimer);
  jsonOnlyTimer = null;
  cancelPlaybackRenderLoop();
}

function finishPlaybackSession() {
  stopPlayback();
  cleanupPlaybackVideo();
  clearVideoElement();
  setPlaybackInfo("回放已停止。");
}

function startJsonOnlyPlayback(startIdx = 0) {
  if (!frameByTime.length) return;
  const fps = poseData.fps || 15;
  const rate = Number.isFinite(playbackSpeed) && playbackSpeed > 0 ? playbackSpeed : 1;
  let idx = Math.max(0, Math.min(startIdx, frameByTime.length - 1));
  jsonOnlyFrameIdx = idx;
  clearInterval(jsonOnlyTimer);
  videoEl.style.display = "none";

  jsonOnlyTimer = setInterval(async () => {
    if (idx >= frameByTime.length) idx = 0;
    jsonOnlyFrameIdx = idx;
    const entry = frameByTime[idx];
    await renderFrameEntry(entry);
    seekBar.value = String((idx / frameByTime.length) * 1000);
    timeLabel.textContent = `${idx + 1}/${frameByTime.length}`;
    syncActiveEventFromPlaybackPosition({ timeSec: entry?.t, frameIdx: entry?.frameIdx });
    idx += 1;
  }, 1000 / (fps * rate));
}
