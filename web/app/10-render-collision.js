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

function eventPlaybackFrameKeys(ev) {
  const keys = new Set();
  const fi = parseInt(ev?.frame_idx, 10) || 0;
  const sfi = parseInt(ev?.source_frame_idx, 10) || 0;
  if (fi > 0) keys.add(fi);
  if (sfi > 0) keys.add(sfi);
  return keys;
}

function reviewEntryFrameKeys(entry) {
  const keys = new Set();
  const fi = parseInt(entry?.frame_idx, 10) || 0;
  const sfi = parseInt(entry?.source_frame_idx, 10) || 0;
  if (fi > 0) keys.add(fi);
  if (sfi > 0) keys.add(sfi);
  return keys;
}

function eventFramesOverlapWithReview(ev, entry) {
  const evKeys = eventPlaybackFrameKeys(ev);
  const entryKeys = reviewEntryFrameKeys(entry);
  for (const k of evKeys) {
    if (entryKeys.has(k)) return true;
  }
  return false;
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

/** 标真匹配：同帧 + 货位 id 重叠即可（collision/alarm 仅计算差异） */
function eventMatchesReviewEntry(ev, entry) {
  if (!ev || !entry) return false;
  if (!eventFramesOverlapWithReview(ev, entry)) return false;
  const evIds = boxIdsFromTokenList(ev?.box_tokens);
  const revIds = reviewEntryBoxIds(entry);
  for (const id of evIds) {
    if (revIds.has(id)) return true;
  }
  return false;
}

/** 与准确率 build_ground_truth_segments 一致：连续相同范本货框合并为段 */
function buildVerifiedGroundTruthSegments() {
  const sourceEvents =
    typeof getPlaybackGroundTruthEvents === "function"
      ? getPlaybackGroundTruthEvents()
      : playbackEvents;
  if (!sourceEvents?.length) return [];
  const entries = sourceEvents
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

let cachedVerifiedSegments = null;
let cachedVerifiedSegmentsKey = "";

function invalidateVerifiedSegmentsCache() {
  cachedVerifiedSegments = null;
  cachedVerifiedSegmentsKey = "";
}

function getVerifiedGroundTruthSegmentsCached() {
  const key = `${playbackEvents?.length || 0}:${verifiedTrueKeys.size}:${activeEventKey || ""}`;
  if (cachedVerifiedSegments && key === cachedVerifiedSegmentsKey) {
    return cachedVerifiedSegments;
  }
  cachedVerifiedSegments = buildVerifiedGroundTruthSegments();
  cachedVerifiedSegmentsKey = key;
  return cachedVerifiedSegments;
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

const MAX_SEEK_ACCURACY_DOTS = 120;

function invalidatePlaybackAccuracyOverlay() {
  playbackAccuracyOverlayCache = null;
  invalidateVerifiedSegmentsCache();
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
    const fi = Number(frame) || 0;
    if (fi <= 0) return;
    const covered = overlay.segments.some(
      (seg) =>
        fi >= seg.frame_start &&
        fi <= seg.frame_end &&
        tokenMatchesAnyList(token, seg.tokens)
    );
    if (!covered) falseAlarmFrames.push(fi);
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
  if (alarmSet?.size) {
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
  if (!ev || typeof isEventVerified !== "function" || !isEventVerified(ev)) return false;
  const overlay = getPlaybackAccuracyOverlay();
  if (!overlay?.segments?.length) return false;
  const fi = parseInt(ev.frame_idx, 10) || 0;
  if (fi <= 0) return false;
  const tokens = eventGroundTruthTokens(ev);
  if (!tokens.length) return false;
  return overlay.segments.some(
    (seg) =>
      !seg.detected &&
      fi >= seg.frame_start &&
      fi <= seg.frame_end &&
      tokens.some((t) => tokenMatchesAnyList(t, seg.tokens))
  );
}

/** 告警不在任标真范本段内 → 误报 */
function isPlaybackEventFalseAlarm(ev) {
  if (!ev || String(ev.event_type || "").trim() !== "alarm") return false;
  const overlay = getPlaybackAccuracyOverlay();
  if (!overlay?.segments?.length) return false;
  const fi = parseInt(ev.frame_idx, 10) || 0;
  if (fi <= 0) return false;
  const tokens = typeof normalizeBoxTokenList === "function"
    ? normalizeBoxTokenList(ev.box_tokens)
    : canonicalizeBoxTokenList(ev.box_tokens);
  if (!tokens.length) return false;
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

function getFrameCollisionSets(frame, inferW, inferH) {
  const overlay =
    typeof getPlaybackCollisionOverlayForFrame === "function"
      ? getPlaybackCollisionOverlayForFrame(frame)
      : null;
  if (overlay) {
    return {
      collisionSet: new Set(overlay.collisions || []),
      alarmSet: new Set(overlay.alarm_collisions || []),
    };
  }
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
    });
  });
  frameByTime.sort((a, b) => a.t - b.t);
}

/** frame_interval=1 时由 manifest 本地合成时间轴，避免拉取全量 timeline JSON */
function buildSyntheticFrameIndexFromManifest() {
  const total = Number(poseData?.frame_count) || 0;
  const fps = Number(poseData?.fps) || 15;
  const interval = Math.max(1, Number(poseData?.frame_interval) || 1);
  if (!total || interval !== 1) return false;

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

function buildFrameIndex(recordId = null) {
  frameByTime = [];
  resetFrameFetchState();
  resetPlaybackCollisionTracker();
  // 沙盒回放时跳过 pose 内嵌标注，稍后由 applyPlaybackSandboxAnnotation 加载
  if (!playbackSandboxSessionId) {
    syncAnnotationBoxesFromPose();
  }
  if (!poseData) return Promise.resolve();

  if ((poseData.schema || 1) >= 2 && recordId) {
    const inferW = poseData.infer_width || 640;
    const inferH = poseData.infer_height || 480;

    if (buildSyntheticFrameIndexFromManifest()) {
      if (typeof renderAccuracySeekMarkers === "function") renderAccuracySeekMarkers();
      return Promise.resolve();
    }

    return fetch(`${recordApiUrl(recordId, "/timeline")}?light=1`)
      .then((res) => (res.ok ? res.json() : { timeline: [] }))
      .then((body) => {
        applyTimelineRowsToFrameIndex(body.timeline || [], inferW, inferH);
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

/** 由视频时间直接换算帧号（与 manifest fps 对齐，O(1)） */
function frameIdxAtVideoTime(timeSec) {
  const fps = Number(poseData?.fps) || 25;
  const total = Number(poseData?.frame_count) || frameByTime.length || 0;
  if (!total) return 0;
  const idx = Math.floor(Math.max(0, Number(timeSec) || 0) * fps) + 1;
  return Math.min(total, Math.max(1, idx));
}

function playbackInferSize() {
  return {
    w: poseData?.infer_width || frameByTime[0]?.w || 852,
    h: poseData?.infer_height || frameByTime[0]?.h || 480,
  };
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

/** 播放专用：静态货框 + 碰撞黄/告警红 + 骨架连线 */
function drawSkeletonPlaybackOnly(frame, inferW, inferH) {
  const { cw, ch } = syncCanvasSize();
  ctx.clearRect(0, 0, cw, ch);
  drawAnnotationBoxesStatic();
  if (frame && annotationBoxes.length) {
    const collisionSets = collisionSetsForPlaybackFrame(frame, inferW, inferH);
    drawAnnotationBoxesCollisionOnly(frame, inferW, inferH, collisionSets);
  }
  if (!frame?.persons?.length) return;

  const layout = frozenPlaybackLayout || getDisplayLayout();
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

function clearFrozenPlaybackLayout() {
  frozenPlaybackLayout = null;
  frozenPlaybackCanvasCss = null;
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
  if (canvas._layoutCssW === cssW && canvas._layoutCssH === cssH) {
    return { cw: cssW, ch: cssH };
  }
  canvas._layoutCssW = cssW;
  canvas._layoutCssH = cssH;
  canvas.width = Math.floor(cssW * dpr);
  canvas.height = Math.floor(cssH * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { cw: cssW, ch: cssH };
}

/** 回放/复核时标真范本货框高亮（含连续标真片段覆盖的帧范围） */
function getReviewBoxHighlightContext(frameIdx = null) {
  if (!playbackEvents?.length || !annotationBoxes.length) return null;
  if (!eventsPanel || eventsPanel.classList.contains("hidden")) return null;

  const fi =
    frameIdx != null && Number(frameIdx) > 0
      ? Number(frameIdx)
      : lastRenderedFrameIdx >= 1
        ? lastRenderedFrameIdx
        : typeof getCurrentPlaybackFrameIdx === "function"
          ? getCurrentPlaybackFrameIdx()
          : null;

  const confirmedByToken = new Map();

  if (fi != null && fi > 0) {
    for (const seg of getVerifiedGroundTruthSegmentsCached()) {
      if (fi < seg.frame_start || fi > seg.frame_end) continue;
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
    const boxes =
      typeof getEventConfirmedBoxes === "function" ? getEventConfirmedBoxes(activeEv) : [];
    boxes.forEach((token) => {
      for (const key of boxTokenLookupKeys(token)) {
        confirmedByToken.set(key, true);
      }
    });
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

function invalidateAnnotationDisplayCache() {
  annotationDisplayCacheKey = "";
  annotationDisplayCache = [];
}

/** 货框显示多边形（layout/标注不变时可复用，避免每帧重算坐标） */
function getAnnotationDisplayCache() {
  if (!annotationBoxes.length) return [];
  const pl = window.previewLayout;
  if (!pl?.resolvePolygonFramePoints || !pl?.mapPointToDisplay) return [];

  const { frameW, frameH } = getVideoFrameSize();
  const layout = getDisplayLayout();
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

function drawAnnotationBoxes(frame, inferW, inferH, collisionSets = null, reviewCtx = undefined) {
  if (!annotationBoxes.length) return;

  const { collisionSet, alarmSet } =
    collisionSets || getFrameCollisionSets(frame, inferW, inferH);
  const frameIdx =
    lastRenderedFrameIdx >= 1
      ? lastRenderedFrameIdx
      : Number(frame?.frame_idx) || Number(frame?.source_frame_idx) || 0;
  reviewCtx = reviewCtx === undefined ? getReviewBoxHighlightContext(frameIdx) : reviewCtx;
  const duringVideoPlayback = !videoEl.paused && videoEl.readyState >= 2;
  const { missTokens, falseAlarmTokens } = duringVideoPlayback
    ? { missTokens: new Set(), falseAlarmTokens: new Set() }
    : getAccuracyOutlineForFrame(frameIdx, alarmSet);

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

/** COCO17 手腕关键点索引 */
const WRIST_KPT_INDICES = new Set([9, 10]);

/** 延长手腕探针可视化（橙/黄，与青色骨架区分） */
function drawHandExtendedProbes(frame, inferW, inferH) {
  if (typeof collectPlaybackHandProbePoints !== "function") return;
  if (typeof isPlaybackHandProbeEnabled === "function" && !isPlaybackHandProbeEnabled()) {
    return;
  }
  const probes = collectPlaybackHandProbePoints(frame);
  if (!probes.length) return;

  const layout = getDisplayLayout();
  const sideStyle = {
    left: { fill: "#f97316", stroke: "#fff7ed", dash: [5, 4] },
    right: { fill: "#eab308", stroke: "#fefce8", dash: [5, 4] },
  };

  ctx.save();
  probes.forEach((probe) => {
    const style = sideStyle[probe.side] || sideStyle.left;
    const [wx, wy] = mapInferToDisplay(probe.wx, probe.wy, inferW, inferH, layout);
    const [px, py] = mapInferToDisplay(probe.x, probe.y, inferW, inferH, layout);

    if (probe.kind === "hand_sim") {
      ctx.setLineDash(style.dash);
      ctx.lineWidth = 2.5;
      ctx.strokeStyle = style.fill;
      ctx.globalAlpha = 0.92;
      ctx.beginPath();
      ctx.moveTo(wx, wy);
      ctx.lineTo(px, py);
      ctx.stroke();

      ctx.setLineDash([]);
      ctx.globalAlpha = 1;
      ctx.fillStyle = style.fill;
      ctx.strokeStyle = style.stroke;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(px, py, 5, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();

      ctx.strokeStyle = "rgba(255, 255, 255, 0.95)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(px - 3, py);
      ctx.lineTo(px + 3, py);
      ctx.moveTo(px, py - 3);
      ctx.lineTo(px, py + 3);
      ctx.stroke();

      ctx.font = "bold 10px system-ui, sans-serif";
      ctx.fillStyle = "rgba(0, 0, 0, 0.75)";
      ctx.fillText(probe.side === "left" ? "L" : "R", px + 7, py - 7);
    } else {
      ctx.setLineDash([3, 3]);
      ctx.lineWidth = 1.5;
      ctx.strokeStyle = style.fill;
      ctx.globalAlpha = 0.75;
      ctx.beginPath();
      const r = 5;
      ctx.moveTo(px, py - r);
      ctx.lineTo(px + r, py);
      ctx.lineTo(px, py + r);
      ctx.lineTo(px - r, py);
      ctx.closePath();
      ctx.stroke();
    }
  });
  ctx.restore();
}

function drawSkeleton(frame, inferW, inferH, collisionSets = null, opts = {}) {
  const lite = opts.lite === true;
  const { cw, ch } = syncCanvasSize();
  ctx.clearRect(0, 0, cw, ch);
  if (lite) {
    drawAnnotationBoxesCollisionOnly(frame, inferW, inferH, collisionSets);
  } else {
    drawAnnotationBoxes(frame, inferW, inferH, collisionSets, undefined);
  }
  if (!lite) drawDetBboxes(frame, inferW, inferH);
  if (!frame?.persons?.length) return;

  const layout = getDisplayLayout();
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
    const handProbeOn =
      !lite &&
      typeof isPlaybackHandProbeEnabled === "function" &&
      isPlaybackHandProbeEnabled();
    kpts.forEach((kp, i) => {
      if (!kp || kp[2] < SCORE_MIN) return;
      const [x, y] = mapInferToDisplay(kp[0], kp[1], inferW, inferH, layout);
      ctx.fillStyle = ["#22d3ee", "#a78bfa", "#f472b6"][i % 3];
      let radius = lite ? 2 : 2.5;
      if (WRIST_KPT_INDICES.has(i)) {
        radius = handProbeOn ? 1.8 : lite ? 1.8 : 2;
      }
      ctx.beginPath();
      ctx.arc(x, y, radius, 0, Math.PI * 2);
      ctx.fill();
    });
  });

  if (!lite) drawHandExtendedProbes(frame, inferW, inferH);
}

function isVideoPlaybackActive() {
  return Boolean(videoEl?.src) && !videoEl.paused && videoEl.readyState >= 2;
}

function collisionSetsForPlaybackFrame(frame, inferW, inferH) {
  // 沙盒/碰撞变体 overlay 优先（getFrameCollisionSets 内已处理）
  return getFrameCollisionSets(frame, inferW, inferH);
}

/** 播放热路径：全量缓存就绪后仅同步绘制骨架线 */
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
}

function redrawCurrentFrame() {
  if (playbackRenderLoopActive && videoEl && !videoEl.paused) return;
  renderGeneration++;
  lastRenderedFrameIdx = -1;
  tickPoseFrameIdx = -1;
  tickVideoFrameIdx = -1;
  lastEventSyncFrameIdx = -1;
  if (videoEl.src && videoEl.readyState >= 1) {
    void renderAtTime(videoEl.currentTime);
  } else if (frameByTime.length) {
    void renderFrameEntry(frameByTime[0]);
  }
}

async function renderFrameEntry(hit, renderGen) {
  if (!hit) return;
  const frame = hit.frame || frameCache.get(hit.frameIdx) || (await ensureFrame(hit.frameIdx));
  if (renderGen != null && renderGen !== renderGeneration) return;
  if (!frame) return;
  if (hit.frameIdx === lastRenderedFrameIdx) return;
  lastRenderedFrameIdx = hit.frameIdx;
  tickPoseFrameIdx = hit.frameIdx;
  tickVideoFrameIdx = hit.frameIdx;
  const collisionSets = collisionSetsForPlaybackFrame(frame, hit.w, hit.h);
  const lite = isVideoPlaybackActive();
  drawSkeleton(frame, hit.w, hit.h, collisionSets, { lite });
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

/** 唯一入口：视频 play 时启动一条骨架渲染循环 */
function ensurePlaybackRenderLoop() {
  if (!videoEl?.src || videoEl.paused || videoEl.ended) return;
  cancelPlaybackRenderLoop();
  frozenPlaybackLayout = getDisplayLayout();
  frozenPlaybackCanvasCss = syncCanvasSize({ force: true });
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
  if (videoEl.duration && Number.isFinite(videoEl.duration)) {
    const now = performance.now();
    if (now - lastPlaybackUiSyncMs >= 120) {
      lastPlaybackUiSyncMs = now;
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
