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

const MAX_SEEK_ACCURACY_DOTS = 120;

function invalidatePlaybackAccuracyOverlay() {
  playbackAccuracyOverlayCache = null;
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

function buildFrameIndex(recordId = null) {
  frameByTime = [];
  resetFrameFetchState();
  resetPlaybackCollisionTracker();
  syncAnnotationBoxesFromPose();
  if (!poseData) return Promise.resolve();

  if ((poseData.schema || 1) >= 2 && recordId) {
    return fetch(recordApiUrl(recordId, "/timeline"))
      .then((res) => (res.ok ? res.json() : { timeline: [] }))
      .then((body) => {
        const inferW = poseData.infer_width || 640;
        const inferH = poseData.infer_height || 480;
        (body.timeline || []).forEach((row) => {
          frameByTime.push({
            t: row.timestamp_sec ?? 0,
            frameIdx: row.frame_idx,
            w: row.infer_width || inferW,
            h: row.infer_height || inferH,
          });
        });
        frameByTime.sort((a, b) => a.t - b.t);
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

function syncCanvasSize() {
  const wrap = stageWrap || document.querySelector(".stage-wrap");
  if (!wrap) return { cw: 1, ch: 1 };
  const rect = wrap.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const cssW = Math.max(1, Math.floor(rect.width));
  const cssH = Math.max(1, Math.floor(rect.height));
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
    for (const seg of buildVerifiedGroundTruthSegments()) {
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

function drawAnnotationBoxes(frame, inferW, inferH, collisionSets = null, reviewCtx = null) {
  if (!annotationBoxes.length) return;

  const { collisionSet, alarmSet } =
    collisionSets || getFrameCollisionSets(frame, inferW, inferH);
  const frameIdx =
    lastRenderedFrameIdx >= 1
      ? lastRenderedFrameIdx
      : Number(frame?.frame_idx) || Number(frame?.source_frame_idx) || 0;
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

function drawSkeleton(frame, inferW, inferH, collisionSets = null) {
  const { cw, ch } = syncCanvasSize();
  ctx.clearRect(0, 0, cw, ch);
  drawAnnotationBoxes(frame, inferW, inferH, collisionSets);
  drawDetBboxes(frame, inferW, inferH);
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

function redrawCurrentFrame() {
  lastRenderedFrameIdx = -1;
  tickPoseFrameIdx = -1;
  lastEventSyncFrameIdx = -1;
  if (videoEl.src && videoEl.readyState >= 1) {
    void renderAtTime(videoEl.currentTime);
  } else if (frameByTime.length) {
    void renderFrameEntry(frameByTime[0]);
  }
}

async function renderFrameEntry(hit, renderGen) {
  if (!hit) return;
  const frame = hit.frame || (await ensureFrame(hit.frameIdx));
  if (renderGen != null && renderGen !== renderGeneration) return;
  if (!frame) return;
  if (hit.frameIdx === lastRenderedFrameIdx) return;
  lastRenderedFrameIdx = hit.frameIdx;
  tickPoseFrameIdx = hit.frameIdx;
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
}

async function renderAtTimeCore(timeSec) {
  const gen = ++renderGeneration;
  const hit = findFrameAt(timeSec);
  if (!hit) {
    if (gen !== renderGeneration) return;
    lastRenderedFrameIdx = -1;
    const { cw, ch } = syncCanvasSize();
    ctx.clearRect(0, 0, cw, ch);
    return;
  }
  if ((poseData?.schema || 1) >= 2 && hit.frameIdx != null) {
    await ensureFrameChunkLoaded(hit.frameIdx);
  }
  if (gen !== renderGeneration) return;
  await renderFrameEntry(hit, gen);
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

function tick() {
  let syncFrameIdx = null;
  if (videoEl.readyState >= 2) {
    const hit = findFrameAt(videoEl.currentTime);
    const nextIdx = hit?.frameIdx ?? -1;
    if (nextIdx >= 0 && nextIdx !== tickPoseFrameIdx) {
      void renderAtTime(videoEl.currentTime);
    }
    syncFrameIdx = nextIdx >= 0 ? nextIdx : null;
  }
  if (videoEl.duration && Number.isFinite(videoEl.duration)) {
    seekBar.value = String((videoEl.currentTime / videoEl.duration) * 1000);
    timeLabel.textContent = formatTime(videoEl.currentTime);
  }
  if (syncFrameIdx != null && syncFrameIdx !== lastEventSyncFrameIdx) {
    lastEventSyncFrameIdx = syncFrameIdx;
    syncActiveEventFromPlaybackPosition({
      timeSec: videoEl.currentTime,
      frameIdx: syncFrameIdx,
    });
  }
  rafId = requestAnimationFrame(tick);
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
  cancelAnimationFrame(rafId);
  rafId = null;
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
